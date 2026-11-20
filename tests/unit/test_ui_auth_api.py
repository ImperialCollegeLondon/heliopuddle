"""The UI against the real API in ``local`` auth mode, in each serving mode (AUTH-16, UI-03)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.api import create_api
from heliopuddle.server.app import create_app
from heliopuddle.server.context import AppContext
from heliopuddle.server.ui import create_ui
from heliopuddle.settings import Settings
from tests.local_auth import PASSWORD, add_user, local_context

pytestmark = pytest.mark.req("AUTH-16")

Factory = Callable[..., HelioPuddleClient]


@pytest.fixture
def lctx(tmp_path: Path, config_dir: Path, migrated_db: Path) -> Iterator[AppContext]:
    ctx = local_context(tmp_path, config_dir, migrated_db)
    add_user(ctx, "root", ["hp-admin"])
    add_user(ctx, "viewer", ["hp-reader"])
    add_user(ctx, "elsewhere", ["hp-writer:other"])
    yield ctx
    ctx.dispose()


def over(api: TestClient) -> Factory:
    """A client factory whose clients talk to ``api`` (an in-process HTTP client)."""

    def make(
        project: str | None,
        user: str | None = None,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> HelioPuddleClient:
        return HelioPuddleClient(
            "http://testserver",
            project,
            user=user,
            http_client=api,
            token=token,
            username=username,
            password=password,
        )

    return make


def sign_in(browser: TestClient, username: str, base: str = "") -> None:
    response = browser.post(
        f"{base}/login",
        data={"username": username, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


@pytest.fixture
def separate(lctx: AppContext) -> Iterator[TestClient]:
    """The UI as its own process: it reaches the API only over HTTP (HELIOPUDDLE_UI_API_URL)."""
    with TestClient(create_api(lctx)) as api:
        ui = create_ui(lctx.settings, client_factory=over(api))
        yield TestClient(ui, raise_server_exceptions=False)


def test_login_and_browse_through_the_real_api(separate: TestClient) -> None:
    assert separate.get("/projects/demo", follow_redirects=False).status_code == 303
    wrong = separate.post("/login", data={"username": "root", "password": "wrong-password-1"})
    assert wrong.status_code == 401
    sign_in(separate, "root")
    page = separate.get("/projects/demo")
    assert page.status_code == 200
    assert 'data-user="root"' in page.text
    assert "hp-admin" in page.text
    assert "Index now" in page.text  # an admin can operate


def test_a_reader_sees_files_but_no_write_controls(separate: TestClient) -> None:
    sign_in(separate, "viewer")
    page = separate.get("/projects/demo")
    assert page.status_code == 200
    assert "Index now" not in page.text
    assert "Reconcile now" not in page.text
    rules = separate.get("/projects/demo/rules")
    assert rules.status_code == 200
    assert "Run once" not in rules.text
    # the API still enforces: a forged post is refused
    forged = separate.post(
        "/projects/demo/storage/primary/index",
        data={"mode": "full"},
        follow_redirects=False,
    )
    assert forged.status_code == 403
    reconcile = separate.post(
        "/projects/demo/reconcile", data={"verify": "true"}, follow_redirects=False
    )
    assert reconcile.status_code == 403


def test_the_picker_and_project_pages_follow_the_users_projects(separate: TestClient) -> None:
    sign_in(separate, "elsewhere")  # a writer on another project only
    picker = separate.get("/")
    assert picker.status_code == 200
    assert "demo" not in picker.text
    assert separate.get("/projects/demo").status_code == 404


def test_the_ui_works_on_its_own_port_under_a_root_path(lctx: AppContext) -> None:
    settings = lctx.settings.model_copy(update={"ui_root_path": "/ui"})
    with TestClient(create_api(lctx)) as api:
        browser = TestClient(
            create_ui(settings, client_factory=over(api)), raise_server_exceptions=False
        )
        unauth = browser.get("/ui/projects/demo", follow_redirects=False)
        assert unauth.headers["location"] == "/ui/login?next=%2Fprojects%2Fdemo"
        sign_in(browser, "root", "/ui")
        page = browser.get("/ui/projects/demo")
        assert page.status_code == 200
        assert 'action="/ui/logout"' in page.text
        out = browser.post("/ui/logout", follow_redirects=False)
        assert out.headers["location"] == "/ui/login?logged_out=1"


def test_the_ui_shares_the_apis_port(lctx: AppContext) -> None:
    holder: list[TestClient] = []

    def factory(*args: object, **kwargs: object) -> HelioPuddleClient:
        return over(holder[0])(*args, **kwargs)  # type: ignore[arg-type]

    app = create_app(lctx.settings, lctx, api=True, ui=True, client_factory=factory)
    with TestClient(app, raise_server_exceptions=False) as browser:
        api = TestClient(app, base_url="http://testserver/api/v1")
        holder.append(api)
        assert browser.get("/projects/demo", follow_redirects=False).status_code == 303
        sign_in(browser, "root")
        page = browser.get("/projects/demo")
        assert page.status_code == 200
        assert "Index now" in page.text
        # the API's own routes are not behind the UI session
        assert browser.get("/api/v1/auth/config").status_code == 200


def test_header_mode_ui_needs_no_login(lctx: AppContext, tmp_path: Path) -> None:
    settings = Settings(
        auth_mode="header", config_dir=lctx.settings.config_dir, data_dir=tmp_path / "other"
    )
    seen: list[str | None] = []

    def factory(project: str | None, user: str | None = None, **kw: object) -> HelioPuddleClient:
        seen.append(user)
        return HelioPuddleClient("http://nowhere.invalid/api/v1", project, user=user)

    browser = TestClient(create_ui(settings, client_factory=factory), raise_server_exceptions=False)
    assert browser.get("/login").status_code == 404
    browser.get("/", headers={"X-Forwarded-User": "bob"})
    assert seen == ["bob"]
