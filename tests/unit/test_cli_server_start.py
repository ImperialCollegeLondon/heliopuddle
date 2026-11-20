"""Process modes, ports and services of ``server start``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from heliopuddle.cli import app
from heliopuddle.server import start as start_mod
from heliopuddle.server.services.runtime import PrefectStartupError
from tests.unit import _cli_helpers as _fixtures
from tests.unit._cli_helpers import FakeRuntime, db_url, runner

env = _fixtures.env
fake = _fixtures.fake


@pytest.mark.req("SVC-01")
def test_unreachable_prefect_exits_non_zero(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(settings: Any) -> None:
        raise PrefectStartupError("Prefect API at http://x/api (PREFECT_API_URL) is unreachable")

    monkeypatch.setattr(start_mod, "start_prefect", failing)

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 1
    assert "PREFECT_API_URL" in result.output
    assert "unreachable" in result.output
    assert fake.services_started == 0


@pytest.mark.req("SVC-01")
def test_services_failing_to_start_exit_non_zero(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing:
        def __init__(self, deployments: list[Any]) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("Serving the deployments failed: boom")

        def stop(self) -> None:
            fake.services_stopped += 1

    monkeypatch.setattr(start_mod, "ServicesRunner", Failing)

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 1
    assert "Serving the deployments failed" in result.output
    assert fake.services_stopped == 1
    assert fake.prefect_stopped == 1


@pytest.mark.req("DEP-02")
def test_host_and_port_flags_override_settings(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_HOST", "127.0.0.2")
    monkeypatch.setenv("HELIOPUDDLE_PORT", "9000")

    result = runner.invoke(app, ["server", "start", "--host", "0.0.0.0", "--port", "9123"])

    assert result.exit_code == 0, result.output
    [spec] = fake.servers
    assert (spec.host, spec.port) == ("0.0.0.0", 9123)


@pytest.mark.req("DEP-02")
def test_no_ui_serves_only_the_api(env: dict[str, str], fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["server", "start", "--no-ui"])

    assert result.exit_code == 0, result.output
    assert [s.name for s in fake.servers] == ["api"]
    assert fake.services_started == 1


@pytest.mark.req("DEP-02")
def test_no_services_starts_no_runner(env: dict[str, str], fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["server", "start", "--no-services"])

    assert result.exit_code == 0, result.output
    assert fake.services_started == 0
    assert fake.prefect_started == 1  # the API's inline verify still needs Prefect


@pytest.mark.req("DEP-02")
def test_services_only_runs_no_http_server(env: dict[str, str], fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["server", "start", "--no-api", "--no-ui"])

    assert result.exit_code == 0, result.output
    assert fake.servers == []
    assert fake.waited
    assert fake.services_started == 1


@pytest.mark.req("DEP-02")
def test_nothing_to_run_is_an_error(env: dict[str, str], fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["server", "start", "--no-api", "--no-ui", "--no-services"])

    assert result.exit_code == 2
    assert "Nothing to run" in result.output


@pytest.mark.req("UI-03")
def test_ui_on_its_own_port(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_UI_PORT", "8181")

    result = runner.invoke(app, ["server", "start", "--port", "8180"])

    assert result.exit_code == 0, result.output
    assert [(s.name, s.port) for s in fake.servers] == [("api", 8180), ("ui", 8181)]


@pytest.mark.req("UI-03")
def test_ui_only_process_needs_no_db_or_config(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_CONFIG_DIR", str(tmp_path / "no-config-here"))
    monkeypatch.setenv("HELIOPUDDLE_UI_API_URL", "http://api.example:8080/api/v1")

    result = runner.invoke(
        app, ["server", "start", "--no-api", "--no-services", "--no-migrate", "--port", "8282"]
    )

    assert result.exit_code == 0, result.output
    assert [(s.name, s.port) for s in fake.servers] == [("ui", 8282)]
    assert fake.prefect_started == 0
    assert not (Path(env["HELIOPUDDLE_DATA_DIR"]) / "heliopuddle.db").exists()


@pytest.mark.req("UI-03")
def test_ui_without_api_needs_ui_api_url(env: dict[str, str], fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["server", "start", "--no-api", "--no-services"])

    assert result.exit_code == 2
    assert "HELIOPUDDLE_UI_API_URL" in result.output


@pytest.mark.req("UI-03")
def test_shared_port_rejects_two_root_paths(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELIOPUDDLE_API_ROOT_PATH", "/a")
    monkeypatch.setenv("HELIOPUDDLE_UI_ROOT_PATH", "/b")

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 2
    assert "different root paths" in result.output


def test_settings_are_exported_for_flow_runs(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HELIOPUDDLE_CONFIG_DIR", "config")  # relative to the cwd

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert os.environ["HELIOPUDDLE_CONFIG_DIR"] == str((tmp_path / "config").resolve())
    assert os.environ["HELIOPUDDLE_DATABASE_URL"] == db_url(env)


@pytest.mark.req("DEP-07")
@pytest.mark.parametrize(
    ("index_yaml", "field_name"),
    [
        ("  full_schedule: 'every night'\n", "full_schedule"),
        ("  batch_size: 0\n", "batch_size"),
        ("  settle_time: soon\n", "settle_time"),
        ("  incremental_prefixes: ['a/{project}']\n", "incremental_prefixes"),
    ],
)
def test_invalid_index_config_stops_startup(
    env: dict[str, str], fake: FakeRuntime, config_dir: Path, index_yaml: str, field_name: str
) -> None:
    (config_dir / "indexed.yaml").write_text(
        "project: indexed\n"
        "storage_locations:\n"
        "  - name: a\n"
        "    url: file:///tmp/indexed\n"
        f"index:\n{index_yaml}"
    )

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 2, result.output
    assert "Invalid project config" in result.output
    assert "indexed.yaml" in result.output
    assert f"index.{field_name}" in result.output
    assert fake.prefect_started == 0
    assert fake.services_started == 0


@pytest.mark.req("SVC-09")
def test_start_prepares_index_services_then_adopts(env: dict[str, str], fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 0, result.output
    assert fake.prepared == [["verify-files"]]
    assert fake.adopted == [["verify-files"]]


@pytest.mark.req("SVC-09")
def test_no_services_means_no_index_preparation_or_adoption(
    env: dict[str, str], fake: FakeRuntime
) -> None:
    result = runner.invoke(app, ["server", "start", "--no-services"])

    assert result.exit_code == 0, result.output
    assert fake.prepared == [] and fake.adopted == []


@pytest.mark.req("SVC-09")
def test_failing_index_preparation_stops_startup(
    env: dict[str, str], fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(projects: Any, deps: Any) -> None:
        raise RuntimeError("prefect said no")

    monkeypatch.setattr(start_mod, "prepare_index_services", broken)

    result = runner.invoke(app, ["server", "start"])

    assert result.exit_code == 1
    assert (
        "Preparing the service deployments failed: RuntimeError: prefect said no" in result.output
    )
    assert fake.services_started == 0
    assert fake.prefect_stopped == 1
