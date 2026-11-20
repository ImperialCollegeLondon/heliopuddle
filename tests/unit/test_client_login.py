"""``HelioPuddleClient.login()``: the token the UI keeps in its session (AUTH-16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from heliopuddle.client import AuthenticationError, LoginResult
from tests.unit._client_auth_helpers import Fake

pytestmark = pytest.mark.req("AUTH-16")


def test_login_fetches_a_token_and_reports_its_expiry() -> None:
    fake = Fake(expires_in=600)
    with fake.client(username="ann", password="pw-long-enough") as client:
        result = client.login()
    assert isinstance(result, LoginResult)
    assert result.access_token == "tok1"
    assert result.expires_at is not None
    left = result.expires_at - datetime.now(UTC)
    assert timedelta(seconds=590) < left <= timedelta(seconds=600)


def test_login_reuses_the_cached_token() -> None:
    fake = Fake()
    with fake.client(username="ann", password="pw-long-enough") as client:
        first = client.login()
        assert client.login() == first
        client.list_projects()  # uses the same token
    assert fake.issued == 1


def test_login_returns_a_supplied_token_as_is() -> None:
    fake = Fake()
    with fake.client(token="given") as client:
        result = client.login()
    assert result.access_token == "given"
    assert result.expires_at is None
    assert not fake.token_requests


def test_login_refused_credentials_raise_authentication_error() -> None:
    fake = Fake()
    fake.token_error = (400, {"error": "invalid_grant", "error_description": "bad"})
    with (
        fake.client(username="ann", password="wrong-password") as client,
        pytest.raises(AuthenticationError) as info,
    ):
        client.login()
    assert "wrong-password" not in str(info.value)


def test_login_in_header_mode_or_without_credentials_is_an_error() -> None:
    with (
        Fake(mode="header").client(username="ann", password="pw-long-enough") as client,
        pytest.raises(AuthenticationError, match="header mode"),
    ):
        client.login()
    with Fake().client() as client, pytest.raises(AuthenticationError, match="no credentials"):
        client.login()


def test_login_result_keeps_the_token_out_of_repr() -> None:
    result = LoginResult("secret-token-value", datetime(2026, 1, 1, tzinfo=UTC))
    assert "secret-token-value" not in repr(result)
    fake = Fake()
    with fake.client(token="secret-token-value") as client:
        assert "secret-token-value" not in repr(client)
