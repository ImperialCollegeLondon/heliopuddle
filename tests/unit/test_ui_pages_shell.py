"""The page shell, design tokens and no-JavaScript basics of the M6 pages."""

from __future__ import annotations

import re

import pytest

from heliopuddle.server.schemas import FileState
from tests.unit._ui_helpers import (
    UI_DIR,
    Factory,
    file_with,
    section,
    ui_client,
)
from tests.unit._ui_pages_helpers import A, M, P, U, fleet_factory, three_locations
from tests.unit.test_ui_rules import rule


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(fg: str, bg: str) -> float:
    high, low = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.req("UI-09")
@pytest.mark.parametrize(
    ("path", "tab"),
    [
        ("/projects/demo", "Overview"),
        ("/projects/demo/files", "Files"),
        ("/projects/demo/files/1", "Files"),
        ("/projects/demo/storage", "Storage"),
        ("/projects/demo/storage/ingest", "Storage"),
        ("/projects/demo/rules", "Rules"),
    ],
)
def test_project_pages_share_the_shell_with_tabs_switcher_and_search(path: str, tab: str) -> None:
    factory = fleet_factory(projects=["demo", "other"], rules=[rule()])
    html = ui_client(factory, prefect_ui_url="http://p:4200").get(path).text
    header = section(html, '<header class="hp-header">', "</header>")
    assert '<svg class="logo"' in header and "heliopuddle" in header
    switcher = section(header, '<details class="menu switcher">', "</details>")
    assert 'href="/projects/other"' in switcher
    assert 'href="/projects/demo" aria-current="true"' in switcher
    tabs = section(header, '<nav class="ptabs" aria-label="Project">', "</nav>")
    for _name, href in (
        ("Overview", "/projects/demo"),
        ("Files", "/projects/demo/files"),
        ("Storage", "/projects/demo/storage"),
        ("Rules", "/projects/demo/rules"),
    ):
        assert f'href="{href}"' in tabs
    current = re.findall(r'aria-current="page">(\w+)</a>', tabs)
    assert current == [tab]
    assert 'action="/search"' in header and 'name="q"' in header
    assert '<input type="hidden" name="project" value="demo">' in header
    assert "Search files in demo" in header
    assert 'href="http://p:4200"' in header
    assert "Actions" in header  # header mode: every control is shown


@pytest.mark.req("UI-09")
def test_home_search_covers_all_projects_and_has_no_project_tabs() -> None:
    html = ui_client(Factory(projects=["a", "b"])).get("/").text
    header = section(html, '<header class="hp-header">', "</header>")
    assert "Search files in all projects" in header
    assert 'name="project"' not in header
    assert 'class="ptabs"' not in header
    assert "Actions" not in header


@pytest.mark.req("UI-09")
def test_pages_use_the_bundled_stylesheet_script_and_fonts_and_no_cdn() -> None:
    html = ui_client(fleet_factory()).get("/projects/demo/files").text
    assert 'href="/static/hp.css"' in html and 'src="/static/hp.js" defer' in html
    assert "/static/fonts/manrope-latin-wght.woff2" in html
    for template in UI_DIR.rglob("*.html"):
        text = template.read_text()
        assert "https://" not in text, template.name
    css = (UI_DIR / "static" / "hp.css").read_text()
    assert "https://" not in css and "http://" not in css
    for font in re.findall(r'url\("([^"]+)"\)', css):
        assert (UI_DIR / "static" / font).is_file(), font
    licences = {p.name for p in (UI_DIR / "static" / "fonts").glob("OFL-*.txt")}
    assert licences == {"OFL-Manrope.txt", "OFL-IBM-Plex-Sans.txt", "OFL-IBM-Plex-Mono.txt"}


@pytest.mark.req("UI-09")
def test_design_tokens_and_state_colours_are_css_custom_properties() -> None:
    css = (UI_DIR / "static" / "hp.css").read_text()
    for token, value in (
        ("--bg", "#F5F6FA"),
        ("--surface", "#FFFFFF"),
        ("--border", "#E1E4EC"),
        ("--ink", "#0E1324"),
        ("--muted", "#5B6478"),
        ("--accent", "#FF8A3D"),
        ("--link", "#3438C9"),
        ("--available-bg", "#DDF5EA"),
        ("--missing-fg", "#A3122A"),
    ):
        assert f"{token}: {value}" in css, token
    assert "@media (max-width: 900px)" in css and "@media (max-width: 600px)" in css


@pytest.mark.req("UI-09")
@pytest.mark.parametrize(
    ("fg", "bg"),
    [
        ("#0E1324", "#F5F6FA"),  # text on the page
        ("#5B6478", "#FFFFFF"),  # muted text
        ("#5B6478", "#F5F6FA"),
        ("#3438C9", "#FFFFFF"),  # links
        ("#0B6B45", "#DDF5EA"),  # available
        ("#2B33B8", "#E1E6FF"),  # pending, overwriting
        ("#9A3D00", "#FFEBD9"),  # unknown
        ("#A3122A", "#FFE0E3"),  # missing
        ("#5A2BB0", "#EEE6FF"),  # deleting
        ("#5B6478", "#F0F2F7"),  # deleted, no copy
        ("#B8C0E0", "#121736"),  # header links
    ],
)
def test_colour_pairs_meet_wcag_aa(fg: str, bg: str) -> None:
    assert _contrast(fg, bg) >= 4.5


@pytest.mark.req("UI-09")
def test_states_have_a_symbol_as_well_as_a_colour() -> None:
    files = [
        file_with(1, ("ingest", A), ("archive", P)),
        file_with(2, ("ingest", U), ("archive", M)),
        file_with(3, ("ingest", FileState.DELETING), ("archive", FileState.OVERWRITING)),
    ]
    factory = Factory(files=files, storage_locations=three_locations())
    html = ui_client(factory).get("/projects/demo/files").text
    for label, symbol in (
        ("available", "✓"),
        ("pending", "◌"),
        ("unknown", "?"),
        ("missing", "!"),
        ("deleting", "\u00d7"),
        ("overwriting", "↻"),
    ):
        badge = f'class="st st-{label}" role="img" title="{label}" aria-label="{label}">{symbol}<'
        assert badge in html, label
    legend = section(html, '<ul class="legend"', "</ul>")
    assert "no copy" in legend and "available" in legend


@pytest.mark.req("UI-09")
def test_wide_tables_scroll_in_their_own_box() -> None:
    html = ui_client(fleet_factory()).get("/projects/demo/files").text
    assert '<div class="tbl-wrap">\n      <table class="ft">' in html
    css = (UI_DIR / "static" / "hp.css").read_text()
    assert ".tbl-wrap { overflow-x: auto; }" in css


@pytest.mark.req("UI-09")
def test_basics_work_without_javascript() -> None:
    """Menus are <details>, filters and paging are links or GET forms, actions are POST forms."""
    factory = fleet_factory(rules=[rule()])
    client = ui_client(factory)
    overview = client.get("/projects/demo").text
    assert '<details class="menu actions">' in overview
    assert '<form class="stack" method="post" action="/projects/demo/reconcile">' in overview
    files = client.get("/projects/demo/files", params={"file": "3"}).text
    assert '<aside id="inspector" class="inspector"' in files and "hidden" not in section(
        files, '<aside id="inspector"', ">"
    )
    assert 'method="get" action="/projects/demo/files"' in files
    assert "onclick" not in files.lower()
    script = (UI_DIR / "static" / "hp.js").read_text()
    for feature in ("hp-suggest", "data-inspect", "replaceState", "data-columns", "details.menu"):
        assert feature in script


@pytest.mark.req("UI-09")
def test_header_mode_user_menu_shows_the_header_user() -> None:
    client = ui_client(fleet_factory(), user_header="X-User")
    html = client.get("/projects/demo", headers={"X-User": "ann.lee"}).text
    menu = section(html, '<details class="menu usermenu">', "</details>")
    assert ">AL</summary>" in menu and 'data-user="ann.lee"' in menu
    assert "Log out" not in menu
    assert "usermenu" not in client.get("/projects/demo").text
