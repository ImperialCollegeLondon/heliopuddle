"""M4 browser controls against the built image and real Prefect deployments."""

from __future__ import annotations

import re
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Locator, expect

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.schemas import FileState

from .harness import REPORTS, HpContainer


def field(form: Locator, name: str) -> Locator:
    """Find a one-off field by its rule-model name, optionally qualified."""
    return form.locator(f'[name="{name}"], [name$=".{name}"], [name$="_{name}"]').first


def wait_for_run(url: str) -> None:
    """Follow the run link to the real, published Prefect API."""
    base, run_id = url.split("/runs/flow-run/")
    deadline = time.monotonic() + 180
    with httpx.Client(timeout=30) as client:
        while time.monotonic() < deadline:
            response = client.get(f"{base}/api/flow_runs/{run_id}")
            response.raise_for_status()
            state = response.json()["state"]
            if state["type"] in {"COMPLETED", "FAILED", "CRASHED", "CANCELLED"}:
                assert state["type"] == "COMPLETED", state
                return
            time.sleep(0.5)
    pytest.fail(f"UI-submitted run did not finish: {url}")


@pytest.mark.req("UI-04", "API-14", "SVC-25")
@pytest.mark.slow
def test_rules_page_enable_disable_and_complete_one_off_form(
    hp_container: HpContainer,
    hp_client: HelioPuddleClient,
    browser: Browser,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.dat"
    source.write_bytes(b"one-off from the browser")
    record = hp_client.add_file(source, "e2e/one-off/browser.dat")
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    page.on("dialog", lambda dialog: dialog.accept())  # the one-off form asks to confirm
    try:
        page.goto(f"{hp_container.base_url}/projects/{hp_container.project}/rules")
        row = page.locator('tr[data-rule="archive-ui"]')
        expect(row).to_contain_text("archive-ui")
        expect(row.get_by_text("0 0 1 1 *", exact=True)).to_be_visible()
        expect(page.locator(".flow-label", has_text="archive-ui")).to_be_visible()
        row.get_by_role("button", name="Disable", exact=True).click()
        expect(
            page.locator('tr[data-rule="archive-ui"] button[aria-label="Enable"]')
        ).to_be_visible()
        assert not hp_client.get_rule("archive-ui").effective_enabled
        page.locator('tr[data-rule="archive-ui"]').get_by_role(
            "button", name="Enable", exact=True
        ).click()
        expect(
            page.locator('tr[data-rule="archive-ui"] button[aria-label="Disable"]')
        ).to_be_visible()
        assert hp_client.get_rule("archive-ui").effective_enabled

        page.get_by_role("link", name="One-off run…").click()  # opens the collapsed panel
        one_off = page.locator('form:has(select[name$="action"])').first
        expect(one_off).to_be_visible()
        names = {
            "action",
            "source",
            "destination",
            "paths",
            "exclude",
            "is_latest",
            *(
                f"age_{date}_{bound}"
                for date in ("modified", "created", "content_date", "arrived")
                for bound in ("older_than", "younger_than")
            ),
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
        }
        for name in sorted(names):
            expect(field(one_off, name)).to_have_count(1)
        field(one_off, "action").select_option("copy")
        field(one_off, "source").select_option("primary")
        field(one_off, "destination").select_option("archive")
        field(one_off, "paths").fill(record.path)
        field(one_off, "check_for_space_overhead").fill("0B")
        field(one_off, "max_files").fill("1")
        field(one_off, "dry_run").check()
        before = hp_client.get_file(record.id).model_dump()
        one_off.locator('button[type="submit"]').click()
        link = page.locator('a[href*="/runs/flow-run/"]').first
        expect(link).to_be_visible()
        dry_url = link.get_attribute("href")
        assert dry_url is not None
        wait_for_run(dry_url)
        assert hp_client.get_file(record.id).model_dump() == before

        page.locator("#one-off > summary").click()
        one_off = page.locator('form:has(select[name$="action"])').first
        field(one_off, "action").select_option("copy")
        field(one_off, "source").select_option("primary")
        field(one_off, "destination").select_option("archive")
        field(one_off, "paths").fill(record.path)
        field(one_off, "check_for_space_overhead").fill("0B")
        field(one_off, "max_files").fill("1")
        field(one_off, "dry_run").uncheck()
        one_off.locator('button[type="submit"]').click()
        real_url = page.locator('a[href*="/runs/flow-run/"]').first.get_attribute("href")
        assert real_url is not None and real_url != dry_url
        wait_for_run(real_url)
        assert {
            copy.storage_location: copy.state
            for copy in hp_client.get_file(record.id).storage_locations
        } == {
            "primary": FileState.AVAILABLE,
            "archive": FileState.AVAILABLE,
        }
        deployment_link = page.locator('a[href*="/deployments/deployment/"]').first
        expect(deployment_link).to_be_visible()
        href = deployment_link.get_attribute("href")
        assert href is not None
        with httpx.Client(timeout=30) as client:
            response = client.get(href, headers={"Accept": "text/html"})
            assert response.status_code == 200
    finally:
        screenshot = REPORTS / "screenshots" / "m4-rules.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
        page.close()


@pytest.mark.req("UI-04", "API-15")
def test_file_detail_and_list_show_copy_states_and_toggle_ignore(
    hp_container: HpContainer,
    hp_client: HelioPuddleClient,
    browser: Browser,
    tmp_path: Path,
) -> None:
    source = tmp_path / "ignored.dat"
    source.write_bytes(b"ignore controls")
    record = hp_client.add_file(source, "e2e/ignored/browser.dat")
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        page.goto(f"{hp_container.base_url}/projects/{hp_container.project}/files/{record.id}")
        expect(page.locator("table.copies").get_by_text("primary", exact=True)).to_be_visible()
        page.get_by_role("button", name=re.compile(r"ignore", re.I)).first.click()
        assert hp_client.get_file(record.id).ignored_from_all_rules
        expect(page.get_by_text(re.compile("ignored", re.I)).first).to_be_visible()
        page.get_by_role(
            "button", name=re.compile(r"un.?ignore|include|enable", re.I)
        ).first.click()
        assert not hp_client.get_file(record.id).ignored_from_all_rules

        page.goto(f"{hp_container.base_url}/projects/{hp_container.project}/files")
        expect(page.locator("table.ft thead")).to_contain_text("primary")
        row = page.locator("tr").filter(has=page.get_by_role("link", name=record.path, exact=True))
        expect(row.locator('[aria-label="available"]').first).to_be_visible()
        row.get_by_role("button", name=re.compile("ignore", re.I)).click()
        expect(page).to_have_url(re.compile(r"/files$"))
        assert hp_client.get_file(record.id).ignored_from_all_rules
        page.goto(
            f"{hp_container.base_url}/projects/{hp_container.project}/files?view=ignored"
            f"&file={record.id}"
        )
        inspector = page.locator("#inspector")
        expect(inspector).to_contain_text("ignored by rules")
        inspector.get_by_role("button", name="Include in rules").click()
        assert not hp_client.get_file(record.id).ignored_from_all_rules
        expect(page).to_have_url(re.compile(rf"view=ignored&file={record.id}"))
    finally:
        screenshot = REPORTS / "screenshots" / "m4-files.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
        page.close()


@pytest.mark.req("UI-08")
def test_reconcile_now_submits_a_run_and_links_to_prefect(
    hp_container: HpContainer,
    browser: Browser,
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        page.goto(f"{hp_container.base_url}/projects/{hp_container.project}")
        page.locator("summary", has_text="Actions").click()
        form = page.locator('form:has(select[name="rules"])')
        expect(form.get_by_label("Verify", exact=False)).not_to_be_checked()
        options = form.locator('select[name="rules"] option')
        assert options.count() >= 1
        for index in range(options.count()):
            assert options.nth(index).evaluate("option => option.selected")
        form.get_by_role("button", name="Reconcile now").click()
        notice = page.get_by_role("status")
        expect(notice).to_contain_text("Reconcile run submitted")
        href = notice.locator('a[href*="/runs/flow-run/"]').get_attribute("href")
        assert href is not None
        base, run_id = href.split("/runs/flow-run/")
        with httpx.Client(timeout=30) as client:
            response = client.get(f"{base}/api/flow_runs/{run_id}")
            response.raise_for_status()
            assert response.json()["deployment_id"]
    finally:
        screenshot = REPORTS / "screenshots" / "m5-reconcile.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
        page.close()
