"""Server wiring of the rclone backend: config rules, API, hooks (STO-05, STO-20, DEP-21)."""

from __future__ import annotations

import contextlib
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heliopuddle.config import ConfigError, load_project
from heliopuddle.server.api import create_api
from heliopuddle.server.api.errors import install_rclone_busy
from heliopuddle.server.context import AppContext, set_context
from heliopuddle.server.services import rclone_hooks
from heliopuddle.settings import Settings
from heliopuddle.storage import backends, rclone
from heliopuddle.storage import credentials as creds
from heliopuddle.storage.rclone import session as rclone_session

REMOTE_CONF = "[archive]\ntype = memory\n"


@pytest.fixture
def fake_rclone(tmp_path: Path) -> Path:
    exe = tmp_path / "bin" / "rclone"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


@pytest.fixture
def installed(settings: Settings, fake_rclone: Path) -> Iterator[Settings]:
    """install_rclone with a fake binary; everything restored afterwards."""
    configured = settings.model_copy(update={"rclone_binary": str(fake_rclone)})
    rclone_hooks.install_rclone(configured)
    yield configured
    creds.unregister_problem_check(rclone_hooks.rclone_problems)
    rclone.set_session_hooks(None, None)
    rclone.configure(binary=None, auto_download=True)
    rclone_hooks._installed = False  # pyright: ignore[reportPrivateUsage]


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rclone-project.yaml"
    path.write_text(body)
    return path


@pytest.mark.req("STO-05")
def test_server_only_is_the_rclone_protocol() -> None:
    assert backends.server_only("rclone://archive/a/b")
    assert backends.server_only("RCLONE://archive")
    assert not backends.server_only("file:///data")
    assert not backends.server_only("prefect-block:x")
    assert not backends.server_only(None)
    assert not backends.server_only("relative/path")


@pytest.mark.req("STO-05")
def test_rclone_cannot_be_the_first_storage_location(tmp_path: Path) -> None:
    body = """\
project: p
storage_locations:
  - name: remote
    url: rclone://archive/p
    storage_options: {config_file: /x/rclone.conf}
  - name: primary
    url: file:///data
"""
    with pytest.raises(ConfigError, match=r"first storage location .* cannot be an rclone"):
        load_project(_write(tmp_path, body))


@pytest.mark.req("STO-05", "STO-20")
def test_rclone_after_the_first_storage_location_needs_services(tmp_path: Path) -> None:
    body = f"""\
project: p
storage_locations:
  - name: primary
    url: file://{tmp_path}/data
  - name: remote
    url: rclone://archive/p
    storage_options: {{config_file: {tmp_path}/rclone.conf}}
"""
    project = load_project(_write(tmp_path, body))
    primary, remote = project.storage_locations
    assert not primary.needs_services
    assert remote.needs_services


@pytest.mark.req("STO-20")
def test_problems_name_a_missing_binary(settings: Settings, tmp_path: Path) -> None:
    rclone.configure(binary=str(tmp_path / "missing"), auto_download=False)
    try:
        problems = rclone_hooks.rclone_problems(
            "rclone://archive/p", {"config_file": str(tmp_path / "rclone.conf")}
        )
    finally:
        rclone.configure(binary=None, auto_download=True)
    assert len(problems) == 1
    assert "not an executable" in problems[0]


@pytest.mark.req("STO-20")
def test_problems_for_config_sources_and_remotes(installed: Settings, tmp_path: Path) -> None:
    conf = tmp_path / "rclone.conf"
    check = rclone_hooks.rclone_problems
    assert check("file:///data", {}) == []
    assert "exactly one of" in check("rclone://archive/p", {})[0]
    assert "does not exist" in check("rclone://archive/p", {"config_file": str(conf)})[0]

    conf.write_text(REMOTE_CONF)
    rclone_hooks._cache.clear()  # pyright: ignore[reportPrivateUsage]
    assert check("rclone://archive/p", {"config_file": str(conf)}) == []
    assert check("rclone://other/p", {"config_file": str(conf)}) == [
        f"rclone remote 'other' is not defined in config file {str(conf)!r}"
    ]


@pytest.mark.req("STO-20")
def test_problem_lookups_are_cached(
    installed: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = tmp_path / "rclone.conf"
    conf.write_text(REMOTE_CONF)
    calls: list[str] = []
    real = rclone_hooks.load_config

    def counting(source: Any, store: Any) -> str:
        calls.append(source.name)
        return real(source, store)

    monkeypatch.setattr(rclone_hooks, "load_config", counting)
    for _ in range(3):
        assert rclone_hooks.rclone_problems("rclone://archive/p", {"config_file": str(conf)}) == []
    assert len(calls) == 1


@pytest.mark.req("STO-20", "DEP-21")
def test_install_configures_hooks_and_settings(installed: Settings) -> None:
    assert isinstance(rclone.manager.block_store, rclone_hooks.PrefectConfigBlockStore)
    assert rclone.manager.lock_factory is rclone_hooks.prefect_lock
    assert rclone.find_rclone() == installed.rclone_binary
    # replace=False keeps an earlier install (a flow-run process, or a test's hooks).
    rclone.set_session_hooks(None, None)
    rclone_hooks.install_rclone(installed, replace=False)
    assert rclone.manager.block_store is None


@pytest.mark.req("STO-20")
def test_prefect_lock_creates_limits_and_holds_every_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[str, int]] = []
    held: list[tuple[list[str], float | None, bool]] = []

    @contextlib.contextmanager
    def fake_concurrency(names: list[str], **kw: Any) -> Iterator[None]:
        held.append((names, kw["timeout_seconds"], kw["strict"]))
        yield

    monkeypatch.setattr(
        rclone_hooks, "ensure_concurrency_limit", lambda n, s: created.append((n, s))
    )
    monkeypatch.setattr(rclone_hooks, "hp_concurrency", fake_concurrency)
    with rclone_hooks.prefect_lock(["rclone:a", "rclone:b"], 5.0):
        pass
    assert created == [("rclone:a", 1), ("rclone:b", 1)]
    assert held == [(["rclone:a", "rclone:b"], 5.0, True)]


class _FakeBlock:
    store: ClassVar[dict[str, str]] = {}

    def __init__(self, contents: str) -> None:
        self.config_file_contents = contents

    @classmethod
    def load(cls, name: str, _sync: bool = True) -> _FakeBlock:
        if name not in cls.store:
            raise ValueError(f"Unable to find block document named {name}")
        return cls(cls.store[name])

    def save(self, name: str, overwrite: bool = False, _sync: bool = True) -> None:
        assert overwrite
        self.store[name] = self.config_file_contents


@pytest.mark.req("STO-20")
def test_block_store_loads_and_saves_config_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeBlock.store = {"sp": REMOTE_CONF}
    monkeypatch.setattr(rclone_hooks, "config_block_class", lambda: _FakeBlock)
    store = rclone_hooks.PrefectConfigBlockStore()
    assert store.load("sp") == REMOTE_CONF
    store.save("sp", REMOTE_CONF + "token = new\n")
    assert _FakeBlock.store["sp"].endswith("token = new\n")
    with pytest.raises(LookupError):
        store.load("missing")


@pytest.mark.req("STO-20")
def test_block_class_matches_the_managed_file_transfer_block() -> None:
    cls = rclone_hooks.config_block_class()
    assert cls.get_block_type_name() == "RClone Remote Config File [ManagedFileTransfer]"
    assert {"remote_name", "config_file_contents"} <= set(cls.model_fields)


def _busy_app(wait: float) -> FastAPI:
    app = FastAPI()
    install_rclone_busy(app, lambda: wait)

    @app.get("/wait")
    def current_wait() -> dict[str, float | None]:
        return {"wait": rclone_session._WAIT.get()}  # pyright: ignore[reportPrivateUsage]

    @app.get("/busy")
    def busy() -> None:
        raise rclone.RcloneBusyError("config block 'sp' is in use")

    return app


@pytest.mark.req("STO-20")
def test_api_requests_wait_a_bounded_time_and_answer_503() -> None:
    with TestClient(_busy_app(7.5)) as client:
        assert client.get("/wait").json() == {"wait": 7.5}
        response = client.get("/busy")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "8"
    assert response.json()["reason"] == "rclone_busy"


@pytest.mark.req("STO-05")
def test_rclone_locations_have_no_direct_access(
    tmp_path: Path, store: Path, settings: Settings, migrated_db: Path
) -> None:
    (settings.config_dir / "p.yaml").write_text(
        f"""\
project: p
storage_locations:
  - name: primary
    url: file://{store}
  - name: remote
    url: rclone://archive/p
    storage_options: {{config_file: {tmp_path}/rclone.conf}}
"""
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(migrated_db, settings.data_dir / "heliopuddle.db")
    ctx = AppContext.from_settings(settings)
    ctx.sync_config()
    try:
        with TestClient(create_api(ctx)) as client:
            rows = {r["name"]: r for r in client.get("/projects/p/storage-locations").json()}
    finally:
        set_context(None)
        ctx.dispose()
    assert rows["primary"]["direct_access"] is True
    assert rows["remote"]["direct_access"] is False


@pytest.mark.req("STO-05")
@pytest.mark.parametrize(
    ("direct_access", "status", "direct"),
    [(True, "ok", True), (False, "ok", False), (True, "incomplete", False)],
)
def test_client_transfers_through_the_api_without_direct_access(
    monkeypatch: pytest.MonkeyPatch, direct_access: bool, status: str, direct: bool
) -> None:
    from heliopuddle.client._base import ClientBase
    from heliopuddle.server.schemas import StorageLocationDetail

    detail = StorageLocationDetail(
        name="remote",
        url="rclone://archive/p",
        accept_external_changes=True,
        active=True,
        status=status,  # pyright: ignore[reportArgumentType]
        direct_access=direct_access,
    )
    client = object.__new__(ClientBase)
    monkeypatch.setattr(client, "get_storage_location", lambda name=None: detail, raising=False)
    found = client._transfer_location("remote")  # pyright: ignore[reportPrivateUsage]
    assert (found is detail) is direct


@pytest.mark.req("STO-05")
def test_registering_rclone_again_does_not_fail() -> None:
    """Older fsspec lists the entry point already; a second import must not raise."""
    import importlib

    from fsspec.registry import known_implementations

    assert "rclone" in known_implementations
    importlib.reload(backends)
    assert "rclone" in known_implementations
