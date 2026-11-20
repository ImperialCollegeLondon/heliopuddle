"""Rule controls: ignore, quota and notifications."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prefect.client.orchestration import get_client
from prefect.context import SyncClientContext
from prefect.settings import PREFECT_API_URL, temporary_settings

from heliopuddle.config.models import RuleConfig
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import RuleState
from heliopuddle.server.schemas import FileState
from heliopuddle.server.services import rule_notify
from tests.unit._rule_controls_helpers import (
    SPEC,
    FakePrefect,
    api,
    configured,
    prefect,
)

_FIXTURES = (
    api,
    configured,
    prefect,
)  # referenced so ruff sees the imports used


@pytest.mark.req("API-15", "SVC-24")
def test_ignore_is_recorded_filtered_and_unignore_notifies_after_commit(
    api: TestClient, configured: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from heliopuddle.server.api.files import rules_ignore as rules_ignore_api

    with configured.write_session() as session:
        result = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path="data/file.dat",
            hash="abc",
            size=1,
        )
    seen: list[bool] = []

    def changed(ctx: AppContext, project: str, file_ids: Any) -> None:
        assert project == "demo" and list(file_ids) == [result.file_id]
        with ctx.sessionmaker() as session:
            file = models.get_file(session, result.file_id)
            assert file is not None
            seen.append(file.ignored_from_all_rules)

    monkeypatch.setattr(rules_ignore_api, "notify_file_changes", changed)
    url = f"/files/{result.file_id}/rules-ignore"
    ignored = api.patch(url, json={"ignored": True}, headers={"X-Forwarded-User": "tester"})
    assert ignored.status_code == 200
    assert ignored.json()["ignored_from_all_rules"] is True
    assert ignored.json()["updated_by"] == "tester"
    assert api.get(f"/files/{result.file_id}").json()["ignored_from_all_rules"]
    assert (
        api.get("/projects/demo/files", params={"ignored_from_all_rules": True}).json()["total"]
        == 1
    )
    assert (
        api.get("/projects/demo/files", params={"ignored_from_all_rules": False}).json()["total"]
        == 0
    )
    unignored = api.patch(url, json={"ignored": False})
    assert unignored.status_code == 200
    assert unignored.json()["updated_at"] > ignored.json()["updated_at"]
    assert seen == [False]
    assert api.patch("/files/999/rules-ignore", json={"ignored": True}).status_code == 404
    assert api.patch(url, json={"ignore": True}).status_code == 422


@pytest.mark.req("API-03")
def test_storage_location_quota_is_exposed(api: TestClient, configured: AppContext) -> None:
    cfg = configured.projects["demo"]
    cfg.storage_locations[1] = cfg.storage_locations[1].model_copy(update={"quota": 500_000_000})
    assert api.get("/projects/demo/storage-locations/secondary").json()["quota"] == 500_000_000
    assert api.get("/projects/demo/storage-locations").json()[1]["quota"] == 500_000_000


@pytest.mark.req("SVC-24")
@pytest.mark.parametrize(
    "storage_locations, matching_source, expected_sources, expected_triggered",
    [
        (None, 1, [1, 2], ["archive"]),
        ({"secondary"}, 2, [2], ["other-source"]),
        (set(), 2, [], []),
    ],
)
def test_notifications_dispatch_then_select_matching_enabled_rules_including_latest_flips(
    configured: AppContext,
    prefect: FakePrefect,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    storage_locations: set[str] | None,
    matching_source: int,
    expected_sources: list[int],
    expected_triggered: list[str],
) -> None:
    from heliopuddle.server.services import rule_control

    cfg = configured.projects["demo"]
    cfg.rules.extend(
        [
            RuleConfig(name="runtime-off", **SPEC),
            RuleConfig(
                name="other-source", action="delete", source="secondary", match={"paths": ["**"]}
            ),
        ]
    )
    with configured.write_session() as session:
        first = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path="data/file_v1.dat",
            hash="a",
            size=1,
        )
        models.set_copy_state(session, first.copy_id, FileState.AVAILABLE)
    with configured.write_session() as session:
        second = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path="data/file_v2.dat",
            hash="b",
            size=1,
        )
        models.set_copy_state(session, second.copy_id, FileState.AVAILABLE)
        third = models.register_file(
            session,
            project="demo",
            storage_location="primary",
            path="data/file_v3.dat",
            hash="c",
            size=1,
        )
        models.set_copy_state(session, third.copy_id, FileState.AVAILABLE)
        session.add(RuleState(project_id=1, rule_name="runtime-off", enabled=False))
    dispatched: list[dict[str, Any]] = []
    selections: list[tuple[int, set[int]]] = []
    triggered: list[str] = []

    class Thread:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            dispatched.append(self.kwargs)

    def select_candidates(
        session: Any, project_id: int, source_id: int, match: Any, **kwargs: Any
    ) -> list[Any]:
        selections.append((source_id, set(kwargs["file_ids"])))
        assert project_id == 1 and kwargs["limit"] == 1
        return [SimpleNamespace(file_id=first.file_id)] if source_id == matching_source else []

    def trigger(ctx: AppContext, project: str, name: str) -> Any:
        # A separate write succeeds only after the selection session has closed.
        with ctx.write_session() as session:
            assert models.get_file(session, second.file_id) is not None
        triggered.append(name)
        raise ConnectionError("Prefect offline")

    monkeypatch.setattr(rule_notify.threading, "Thread", Thread)
    monkeypatch.setattr(rule_control, "trigger_rule", trigger)
    monkeypatch.setitem(
        sys.modules,
        "heliopuddle.server.models.rules",
        SimpleNamespace(select_candidates=select_candidates),
    )
    if storage_locations is None:
        rule_control.notify_file_changes(configured, "demo", [second.file_id, third.file_id])
    else:
        rule_control.notify_file_changes(
            configured,
            "demo",
            [second.file_id, third.file_id],
            storage_locations=storage_locations,
        )
    assert not selections and not triggered
    if storage_locations == set():
        assert not dispatched
        return
    assert len(dispatched) == 1 and dispatched[0]["daemon"] is False
    dispatched[0]["target"](*dispatched[0]["args"])
    assert selections == [
        (source, {first.file_id, second.file_id, third.file_id}) for source in expected_sources
    ]
    assert triggered == expected_triggered
    assert "Could not trigger" in caplog.text and "Prefect offline" in caplog.text


@pytest.mark.req("SVC-24")
def test_notification_dispatch_and_selection_failures_never_fail_mutations(
    configured: AppContext,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from heliopuddle.server.services import rule_control

    def broken_thread(**kwargs: Any) -> Any:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(rule_notify.threading, "Thread", broken_thread)
    rule_control.notify_file_changes(configured, "demo", [1])
    assert "Could not dispatch" in caplog.text
    before = len(caplog.records)
    rule_control.notify_file_changes(configured, "demo", [])
    rule_control.notify_file_changes(configured, "missing", [1])
    assert len(caplog.records) == before
    rule_notify._notify(configured, "missing", (1,))
    assert "Could not notify" in caplog.text


@pytest.mark.req("SVC-24")
def test_notification_thread_keeps_the_callers_prefect_settings(
    configured: AppContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from heliopuddle.server.services import rule_control

    done = threading.Event()
    seen: list[str | None] = []

    def observe(*args: Any) -> None:
        seen.append(PREFECT_API_URL.value())
        done.set()

    monkeypatch.setattr(rule_notify, "_notify", observe)
    with temporary_settings({PREFECT_API_URL: "http://notify-prefect.example/api"}):
        rule_control.notify_file_changes(configured, "demo", [1])
        assert done.wait(5), "notification thread did not execute"
    assert seen == ["http://notify-prefect.example/api"]


@pytest.mark.req("SVC-24")
def test_notification_does_not_inherit_a_request_scoped_client(
    configured: AppContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from heliopuddle.server.services import rule_control

    dispatched: list[dict[str, Any]] = []
    seen: list[str] = []

    class DeferredThread:
        def __init__(self, **kwargs: Any) -> None:
            dispatched.append(kwargs)

        def start(self) -> None:
            pass

    def observe(*args: Any) -> None:
        with get_client(sync_client=True) as client:
            seen.append(str(client.api_url))

    monkeypatch.setattr(rule_notify.threading, "Thread", DeferredThread)
    monkeypatch.setattr(rule_notify, "_notify", observe)
    with temporary_settings({PREFECT_API_URL: "http://notify-prefect.example/api"}):
        context = SyncClientContext()
        monkeypatch.setattr(context.client, "raise_for_api_version_mismatch_once", lambda: None)
        with context:
            rule_control.notify_file_changes(configured, "demo", [1])
    task = dispatched[0]
    task["target"](*task["args"])
    assert seen == ["http://notify-prefect.example/api/"]


@pytest.mark.req("SVC-24", "API-15")
def test_mutation_routes_notify_once_only_after_their_changes_commit(
    api: TestClient,
    configured: AppContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from heliopuddle.server.api.files import accept as accept_api
    from heliopuddle.server.api.files import metadata as metadata_api
    from heliopuddle.server.api.files import register as register_api
    from heliopuddle.server.api.files import rules_ignore as rules_ignore_api
    from heliopuddle.server.api.uploads import complete as complete_api

    notifications: list[int] = []

    def notify(ctx: AppContext, project: str, file_ids: Any) -> None:
        with ctx.sessionmaker() as session:
            for file_id in file_ids:
                file = models.get_file(session, file_id)
                assert file is not None and file.project.name == project
                assert not file.ignored_from_all_rules
                notifications.append(file_id)

    def verify(copy_id: int) -> None:
        with configured.write_session() as session:
            models.set_copy_state(session, copy_id, FileState.AVAILABLE)

    for module in (register_api, accept_api, metadata_api, rules_ignore_api, complete_api):
        monkeypatch.setattr(module, "notify_file_changes", notify)
    monkeypatch.setattr(register_api.verify_file, "fn", verify)
    body = {"path": "data/a.dat", "hash": "abc", "size": 1}
    file_id = api.post("/projects/demo/files", json=body).json()["id"]
    assert notifications == [file_id]
    assert api.post("/projects/demo/files", json=body).status_code == 200
    assert notifications == [file_id]
    assert api.patch(f"/files/{file_id}/metadata", json={"updated": True}).status_code == 200
    assert len(notifications) == 2
    assert api.patch(f"/files/{file_id}/rules-ignore", json={"ignored": True}).status_code == 200
    assert len(notifications) == 2
    assert api.patch(f"/files/{file_id}/rules-ignore", json={"ignored": False}).status_code == 200
    assert len(notifications) == 3
    monkeypatch.setattr(accept_api.accept_copy, "fn", lambda *args: None)
    assert api.post(f"/files/{file_id}/accept", json={}).status_code == 200
    assert len(notifications) == 4
    monkeypatch.setattr(
        complete_api, "complete_upload", lambda *args: SimpleNamespace(file_id=file_id)
    )
    assert api.post("/uploads/token/complete").status_code == 200
    assert notifications == [file_id] * 5
