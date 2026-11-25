"""Fixtures for the rclone backend unit tests: a stand-in rclone over local dirs."""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from heliopuddle.storage.rclone import configure, manager, set_session_hooks

FAKE_SCRIPT = Path(__file__).with_name("_fake_rclone.py")

#: ``(name, **options) -> config file`` with one alias remote over a fresh dir.
RemoteFactory = Callable[..., Path]


def write_fake_binary(directory: Path) -> Path:
    """An executable ``rclone`` that runs the stand-in with this interpreter."""
    binary = directory / "rclone"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_SCRIPT}" "$@"\n')
    binary.chmod(0o755)
    return binary


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short private directory (Unix socket paths are limited to ~100 bytes)."""
    path = Path(tempfile.mkdtemp(prefix="hpr"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def fake_rclone(short_tmp: Path) -> Iterator[Path]:
    """Use the stand-in rclone; sessions close 0.2s after their last use."""
    binary = write_fake_binary(short_tmp)
    sessions = short_tmp / "sessions"
    sessions.mkdir()
    configure(binary=str(binary), idle=0.2, tmp_dir=sessions)
    yield binary
    manager.close(10, force=True)
    set_session_hooks()
    configure(binary=None, idle=2.0, tmp_dir=None)


@pytest.fixture
def make_remote(fake_rclone: Path, short_tmp: Path) -> RemoteFactory:
    """Write a config file with one alias remote (and test knobs) over a new directory."""
    counter = iter(range(10_000))

    def make(name: str = "remote", **options: str) -> Path:
        n = next(counter)
        data = short_tmp / f"data{n}"
        data.mkdir()
        config = short_tmp / f"rclone{n}.conf"
        lines = [f"[{name}]", "type = alias", f"remote = {data}"]
        lines += [f"{k} = {v}" for k, v in options.items()]
        config.write_text("\n".join(lines) + "\n")
        return config

    return make


def data_dir(config: Path) -> Path:
    """The directory behind the (first) remote of a config written by ``make_remote``."""
    for line in config.read_text().splitlines():
        if line.startswith("remote = "):
            return Path(line.removeprefix("remote = "))
    raise AssertionError("no remote line")
