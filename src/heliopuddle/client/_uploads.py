"""Adding and registering files: the reserve, write, renew, complete protocol."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from heliopuddle.client._base import ClientBase
from heliopuddle.client._http import api_error, retry_after, server_now
from heliopuddle.client._renewer import GuardedReader, Renewer
from heliopuddle.client.errors import HelioPuddleError
from heliopuddle.server.schemas import (
    FileCheck,
    FileCheckResult,
    FileCreate,
    FileRead,
    UploadRequest,
    UploadReservation,
)
from heliopuddle.storage.hashing import CHUNK_SIZE, md5_file
from heliopuddle.storage.location import StorageLocation

COMPLETE_MAX_ATTEMPTS = 4
"""Initial completion plus at most three retries of a 503 with Retry-After."""


class UploadsMixin(ClientBase):
    """``check``, ``add_file`` and ``register_file`` of :class:`HelioPuddleClient`."""

    def check(self, path: str, hash: str) -> FileCheckResult:
        """Return ``POST /projects/{p}/files/check``: is there a live file at ``path``?

        Args:
            path: relative path in the store.
            hash: hex md5 of the content to compare with.

        Returns:
            ``exists``, ``hash_matches`` and the live record (if any).
        """
        body = FileCheck(path=path, hash=hash)
        response = self._request(
            "POST", f"/projects/{self._project()}/files/check", json=body.model_dump()
        )
        return FileCheckResult.model_validate(response.json())

    def add_file(
        self,
        local_path: str | Path,
        path: str,
        storage_location: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        overwrite: bool = False,
        content_date: datetime | None = None,
        version_number: int | None = None,
        version_text: str | None = None,
    ) -> FileRead:
        """Hash ``local_path`` and upload it to ``path`` under a reservation (CLI-08).

        The upload protocol (docs/design/operations.md, "Add a file"):

        1. **Reserve** ``POST /projects/{p}/uploads`` with the hash and size.
           Nothing is written before the server answers:

           - ``unchanged`` (same hash, copy ``available``): the existing record
             is returned; nothing is written (``metadata`` and
             ``content_date`` are ignored);
           - a different hash without ``overwrite``: :class:`ConflictError`;
           - the path is owned by another operation: :class:`BusyError`;
           - ``new`` or ``overwrite``: the answer names the ``write_path`` (the
             final path, or a staging path for an overwrite) and a token.

        2. **Write** the content directly to ``write_path`` when credential
           access is allowed, otherwise ``PUT /uploads/{token}/content`` through
           the API under writer access. Direct transfers renew the reservation
           (``POST /uploads/{token}/renew``) about every third of its lease
           while the transfer runs. A source or store failure aborts the write
           without committing partial bytes.
        3. **Complete** ``POST /uploads/{token}/complete``: the server verifies
           the content and publishes it. A ``503`` with ``Retry-After`` is
           retried at most three times, renewing the reservation during waits.

        On any failure after the reservation (write, lost reservation,
        complete), the reservation is released (``DELETE /uploads/{token}``)
        and the error re-raised.

        Args:
            local_path: the local file to add.
            path: relative, ``/``-separated destination path in the store.
            storage_location: storage location name; None means the project's first.
            metadata: optional JSON metadata stored with the record.
            overwrite: replace a live file at ``path`` that has a different hash.
            content_date: optional content date; overrides a date taken from
                the path by the project's version rules.
            version_number: optional number version for a new file, replacing
                the version the project's version rules derive from the path
                (the series still comes from the path).
            version_text: optional text version, the same way; give at most
                one of ``version_number`` and ``version_text``. A live file
                keeps its version: an equal override, or none, is accepted.

        Returns:
            The record (``FileRecord``), already verified by the API: ``state``
            is ``available``, or ``unknown`` if the store copy did not match
            the hash.

        Raises:
            ValueError: both ``version_number`` and ``version_text`` were given
                (no request is made).
            FileNotFoundError: ``local_path`` is not a file (nothing is written).
            ConflictError: a live file with a different hash exists at ``path``
                and ``overwrite`` is False, or the live file has a version
                different from the override (nothing is written).
            BusyError: another operation owns the path (nothing is written).
            NotFoundError: unknown project or storage location.
            HelioPuddleError: any other API error (e.g. ``422`` for a bad path
                or a version of the other kind than the series', or ``409``
                from complete if the reservation was taken over).
        """
        check_version_override(version_number, version_text)
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(f"not a file: {source}")
        project = self._project()
        location = self._transfer_location(storage_location)
        digest = md5_file(source)
        size = source.stat().st_size

        body = UploadRequest(
            path=path,
            hash=digest,
            size=size,
            storage_location=location.name if location is not None else storage_location,
            overwrite=overwrite,
            metadata=metadata,
            content_date=content_date,
            version_number=version_number,
            version_text=version_text,
        )
        response = self._request(
            "POST",
            f"/projects/{project}/uploads",
            json=body.model_dump(mode="json", exclude_none=True),
        )
        reservation = UploadReservation.model_validate(response.json())
        if reservation.mode == "unchanged":
            if reservation.file is None:
                raise HelioPuddleError(response.status_code, "unchanged upload without a record")
            return reservation.file
        if reservation.token is None or reservation.write_path is None:
            raise HelioPuddleError(response.status_code, "reservation without token or write_path")

        token = quote(reservation.token, safe="")
        try:
            with (
                Renewer(self, token, reservation.deadline, server_now(response)) as renewer,
                source.open("rb") as raw,
            ):
                if location is None:

                    def content() -> Iterator[bytes]:
                        raw.seek(0)
                        while chunk := raw.read(CHUNK_SIZE):
                            yield chunk

                    # The server renews during receive/write and claims the token
                    # while staging. A client renewal must not race that claim.
                    with renewer.completing():
                        self._request(
                            "PUT",
                            f"/uploads/{token}/content",
                            headers={"Content-Type": "application/octet-stream"},
                            body_factory=content,
                        )
                else:
                    store = StorageLocation(
                        location.url, location.storage_options, location.create_root
                    )
                    store.write(reservation.write_path, GuardedReader(raw, renewer))  # type: ignore[arg-type]
                renewer.raise_if_lost()
                return self._complete_upload(token, renewer)
        except BaseException:
            with contextlib.suppress(Exception):
                self._request("DELETE", f"/uploads/{token}")
            raise

    def register_file(
        self,
        path: str,
        hash: str,
        size: int,
        storage_location: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        content_date: datetime | None = None,
        version_number: int | None = None,
        version_text: str | None = None,
    ) -> FileRead:
        """Register a file another tool has already written to the store (CLI-13).

        Calls ``POST /projects/{p}/files``: the server records the file and
        verifies the store object inline. Nothing is read or written by the
        client, so the caller must have written the content at ``path`` in
        the storage location first. Use :meth:`add_file` to transfer content.

        Args:
            path: relative, ``/``-separated path of the object in the store.
            hash: hex md5 of the content.
            size: size of the content in bytes.
            storage_location: storage location name; None means the project's first.
            metadata: optional JSON metadata stored with a new record.
            content_date: optional content date; overrides a date taken from
                the path by the project's version rules.
            version_number: optional number version for a new file, replacing
                the version the project's version rules derive from the path
                (the series still comes from the path).
            version_text: optional text version, the same way; give at most
                one of ``version_number`` and ``version_text``. A live file
                keeps its version: an equal override, or none, is accepted.

        Returns:
            The record: a new file (``201``), or the existing live file when
            it has the same hash (``200``; ``metadata`` and ``content_date``
            are then ignored). ``state`` is ``available``, or ``unknown`` if
            the store object did not match ``hash`` and ``size``.

        Raises:
            ValueError: both ``version_number`` and ``version_text`` were given
                (no request is made).
            ConflictError: a live file with a different hash exists at ``path``
                (to replace it, use :meth:`add_file` with ``overwrite=True``),
                or the live file has a version different from the override.
            BusyError: another operation owns the path.
            NotFoundError: unknown project or storage location.
            HelioPuddleError: any other API error (e.g. ``422`` for a bad path
                or a version of the other kind than the series').
        """
        check_version_override(version_number, version_text)
        body = FileCreate(
            path=path,
            hash=hash,
            size=size,
            storage_location=storage_location,
            metadata=metadata,
            content_date=content_date,
            version_number=version_number,
            version_text=version_text,
        )
        response = self._request(
            "POST",
            f"/projects/{self._project()}/files",
            json=body.model_dump(mode="json", exclude_none=True),
        )
        return FileRead.model_validate(response.json())

    def _complete_upload(self, token: str, renewer: Renewer) -> FileRead:
        for attempt in range(COMPLETE_MAX_ATTEMPTS):
            # Complete claims the token server-side. Do not race that claim
            # with a renew, which would otherwise look like a lost lease.
            with renewer.completing():
                done = self._request("POST", f"/uploads/{token}/complete", accept=(503,))
                if done.status_code != 503:
                    renewer.stop()
                    return FileRead.model_validate(done.json())
                delay = retry_after(done.headers.get("retry-after", ""))
                if delay is None or attempt == COMPLETE_MAX_ATTEMPTS - 1:
                    raise api_error(done)
            renewer.wait(delay)
        raise AssertionError("completion retries exhausted")  # pragma: no cover


def check_version_override(version_number: int | None, version_text: str | None) -> None:
    """Refuse both version overrides at once, before any request (CLI-02, CLI-13).

    Raises:
        ValueError: both are given.
    """
    if version_number is not None and version_text is not None:
        raise ValueError("give either version_number or version_text, not both")
