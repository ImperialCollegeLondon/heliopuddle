"""Credentials and server-only storage locations once blocks resolve (STO-05, STO-18).

Covers what only shows after resolution: a first storage location or a
``direct_access`` flag decided by a block, a malformed placeholder never
echoing an expanded secret, and secrets inside JSON-text Secrets redacted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from heliopuddle.config import ConfigError, load_project
from heliopuddle.server.api.projects._shared import (
    _direct_access,  # pyright: ignore[reportPrivateUsage]
)
from heliopuddle.storage import credentials as creds
from heliopuddle.storage.credentials import StorageLocationIncomplete, location_problems, resolve
from heliopuddle.storage.redact import redact_text
from tests.unit.test_storage_credentials import Clock, FakeResolver


@pytest.fixture
def blocks() -> Iterator[FakeResolver]:
    clock = Clock()
    creds.set_clock(clock)
    resolver = FakeResolver()
    creds.set_block_resolver(resolver)
    yield resolver
    creds.set_block_resolver(None)
    creds.set_clock(None)


def _project(tmp_path: Path, body: str) -> list:
    path = tmp_path / "p.yaml"
    path.write_text(f"project: p\nstorage_locations:\n{body}")
    return load_project(path).storage_locations


BLOCK_FIRST = """\
  - name: primary
    block: primary-fs
  - name: archive
    url: file:///archive
"""


@pytest.mark.req("STO-05", "STO-19")
def test_first_location_from_a_block_cannot_resolve_to_rclone(
    tmp_path: Path, blocks: FakeResolver
) -> None:
    first, second = _project(tmp_path, BLOCK_FIRST)
    assert first.is_first and not second.is_first
    blocks.filesystems["primary-fs"] = ("rclone://archive/p", {"config_file": "/x.conf"})
    with pytest.raises(StorageLocationIncomplete) as error:
        resolve(first)
    assert "first storage location receives uploads" in error.value.problems[0]
    assert "storage_locations.0" in error.value.problems[0]


@pytest.mark.req("STO-05", "STO-18")
def test_first_location_from_a_whole_url_secret_cannot_resolve_to_rclone(
    tmp_path: Path, blocks: FakeResolver
) -> None:
    (first,) = _project(
        tmp_path, "  - name: primary\n    url: '{{ prefect.blocks.secret.primary-url }}'\n"
    )
    blocks.secrets["primary-url"] = "rclone://archive/p"
    problems = location_problems(first)
    assert len(problems) == 1
    assert "cannot be an rclone storage location" in problems[0]


@pytest.mark.req("STO-05", "STO-19")
def test_a_later_location_may_resolve_to_rclone(tmp_path: Path, blocks: FakeResolver) -> None:
    _, second = _project(
        tmp_path,
        "  - name: primary\n    url: file:///data\n  - name: remote\n    block: remote-fs\n",
    )
    blocks.filesystems["remote-fs"] = ("rclone://archive/p", {"config_file": "/x.conf"})
    url, _ = resolve(second)
    assert url == "rclone://archive/p"


@pytest.mark.req("STO-05", "STO-18")
def test_direct_access_follows_the_resolved_url(tmp_path: Path, blocks: FakeResolver) -> None:
    _, remote = _project(
        tmp_path,
        "  - name: primary\n    url: file:///data\n  - name: remote\n    block: remote-fs\n",
    )
    # Unresolvable: never direct.
    assert _direct_access("prefect-block:remote-fs", remote) is False
    blocks.filesystems["remote-fs"] = ("file:///elsewhere", {})
    creds.clear_cache()
    assert _direct_access("prefect-block:remote-fs", remote) is True
    blocks.filesystems["remote-fs"] = ("rclone://archive/p", {"config_file": "/x.conf"})
    creds.clear_cache()
    assert _direct_access("prefect-block:remote-fs", remote) is False


@pytest.mark.req("STO-05")
def test_direct_access_without_blocks_or_config() -> None:
    assert _direct_access("rclone://archive/p", None) is False
    assert _direct_access("file:///data", None) is True


@pytest.mark.req("STO-18")
def test_bad_placeholder_error_does_not_echo_an_expanded_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HP_TEST_PASSWORD", "Very-Secret-Password-42")
    with pytest.raises(ConfigError) as error:
        _project(
            tmp_path,
            "  - name: a\n    url: memory://x\n    storage_options:\n"
            "      password: '${HP_TEST_PASSWORD}{{ prefect.blocks.secret.Bad_Name }}'\n",
        )
    message = str(error.value)
    assert "storage_options.password" in message
    assert "Very-Secret-Password-42" not in message


@pytest.mark.req("STO-18", "STO-16")
def test_values_inside_a_json_text_secret_are_redacted(
    tmp_path: Path, blocks: FakeResolver
) -> None:
    (location,) = _project(
        tmp_path,
        "  - name: a\n    url: memory://x\n"
        "    storage_options: '{{ prefect.blocks.secret.opts }}'\n",
    )
    blocks.secrets["opts"] = json.dumps({"auth": {"password": "Nested-Json-Password-7"}})
    _, options = resolve(location)
    assert options == {"auth": {"password": "Nested-Json-Password-7"}}
    assert "Nested-Json-Password-7" not in redact_text("login failed: Nested-Json-Password-7")
