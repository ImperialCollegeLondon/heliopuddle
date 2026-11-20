"""Symlinks and credentials in storage URLs (STO-15, STO-16)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from heliopuddle.server.start import StartupError, ensure_storage_roots
from heliopuddle.storage import StorageLocation, StorageRootMissingError
from heliopuddle.storage.location import SymlinkError
from heliopuddle.storage.redact import redact_text, redact_url


@pytest.fixture
def store(tmp_path: Path) -> StorageLocation:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "real.txt").write_bytes(b"real")
    (root / "sub" / "inner.txt").write_bytes(b"inner")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top secret")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leak.txt").write_bytes(b"leak")
    (root / "link.txt").symlink_to(secret)
    (root / "linkdir").symlink_to(outside_dir, target_is_directory=True)
    return StorageLocation(f"file://{root}")


@pytest.mark.req("STO-15")
def test_listing_skips_symlinks_to_files_and_directories(store: StorageLocation) -> None:
    assert [e.path for e in store.list_files()] == ["real.txt", "sub/inner.txt"]


@pytest.mark.req("STO-15")
def test_reads_and_writes_refuse_symlinks(store: StorageLocation, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    with pytest.raises(SymlinkError):
        store.open_read("link.txt")
    with pytest.raises(SymlinkError):
        store.open_read("linkdir/leak.txt")
    with pytest.raises(SymlinkError):
        store.md5("link.txt")
    with pytest.raises(SymlinkError):
        store.write("link.txt", secret)
    with pytest.raises(SymlinkError):
        store.write("linkdir/new.txt", secret)
    with pytest.raises(SymlinkError):
        store.move("real.txt", "linkdir/moved.txt")
    assert not (tmp_path / "outside" / "new.txt").exists()
    assert secret.read_bytes() == b"top secret"
    with store.open_read("sub/inner.txt") as f:  # regular files still work
        assert f.read() == b"inner"


@pytest.mark.req("STO-15")
def test_a_symlinked_root_is_allowed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.txt").write_bytes(b"a")
    (tmp_path / "root").symlink_to(real, target_is_directory=True)
    store = StorageLocation(f"file://{tmp_path / 'root'}")
    assert [e.path for e in store.list_files()] == ["a.txt"]
    with store.open_read("a.txt") as f:
        assert f.read() == b"a"


@pytest.mark.req("STO-16")
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sftp://user:pa:ss@host/path", "sftp://user:***@host/path"),
        ("sftp://user:secret@host:2222/a/b", "sftp://user:***@host:2222/a/b"),
        ("sftp://user@host/path", "sftp://user@host/path"),
        ("file:///data", "file:///data"),
        ("s3://bucket/prefix", "s3://bucket/prefix"),
        ("https://h/c?sig=abc123&x=1", "https://h/c?sig=***&x=1"),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected


@pytest.mark.req("STO-16")
def test_redact_text_hides_urls_quoted_in_messages() -> None:
    text = "ConnectionError: cannot reach sftp://bob:hunter2@host/x (timeout)"
    assert redact_text(text) == "ConnectionError: cannot reach sftp://bob:***@host/x (timeout)"


@pytest.mark.req("STO-16")
def test_root_missing_error_does_not_carry_the_password() -> None:
    url = "sftp://bob:hunter2@host/x"
    error = StorageRootMissingError(url, f"boom {url}")
    assert "hunter2" not in str(error)
    assert "hunter2" not in error.url
    assert "bob:***@host" in str(error)


@pytest.mark.req("STO-16")
def test_startup_error_does_not_print_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(
        name="remote", url="sftp://bob:hunter2@host/x", storage_options={}, create_root="auto"
    )
    ctx = SimpleNamespace(projects={"p": SimpleNamespace(project="p", storage_locations=[cfg])})

    class Boom:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def ensure_root(self) -> None:
            raise OSError("cannot connect to sftp://bob:hunter2@host/x")

    monkeypatch.setattr("heliopuddle.server.start.StorageLocation", Boom)
    with pytest.raises(StartupError) as error:
        ensure_storage_roots(ctx)  # type: ignore[arg-type]
    assert "hunter2" not in str(error.value)
    assert "bob:***@host" in str(error.value)
