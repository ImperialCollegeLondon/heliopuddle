"""Checking a committed write against its spool, per the ``verify`` mode (STO-05).

- ``native``: the provider's own hash of the stored object (``operations/stat``
  with hashes) against the same hash of the spool (``operations/hashsum``, or
  the md5 hp computed while spooling when the provider reports md5);
- ``size``: sizes only;
- ``content``: nothing here; hp reads the destination back;
- ``auto``: ``native`` when the remote reports a hash, else ``content``.

Successful writes are remembered per process, so :func:`verified_md5` can
later vouch for an object's md5 without reading it, as long as the provider's
hash (or, for ``size``, the size) still matches.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from heliopuddle.storage.rclone.client import RcClient
from heliopuddle.storage.rclone.errors import RcloneConfigError, RcloneVerifyError

__all__ = ["VERIFY_MODES", "Written", "check_written", "forget", "move_record", "verified_md5"]

VERIFY_MODES = ("auto", "native", "content", "size")

#: Hash types to compare, most preferred first: md5 needs no extra pass over
#: the spool; Box reports sha1, OneDrive and SharePoint quickxor.
HASH_PREFERENCE = (
    "md5",
    "sha1",
    "quickxor",
    "sha256",
    "crc32c",
    "dropbox",
    "hidrive",
    "mailru",
    "whirlpool",
    "crc32",
)

_MAX_RECORDS = 100_000
_records: OrderedDict[tuple[str, str, str], Written] = OrderedDict()
_records_lock = threading.Lock()


@dataclass(frozen=True)
class Written:
    """What hp knows about an object it wrote and verified.

    Attributes:
        size: the size written.
        kind: the native hash type compared, ``"size"``, or None (content).
        value: the native hash value (lowercase), for a hash kind.
        md5: the md5 hp computed while spooling.
    """

    size: int
    kind: str | None
    value: str | None
    md5: str


def validate_mode(mode: str) -> str:
    """Return ``mode`` if it is a ``verify`` mode.

    Raises:
        RcloneConfigError: it is not one of :data:`VERIFY_MODES`.
    """
    if mode not in VERIFY_MODES:
        raise RcloneConfigError(f"verify must be one of {', '.join(VERIFY_MODES)}: {mode!r}")
    return mode


def remote_state(
    client: RcClient, fs: str, remote: str, kinds: tuple[str, ...] = HASH_PREFERENCE
) -> tuple[int, dict[str, str]]:
    """Return the object's size and the provider hashes among ``kinds`` (lowercase).

    Raises:
        FileNotFoundError: there is no such object.
    """
    opt: dict[str, Any] = {"showHash": True, "hashTypes": list(kinds)} if kinds else {}
    item: Any = client.call("operations/stat", fs=fs, remote=remote, opt=opt).get("item")
    if not item or item.get("IsDir"):
        raise FileNotFoundError(f"rclone object {fs}{remote} does not exist")
    hashes = {str(k).lower(): str(v).lower() for k, v in (item.get("Hashes") or {}).items() if v}
    return int(item.get("Size", -1)), hashes


def _spool_hash(client: RcClient, spool: Path, kind: str, md5: str) -> str:
    if kind == "md5":
        return md5
    if not spool.is_file():  # never hash a directory (or the whole disk)
        raise RcloneVerifyError(f"spool {spool} is not a file")
    result = client.call("operations/hashsum", fs=str(spool), hashType=kind)
    lines = result.get("hashsum") or [""]
    return str(lines[0]).split(" ", 1)[0].lower()


def check_written(
    client: RcClient, fs: str, remote: str, spool: Path, md5: str, size: int, mode: str
) -> Written:
    """Verify an object just copied from ``spool`` per ``mode``.

    Raises:
        RcloneVerifyError: the size or the native hash differs, or ``native``
            was asked for on a remote that reports no hash.
    """
    if mode == "content":
        return Written(size, None, None, md5)
    stored_size, hashes = remote_state(client, fs, remote)
    if stored_size >= 0 and stored_size != size:
        raise RcloneVerifyError(
            f"rclone {fs}{remote}: stored size {stored_size} differs from the {size} bytes written"
        )
    if mode == "size":
        return Written(size, "size", None, md5)
    kind = next((k for k in HASH_PREFERENCE if hashes.get(k)), None)
    if kind is None:
        if mode == "native":
            raise RcloneVerifyError(
                f"rclone {fs}{remote}: verify native, but the remote reports no hash"
            )
        return Written(size, None, None, md5)  # auto: hp reads the content back
    if _spool_hash(client, spool, kind, md5) != hashes[kind]:
        raise RcloneVerifyError(
            f"rclone {fs}{remote}: the stored {kind} differs from what was written"
        )
    return Written(size, kind, hashes[kind], md5)


def remember(key: tuple[str, str, str], written: Written) -> None:
    """Record a verified write (bounded, least recently written dropped first)."""
    with _records_lock:
        _records[key] = written
        _records.move_to_end(key)
        while len(_records) > _MAX_RECORDS:
            _records.popitem(last=False)


def forget(key: tuple[str, str, str]) -> None:
    """Drop the record of a removed object."""
    with _records_lock:
        _records.pop(key, None)


def move_record(src: tuple[str, str, str], dst: tuple[str, str, str]) -> None:
    """Carry a record along with a moved object (a failed record is simply dropped)."""
    with _records_lock:
        written = _records.pop(src, None)
        _records.pop(dst, None)
        if written is not None:
            _records[dst] = written


def verified_md5(client: RcClient, key: tuple[str, str, str]) -> str | None:
    """The md5 hp wrote at ``key`` if the provider still vouches for the same bytes.

    ``native``: the provider's hash still equals the one compared at write
    time; ``size``: the size still matches. Otherwise (``content``, unknown
    objects) None, and the caller reads the content.
    """
    with _records_lock:
        written = _records.get(key)
    if written is None or written.kind is None:
        return None
    _lock_name, fs, remote = key
    kinds = (written.kind,) if written.kind != "size" else ()
    try:
        size, hashes = remote_state(client, fs, remote, kinds)
    except FileNotFoundError:
        forget(key)
        return None
    if size != written.size:
        return None
    if written.kind == "size" or hashes.get(written.kind) == written.value:
        return written.md5
    return None
