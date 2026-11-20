"""AUTH-16: log in to the UI of an image in ``local`` auth mode, browse, and log out."""

from __future__ import annotations

from pathlib import Path
from re import compile

import pytest
from playwright.sync_api import Browser, expect

from heliopuddle.client import HelioPuddleClient, PermissionDeniedError

from .conftest import LocalAuthHp
from .harness import ROOT

REVIEWS = ROOT / "docs" / "spec" / "reviews"


@pytest.mark.req("AUTH-16")
def test_login_browse_and_logout_in_local_mode(
    hp_local_auth: LocalAuthHp, browser: Browser, tmp_path: Path
) -> None:
    hp = hp_local_auth.hp
    local = tmp_path / "login-check.csv"
    local.write_text("a,b\n1,2\n")
    with hp_local_auth.client() as client:  # a token from the API's own login
        record = client.add_file(local, "e2e/login/login-check.csv")

    REVIEWS.mkdir(parents=True, exist_ok=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        # an anonymous visit is sent to the login form
        page.goto(f"{hp.base_url}/")
        expect(page).to_have_url(compile(r"/login$"))
        expect(page.get_by_role("heading", name="Log in")).to_be_visible()

        # a wrong password gives a generic message and keeps the user out
        page.get_by_label("Username").fill("admin")
        page.get_by_label("Password").fill("not-the-password-1")
        page.get_by_role("button", name="Log in").click()
        expect(page.get_by_text("Incorrect username or password.")).to_be_visible()
        expect(page.get_by_label("Password")).to_have_value("")
        page.screenshot(path=str(REVIEWS / "m5a-login.png"), full_page=True)

        # the right password lands on the Overview, with the user in the header menu
        page.get_by_label("Password").fill(hp_local_auth.admin_password)
        page.get_by_role("button", name="Log in").click()
        expect(page.get_by_text(record.name).first).to_be_visible(timeout=15_000)
        expect(page.locator(".session .user")).to_have_text("admin")
        page.locator("summary.avatar").click()
        expect(page.get_by_role("button", name="Log out")).to_be_visible()
        page.locator("summary.avatar").click()
        page.locator("summary", has_text="Actions").click()  # admin may index
        expect(page.get_by_role("button", name="Full index of primary")).to_be_visible()
        page.screenshot(path=str(REVIEWS / "m5a-files.png"), full_page=True)
        page.keyboard.press("Escape")

        # logging out drops the session
        page.locator("summary.avatar").click()
        page.get_by_role("button", name="Log out").click()
        expect(page.get_by_text("You have been logged out.")).to_be_visible()
        page.goto(f"{hp.base_url}/projects/{hp.project}")
        expect(page).to_have_url(compile(r"/login\?next="))
    finally:
        page.close()


@pytest.mark.req("AUTH-07", "AUTH-08", "AUTH-15", "AUTH-16", "UI-08")
def test_reader_browses_without_backend_credentials_or_reconcile(
    hp_local_auth: LocalAuthHp, browser: Browser
) -> None:
    hp = hp_local_auth.hp
    username = "e2e-reader"
    password = "e2e-reader-password-1"
    with hp_local_auth.client() as admin:
        admin.create_user(username, password, roles=[f"hp-reader:{hp.project}"])
    with HelioPuddleClient(hp.api_url, hp.project, username=username, password=password) as reader:
        assert reader.list_storage_locations()[0].url == ""
        with pytest.raises(PermissionDeniedError) as error:
            reader.get_storage_location()
        assert error.value.required_roles == [
            "hp-client-storage-read-write",
            f"hp-client-storage-read-write:{hp.project}",
            "hp-admin",
        ]

    page = browser.new_page()
    try:
        page.goto(f"{hp.base_url}/login")
        page.get_by_label("Username").fill(username)
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Log in").click()
        expect(page.locator(".session .user")).to_have_text(username)
        expect(page.locator('form[action$="/reconcile"]')).to_have_count(0)
        expect(page.locator('form[action$="/index"]')).to_have_count(0)
        page.goto(f"{hp.base_url}/projects/{hp.project}/storage/primary")
        expect(page.locator(".sl-url").first).to_have_text("Restricted")
        expect(page.locator('form[action$="/index"]')).to_have_count(0)
        forged = page.request.post(
            f"{hp.base_url}/projects/{hp.project}/reconcile", form={"verify": "true"}
        )
        assert forged.status == 403
    finally:
        page.close()
