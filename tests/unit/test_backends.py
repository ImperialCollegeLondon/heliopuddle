"""STO-06 and DEP-08: backend knowledge is isolated in ``storage/backends.py``."""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import fsspec
import pytest

from heliopuddle.storage import StorageLocation, backends

SRC = Path(__file__).resolve().parents[2] / "src" / "heliopuddle"
ROOT = SRC.parents[1]
BACKENDS = SRC / "storage" / "backends.py"
# hp's own rclone:// filesystem is a backend package (like s3fs), not hp code
# that uses storage, so it may name what it implements.
BACKEND_PACKAGES = (SRC / "storage" / "rclone",)

# Protocol names, URL forms, backend packages and backend metadata keys that
# may only appear in storage/backends.py.
FORBIDDEN_TOKENS = [
    r"az://",
    r"abfss?://",
    r"abfs",
    r"adlfs",
    r"azure",
    r"blob",
    r"s3://",
    r"s3a",
    r"s3fs",
    r"boto",
    r"gs://",
    r"gcs://",
    r"gcsfs",
    r"\bgcs\b",
    r"smb://",
    r"smbprotocol",
    r"sftp",
    r"paramiko",
    r"content_md5",
    r"content_settings",
    r"etag",
    r"storageclass",
    r"md5hash",
    r"last_modified",
    r"lastmodified",
]
_PATTERN = re.compile("|".join(f"(?:{t})" for t in FORBIDDEN_TOKENS), re.IGNORECASE)

SCANNED_SUFFIXES = {".py", ".html", ".css", ".js", ".mako", ".yaml", ".toml"}

# The target protocols of STO-06, per extra (DEP-08).
TARGET_PROTOCOLS = ["file", "smb", "sftp", "s3", "az", "abfs", "gs", "gcs", "rclone"]


@pytest.mark.req("STO-06")
def test_no_backend_knowledge_outside_backends_module() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*")):
        if path == BACKENDS or not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if any(package in path.parents for package in BACKEND_PACKAGES):
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            match = _PATTERN.search(line)
            if match:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {match.group(0)!r}: {line}")
    assert not offenders, "backend-specific code outside storage/backends.py:\n" + "\n".join(
        offenders
    )


@pytest.mark.req("STO-06")
def test_the_scan_catches_backend_tokens() -> None:
    for sample in ("url='az://c/p'", "import s3fs", "info['ETag']", "SFTP", "md5Hash"):
        assert _PATTERN.search(sample), sample
    assert not _PATTERN.search("fs.makedirs(parent, exist_ok=True)")


@pytest.mark.req("STO-06", "DEP-08")
@pytest.mark.parametrize("protocol", TARGET_PROTOCOLS)
def test_target_protocols_resolve(protocol: str) -> None:
    cls = fsspec.get_filesystem_class(protocol)
    protocols = (cls.protocol,) if isinstance(cls.protocol, str) else tuple(cls.protocol)
    assert protocol in protocols


@pytest.mark.req("STO-06")
@pytest.mark.parametrize(
    ("url", "options"),
    [
        ("s3://bucket/prefix", {"anon": True}),
        ("gs://bucket/prefix", {"token": "anon"}),
        ("az://container/prefix", {"account_name": "devaccount"}),
    ],
)
def test_storage_location_builds_for_object_store_urls(
    url: str, options: dict[str, object]
) -> None:
    # Building connects to nothing.
    loc = StorageLocation(url, options)
    assert loc.root == url.split("://", 1)[1]


@pytest.mark.req("DEP-08")
def test_extras_declared() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    expected = {
        "s3": "s3fs",
        "azure": "adlfs",
        "gcs": "gcsfs",
        "sftp": "paramiko",
        "smb": "smbprotocol",
    }
    for extra, package in expected.items():
        assert any(dep.startswith(package) for dep in extras[extra]), extra
    assert all(extra in extras["all"][0] for extra in expected)
    assert not any(dep.startswith(tuple(expected.values())) for dep in project["dependencies"])


@pytest.mark.req("STO-06")
@pytest.mark.parametrize(
    ("protocols", "root", "anchor"),
    [
        ("file", "/data/project", "/data/project"),
        ("memory", "/root", "/root"),
        (("abfs", "az"), "container/a/b", "container"),
        (("s3", "s3a"), "bucket", "bucket"),
        ("gs", "/bucket/x/", "bucket"),
    ],
)
def test_root_anchor(protocols: str | tuple[str, ...], root: str, anchor: str) -> None:
    assert backends.root_anchor(protocols, root) == anchor


class _Settings:
    def __init__(self, content_md5: object) -> None:
        self.content_md5 = content_md5


_RAW = bytes(range(16))
_HEXD = _RAW.hex()
_B64 = "AAECAwQFBgcICQoLDA0ODw=="


@pytest.mark.req("STO-06")
@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"md5": _HEXD.upper()}, _HEXD),
        ({"md5": _B64}, _HEXD),
        ({"content_settings": {"content_md5": _RAW}}, _HEXD),
        ({"content_settings": {"content_md5": bytearray(_RAW)}}, _HEXD),
        ({"content_settings": _Settings(_RAW)}, _HEXD),
        ({"content_settings": _Settings(None)}, None),
        ({"content_md5": _B64}, _HEXD),
        ({"content_md5": "00" * 16}, "00" * 16),
        ({"content_md5": b"short"}, None),
        ({"content_md5": "not base64!"}, None),
        ({"content_md5": "AAAA"}, None),  # valid base64, not 16 bytes
        ({"md5Hash": _B64}, _HEXD),
        ({"ETag": '"' + "AB" * 16 + '"', "StorageClass": "STANDARD"}, "ab" * 16),
        ({"ETag": '"' + "ab" * 16 + '-2"', "StorageClass": "STANDARD"}, None),
        ({"ETag": "ab" * 16}, None),  # not an S3 listing
        ({"ETag": "zz" * 16, "StorageClass": "STANDARD"}, None),
        ({"content_md5": None, "md5Hash": _B64}, _HEXD),
        ({}, None),
    ],
)
def test_native_md5(info: dict[str, object], expected: str | None) -> None:
    assert backends.native_md5(info) == expected


_T = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.req("STO-06")
@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"mtime": _T.timestamp()}, _T),
        ({"mtime": _T}, _T),
        ({"mtime": _T.replace(tzinfo=None)}, _T),
        ({"last_modified": _T}, _T),
        ({"LastModified": _T}, _T),
        ({"updated": "2026-01-02T03:04:05Z"}, _T),
        ({"updated": "2026-01-02T03:04:05"}, _T),
        ({"updated": "yesterday", "created": _T.timestamp()}, _T),
        ({"LastModified": "", "created": _T.timestamp()}, _T),
        ({"size": 1}, None),
    ],
)
def test_info_mtime(info: dict[str, object], expected: datetime | None) -> None:
    assert backends.info_mtime(info) == expected
