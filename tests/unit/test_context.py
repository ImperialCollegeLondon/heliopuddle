"""The process-wide app context."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from heliopuddle.config import ConfigError
from heliopuddle.server.context import get_context, reset_context
from heliopuddle.settings import get_settings


@pytest.fixture
def clean() -> Iterator[None]:
    reset_context()
    get_settings.cache_clear()
    yield
    reset_context()
    get_settings.cache_clear()


@pytest.mark.usefixtures("clean")
def test_get_context_builds_from_env_settings_once(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HELIOPUDDLE_DATA_DIR", str(tmp_path / "data"))
    ctx = get_context()
    assert get_context() is ctx
    assert list(ctx.projects) == ["demo"]
    assert ctx.storage_location_config("demo", "primary") is not None
    assert ctx.storage_location_config("demo", "nope") is None
    assert ctx.storage_location_config("nope", "primary") is None
    assert (tmp_path / "data").is_dir()  # SQLite parent dir created
    ctx.dispose()


@pytest.mark.req("DEP-05")
@pytest.mark.usefixtures("clean")
def test_get_context_fails_fast_on_bad_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bad.yaml").write_text("project: [\n")
    monkeypatch.setenv("HELIOPUDDLE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ConfigError, match=r"bad\.yaml"):
        get_context()


@pytest.mark.req("DEP-10")
@pytest.mark.usefixtures("clean")
def test_get_context_without_project_files_uses_the_default_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_CONFIG_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("HELIOPUDDLE_DATA_DIR", str(tmp_path / "data"))
    ctx = get_context()
    assert list(ctx.projects) == ["default"]
    location = ctx.storage_location_config("default", "primary")
    assert location is not None
    assert location.url == (tmp_path / "data" / "primary-storage").resolve().as_uri()
    assert (tmp_path / "data" / "default_config.yaml").is_file()
    ctx.dispose()
