"""Assets, error page, rule notice and column chooser of the M6 pages."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from heliopuddle.client import NotFoundError
from tests.unit._ui_helpers import (
    RUN_ID,
    UI_DIR,
    Factory,
    file_with,
    section,
    sl_read,
    ui_client,
)
from tests.unit._ui_pages_helpers import A, fleet_factory
from tests.unit.test_ui_rules import rule


@pytest.mark.req("AUTH-16", "UI-09")
def test_no_cdn_or_backend_names_leak_into_assets() -> None:
    for path in (UI_DIR / "static").glob("*.js"):
        assert "http" not in Path(path).read_text().replace("http_", "")


@pytest.mark.req("UI-09")
def test_error_page_offers_the_way_back() -> None:
    resp = ui_client(Factory(error=NotFoundError(404, "gone"))).get("/projects/demo/files")
    assert resp.status_code == 404
    assert 'class="error-code" aria-hidden="true">404<' in resp.text
    assert "Back to projects" in resp.text


@pytest.mark.req("UI-07")
def test_rule_run_notice_links_to_prefect() -> None:
    html = (
        ui_client(fleet_factory(rules=[rule()]), prefect_ui_url="http://p")
        .get("/projects/demo/rules", params={"rule_run": RUN_ID})
        .text
    )
    assert "Rule run submitted" in html and f"http://p/runs/flow-run/{RUN_ID}" in html


@pytest.mark.req("UI-12")
def test_every_state_column_can_be_unticked() -> None:
    sls = [sl_read(f"s{i}") for i in range(1, 4)]
    client = ui_client(Factory(files=[file_with(1, ("s1", A))], storage_locations=sls))
    html = client.get("/projects/demo/files", params={"columns": "-"}).text
    assert re.findall(r'title="Copy state in', section(html, "<thead>", "</thead>")) == []
    menu = section(html, '<details class="menu columns-menu">', "</details>")
    assert " checked>" not in menu
    assert "columns=-" in html  # kept in paging and view links


@pytest.mark.req("UI-12")
def test_column_chooser_waits_for_apply() -> None:
    js = (Path(__file__).parents[2] / "src/heliopuddle/server/ui/static/hp.js").read_text()
    chooser = js[js.index("form[data-columns]") :]
    assert 'addEventListener("change", limit)' in chooser  # only the six-box cap reacts
    assert "limit();\n        apply();" not in chooser  # no reload on each tick
    assert 'addEventListener("submit"' in chooser  # Apply does the reload


@pytest.mark.req("UI-11")
def test_overview_header_has_no_duplicate_totals_line() -> None:
    html = ui_client(fleet_factory()).get("/projects/demo").text
    head = section(html, '<div class="page-head">', "</div>")
    assert "live ·" not in head
