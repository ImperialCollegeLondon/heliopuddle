"""M4 form controls go through a stub HTTP client, never database access."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from heliopuddle.client import HelioPuddleError
from heliopuddle.config.models import AGE_FIELDS, AgeCriteria, AgeRange
from heliopuddle.server.schemas import FileState, FileStorageLocationRead, RuleRead
from tests.unit._ui_helpers import RUN_ID, Factory, make_file, ui_client

pytestmark = pytest.mark.req("UI-04")


def rule() -> RuleRead:
    return RuleRead(
        name="archive",
        action="copy",
        source="primary",
        destination="secondary",
        match={"paths": ["data/**"], "exclude": ["data/private/**"]},
        schedule="0 2 * * *",
        effective_enabled=True,
        deployment="run_rule/rule-p1-demo-archive",
        deployment_id=RUN_ID,
        deployment_url=f"http://prefect/deployments/deployment/{RUN_ID}",
        watermark_at=datetime(2026, 3, 1, tzinfo=UTC),
        watermark_id=9,
    )


def test_rules_page_shows_config_state_progress_and_complete_one_off_form() -> None:
    factory = Factory(rules=[rule()])
    response = ui_client(factory, prefect_ui_url="https://public.example/prefect").get(
        "/projects/demo/rules"
    )
    assert response.status_code == 200
    html = response.text
    for text in ("archive", "primary", "secondary", "0 2 * * *", "2026-03-01", "data/private/**"):
        assert text in html
    for name in (
        "action",
        "source",
        "destination",
        "paths",
        "exclude",
        "is_latest",
        *(f"age_{f}_{b}" for f in AGE_FIELDS for b in ("older_than", "younger_than")),
        "overwrite",
        "check_for_space",
        "check_for_space_overhead",
        "update_only_if_newer",
        "dry_run",
        "retrigger",
        "pause_between_runs",
        "max_files",
        "max_bytes",
        "max_duration",
    ):
        assert f'name="{name}"' in html
    for name in ("older_than", "age_by", "content_date_after", "content_date_before"):
        assert f'name="{name}"' not in html
    assert 'class="age-grid"' in html
    assert 'placeholder="30d or 2025-06-01"' in html  # durations and dates both work
    assert "/projects/demo/rules/archive/disable" in html
    assert "/projects/demo/rules/archive/run" in html
    assert f"https://public.example/prefect/deployments/deployment/{RUN_ID}" in html
    assert factory.made[0].closed


@pytest.mark.parametrize(
    ("route", "body", "call"),
    [
        ("archive/enable", {}, ("enable_rule", "archive")),
        ("archive/disable", {}, ("disable_rule", "archive")),
        ("archive/run", {"mode": "full", "dry_run": "on"}, ("run_rule", ("archive", "full", True))),
        ("archive/run", {"mode": "incremental"}, ("run_rule", ("archive", "incremental", False))),
    ],
)
def test_rule_actions_use_protected_posts_and_redirect(
    route: str, body: dict[str, str], call: tuple[str, Any]
) -> None:
    factory = Factory(rules=[rule()])
    browser = ui_client(factory, ui_root_path="/hp")
    url = f"/hp/projects/demo/rules/{route}"
    assert (
        browser.post(url, data=body, headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert not factory.made
    response = browser.post(url, data=body, follow_redirects=False)
    assert response.status_code == 303
    assert call in factory.made[-1].calls
    assert response.headers["location"].startswith("/hp/projects/demo/rules")
    if route.endswith("/run"):
        assert parse_qs(urlsplit(response.headers["location"]).query)["rule_run"] == [RUN_ID]


def test_one_off_form_preserves_all_fields_and_reports_validation() -> None:
    factory = Factory(rules=[rule()])
    browser = ui_client(factory)
    body = {
        "action": "move",
        "source": "primary",
        "destination": "secondary",
        "paths": "data/**\nother/*.dat",
        "exclude": "data/private/**",
        "is_latest": "false",
        "age_modified_older_than": "2d",
        "age_modified_younger_than": "1y6mo",
        "age_created_older_than": "",
        "age_created_younger_than": " ",
        "age_content_date_older_than": "2026-03-01",
        "age_content_date_younger_than": "90 days",
        "age_arrived_older_than": "1d",
        "overwrite": "on",
        "check_for_space": "on",
        "check_for_space_overhead": "3GB",
        "update_only_if_newer": "on",
        "dry_run": "on",
        "retrigger": "on",
        "pause_between_runs": "45s",
        "max_files": "3",
        "max_bytes": "",
        "max_duration": "5m",
    }
    response = browser.post("/projects/demo/rules/run-once", data=body, follow_redirects=False)
    assert response.status_code == 303
    spec = factory.made[-1].calls[-1][1]
    if hasattr(spec, "model_dump"):
        spec = spec.model_dump(mode="json")
    assert spec["match"]["paths"] == ["data/**", "other/*.dat"]
    assert spec["match"]["is_latest"] is False
    assert spec["match"]["age"] == {
        "modified": {"older_than": "2d", "younger_than": "545d"},
        "created": None,  # blank inputs are left out
        "content_date": {"older_than": "2026-03-01T00:00:00+00:00", "younger_than": "90d"},
        "arrived": {"older_than": "1d", "younger_than": None},
    }
    assert spec["options"]["batch"] == {"max_files": 3, "max_bytes": None, "max_duration": "5m"}
    assert spec["options"]["check_for_space_overhead"] == 3_000_000_000
    assert spec["options"]["overwrite"] and spec["options"]["update_only_if_newer"]
    assert spec["options"]["dry_run"] and spec["options"]["retrigger"]
    assert spec["options"]["pause_between_runs"] == "45s"
    invalid = browser.post(
        "/projects/demo/rules/run-once", data={**body, "max_files": "not a number"}
    )
    assert invalid.status_code == 400
    assert "max_files" in invalid.text
    factory.action_error = HelioPuddleError(422, "source storage location does not exist")
    response = browser.post("/projects/demo/rules/run-once", data=body)
    assert response.status_code == 422 and "source storage location" in response.text


def test_file_list_and_detail_show_each_copy_and_ignore_toggle() -> None:
    file = make_file(1)
    file.ignored_from_all_rules = True
    file.storage_locations.append(
        FileStorageLocationRead(
            storage_location="archive", state=FileState.UNKNOWN, updated_at=file.updated_at
        )
    )
    factory = Factory(files=[file])
    browser = ui_client(factory)
    for url in ("/projects/demo/files?file=1", "/projects/demo/files/1"):
        response = browser.get(url)
        assert response.status_code == 200
        assert "primary" in response.text and "archive" in response.text
        assert "available" in response.text and "unknown" in response.text
        assert "Ignored from all rules" in response.text
        assert "/projects/demo/files/1/rules-ignore" in response.text
        assert "Include in rules" in response.text
    response = browser.post(
        "/projects/demo/files/1/rules-ignore",
        data={"ignored": "false"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert ("set_rules_ignored", (1, False)) in factory.made[-1].calls
    before = len(factory.made)
    assert (
        browser.post(
            "/projects/demo/files/1/rules-ignore",
            data={"ignored": "true"},
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert len(factory.made) == before
    file.project = "other"
    response = browser.post("/projects/demo/files/1/rules-ignore", data={"ignored": "true"})
    assert response.status_code == 404
    assert not any(call[0] == "set_rules_ignored" for call in factory.made[-1].calls)


def test_rule_forms_show_api_errors_and_reject_bad_mode() -> None:
    factory = Factory(rules=[rule()], action_error=HelioPuddleError(503, "Prefect unavailable"))
    browser = ui_client(factory)
    response = browser.post("/projects/demo/rules/archive/disable")
    assert response.status_code == 503 and "Prefect unavailable" in response.text
    response = browser.post("/projects/demo/rules/archive/run", data={"mode": "partial"})
    assert response.status_code == 400 and "mode" in response.text


def test_rules_page_shows_the_full_rule_summary_not_a_truncated_one() -> None:
    long = rule()
    long.match.paths = [f"instrument/level{i}/**" for i in range(8)]
    html = ui_client(Factory(rules=[long])).get("/projects/demo/rules").text
    assert ", ".join(long.match.paths) in html  # all of it, in the page
    assert "Selects" in html and "Options" in html
    assert "no overwrite" in html


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"age_modified_older_than": "soon"}, "match.age.modified.older_than"),
        ({"age_arrived_younger_than": "-1d"}, "match.age.arrived.younger_than"),
        (
            {"age_created_older_than": "7d", "age_created_younger_than": "1d"},
            "older_than 7d must be shorter than younger_than 1d",
        ),
        (
            {
                "age_content_date_older_than": "2024-01-01",
                "age_content_date_younger_than": "2025-01-01",
            },
            "must be later than",
        ),
    ],
)
def test_one_off_form_reports_invalid_age_bounds(fields: dict[str, str], message: str) -> None:
    factory = Factory(rules=[rule()])
    body = {"action": "copy", "source": "primary", "destination": "secondary", "paths": "**"}
    response = ui_client(factory).post("/projects/demo/rules/run-once", data=body | fields)
    assert response.status_code == 400
    assert "Invalid rule specification" in response.text
    assert message in response.text
    assert not any(call[0] == "run_once" for made in factory.made for call in made.calls)


def test_one_off_form_without_ages_sends_no_age_criteria() -> None:
    factory = Factory(rules=[rule()])
    body = {"action": "copy", "source": "primary", "destination": "secondary", "paths": "**"}
    blanks = {f"age_{f}_{b}": "" for f in AGE_FIELDS for b in ("older_than", "younger_than")}
    response = ui_client(factory).post(
        "/projects/demo/rules/run-once", data=body | blanks, follow_redirects=False
    )
    assert response.status_code == 303
    spec = factory.made[-1].calls[-1][1]
    assert spec.match.age == AgeCriteria()


def test_rules_page_names_every_age_criterion() -> None:
    aged = rule()
    aged.match.age = AgeCriteria(
        modified=AgeRange(older_than=timedelta(days=30)),
        created=AgeRange(younger_than=timedelta(weeks=1)),
        content_date=AgeRange(
            older_than=datetime(2025, 6, 1, tzinfo=UTC),
            younger_than=datetime(2024, 1, 1, tzinfo=UTC),
        ),
        arrived=AgeRange(older_than=timedelta(days=3)),
    )
    html = ui_client(Factory(rules=[aged])).get("/projects/demo/rules").text
    assert (
        "modified older than 30d, created younger than 7d, "
        "content date between 2024-01-01 and 2025-06-01, arrived older than 3d"
    ) in html
