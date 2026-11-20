"""DEP-18: the client, test harness and file CLI work at their dependency floors.

These tests import only what the core install (plus ``cli`` for the CLI test)
provides, so CI also runs them against the lowest versions the lower bounds
allow (``uv run --resolution lowest-direct``; see docs/design/deployment.md).
They must never import server code.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from heliopuddle.client import (
    BusyError,
    ConflictError,
    HelioPuddleClient,
    HelioPuddleError,
    NotFoundError,
)
from heliopuddle.testing import Call, FakeHelioPuddleClient

NOW = "2026-01-02T03:04:05Z"


def _record(path: str = "a/x.txt", **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 7,
        "project": "demo",
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "series_id": 3,
        "size": 3,
        "hash": "0" * 32,
        "hash_algo": "md5",
        "metadata": {"k": 1},
        "is_latest": True,
        "created_at": NOW,
        "updated_at": NOW,
        "storage_locations": [
            {"storage_location": "primary", "state": "available", "updated_at": NOW}
        ],
    }
    record.update(overrides)
    return record


@pytest.fixture
def seen() -> list[httpx.Request]:
    return []


def _client(seen: list[httpx.Request], answer: Any) -> HelioPuddleClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    http = httpx.Client(base_url="http://hp/api/v1", transport=httpx.MockTransport(handler))
    return HelioPuddleClient("http://hp/api/v1", "demo", user="alice", http_client=http)


@pytest.mark.req("DEP-18", "CLI-13")
def test_register_file_sends_the_record_and_parses_the_answer(seen: list[httpx.Request]) -> None:
    client = _client(seen, lambda r: httpx.Response(201, json=_record()))

    record = client.register_file(
        "a/x.txt", "0" * 32, 3, metadata={"k": 1}, content_date=datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert (record.id, record.path, record.state) == (7, "a/x.txt", "available")
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", "/api/v1/projects/demo/files")
    assert request.headers["X-Forwarded-User"] == "alice"
    body = json.loads(request.content)
    assert body == {
        "path": "a/x.txt",
        "hash": "0" * 32,
        "size": 3,
        "metadata": {"k": 1},
        "content_date": "2026-01-02T00:00:00Z",
        "overwrite": False,
    }


@pytest.mark.req("DEP-18", "CLI-13")
@pytest.mark.parametrize(
    ("override", "stored"),
    [({"version_number": 12}, (12, None)), ({"version_text": "rc-1"}, (None, "rc-1"))],
)
def test_register_file_sends_a_version_override(
    seen: list[httpx.Request], override: dict[str, Any], stored: tuple[int | None, str | None]
) -> None:
    client = _client(seen, lambda r: httpx.Response(201, json=_record(**override)))

    record = client.register_file("a/x.txt", "0" * 32, 3, **override)

    assert (record.version_number, record.version_text) == stored
    body = json.loads(seen[0].content)
    assert {k: body[k] for k in override} == override
    assert not {"version_number", "version_text"} - set(override) & set(body)


@pytest.mark.req("DEP-18", "CLI-02")
def test_add_file_reserves_with_a_version_override(
    seen: list[httpx.Request], tmp_path: Path
) -> None:
    source = tmp_path / "x.txt"
    source.write_bytes(b"abc")

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            return httpx.Response(
                200, json={"mode": "unchanged", "file": _record(version_number=4)}
            )
        return httpx.Response(
            403, json={"detail": "no", "required_roles": ["hp-client-storage-read-write"]}
        )

    record = _client(seen, answer).add_file(source, "a/x.txt", version_number=4)

    assert record.version_number == 4
    reserve = next(r for r in seen if r.url.path.endswith("/uploads"))
    body = json.loads(reserve.content)
    assert body["version_number"] == 4 and "version_text" not in body


@pytest.mark.req("DEP-18", "CLI-02", "CLI-13")
def test_both_version_overrides_raise_before_any_request(
    seen: list[httpx.Request], tmp_path: Path
) -> None:
    source = tmp_path / "x.txt"
    source.write_bytes(b"abc")
    client = _client(seen, lambda r: httpx.Response(500))

    with pytest.raises(ValueError, match="version_number or version_text"):
        client.register_file("a/x.txt", "0" * 32, 3, version_number=1, version_text="1")
    with pytest.raises(ValueError, match="version_number or version_text"):
        client.add_file(source, "a/x.txt", version_number=1, version_text="1")
    assert seen == []


@pytest.mark.req("DEP-18", "CLI-13")
def test_a_different_version_for_a_live_path_is_a_conflict(seen: list[httpx.Request]) -> None:
    existing = _record(version_number=3)
    client = _client(
        seen, lambda r: httpx.Response(409, json={"detail": "version differs", "file": existing})
    )

    with pytest.raises(ConflictError) as conflict:
        client.register_file("a/x.txt", "0" * 32, 3, version_number=4)

    assert conflict.value.file.version_number == 3


@pytest.mark.req("DEP-18")
def test_api_errors_map_to_client_errors(seen: list[httpx.Request]) -> None:
    answers = iter(
        [
            httpx.Response(409, json={"detail": "different", "file": _record()}),
            httpx.Response(409, json={"detail": "busy"}, headers={"Retry-After": "4"}),
            httpx.Response(404, json={"detail": "no project"}),
            httpx.Response(422, json={"detail": [{"loc": ["body", "path"], "msg": "bad"}]}),
        ]
    )
    client = _client(seen, lambda r: next(answers))

    with pytest.raises(ConflictError) as conflict:
        client.register_file("a/x.txt", "1" * 32, 3)
    assert conflict.value.file.id == 7
    with pytest.raises(BusyError) as busy:
        client.register_file("a/x.txt", "1" * 32, 3)
    assert busy.value.retry_after == 4
    with pytest.raises(NotFoundError):
        client.register_file("a/x.txt", "1" * 32, 3)
    with pytest.raises(HelioPuddleError) as invalid:
        client.register_file("a/x.txt", "1" * 32, 3)
    assert invalid.value.status_code == 422


@pytest.mark.req("DEP-18")
def test_reads_and_changes_round_trip(seen: list[httpx.Request]) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files") and request.method == "GET":
            return httpx.Response(
                200, json={"items": [_record()], "total": 1, "limit": 500, "offset": 0}
            )
        if request.method == "DELETE":
            return httpx.Response(200, json=_record(deleted_at=NOW))
        if request.method == "PATCH":
            return httpx.Response(200, json=_record(metadata=json.loads(request.content)))
        return httpx.Response(200, json=_record())

    client = _client(seen, answer)
    assert [r.path for r in client.find_all(path="a/*", order="path")] == ["a/x.txt"]
    assert client.get_file_by_path("a/x.txt").id == 7
    assert client.update_metadata(7, {"z": 2}, replace=True).metadata == {"z": 2}
    assert client.remove_file(7).deleted_at is not None
    assert seen[0].url.params["path"] == "a/*"


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeHelioPuddleClient]:
    with FakeHelioPuddleClient(project="demo", store=tmp_path / "store", user="t") as client:
        yield client


@pytest.mark.req("DEP-18", "CLI-14")
def test_fake_add_find_and_remove(fake: FakeHelioPuddleClient, tmp_path: Path) -> None:
    source = tmp_path / "a.csv"
    source.write_bytes(b"rows")
    record = fake.add_file(source, "r/a.csv", content_date=datetime(2026, 1, 1))
    assert (tmp_path / "store/r/a.csv").read_bytes() == b"rows"
    assert [f.path for f in fake.find_all(path="r/*")] == ["r/a.csv"]
    fake.remove_file(record.id)
    assert fake.live_paths() == []
    assert fake.calls[0] == Call("add_file", "r/a.csv")


@pytest.mark.req("DEP-18", "CLI-14")
def test_pytest_plugin_fixture(heliopuddle_fake: FakeHelioPuddleClient) -> None:
    heliopuddle_fake.seed("s/a.txt", b"abc")
    assert heliopuddle_fake.record_at("s/a.txt") is not None


@pytest.mark.req("DEP-18", "CLI-15")
def test_file_cli_lists_through_the_client(
    monkeypatch: pytest.MonkeyPatch, fake: FakeHelioPuddleClient
) -> None:
    typer_testing = pytest.importorskip("typer.testing")
    from heliopuddle.cli import app
    from heliopuddle.cli_file import session as cli_file_session

    fake.seed("x/b.txt", b"b")
    monkeypatch.setattr(cli_file_session, "_client_factory", lambda *a, **k: fake)

    result = typer_testing.CliRunner().invoke(app, ["file", "list", "x/*", "--user", "u"])

    assert result.exit_code == 0, result.output
    assert "x/b.txt" in result.output


@pytest.mark.req("DEP-18", "CLI-14")
def test_fake_keeps_a_version_override(fake: FakeHelioPuddleClient) -> None:
    record = fake.register_file("v/a.txt", "0" * 32, 3, version_text="rc-1")
    assert (record.version_number, record.version_text) == (None, "rc-1")
    with pytest.raises(ConflictError):
        fake.register_file("v/a.txt", "0" * 32, 3, version_text="rc-2")
    with pytest.raises(ValueError):
        fake.register_file("v/b.txt", "0" * 32, 3, version_number=1, version_text="1")
    assert fake.live_paths() == ["v/a.txt"]


@pytest.mark.req("DEP-18", "CLI-15")
def test_file_cli_adds_with_a_version_number(
    monkeypatch: pytest.MonkeyPatch, fake: FakeHelioPuddleClient, tmp_path: Path
) -> None:
    typer_testing = pytest.importorskip("typer.testing")
    from heliopuddle.cli import app
    from heliopuddle.cli_file import session as cli_file_session

    source = tmp_path / "a.csv"
    source.write_bytes(b"rows")
    monkeypatch.setattr(cli_file_session, "_client_factory", lambda *a, **k: fake)
    runner = typer_testing.CliRunner()

    added = runner.invoke(
        app, ["file", "add", str(source), "r/a.csv", "--version-number", "7", "--user", "u"]
    )
    assert added.exit_code == 0, added.output
    shown = runner.invoke(app, ["file", "show", "--path", "r/a.csv", "--user", "u"])
    assert shown.exit_code == 0, shown.output
    assert "version:       v7" in shown.output


def _public_methods(cls: type) -> dict[str, Any]:
    return {
        name: member
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


@pytest.mark.req("CLI-14")
def test_fake_methods_have_the_real_clients_signatures() -> None:
    real = _public_methods(HelioPuddleClient)
    fake = _public_methods(FakeHelioPuddleClient)
    shared = sorted(set(real) & set(fake))
    assert {"add_file", "find", "find_all", "health", "register_file", "remove_file"} <= set(shared)
    for name in shared:
        real_sig, fake_sig = inspect.signature(real[name]), inspect.signature(fake[name])
        assert list(fake_sig.parameters.values()) == list(real_sig.parameters.values()), name
        assert fake_sig.return_annotation in {
            real_sig.return_annotation,
            str(real_sig.return_annotation).replace("FileRead", "FileRecord"),
        }, name


@pytest.mark.req("CLI-14")
def test_fake_health_has_the_real_schema(fake: FakeHelioPuddleClient) -> None:
    health = fake.health()
    assert type(health).__name__ == "HealthResponse"
    assert health.model_dump() == {"status": "ok", "database": "ok", "prefect": "ok"}
