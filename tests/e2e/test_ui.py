"""The M6 UI against the built image: Overview, Files with the inspector, the file page,
typeahead search, a run with JavaScript off, and screenshots at desktop and phone widths.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from heliopuddle.client import HelioPuddleClient

from .harness import REPORTS, HpContainer

SHOTS = REPORTS / "screenshots"
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


def shoot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def no_page_scroll(page: Page) -> None:
    """Wide content scrolls in its own box, never the page (UI-09)."""
    width = page.evaluate("document.documentElement.scrollWidth")
    viewport = page.evaluate("window.innerWidth")
    assert width <= viewport, (width, viewport)


def add(client: HelioPuddleClient, tmp_path: Path, path: str, text: str) -> int:
    local = tmp_path / Path(path).name
    local.write_text(text)
    return client.add_file(local, path).id


@pytest.mark.req("UI-01", "UI-09", "UI-11")
def test_home_redirects_to_the_overview_with_the_shell_and_figures(
    hp_container: HpContainer, hp_client: HelioPuddleClient, browser: Browser, tmp_path: Path
) -> None:
    add(hp_client, tmp_path, "e2e/ui/overview/listed.csv", "a,b\n1,2\n")
    for name, viewport in (("m6-overview", DESKTOP), ("m6-overview-phone", PHONE)):
        page = browser.new_page(viewport=viewport)
        try:
            page.goto(f"{hp_container.base_url}/")
            if page.locator(".project-grid").count():  # more than one project: pick ours
                page.locator(f'a.project-card[href$="/projects/{hp_container.project}"]').click()
            expect(page).to_have_url(re.compile(rf"/projects/{hp_container.project}$"))
            expect(page.get_by_role("heading", name="Overview")).to_be_visible()
            expect(page.locator("table.heatmap")).to_contain_text("primary")
            expect(page.get_by_text("listed.csv").first).to_be_visible(timeout=15_000)
            tabs = page.get_by_role("navigation", name="Project")
            for tab in ("Overview", "Files", "Storage", "Rules"):
                expect(tabs.get_by_role("link", name=tab)).to_be_visible()
            expect(page.locator(".ptabs a[aria-current=page]")).to_have_text("Overview")
            no_page_scroll(page)
        finally:
            shoot(page, name)
            page.close()


@pytest.mark.req("UI-02", "UI-06", "UI-12")
def test_files_views_filters_and_the_inspector(
    hp_container: HpContainer, hp_client: HelioPuddleClient, browser: Browser, tmp_path: Path
) -> None:
    add(hp_client, tmp_path, "e2e/ui/inspect/detail_v001.csv", "a,b\n1,2\n")
    second = add(hp_client, tmp_path, "e2e/ui/inspect/detail_v002.csv", "a,b\n3,4\n")
    base = f"{hp_container.base_url}/projects/{hp_container.project}"
    page = browser.new_page(viewport=DESKTOP)
    try:
        page.goto(f"{base}/files?path=e2e/ui/inspect/**")
        chips = page.locator(".chips")
        expect(chips.get_by_role("link", name=re.compile("Remove filter Path"))).to_be_visible()
        rows = page.locator("table.ft tbody tr")
        expect(rows).to_have_count(2)
        expect(page.locator("table.ft thead")).to_contain_text("primary")

        page.locator(".vtabs a", has_text="Latest").click()
        expect(page).to_have_url(re.compile(r"view=latest"))
        expect(rows).to_have_count(1)

        # with JavaScript, choosing a row opens the inspector without a reload
        rows.first.locator("td").nth(2).click()
        inspector = page.locator("#inspector")
        expect(inspector).to_be_visible()
        expect(inspector).to_contain_text("detail_v002.csv")
        expect(page).to_have_url(re.compile(rf"file={second}"))
        expect(inspector.locator(".timeline li")).to_have_count(2)
        expect(inspector.locator(".copy-card")).to_contain_text("primary")
        shoot(page, "m6-files-inspector")

        # the inspector closes in place too
        inspector.locator("[data-close]").click()
        expect(inspector).to_be_hidden()
        expect(page).not_to_have_url(re.compile(r"file="))

        # the path link opens the inspector; "Open page" goes to the file page
        page.get_by_role("link", name="e2e/ui/inspect/detail_v002.csv").click()
        expect(inspector).to_be_visible()
        inspector.get_by_role("link", name=re.compile("Open page")).click()
        expect(page.locator("h1")).to_contain_text("detail_v002.csv")
        history = page.locator("table.versions")
        expect(history.get_by_role("link", name="e2e/ui/inspect/detail_v001.csv")).to_be_visible()
        expect(history.locator("tr.current")).to_contain_text("latest")
        shoot(page, "m6-file-page")
    finally:
        page.close()


@pytest.mark.req("UI-12", "UI-09")
def test_files_with_inspector_at_phone_width(
    hp_container: HpContainer, hp_client: HelioPuddleClient, browser: Browser, tmp_path: Path
) -> None:
    file_id = add(hp_client, tmp_path, "e2e/ui/phone/narrow.csv", "x\n")
    page = browser.new_page(viewport=PHONE)
    try:
        page.goto(f"{hp_container.base_url}/projects/{hp_container.project}/files?file={file_id}")
        expect(page.locator("#inspector")).to_contain_text("narrow.csv")
        no_page_scroll(page)
    finally:
        shoot(page, "m6-files-phone")
        page.close()


@pytest.mark.req("UI-14")
def test_typeahead_shows_matches_as_you_type(
    hp_container: HpContainer, hp_client: HelioPuddleClient, browser: Browser, tmp_path: Path
) -> None:
    file_id = add(hp_client, tmp_path, "e2e/ui/search/typeahead_target.csv", "t\n")
    page = browser.new_page(viewport=DESKTOP)
    try:
        page.goto(f"{hp_container.base_url}/projects/{hp_container.project}/files")
        box = page.locator("#hp-search")
        box.click()
        box.press_sequentially("typeahead_tar", delay=30)
        suggestions = page.locator("#hp-suggest")
        expect(suggestions).to_be_visible()
        option = suggestions.get_by_role("option").first
        expect(option).to_contain_text("typeahead_target.csv")
        shoot(page, "m6-typeahead")
        box.press("ArrowDown")
        box.press("Enter")
        expect(page).to_have_url(re.compile(rf"/files/{file_id}$"))
        # submitting the box goes to the results page, grouped by project
        page.locator("#hp-search").fill("typeahead_target")
        page.locator("#hp-search").press("Enter")
        expect(page).to_have_url(re.compile(r"/search\?q=typeahead_target"))
        expect(page.locator(".result-group")).to_contain_text(hp_container.project)
    finally:
        page.close()


@pytest.mark.req("UI-09", "UI-06", "UI-14")
def test_the_basics_work_with_javascript_off(
    hp_container: HpContainer, hp_client: HelioPuddleClient, browser: Browser, tmp_path: Path
) -> None:
    file_id = add(hp_client, tmp_path, "e2e/ui/nojs/plain.csv", "p\n")
    context = browser.new_context(java_script_enabled=False, viewport=DESKTOP)
    page = context.new_page()
    base = f"{hp_container.base_url}/projects/{hp_container.project}"
    try:
        page.goto(f"{base}/files")
        # a filter chip is a <details> with a GET form
        page.locator("summary", has_text="+ Path").click()
        page.locator('input[name="path"]').fill("e2e/ui/nojs/*")
        page.get_by_role("button", name="Apply").first.click()
        expect(page).to_have_url(re.compile(r"path=e2e%2Fui%2Fnojs%2F"))
        expect(page.locator("table.ft tbody tr")).to_have_count(1)
        # without JavaScript the path goes to the file page
        page.get_by_role("link", name="e2e/ui/nojs/plain.csv").click()
        expect(page).to_have_url(re.compile(rf"/files/{file_id}$"))
        # ?file= renders the inspector on the server
        page.goto(f"{base}/files?file={file_id}")
        expect(page.locator("#inspector")).to_contain_text("plain.csv")
        # the header search is a plain form
        page.locator("#hp-search").fill("plain.csv")
        page.locator("#hp-search").press("Enter")
        expect(page.locator(".result-group")).to_contain_text("plain.csv")
        # menus are <details> and open without JavaScript
        page.goto(base)
        page.locator("summary", has_text="Actions").click()
        expect(page.get_by_role("button", name="Reconcile now")).to_be_visible()
        shoot(page, "m6-no-javascript")
    finally:
        context.close()


@pytest.mark.req("UI-13", "UI-07")
def test_storage_pages(hp_container: HpContainer, browser: Browser) -> None:
    for name, viewport in (("m6-storage", DESKTOP), ("m6-storage-phone", PHONE)):
        page = browser.new_page(viewport=viewport)
        try:
            page.goto(f"{hp_container.base_url}/projects/{hp_container.project}/storage")
            page.locator('a[href$="/storage/archive"]').first.click()
            expect(page.get_by_role("heading", name="archive", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Index · full")).to_be_visible()
            expect(page.get_by_text("Rules touching archive")).to_be_visible()
            no_page_scroll(page)
        finally:
            shoot(page, name)
            page.close()


def write_cdf(path: Path, records: int) -> None:
    """A small CDF: an epoch, a 3-vector and a global attribute."""
    import cdflib
    import numpy as np
    from cdflib import cdfwrite

    cdf = cdfwrite.CDF(str(path), delete=True)
    cdf.write_globalattrs({"Project": {0: "Preview e2e"}, "Descriptor": {0: "MAG>Magnetometer"}})
    start = cdflib.cdfepoch.compute_tt2000([2026, 1, 1, 0, 0, 0, 0, 0, 0])
    epoch = start + np.arange(records, dtype=np.int64) * 1_000_000_000
    cdf.write_var(
        {
            "Variable": "Epoch",
            "Data_Type": cdfwrite.CDF.CDF_TIME_TT2000,
            "Num_Elements": 1,
            "Rec_Vary": True,
            "Dim_Sizes": [],
        },
        var_attrs={"FIELDNAM": "Time"},
        var_data=epoch,
    )
    cdf.write_var(
        {
            "Variable": "B_GSE",
            "Data_Type": cdfwrite.CDF.CDF_REAL4,
            "Num_Elements": 1,
            "Rec_Vary": True,
            "Dim_Sizes": [3],
        },
        var_attrs={"UNITS": "nT", "DEPEND_0": "Epoch"},
        var_data=np.ones((records, 3), dtype=np.float32),
    )
    cdf.close()


@pytest.mark.req("UI-15")
def test_file_page_previews_a_csv_and_a_cdf(
    hp_container: HpContainer, hp_client: HelioPuddleClient, browser: Browser, tmp_path: Path
) -> None:
    rows = "\n".join(f"{i},{i * 0.5},row {i}" for i in range(250))
    csv_id = add(hp_client, tmp_path, "e2e/ui/preview/table.csv", f"n,half,label\n{rows}\n")
    cdf_local = tmp_path / "mag.cdf"
    write_cdf(cdf_local, 500)
    cdf_id = hp_client.add_file(cdf_local, "e2e/ui/preview/mag.cdf").id
    base = f"{hp_container.base_url}/projects/{hp_container.project}"
    page = browser.new_page(viewport=DESKTOP)
    try:
        # with JavaScript the preview is fetched after the page and swapped in
        page.goto(f"{base}/files/{csv_id}")
        preview = page.locator("#preview")
        table = preview.locator("table.pv-table")
        expect(table.locator("tbody tr")).to_have_count(100, timeout=15_000)
        expect(table.locator("thead")).to_contain_text("half")
        expect(preview.locator(".pv-caption")).to_contain_text("first 100")
        expect(table.locator("tbody tr").first).to_contain_text("row 0")
        shoot(page, "m6-preview-csv")

        page.goto(f"{base}/files/{cdf_id}#preview")
        attrs = preview.locator("details.pv-attrs")
        expect(attrs).to_contain_text("Global attributes", timeout=15_000)
        attrs.locator("summary").click()
        expect(attrs).to_contain_text("Preview e2e")
        expect(preview).to_contain_text("500 records")
        expect(preview.locator("table.pv-table").first.locator("tbody tr")).to_have_count(100)
        shoot(page, "m6-preview-cdf")
    finally:
        page.close()

    # without JavaScript, "Show preview" renders it in the page
    context = browser.new_context(java_script_enabled=False, viewport=DESKTOP)
    page = context.new_page()
    try:
        page.goto(f"{base}/files/{csv_id}")
        page.get_by_role("link", name="Show preview").click()
        expect(page).to_have_url(re.compile(r"preview=1"))
        expect(page.locator("#preview table.pv-table tbody tr")).to_have_count(100)
    finally:
        context.close()
