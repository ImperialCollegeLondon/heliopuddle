"""Shared environment, fake runtime and fixtures for the server CLI tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from heliopuddle.server import start as start_mod
from heliopuddle.server.context import get_context, reset_context

runner = CliRunner()


SETTINGS_ENV = (
    "HELIOPUDDLE_CONFIG_DIR",
    "HELIOPUDDLE_DATA_DIR",
    "HELIOPUDDLE_DATABASE_URL",
    "HELIOPUDDLE_HOST",
    "HELIOPUDDLE_PORT",
    "HELIOPUDDLE_UI_PORT",
    "HELIOPUDDLE_UI_HOST",
    "HELIOPUDDLE_UI_API_URL",
    "HELIOPUDDLE_UI_ROOT_PATH",
    "HELIOPUDDLE_API_ROOT_PATH",
)


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_dir: Path
) -> Iterator[dict[str, str]]:
    """Point ``HELIOPUDDLE_*`` at the tmp config dir and a tmp data dir."""
    for name in SETTINGS_ENV:
        monkeypatch.setenv(name, "")  # record the original value so it is restored
        monkeypatch.delenv(name)
    values = {
        "HELIOPUDDLE_CONFIG_DIR": str(config_dir),
        "HELIOPUDDLE_DATA_DIR": str(tmp_path / "data"),
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    yield values
    reset_context()


def db_url(env: dict[str, str]) -> str:
    return f"sqlite:///{Path(env['HELIOPUDDLE_DATA_DIR']).resolve() / 'heliopuddle.db'}"


@dataclass
class FakeRuntime:
    """Records what ``server start`` would have run."""

    prefect_started: int = 0
    prefect_stopped: int = 0
    services_started: int = 0
    services_stopped: int = 0
    servers: list[start_mod.ServerSpec] = field(default_factory=list)
    waited: bool = False
    contexts: list[Any] = field(default_factory=list)
    prepared: list[Any] = field(default_factory=list)
    adopted: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    """Replace Prefect, the services runner and uvicorn with recorders."""
    rec = FakeRuntime()

    class Runtime:
        def stop(self) -> None:
            rec.prefect_stopped += 1
            rec.events.append("prefect.stop")

    def fake_start_prefect(settings: Any) -> Runtime:
        rec.prefect_started += 1
        return Runtime()

    class Services:
        def __init__(self, deployments: list[Any]) -> None:
            self.deployments = deployments

        def start(self) -> None:
            rec.services_started += 1

        def stop(self) -> None:
            rec.services_stopped += 1
            rec.events.append("services.stop")

        def is_alive(self) -> bool:
            return True

    def fake_run_servers(specs: list[start_mod.ServerSpec], shutdown: Any, services: Any) -> None:
        rec.servers = specs
        rec.contexts.append(get_context() if any(s.name != "ui" for s in specs) else None)

    def fake_wait(shutdown: Any, services: Any) -> None:
        rec.waited = True

    monkeypatch.setattr(start_mod, "start_prefect", fake_start_prefect)
    monkeypatch.setattr(start_mod, "ServicesRunner", Services)
    monkeypatch.setattr(start_mod, "build_deployments", lambda **kw: ["verify-files"])
    monkeypatch.setattr(
        start_mod, "prepare_index_services", lambda projects, deps: rec.prepared.append(deps)
    )
    monkeypatch.setattr(start_mod, "prepare_rule_services", lambda projects, deps: None)
    monkeypatch.setattr(start_mod, "prepare_reconcile_services", lambda deps: None)
    monkeypatch.setattr(start_mod, "reconcile_rule_services", lambda ctx: None)
    monkeypatch.setattr(
        start_mod, "submit_adoption_runs", lambda ctx, deps: rec.adopted.append(deps)
    )
    monkeypatch.setattr(start_mod, "install_rclone", lambda settings: None)
    monkeypatch.setattr(
        start_mod, "register_rclone_block_type", lambda: rec.events.append("rclone.register")
    )
    monkeypatch.setattr(
        start_mod, "close_rclone_sessions", lambda: rec.events.append("rclone.close")
    )
    monkeypatch.setattr(start_mod, "run_servers", fake_run_servers)
    monkeypatch.setattr(start_mod, "wait_for_shutdown", fake_wait)
    return rec
