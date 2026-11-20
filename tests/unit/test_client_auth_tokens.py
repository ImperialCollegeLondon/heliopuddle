"""Client token fetching, refresh and retry."""

from __future__ import annotations

import threading

import pytest

from heliopuddle.client import (
    AuthenticationError,
)
from heliopuddle.client import auth as client_auth
from tests.unit._client_auth_helpers import Fake, bearer


@pytest.mark.req("AUTH-15")
def test_header_mode_sends_user_header_and_no_token() -> None:
    fake = Fake("header")
    with fake.client(username="a", password="b", user="carol", user_header="X-User") as c:
        c.list_projects()
    assert not fake.token_requests
    last = fake.requests[-1]
    assert last.headers["x-user"] == "carol"
    assert "authorization" not in last.headers


@pytest.mark.req("AUTH-15")
def test_no_credentials_sends_user_header_without_config_lookup() -> None:
    fake = Fake("header")
    with fake.client(user="alice") as c:
        c.list_projects()
    assert fake.config_calls == 0
    assert [r.headers["x-forwarded-user"] for r in fake.requests] == ["alice"]


@pytest.mark.req("AUTH-15")
def test_user_header_not_sent_with_a_token_in_local_mode() -> None:
    fake = Fake("local", accepted=set())
    with fake.client(username="alice", password="s3cret-pass-phrase", user="mallory") as c:
        c.list_projects()
    assert "x-forwarded-user" not in fake.requests[-1].headers


@pytest.mark.req("AUTH-15")
def test_token_is_fetched_once_for_many_requests() -> None:
    fake = Fake("local", accepted=set())
    with fake.client(username="alice", password="s3cret-pass-phrase") as c:
        for _ in range(5):
            c.list_projects()
    assert fake.issued == 1
    assert fake.config_calls == 1


@pytest.mark.req("AUTH-15")
def test_expired_token_is_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(client_auth, "_monotonic", lambda: now[0])
    fake = Fake("local", expires_in=100, accepted=set())
    with fake.client(username="alice", password="s3cret-pass-phrase") as c:
        c.list_projects()
        now[0] += 50
        c.list_projects()
        assert fake.issued == 1
        now[0] += 50  # expires_in minus skew has passed
        c.list_projects()
    assert fake.issued == 2
    assert bearer(fake.requests[-1]) == "Bearer tok2"
    assert fake.config_calls == 1  # the mode is not looked up again


@pytest.mark.req("AUTH-15")
def test_401_fetches_a_new_token_and_retries_once() -> None:
    fake = Fake("local", accepted=set())
    with fake.client(username="alice", password="s3cret-pass-phrase") as c:
        c.list_projects()
        fake.accepted = {"tok2"}  # the server forgot tok1
        assert c.list_projects() == []
    assert fake.issued == 2
    assert bearer(fake.requests[-1]) == "Bearer tok2"


@pytest.mark.req("AUTH-15")
def test_401_after_refresh_raises_authentication_error() -> None:
    fake = Fake("local", accepted={"never"})
    fake.accept_issued = False
    with (
        fake.client(username="alice", password="s3cret-pass-phrase") as c,
        pytest.raises(AuthenticationError) as info,
    ):
        c.list_projects()
    assert fake.issued == 2  # initial and one refresh, no more
    api_calls = [r for r in fake.requests if r.url.path.endswith("/projects")]
    assert len(api_calls) == 2
    assert info.value.status_code == 401
    assert "tok" not in str(info.value)


@pytest.mark.req("AUTH-15")
def test_concurrent_requests_share_one_token_fetch() -> None:
    fake = Fake("local", accepted=set())
    errors: list[BaseException] = []
    with fake.client(username="alice", password="s3cret-pass-phrase") as c:

        def work() -> None:
            try:
                c.list_projects()
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert not errors
    assert fake.issued == 1
