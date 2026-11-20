"""Config errors, the default project and storage roots on ``server start``."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from heliopuddle.cli import app
from tests.unit import _cli_helpers as _fixtures
from tests.unit._cli_helpers import FakeRuntime, runner

env = _fixtures.env
fake = _fixtures.fake


def _write_project(config_dir: Path, body: str) -> None:
    for f in config_dir.iterdir():
        f.unlink()
    (config_dir / "p.yaml").write_text("project: p\nstorage_locations:\n" + body)


@pytest.mark.req("DEP-05")
def test_invalid_setting_names_the_variable(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_PORT", "not-a-port")

    for args in (["server", "start"], ["db", "upgrade"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output
        assert "Invalid settings" in result.output
        assert "HELIOPUDDLE_PORT" in result.output


@pytest.mark.req("DEP-05")
def test_invalid_yaml_names_the_file(
    env: dict[str, str], fake: FakeRuntime, config_dir: Path
) -> None:
    (config_dir / "broken.yaml").write_text("project: [unclosed\n")

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 2
    assert "Invalid project config" in result.output
    assert "broken.yaml" in result.output
    assert fake.prefect_started == 0


@pytest.mark.req("DEP-05")
def test_schema_error_names_file_and_field(
    env: dict[str, str], fake: FakeRuntime, config_dir: Path
) -> None:
    (config_dir / "empty.yaml").write_text("project: other\nstorage_locations: []\n")

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 2
    assert "empty.yaml" in result.output
    assert "storage_locations" in result.output


@pytest.mark.req("DEP-05")
def test_unset_env_var_in_config_fails(
    env: dict[str, str], fake: FakeRuntime, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HP_TEST_UNSET_VAR", raising=False)
    (config_dir / "env.yaml").write_text(
        "project: env\nstorage_locations:\n  - name: a\n    url: file://${HP_TEST_UNSET_VAR}\n"
    )

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 2
    assert "env.yaml" in result.output
    assert "HP_TEST_UNSET_VAR" in result.output


@pytest.mark.req("DEP-05")
def test_duplicate_project_names_fail(
    env: dict[str, str], fake: FakeRuntime, config_dir: Path
) -> None:
    (config_dir / "zz-copy.yaml").write_text((config_dir / "demo.yaml").read_text())

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 2
    assert "duplicate project name" in result.output


@pytest.mark.req("DEP-10")
@pytest.mark.parametrize("config", ["missing", "empty"])
def test_no_project_files_creates_and_loads_the_default_config_file(
    env: dict[str, str],
    fake: FakeRuntime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    config: str,
) -> None:
    config_dir = tmp_path / "nowhere"
    if config == "empty":
        config_dir.mkdir()
        (config_dir / "notes.txt").write_text("not a project file")
    monkeypatch.setenv("HELIOPUDDLE_CONFIG_DIR", str(config_dir))
    store = (Path(env["HELIOPUDDLE_DATA_DIR"]) / "primary-storage").resolve()

    with caplog.at_level(logging.INFO, logger="heliopuddle"):
        result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 0, result.output
    ctx = fake.contexts[0]
    assert list(ctx.projects) == ["default"]
    primary = ctx.projects["default"].storage_location()
    assert (primary.name, primary.url) == ("primary", store.as_uri())
    assert store.is_dir()  # created at startup
    with ctx.sessionmaker() as session:
        from heliopuddle.server import models

        assert models.get_project(session, "default") is not None
    config_file = Path(env["HELIOPUDDLE_DATA_DIR"]).resolve() / "default_config.yaml"
    assert config_file.is_file()
    lines = [r.getMessage() for r in caplog.records if "default config file" in r.message]
    assert len(lines) == 1
    assert str(config_dir) in lines[0] and str(store) in lines[0] and str(config_file) in lines[0]
    assert "HELIOPUDDLE_CONFIG_DIR" in lines[0]


@pytest.mark.req("DEP-10")
def test_a_project_file_replaces_the_default(
    env: dict[str, str], fake: FakeRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="heliopuddle"):
        result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert list(fake.contexts[0].projects) == ["demo"]
    assert not any("default config file" in r.message for r in caplog.records)
    assert not (Path(env["HELIOPUDDLE_DATA_DIR"]) / "primary-storage").exists()
    assert not (Path(env["HELIOPUDDLE_DATA_DIR"]) / "default_config.yaml").exists()


@pytest.mark.req("STO-08")
def test_startup_creates_missing_local_roots(
    env: dict[str, str], fake: FakeRuntime, config_dir: Path, tmp_path: Path
) -> None:
    root = tmp_path / "new" / "root"
    _write_project(config_dir, f"  - name: s\n    url: {root.as_uri()}\n")

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert root.is_dir()


@pytest.mark.req("STO-08")
def test_startup_fails_fast_on_a_root_it_may_not_create(
    env: dict[str, str], fake: FakeRuntime, config_dir: Path, tmp_path: Path
) -> None:
    root = tmp_path / "absent"
    _write_project(
        config_dir,
        f"  - name: ok\n    url: {tmp_path.as_uri()}\n"
        f"  - name: strict\n    url: {root.as_uri()}\n    create_root: false\n",
    )

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 2, result.output
    assert "project 'p'" in result.output
    assert "storage location 'strict'" in result.output
    assert root.as_uri() in result.output
    assert not root.exists()
    assert fake.prefect_started == 0 and fake.servers == []


@pytest.mark.req("STO-08")
def test_startup_fails_fast_when_creating_a_root_fails(
    env: dict[str, str],
    fake: FakeRuntime,
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from heliopuddle.storage import StorageLocation

    root = tmp_path / "cannot"
    _write_project(config_dir, f"  - name: s\n    url: {root.as_uri()}\n")

    def broken(self: StorageLocation) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(StorageLocation, "ensure_root", broken)

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 2, result.output
    assert "storage location 's'" in result.output
    assert "PermissionError: denied" in result.output
