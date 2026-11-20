"""The file page's content preview (UI-15) through a stub client, and the image route."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from heliopuddle.client import HelioPuddleError
from heliopuddle.server.schemas import (
    FilePreview,
    FileState,
    PreviewColumn,
    PreviewTable,
    PreviewVariable,
)
from heliopuddle.server.ui.pages.files import preview as ui_preview
from tests.unit._ui_helpers import LATER, UI_DIR, Factory, make_file, section, ui_client

MiB = 1024 * 1024


def preview(**fields: Any) -> FilePreview:
    return FilePreview(**{"file_id": 1, "kind": "table", "format": "csv", **fields})


def table(**fields: Any) -> PreviewTable:
    values: dict[str, Any] = {
        "columns": [
            PreviewColumn(name="time", type="string"),
            PreviewColumn(name="flux", type="double"),
            PreviewColumn(name="flag", type="bool"),
        ],
        "rows": [["2026-01-01T00:00:00", 1.5, True], ["2026-01-01T00:01:00", None, False]],
        "more_rows": True,
        "total_rows": 86400,
    }
    return PreviewTable(**{**values, **fields})


def fragment(factory: Factory, file_id: int = 1, status: int = 200) -> str:
    resp = ui_client(factory).get(f"/projects/demo/files/{file_id}/preview")
    assert resp.status_code == status, resp.text
    return resp.text


def factory_with(p: FilePreview | None = None, **extra: Any) -> Factory:
    files = extra.pop("files", None) or [make_file(1)]
    return Factory(files=files, previews={1: p} if p else {}, **extra)


# --- the file page ---------------------------------------------------------------------------


@pytest.mark.req("UI-15")
def test_file_page_loads_the_preview_after_the_page() -> None:
    factory = factory_with(preview(tables=[table()]))
    html = ui_client(factory).get("/projects/demo/files/1").text
    card = section(html, '<section class="card preview-card" id="preview"')
    assert 'data-preview="/projects/demo/files/1/preview"' in card
    assert 'href="/projects/demo/files/1?preview=1#preview"' in card and "Show preview" in card
    assert factory.calls("preview") == []  # the page itself does not wait for the preview


@pytest.mark.req("UI-15")
def test_file_page_renders_the_preview_inline_without_javascript() -> None:
    factory = factory_with(preview(tables=[table()]))
    html = ui_client(factory).get("/projects/demo/files/1?preview=1").text
    card = section(html, '<section class="card preview-card" id="preview"')
    assert "data-preview=" not in card
    assert "<table" in card and "flux" in card and "first 2 of 86,400 rows" in card
    assert factory.calls("preview") == [1]


@pytest.mark.req("UI-15")
def test_script_fetches_the_preview_fragment() -> None:
    script = (UI_DIR / "static" / "hp.js").read_text()
    assert "[data-preview]" in script and 'getAttribute("data-preview")' in script


@pytest.mark.req("UI-15")
def test_inspector_links_to_the_preview() -> None:
    html = ui_client(factory_with()).get("/projects/demo/files/1/inspector").text
    assert 'href="/projects/demo/files/1#preview"' in html and ">Preview</a>" in html


@pytest.mark.req("UI-15")
def test_deleted_file_has_no_preview_and_does_not_ask_the_api() -> None:
    gone = make_file(1, state=FileState.DELETED, deleted_at=LATER)
    factory = factory_with(files=[gone])
    page = ui_client(factory).get("/projects/demo/files/1").text
    assert "No readable copy to preview." in section(page, 'id="preview"')
    assert "No readable copy to preview." in fragment(factory)
    assert factory.calls("preview") == []


# --- tables ------------------------------------------------------------------------------------


@pytest.mark.req("UI-15")
def test_table_has_types_sticky_header_numbers_and_nulls() -> None:
    html = fragment(factory_with(preview(tables=[table()], storage_location="ingest")))
    assert 'class="pv-scroll"' in html and 'class="pv-table"' in html
    head = section(html, "<thead>", "</thead>")
    assert re.findall(r'<span class="pv-col">([^<]+)</span>', head) == ["time", "flux", "flag"]
    assert re.findall(r'<span class="pv-type">([^<]+)</span>', head) == ["string", "double", "bool"]
    assert '<th scope="col" class="num">' in head  # the numeric column is right-aligned
    body = section(html, "<tbody>", "</tbody>")
    assert '<td class="num">1.5</td>' in body
    assert '<td class="num"><span class="pv-null" title="null">—</span></td>' in body
    assert "<td>true</td>" in body and "<td>false</td>" in body
    assert "first 2 of 86,400 rows" in html
    assert "CSV" in html and "ingest" in html


@pytest.mark.req("UI-15")
@pytest.mark.parametrize(
    ("fields", "caption"),
    [
        ({"more_rows": True, "total_rows": None}, "first 2 rows"),
        ({"more_rows": False, "total_rows": 2}, "2 rows"),
        ({"more_rows": False, "total_rows": None, "rows": [[1, 2.0, False]]}, "1 row"),
    ],
)
def test_table_caption_says_how_many_rows_are_shown(fields: dict[str, Any], caption: str) -> None:
    html = fragment(factory_with(preview(tables=[table(**fields)])))
    assert re.search(rf'<p class="pv-caption">\s*{caption}\b', html), html


@pytest.mark.req("UI-15")
def test_table_hidden_columns_are_noted() -> None:
    html = fragment(factory_with(preview(tables=[table(hidden_columns=12)])))
    assert "12 more columns not shown" in html


@pytest.mark.req("UI-15")
def test_table_cells_are_escaped() -> None:
    evil = table(rows=[["<script>alert(1)</script>", 1, True]], more_rows=False)
    html = fragment(factory_with(preview(tables=[evil])))
    assert "<script>alert" not in html and "&lt;script&gt;" in html


@pytest.mark.req("UI-15")
def test_cdf_tables_variables_and_global_attributes() -> None:
    var = PreviewVariable(
        name="B_GSE",
        type="CDF_REAL4",
        shape=[3],
        records=86400,
        attributes={"UNITS": "nT", "CATDESC": "Magnetic field"},
    )
    epoch = PreviewVariable(name="Epoch", type="CDF_TT2000", shape=[], records=86400)
    first = table(variables=[epoch, var], name="86400 records")
    second = table(name="support", total_rows=1, more_rows=False, rows=[["a", 2, True]])
    p = preview(format="cdf", tables=[first, second], attributes={"Project": "Demo mission"})
    html = fragment(factory_with(p))
    assert "Table 1 · 86,400 records" in html
    assert "support · 1 record" in html
    attrs = section(html, '<details class="pv-attrs"', "</details>")
    assert "Global attributes" in attrs and "Project" in attrs and "Demo mission" in attrs
    variables = section(html, '<details class="pv-vars"', "</dl>\n    </details>\n  </details>")
    assert "Variables · 2" in variables
    assert "B_GSE" in variables and "CDF_REAL4" in variables and "[3]" in variables
    assert "scalar" in variables and "86,400 records" in variables
    assert "UNITS" in variables and "Magnetic field" in variables
    assert html.count('<details class="pv-tbl"') == 2
    assert '<details class="pv-tbl" open>' in html  # the first table is open


@pytest.mark.req("UI-15")
def test_parquet_metadata_is_listed_as_file_attributes() -> None:
    p = preview(format="parquet", tables=[table()], attributes={"writer": "demo"})
    html = fragment(factory_with(p))
    assert "File attributes" in section(html, '<details class="pv-attrs"', "</details>")


@pytest.mark.req("UI-15")
def test_skipped_cdf_variables_are_noted_beside_the_tables() -> None:
    message = "1 variable could not be read and is not shown: broken<x>"
    p = preview(format="cdf", tables=[table()], message=message)
    html = fragment(factory_with(p))
    assert '<p class="pv-caption pv-skipped" role="note">' in html
    assert "broken&lt;x&gt;" in html


# --- text, JSON, Markdown -----------------------------------------------------------------------


@pytest.mark.req("UI-15")
def test_json_is_pretty_printed_with_light_colouring() -> None:
    text = '{\n  "name": "a <b>",\n  "n": 3,\n  "ok": true,\n  "x": null\n}'
    html = fragment(factory_with(preview(kind="json", format="json", text=text)))
    block = section(html, '<pre class="pv-code pv-json">', "</pre>")
    assert '<span class="j-key">&#34;name&#34;</span>' in block
    assert '<span class="j-str">&#34;a &lt;b&gt;&#34;</span>' in block
    assert '<span class="j-num">3</span>' in block
    assert '<span class="j-lit">true</span>' in block and '<span class="j-lit">null</span>' in block


@pytest.mark.req("UI-15")
def test_text_is_shown_monospaced_and_escaped_with_a_truncation_note() -> None:
    p = preview(kind="text", format="txt", text="line <1>\nline 2", text_truncated=True)
    html = fragment(factory_with(p))
    assert '<pre class="pv-code">line &lt;1&gt;\nline 2</pre>' in html
    assert "Showing the start of the file." in html


@pytest.mark.req("UI-15")
def test_markdown_is_rendered_with_raw_html_escaped_and_unsafe_links_dropped() -> None:
    text = (
        "# Title\n\nSome *text* and <b>bold</b>.\n\n"
        "[ok](https://example.org) [mail](mailto:a@b.c) [rel](notes/b.md) "
        "[bad](javascript:alert(1)) [data](data:text/html;base64,AAAA)\n\n"
        "<script>alert(1)</script>\n"
    )
    html = fragment(factory_with(preview(kind="markdown", format="md", text=text)))
    body = section(html, '<div class="pv-md">', "</div>")
    assert "<h1>Title</h1>" in body and "<em>text</em>" in body
    assert "&lt;b&gt;bold&lt;/b&gt;" in body and "<script>" not in body
    assert 'href="https://example.org"' in body and 'href="mailto:a@b.c"' in body
    assert 'href="notes/b.md"' in body
    assert "javascript:" not in body.replace("[bad](javascript:", "")
    assert 'href="javascript' not in body and 'href="data:' not in body


@pytest.mark.req("UI-15")
@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://a.org/x", True),
        ("http://a.org", True),
        ("mailto:x@y.z", True),
        ("docs/readme.md", True),
        ("/abs/path", True),
        ("#anchor", True),
        ("javascript:alert(1)", False),
        ("JaVaScRiPt:alert(1)", False),
        ("vbscript:x", False),
        ("data:text/html,x", False),
        ("file:///etc/passwd", False),
        ("ftp://a.org", False),
    ],
)
def test_markdown_link_filter(url: str, kept: bool) -> None:
    assert ui_preview.safe_link(url) is kept


# --- images ---------------------------------------------------------------------------------


def image_file(size: int = 2048, name: str = "sun.png") -> Any:
    return make_file(1, name=name, path=f"img/{name}", size=size)


@pytest.mark.req("UI-15")
def test_image_is_shown_inline_from_the_raw_route() -> None:
    p = preview(kind="image", format="png", media_type="image/png")
    html = fragment(factory_with(p, files=[image_file()]))
    assert '<img class="pv-img" src="/projects/demo/files/1/raw" alt="sun.png"' in html


@pytest.mark.req("UI-15")
def test_image_over_20_mib_is_not_shown() -> None:
    p = preview(kind="image", format="png", media_type="image/png")
    html = fragment(factory_with(p, files=[image_file(size=20 * MiB + 1)]))
    assert "<img" not in html and "too large to show" in html


@pytest.mark.req("UI-15")
@pytest.mark.parametrize(
    ("name", "media_type"),
    [("sun.png", "image/png"), ("a.JPG", "image/jpeg"), ("v.svg", "image/svg+xml")],
)
def test_raw_route_streams_an_image_sandboxed(name: str, media_type: str) -> None:
    factory = factory_with(files=[image_file(name=name, size=4)], content=b"\x89PNG")
    resp = ui_client(factory).get("/projects/demo/files/1/raw")
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG"
    assert resp.headers["content-type"] == media_type
    assert resp.headers["content-security-policy"].startswith("sandbox")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith("inline")


@pytest.mark.req("UI-15")
def test_raw_route_refuses_large_images_other_formats_and_other_projects() -> None:
    client = ui_client(factory_with(files=[image_file(size=20 * MiB + 1)]))
    big = client.get("/projects/demo/files/1/raw")
    assert big.status_code == 413 and "20 MiB" in big.text
    assert ui_client(factory_with()).get("/projects/demo/files/1/raw").status_code == 404  # csv
    other = factory_with(files=[make_file(1, name="a.png", project="other")])
    assert ui_client(other).get("/projects/demo/files/1/raw").status_code == 404
    gone = factory_with(files=[make_file(1, name="a.png", state=FileState.MISSING)])
    assert ui_client(gone).get("/projects/demo/files/1/raw").status_code == 404
    assert ui_client(factory_with()).get("/projects/demo/files/9/raw").status_code == 404


# --- notices -------------------------------------------------------------------------------------


@pytest.mark.req("UI-15")
@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("none", "No preview for .bin files."),
        ("too_large", "This CDF file is larger than the preview limit (1 GB)."),
        ("error", "Could not read the file as CSV: ValueError: bad"),
    ],
)
def test_no_preview_too_large_and_error_show_the_api_message(kind: str, message: str) -> None:
    html = fragment(factory_with(preview(kind=kind, message=message)))
    assert f'class="pv-notice pv-{kind}"' in html and message in html


@pytest.mark.req("UI-15")
def test_api_failure_is_a_notice_in_the_fragment() -> None:
    factory = factory_with(preview_error=HelioPuddleError(503, "storage location ingest is down"))
    html = fragment(factory)
    assert "pv-notice" in html and "storage location ingest is down" in html
    page = ui_client(factory).get("/projects/demo/files/1?preview=1")
    assert page.status_code == 200 and "storage location ingest is down" in page.text


@pytest.mark.req("UI-15")
def test_fragment_of_an_unknown_file_or_another_project_is_404() -> None:
    assert "not in this project" in fragment(factory_with(), file_id=9, status=404)
    other = factory_with(files=[make_file(1, project="other")])
    fragment(other, status=404)
    assert ui_client(factory_with()).get("/projects/demo/files/x/preview").status_code == 404


@pytest.mark.req("UI-15")
@pytest.mark.parametrize(
    ("error", "text"),
    [
        (httpx.ConnectError("down"), "No preview: the API is unreachable."),
        (HelioPuddleError(500, [{"msg": "x"}]), "No preview: The preview failed."),
    ],
)
def test_unreachable_api_or_odd_errors_are_notices(error: Exception, text: str) -> None:
    assert text in fragment(factory_with(preview_error=error))


@pytest.mark.req("UI-15")
def test_raw_route_passes_api_errors_through() -> None:
    factory = factory_with(files=[image_file()], error=HelioPuddleError(503, "storage down"))
    resp = ui_client(factory).get("/projects/demo/files/1/raw")
    assert resp.status_code == 503 and "storage down" in resp.text


@pytest.mark.req("UI-15")
def test_link_with_an_invalid_url_is_dropped() -> None:
    assert ui_preview.safe_link("http://[::1") is False
    assert ui_preview.safe_link("c:\\x") is False


@pytest.mark.req("UI-15")
def test_table_helpers_without_values_or_counts() -> None:
    empty = PreviewTable(
        columns=[PreviewColumn(name="n", type="int64"), PreviewColumn(name="s", type="string")],
        rows=[],
        more_rows=True,
    )
    assert ui_preview.numeric_columns(empty) == [True, False]
    p = preview(format="parquet")
    assert ui_preview.table_title(empty, 2, p) == "Table 2"
    small = PreviewTable(columns=[], rows=[[], []], more_rows=False, name="t")
    assert ui_preview.table_title(small, 1, p) == "t · 2 rows"
    assert ui_preview.shape_text([4, 8]) == "[4×8]"  # noqa: RUF001


@pytest.mark.req("UI-15")
def test_numbers_written_as_text_count_as_numbers() -> None:
    csv = PreviewTable(
        columns=[PreviewColumn(name=n) for n in ("a", "b", "c", "d")],
        rows=[["1.5", "x", "", "True"], ["-2e3", "3", "", "False"], [" 7 ", "4", "", "1"]],
        more_rows=False,
    )
    assert ui_preview.numeric_columns(csv) == [True, False, False, False]


@pytest.mark.req("UI-15")
def test_markdown_tables_are_rendered() -> None:
    html = str(ui_preview.render_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n"))
    assert "<table>" in html and "<td>2</td>" in html
