"""The project page's "Reconcile now" form goes through the stub HTTP client (UI-08)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from heliopuddle.client import HelioPuddleError
from tests.unit._ui_helpers import RUN_ID, Factory, ui_client
from tests.unit.test_ui_rules import rule

pytestmark = pytest.mark.req("UI-08")


def two_rules() -> Factory:
    other = rule().model_copy(update={"name": "other"})
    return Factory(rules=[rule(), other])


def test_form_has_verify_and_all_rules_selected_by_default() -> None:
    html = ui_client(two_rules()).get("/projects/demo").text
    assert "/projects/demo/reconcile" in html
    assert 'name="verify"' in html
    assert 'name="rules"' in html and "multiple" in html
    assert '<option value="archive" selected>' in html
    assert '<option value="other" selected>' in html
    assert "Reconcile now" in html


@pytest.mark.req("UI-08")
def test_a_single_rule_still_shows_a_multi_row_picker() -> None:
    """With one rule a size-1 multi-select looks like a single-choice dropdown."""
    html = ui_client(Factory(rules=[rule()])).get("/projects/demo").text
    assert '<select name="rules" multiple size="2">' in html


def test_form_without_rules_has_no_select() -> None:
    html = ui_client(Factory()).get("/projects/demo").text
    assert 'name="verify"' in html
    assert 'name="rules"' not in html


def test_post_is_protected_and_redirects_with_a_notice_linking_to_prefect() -> None:
    factory = two_rules()
    browser = ui_client(factory, ui_root_path="/hp", prefect_ui_url="https://p.example/prefect")
    url = "/hp/projects/demo/reconcile"
    forged = browser.post(url, data={"verify": "true"}, headers={"Origin": "https://evil.example"})
    assert forged.status_code == 403
    assert not factory.made
    response = browser.post(
        url,
        data={"verify": "true", "has_rules": "1", "rules": ["archive"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert ("reconcile", (True, ["archive"])) in factory.made[-1].calls
    location = response.headers["location"]
    assert location.startswith("/hp/projects/demo?")
    assert parse_qs(urlsplit(location).query)["reconcile_run"] == [RUN_ID]
    page = browser.get(location.removeprefix("/hp"))
    assert "Reconcile run submitted" in page.text
    assert f"https://p.example/prefect/runs/flow-run/{RUN_ID}" in page.text


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, (False, None)),
        ({"has_rules": "1", "rules": ["archive", "other"]}, (False, None)),
        ({"has_rules": "1", "rules": ["other"]}, (False, ["other"])),
        ({"has_rules": "1"}, (False, [])),
        ({"verify": "on", "has_rules": "1", "rules": ["archive", "other"]}, (True, None)),
    ],
)
def test_form_values_map_to_client_arguments(
    body: dict[str, object], expected: tuple[bool, list[str] | None]
) -> None:
    factory = two_rules()
    response = ui_client(factory).post(
        "/projects/demo/reconcile", data=body, follow_redirects=False
    )
    assert response.status_code == 303
    assert ("reconcile", expected) in factory.made[-1].calls


@pytest.mark.parametrize("status", [409, 422])
def test_api_errors_render_the_error_page(status: int) -> None:
    factory = two_rules()
    factory.action_error = HelioPuddleError(status, "already has an unfinished reconcile run")
    response = ui_client(factory).post("/projects/demo/reconcile", data={})
    assert response.status_code == status
    assert "unfinished reconcile run" in response.text


def test_invalid_verify_value_is_a_bad_request() -> None:
    response = ui_client(two_rules()).post("/projects/demo/reconcile", data={"verify": "maybe"})
    assert response.status_code == 400
