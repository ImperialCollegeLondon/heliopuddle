"""Shared unit-test fixtures: a migrated SQLite DB, a ``file://`` store and Prefect."""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from heliopuddle.server.context import AppContext, set_context
from heliopuddle.server.database.engine import create_engine_for
from heliopuddle.server.database.migrate import upgrade_to_head
from heliopuddle.settings import Settings

PROJECT_YAML = """\
project: demo
storage_locations:
  - name: primary
    url: file://{store}
    storage_options:
      auto_mkdir: true
  - name: secondary
    url: file://{store2}
    accept_external_changes: false
    create_root: false
"""


@pytest.fixture(scope="session")
def prefect_harness() -> Iterator[None]:
    """A temporary Prefect server for the whole test session (slow to start)."""
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness(server_startup_timeout=120):
        yield


SERVICE_MODULES = (
    "heliopuddle.server.services.index",
    "heliopuddle.server.services.index_diff",
    "heliopuddle.server.services.index_finish",
    "heliopuddle.server.services.index_run",
    "heliopuddle.server.services.index_tasks",
    "heliopuddle.server.services.verify",
    "heliopuddle.server.services.delete",
    "heliopuddle.server.services.transfer",
    "heliopuddle.server.services.rules",
    "heliopuddle.server.services.reconcile",
)
"""Service modules whose flows call tasks and Prefect helpers."""


def _inline(task: Any) -> Callable[..., Any]:
    """Return a stand-in for ``task`` that runs ``task.fn`` in this process.

    With ``return_state=True`` it returns a real ``Completed`` or ``Failed``
    Prefect state, like a task run would; otherwise the plain result, raising
    what the function raised.
    """
    from prefect.states import Completed, Failed

    def call(*args: Any, return_state: bool = False, **kwargs: Any) -> Any:
        if not return_state:
            return task.fn(*args, **kwargs)
        try:
            return Completed(data=task.fn(*args, **kwargs))
        except Exception as error:
            return Failed(message=f"{type(error).__name__}: {error}", data=error)

    call.fn = task.fn  # type: ignore[attr-defined]
    return call


NO_PREFECT_URL = "http://127.0.0.1:9/api"  # the discard port: nothing listens


@pytest.fixture(autouse=True)
def _drain_rule_notifications() -> Iterator[None]:
    """Wait for fire-and-forget rule notification threads before the next test.

    ``notify_file_changes`` sends rule triggers from a short-lived thread.
    Left running, one can outlive its test and reach the next test's patched
    Prefect client, which then sees a run it never submitted.
    """
    yield
    for thread in threading.enumerate():
        if thread.name == "hp-rule-notify":
            thread.join(timeout=30)


@pytest.fixture(autouse=True)
def _no_prefect_server(request: pytest.FixtureRequest) -> Iterator[None]:
    """Unit tests without ``prefect_harness`` cannot reach a Prefect server.

    Any task or flow run through the Prefect engine (instead of ``.fn``) then
    fails at once, whatever order the tests run in, rather than quietly using
    a harness another test happened to start.
    """
    if "prefect_harness" in request.fixturenames:
        yield
        return
    from prefect.settings import (
        PREFECT_API_URL,
        PREFECT_CLIENT_MAX_RETRIES,
        temporary_settings,
    )

    with temporary_settings({PREFECT_API_URL: NO_PREFECT_URL, PREFECT_CLIENT_MAX_RETRIES: 0}):
        yield


@pytest.fixture
def inline_prefect() -> Iterator[None]:
    """Run service flows with ``flow.fn`` and no Prefect server.

    Tasks used by the flow modules run inline through their functions (see
    :func:`_inline`), run loggers are plain loggers, and the Prefect global
    concurrency limits and markdown artifacts are no-ops. Use the
    ``prefect_harness`` fixture only for tests that verify behaviour of the
    Prefect server itself. It patches with its own ``MonkeyPatch``, so a test's
    ``monkeypatch.undo()`` leaves it in place.
    """
    import importlib

    with pytest.MonkeyPatch.context() as monkeypatch:
        from prefect.tasks import Task

        for name in SERVICE_MODULES:
            module: ModuleType = importlib.import_module(name)
            for attr, value in list(vars(module).items()):
                if isinstance(value, Task):
                    monkeypatch.setattr(module, attr, _inline(value))
            if hasattr(module, "get_run_logger"):
                monkeypatch.setattr(module, "get_run_logger", lambda: logging.getLogger("test.run"))
            if hasattr(module, "ensure_concurrency_limit"):
                monkeypatch.setattr(module, "ensure_concurrency_limit", lambda *a, **k: None)
            if hasattr(module, "hp_concurrency"):
                monkeypatch.setattr(module, "hp_concurrency", lambda *a, **k: nullcontext())
            if hasattr(module, "create_markdown_artifact"):
                monkeypatch.setattr(module, "create_markdown_artifact", lambda **kw: None)
        yield


@pytest.fixture
def prefect_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``GET /health`` see a healthy Prefect API without a server."""
    from heliopuddle.server.api.health import check as health_api

    class _Resp:
        status_code = 200

    monkeypatch.setattr(health_api, "prefect_api_url", lambda: "http://prefect/api")
    monkeypatch.setattr(health_api.httpx, "get", lambda *a, **k: _Resp())


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Root directory of the ``primary`` storage location."""
    path = tmp_path / "store"
    path.mkdir()
    return path


@pytest.fixture
def config_dir(tmp_path: Path, store: Path) -> Path:
    """A config dir holding the ``demo`` project (two ``file://`` storage locations)."""
    path = tmp_path / "config"
    path.mkdir()
    store2 = tmp_path / "store2"
    store2.mkdir()
    (path / "demo.yaml").write_text(PROJECT_YAML.format(store=store, store2=store2))
    return path


@pytest.fixture
def settings(tmp_path: Path, config_dir: Path) -> Settings:
    """Settings pointing at the tmp config dir and a SQLite DB under tmp_path."""
    return Settings(config_dir=config_dir, data_dir=tmp_path / "data")


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A SQLite database migrated to head once per session, copied for each test."""
    path = tmp_path_factory.mktemp("template-db") / "heliopuddle.db"
    engine = create_engine_for(f"sqlite:///{path}")
    upgrade_to_head(engine)
    engine.dispose()
    return path


@pytest.fixture
def ctx(settings: Settings, migrated_db: Path) -> Iterator[AppContext]:
    """A migrated, config-synced app context installed as the process context."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(migrated_db, settings.data_dir / "heliopuddle.db")
    context = AppContext.from_settings(settings)
    context.sync_config()
    set_context(context)
    yield context
    set_context(None)
    context.dispose()
