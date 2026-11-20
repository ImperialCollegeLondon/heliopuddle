"""UI login, redirects to the login page and the login form (AUTH-16, AUTH-06)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heliopuddle.client import (
    AuthenticationError,
)
from heliopuddle.server.ui import create_ui
from heliopuddle.server.ui.auth import SESSION_COOKIE, safe_next
from tests.unit._ui_auth_helpers import (
    PASSWORD,
    SECRET,
    AuthFactory,
    location,
    login,
    make_settings,
    ui,
)
from tests.unit._ui_helpers import (
    make_file,
)

pytestmark = pytest.mark.req("AUTH-16")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "/login"),
        ("/projects/demo", "/login?next=%2Fprojects%2Fdemo"),
        (
            "/projects/demo?limit=5&path=a%2Fb",
            "/login?next=%2Fprojects%2Fdemo%3Flimit%3D5%26path%3Da%252Fb",
        ),
        ("/projects/demo/rules", "/login?next=%2Fprojects%2Fdemo%2Frules"),
        ("/projects/demo/files/2", "/login?next=%2Fprojects%2Fdemo%2Ffiles%2F2"),
    ],
)
def test_pages_redirect_to_login_without_a_session(path: str, expected: str) -> None:
    factory = AuthFactory()
    response = ui(factory).get(path, follow_redirects=False)
    assert location(response) == expected
    assert not factory.made  # nothing reached the API


def test_posts_redirect_to_login_without_a_session() -> None:
    factory = AuthFactory()
    browser = ui(factory)
    for path in (
        "/projects/demo/reconcile",
        "/projects/demo/storage/primary/index",
        "/projects/demo/files/1/accept",
        "/projects/demo/files/1/rules-ignore",
        "/projects/demo/rules/archive/enable",
        "/projects/demo/rules/run-once",
    ):
        response = browser.post(path, data={"mode": "full"}, follow_redirects=False)
        assert location(response) == "/login", path
    assert not factory.made


def test_login_page_and_static_files_are_public() -> None:
    browser = ui(AuthFactory())
    assert browser.get("/static/hp.css").status_code == 200
    page = browser.get("/login")
    assert page.status_code == 200
    assert 'autocomplete="username"' in page.text
    assert 'autocomplete="current-password"' in page.text
    assert 'type="password"' in page.text
    assert "no-store" in page.headers["cache-control"]


@pytest.mark.req("AUTH-16")
def test_login_page_header_has_no_projects_or_runs_links() -> None:
    browser = ui(AuthFactory(), prefect_ui_url="http://prefect.example")
    page = browser.get("/login")
    assert page.status_code == 200
    header = page.text.split("</header>", 1)[0]
    assert 'class="mainnav"' not in header
    assert ">Projects<" not in header
    assert "prefect.example" not in header
    assert "Runs" not in header
    assert 'class="brand"' in header  # the logo stays


def test_login_stores_a_session_and_follows_next() -> None:
    factory = AuthFactory(files=[make_file(1)])
    browser = ui(factory)
    response = login(browser, next="/projects/demo?limit=5")
    assert location(response) == "/projects/demo?limit=5"
    page = browser.get("/projects/demo")
    assert page.status_code == 200
    assert "data/file-1.csv" in page.text
    # the page's client carried the session's token and no user header
    assert factory.made[-1].token is not None
    assert factory.made[-1].token.startswith("tok-ann")
    assert factory.made[-1].user is None
    assert factory.made[-1].project == "demo"
    assert all(c.closed for c in factory.made)


def test_session_cookie_flags_over_http() -> None:
    browser = ui(AuthFactory())
    cookie = login(browser).headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/;" in lowered or lowered.endswith("path=/")
    assert "secure" not in lowered
    assert "max-age" not in lowered  # a browser-session cookie


def test_session_cookie_is_secure_over_https() -> None:
    factory = AuthFactory()
    app = create_ui(
        make_settings(auth_mode="local", auth_secret_key=SECRET), client_factory=factory
    )
    secure = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    assert "secure" in login(secure).headers["set-cookie"].lower()
    behind_proxy = ui(AuthFactory())
    proxied = login(behind_proxy, headers={"X-Forwarded-Proto": "https"})
    assert "secure" in proxied.headers["set-cookie"].lower()


def test_session_cookie_is_scoped_to_the_root_path() -> None:
    browser = ui(AuthFactory(), ui_root_path="/hp")
    response = login(browser)
    assert "path=/hp;" in response.headers["set-cookie"].lower()
    assert location(response) == "/hp/"


def test_token_and_secret_never_appear_in_pages() -> None:
    factory = AuthFactory(files=[make_file(1)])
    browser = ui(factory)
    login(browser)
    for path in ("/projects/demo", "/login", "/"):
        html = browser.get(path, follow_redirects=True).text
        assert "tok-ann" not in html
        assert SECRET not in html
        assert PASSWORD not in html


def test_wrong_credentials_are_a_generic_401_without_echoing_the_password() -> None:
    browser = ui(AuthFactory())
    wrong = login(browser, password="not-the-password-1")
    unknown = login(browser, username="nobody", password="not-the-password-1")
    assert wrong.status_code == unknown.status_code == 401
    for response in (wrong, unknown):
        assert "Incorrect username or password." in response.text
        assert "not-the-password-1" not in response.text
        assert "set-cookie" not in response.headers
    assert 'value="ann"' in wrong.text  # the user name is kept for retyping
    assert wrong.text.replace("ann", "nobody") == unknown.text
    assert browser.get("/", follow_redirects=False).status_code == 303  # still logged out


def test_empty_credentials_are_refused() -> None:
    response = ui(AuthFactory()).post("/login", data={"username": "", "password": ""})
    assert response.status_code == 401


def test_login_when_the_api_is_unreachable_is_an_error_page() -> None:
    factory = AuthFactory(login_error=AuthenticationError(0, "cannot reach the API"))
    response = login(ui(factory))
    assert response.status_code == 502
    assert "Incorrect" not in response.text


@pytest.mark.parametrize(
    "bad",
    [
        "//evil.example",
        "//evil.example/path",
        "https://evil.example",
        "http://evil.example/x",
        "/\\evil.example",
        "\\\\evil.example",
        "javascript:alert(1)",
        "/\tevil",
        "/\n/evil.example",
        "evil",
        "",
        "/login",
        "/auth/callback?code=1",
    ],
)
def test_open_redirect_attempts_go_to_the_root(bad: str) -> None:
    browser = ui(AuthFactory())
    assert location(login(browser, next=bad)) == "/"
    # and the same through the GET that arrives already logged in
    assert location(browser.get("/login", params={"next": bad}, follow_redirects=False)) == "/"


@pytest.mark.parametrize(
    ("value", "root", "expected"),
    [
        ("/projects/demo", "", "/projects/demo"),
        ("/projects/demo?limit=5&x=a%2Fb", "", "/projects/demo?limit=5&x=a%2Fb"),
        ("/", "", "/"),
        ("/hp/projects/demo", "/hp", "/projects/demo"),
        ("/hp", "/hp", "/"),
        ("/hp//evil.example", "/hp", "/"),
        ("/hpx/projects", "/hp", "/hpx/projects"),
    ],
)
def test_safe_next(value: str, root: str, expected: str) -> None:
    assert safe_next(value, root) == expected


def test_next_is_kept_through_a_failed_login_and_is_escaped() -> None:
    browser = ui(AuthFactory())
    page = browser.post(
        "/login",
        data={"username": "ann", "password": "wrong-password-1", "next": '/p"><script>x</script>'},
    )
    assert "<script>x</script>" not in page.text
    page = browser.get("/login", params={"next": "/projects/demo?a=1&b=2"})
    assert 'name="next" value="/projects/demo?a=1&amp;b=2"' in page.text


def test_login_form_post_must_come_from_this_site() -> None:
    browser = ui(AuthFactory())
    response = login(browser, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = login(browser, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
