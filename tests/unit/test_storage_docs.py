"""The storage user docs cover every backend and credential route (DEP-22)."""

from __future__ import annotations

from pathlib import Path

import pytest

from heliopuddle.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "site" / "storage.md"


@pytest.mark.req("DEP-22")
def test_storage_page_is_in_the_site_nav() -> None:
    assert "storage.md" in (ROOT / "mkdocs.yml").read_text(encoding="utf-8")


@pytest.mark.req("DEP-22")
@pytest.mark.parametrize(
    "needle",
    [
        # every backend
        "file://",
        "smb://",
        "sftp://",
        "s3://",
        "az://",
        "gs://",
        "rclone://",
        # every credential route
        "${",
        "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "{{ prefect.blocks.secret.",
        "block: ",
        "config_block",
        "RClone Remote Config File [ManagedFileTransfer]",
        # the rclone model, verify modes and provider notes
        "rclone:<config block>",
        "Retry-After",
        "`native`",
        "`content`",
        "documentLibrary",
        "box_config_file",
    ],
)
def test_storage_page_covers(needle: str) -> None:
    assert needle in PAGE.read_text(encoding="utf-8")


@pytest.mark.req("DEP-22")
def test_storage_page_names_every_rclone_setting() -> None:
    page = PAGE.read_text(encoding="utf-8")
    for name in Settings.model_fields:
        if name.startswith("rclone_"):
            assert f"HELIOPUDDLE_{name.upper()}" in page, name
