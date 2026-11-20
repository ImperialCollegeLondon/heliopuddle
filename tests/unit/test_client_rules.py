"""HTTP-only rule controls and ignored-file filters (CLI-09)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from heliopuddle.client import HelioPuddleClient, NotFoundError
from heliopuddle.server.schemas import FileRead, RuleRead

pytestmark = pytest.mark.req("CLI-09")


def test_rule_methods_send_only_supported_inputs_and_parse_responses() -> None:
    rule = RuleRead(
        name="archive",
        action="copy",
        source="primary",
        destination="secondary",
        match={"paths": ["**"]},
        effective_enabled=True,
        deployment="run_rule/rule-p1-demo-archive",
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/runs"):
            return httpx.Response(202, json={"flow_run_id": "id", "deployment": rule.deployment})
        if path.endswith("/trigger"):
            return httpx.Response(200, json={"started": False, "flow_run_id": None})
        payload: Any = rule.model_dump(mode="json")
        if path.endswith("/rules"):
            payload = [payload]
        return httpx.Response(200, json=payload)

    with httpx.Client(base_url="http://api", transport=httpx.MockTransport(respond)) as http:
        hp = HelioPuddleClient("", "demo", http_client=http)
        assert hp.list_rules() == [rule]
        assert hp.get_rule("archive") == rule
        assert hp.disable_rule("archive") == rule
        assert hp.enable_rule("archive") == rule
        assert not hp.trigger_rule("archive").started
        assert hp.run_rule("archive", mode="full", dry_run=True).flow_run_id == "id"
        assert json.loads(requests[-1].content) == {
            "kind": "rule",
            "rule": "archive",
            "mode": "full",
            "dry_run": True,
        }
        spec = {"action": "delete", "source": "primary", "match": {"paths": ["**"]}}
        hp.run_once(spec)
        assert json.loads(requests[-1].content)["spec"]["action"] == "delete"
        assert requests[2].url.path == "/projects/demo/rules/archive/disable"


def record(project: str = "demo") -> dict[str, Any]:
    now = datetime.now(UTC)
    return FileRead(
        id=1,
        project=project,
        path="a.dat",
        name="a.dat",
        series_id=1,
        hash="abc",
        size=1,
        hash_algo="md5",
        is_latest=True,
        created_at=now,
        updated_at=now,
        ignored_from_all_rules=True,
    ).model_dump(mode="json")


def test_ignore_resolves_paths_checks_project_ids_and_filters_every_page() -> None:
    requests: list[httpx.Request] = []
    current_project = "demo"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/projects/demo/files":
            return httpx.Response(
                200,
                json={
                    "items": [record()] if request.url.params["offset"] == "0" else [],
                    "total": 2,
                    "limit": 1,
                    "offset": int(request.url.params["offset"]),
                },
            )
        return httpx.Response(200, json=record(current_project))

    with httpx.Client(base_url="http://api", transport=httpx.MockTransport(respond)) as http:
        hp = HelioPuddleClient("", "demo", http_client=http)
        assert hp.set_rules_ignored("a.dat", True).ignored_from_all_rules
        assert requests[0].url.params["path"] == "a.dat"
        assert requests[-1].method == "PATCH"
        assert json.loads(requests[-1].content) == {"ignored": True}
        assert len(list(hp.find_all(ignored_from_all_rules=True, page_size=1))) == 1
        assert all(r.url.params["ignored_from_all_rules"] == "true" for r in requests[-2:])
        hp.find(ignored_from_all_rules=False)
        assert requests[-1].url.params["ignored_from_all_rules"] == "false"
        current_project = "other"
        before = len(requests)
        with pytest.raises(NotFoundError):
            hp.set_rules_ignored(1, False)
        assert len(requests) == before + 1 and requests[-1].method == "GET"
        with pytest.raises(ValueError):
            HelioPuddleClient("", None, http_client=http).set_rules_ignored(1, False)
