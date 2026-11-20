"""Bounded previews of file content (API-18). See docs/design/preview.md.

This package knows nothing about the database or storage locations: it gets a
file name, a function that opens the content, the size, and (when the storage
location has one) a local path, and returns a
:class:`~heliopuddle.server.schemas.preview.FilePreview`.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, cast

from heliopuddle.preview import text
from heliopuddle.preview.cells import Content, clip
from heliopuddle.preview.streams import GuardedReader, StorageReadError
from heliopuddle.server.schemas.preview import FilePreview, PreviewKind

__all__ = ["Format", "StorageReadError", "build_preview", "format_of"]

COPY_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Format:
    """A previewable format.

    Attributes:
        name: the ``format`` reported in the preview.
        reader: which reader handles it.
        media_type: the format's media type.
    """

    name: str
    reader: str
    media_type: str


_FORMATS: dict[str, Format] = {
    ".csv": Format("csv", "csv", "text/csv"),
    ".tsv": Format("tsv", "tsv", "text/tab-separated-values"),
    ".json": Format("json", "json", "application/json"),
    ".jsonl": Format("jsonl", "jsonl", "application/jsonl"),
    ".ndjson": Format("ndjson", "jsonl", "application/x-ndjson"),
    ".txt": Format("txt", "text", "text/plain"),
    ".log": Format("log", "text", "text/plain"),
    ".md": Format("markdown", "markdown", "text/markdown"),
    ".markdown": Format("markdown", "markdown", "text/markdown"),
    ".png": Format("png", "image", "image/png"),
    ".jpg": Format("jpeg", "image", "image/jpeg"),
    ".jpeg": Format("jpeg", "image", "image/jpeg"),
    ".gif": Format("gif", "image", "image/gif"),
    ".webp": Format("webp", "image", "image/webp"),
    ".svg": Format("svg", "image", "image/svg+xml"),
    ".parquet": Format("parquet", "parquet", "application/vnd.apache.parquet"),
    ".cdf": Format("cdf", "cdf", "application/x-cdf"),
}


def format_of(name: str) -> Format | None:
    """The preview format of a file name, by extension (case-insensitive), or None."""
    base = name.rsplit("/", 1)[-1].lower()
    dot = base.rfind(".")
    return _FORMATS.get(base[dot:]) if dot > 0 else None


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("bytes", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.3g} {unit}"
        value /= 1000
    raise AssertionError("unreachable")  # pragma: no cover


_ABSOLUTE_PATH = re.compile(r"(?<![\w.:/])(?:/[^\s'\":/]+)+/?")


def error_message(error: BaseException, *paths: str | None) -> str:
    """A short message for a parse failure: exception type and text, no paths.

    Args:
        error: the parse error.
        paths: local paths that must not appear in the message.
    """
    text = str(error)
    for path in paths:
        if path:
            text = text.replace(path, "<file>")
    text = _ABSOLUTE_PATH.sub("<file>", text)
    return clip(f"{type(error).__name__}: {text}" if text else type(error).__name__, 300)


def build_preview(
    *,
    file_id: int,
    name: str,
    opener: Callable[[], BinaryIO],
    size: int | None,
    max_bytes: int,
    local_path: str | None = None,
    storage_location: str | None = None,
) -> FilePreview:
    """Build the preview of one file's content, reading only what its format needs.

    Args:
        file_id: the file id reported in the preview.
        name: the file name or path; its extension picks the format.
        opener: opens the content for binary reading (called at most once).
        size: the content's size in bytes, if known.
        max_bytes: the largest CDF copied to a temporary file when there is no
            local path (``HELIOPUDDLE_PREVIEW_MAX_BYTES``).
        local_path: a local path of the same content, if the storage has one.
        storage_location: the storage location reported in the preview.

    Returns:
        The preview; content that cannot be parsed gives kind ``error``.

    Raises:
        FileNotFoundError: the content is gone.
        StorageReadError: the storage could not be opened or read.
    """
    fmt = format_of(name)
    preview = FilePreview(
        file_id=file_id,
        kind="none",
        format=fmt.name if fmt else None,
        media_type=fmt.media_type if fmt else None,
        storage_location=storage_location,
    )
    if fmt is None:
        preview.message = "There is no preview for this file type."
        return preview
    if fmt.reader == "image":
        preview.kind = "image"
        return preview
    guards: list[GuardedReader] = []

    def open_guarded() -> BinaryIO:
        try:
            source = opener()
        except FileNotFoundError:
            raise
        except Exception as error:
            raise StorageReadError(f"storage open failed ({type(error).__name__})") from None
        guards.append(GuardedReader(source))
        return cast(BinaryIO, guards[-1])

    temp_paths: list[str] = []
    try:
        content = _read(fmt, open_guarded, size, max_bytes, local_path, temp_paths)
    except (FileNotFoundError, StorageReadError):
        raise
    except Exception as error:
        failure = next((g.failure for g in guards if g.failure is not None), None)
        if failure is not None:
            raise failure from None
        content = Content(kind="error", message=error_message(error, local_path, *temp_paths))
    finally:
        for guard in guards:
            guard.close()
    return _fill(preview, content)


def _fill(preview: FilePreview, content: Content) -> FilePreview:
    kind: PreviewKind = content.kind
    preview.kind = kind
    preview.tables = content.tables
    preview.text = content.text
    preview.text_truncated = content.text_truncated
    preview.attributes = content.attributes
    preview.message = content.message
    return preview


def _read(
    fmt: Format,
    open_guarded: Callable[[], BinaryIO],
    size: int | None,
    max_bytes: int,
    local_path: str | None,
    temp_paths: list[str],
) -> Content:
    reader = fmt.reader
    if reader in ("csv", "tsv"):
        return text.preview_csv(open_guarded(), size, tab=reader == "tsv")
    if reader == "text":
        return text.preview_lines(open_guarded(), size)
    if reader == "markdown":
        return text.preview_markdown(open_guarded(), size)
    if reader == "json":
        return text.preview_json(open_guarded(), size)
    if reader == "jsonl":
        return text.preview_jsonl(open_guarded(), size)
    if reader == "parquet":
        from heliopuddle.preview.parquet import preview_parquet

        return preview_parquet(open_guarded())
    from heliopuddle.preview.cdf import preview_cdf

    if local_path is not None:
        return preview_cdf(local_path)
    too_large = Content(
        kind="too_large",
        message=(
            "This storage location has no local file paths, and CDF files over "
            f"{_human_bytes(max_bytes)} are not copied to preview them."
        ),
    )
    if size is None or size > max_bytes:
        return too_large
    with tempfile.NamedTemporaryFile(prefix="hp-preview-", suffix=".cdf") as temp:
        temp_paths.append(temp.name)
        source = open_guarded()
        copied = 0
        while chunk := source.read(COPY_CHUNK):
            copied += len(chunk)
            if copied > max_bytes:
                return too_large
            temp.write(chunk)
        temp.flush()
        return preview_cdf(temp.name)
