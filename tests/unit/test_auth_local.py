"""Local tokens and per-request verification (AUTH-04, AUTH-05, AUTH-06)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest

from heliopuddle.server.auth import passwords
from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.local import authenticate
from heliopuddle.server.auth.roles import ROLE_ADMIN, ROLE_READER, ROLE_WRITER
from heliopuddle.server.auth.secret import get_secret
from heliopuddle.server.auth.tokens import ISSUER, verify_token
from heliopuddle.server.context import AppContext
from heliopuddle.server.models import users as users_db
from tests.local_auth import add_user, issue_token, local_context

TTL = 3600


@pytest.fixture
def lctx(tmp_path: Path, config_dir: Path, migrated_db: Path) -> Iterator[AppContext]:
    ctx = local_context(tmp_path, config_dir, migrated_db)
    yield ctx
    ctx.dispose()


def issue(ctx: AppContext, username: str, roles: list[str], **kw: Any) -> str:
    return issue_token(ctx, username, roles, TTL, **kw)


def reject(ctx: AppContext, token: str) -> None:
    with pytest.raises(AuthError) as e:
        authenticate(token, ctx)
    assert e.value.status_code == 401


# --- passwords -----------------------------------------------------------------


@pytest.mark.req("AUTH-04")
def test_passwords_are_argon2_hashed_salted_and_verified() -> None:
    h1, h2 = passwords.hash_password("s3cret-password"), passwords.hash_password("s3cret-password")
    assert h1.startswith("$argon2id$")
    assert h1 != h2
    assert "s3cret" not in h1
    assert passwords.verify_password("s3cret-password", h1)
    assert not passwords.verify_password("s3cret-passwor", h1)
    assert not passwords.verify_password("anything", "not-a-hash")


# --- token format ----------------------------------------------------------------


@pytest.mark.req("AUTH-05")
def test_token_has_the_documented_claims(lctx: AppContext) -> None:
    before = int(time.time())
    token = issue(lctx, "ann", [ROLE_READER])
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == ISSUER == "heliopuddle"
    assert claims["sub"] == "ann"
    assert claims["realm_access"] == {"roles": [ROLE_READER]}
    assert before <= claims["iat"] <= int(time.time())
    assert claims["exp"] == claims["iat"] + TTL
    assert (
        claims["jti"]
        and claims["jti"]
        != jwt.decode(issue(lctx, "ann", []), options={"verify_signature": False})["jti"]
    )
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    parsed = verify_token(get_secret(lctx.settings), token)
    assert (parsed.username, parsed.roles) == ("ann", (ROLE_READER,))


@pytest.mark.req("AUTH-05")
@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
def test_token_rejections(lctx: AppContext) -> None:
    add_user(lctx, "ann", [ROLE_READER])
    secret = get_secret(lctx.settings)
    now = int(time.time())
    with lctx.sessionmaker() as session:
        ann = users_db.get_user_by_username(session, "ann")
        assert ann is not None
        uid, cv = ann.id, users_db.credentials_version(ann)
    good = {
        "iss": ISSUER,
        "sub": "ann",
        "iat": now,
        "exp": now + 60,
        "jti": "x",
        "uid": uid,
        "cv": cv,
        "realm_access": {"roles": []},
    }
    assert authenticate(jwt.encode(good, secret, algorithm="HS256"), lctx).username == "ann"

    cases = {
        "alg none": jwt.encode(good, "", algorithm="none"),
        "other algorithm": jwt.encode(good, secret, algorithm="HS512"),
        "wrong key": jwt.encode(good, b"another-key-another-key-another-k", algorithm="HS256"),
        "wrong issuer": jwt.encode({**good, "iss": "someone"}, secret, algorithm="HS256"),
        "no issuer": jwt.encode(
            {k: v for k, v in good.items() if k != "iss"}, secret, algorithm="HS256"
        ),
        "expired": jwt.encode({**good, "exp": now - 10}, secret, algorithm="HS256"),
        "no expiry": jwt.encode(
            {k: v for k, v in good.items() if k != "exp"}, secret, algorithm="HS256"
        ),
        "no subject": jwt.encode(
            {k: v for k, v in good.items() if k != "sub"}, secret, algorithm="HS256"
        ),
        "garbage": "not.a.token",
        "empty": "x",
    }
    tampered = jwt.encode(good, secret, algorithm="HS256")
    head, body, sig = tampered.split(".")
    forged = jwt.encode({**good, "sub": "root"}, secret, algorithm="HS256").split(".")[1]
    cases["tampered payload"] = f"{head}.{forged}.{sig}"
    cases["tampered signature"] = f"{head}.{body}.{sig[:-3]}abc"
    for name, token in cases.items():
        with pytest.raises(AuthError) as e:
            authenticate(token, lctx)
        assert e.value.status_code == 401, name


@pytest.mark.req("AUTH-05")
def test_expired_token_says_so_without_leaking_anything(lctx: AppContext) -> None:
    add_user(lctx, "ann")
    token = issue_token(lctx, "ann", [], 10, now=time.time() - 100)
    with pytest.raises(AuthError, match="expired"):
        authenticate(token, lctx)


# --- per-request user re-read ------------------------------------------------------


@pytest.mark.req("AUTH-05")
def test_roles_come_from_the_database_and_changes_apply_at_once(lctx: AppContext) -> None:
    add_user(lctx, "Ann", [ROLE_READER, "junk-role"])
    token = issue(lctx, "ann", [ROLE_ADMIN])  # the token's own roles claim is not trusted
    principal = authenticate(token, lctx)
    assert principal.username == "Ann"  # as stored
    assert principal.roles == (ROLE_READER,)  # invalid role strings dropped
    assert principal.mode == "local"
    assert principal.can(ROLE_READER) and not principal.can(ROLE_WRITER)

    with lctx.write_session() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        users_db.set_roles(session, user, [ROLE_WRITER, f"{ROLE_READER}:demo"])
    again = authenticate(token, lctx)
    assert again.roles == (ROLE_WRITER, f"{ROLE_READER}:demo")
    assert again.can(ROLE_WRITER)


@pytest.mark.req("AUTH-05")
def test_disabled_user_is_rejected_and_re_enabled_user_accepted(lctx: AppContext) -> None:
    add_user(lctx, "ann")
    token = issue(lctx, "ann", [])
    authenticate(token, lctx)
    with lctx.write_session() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        users_db.set_active(session, user, False)
    reject(lctx, token)
    with lctx.write_session() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        users_db.set_active(session, user, True)
    authenticate(token, lctx)


@pytest.mark.req("AUTH-05")
def test_deleted_or_unknown_user_is_rejected(lctx: AppContext) -> None:
    reject(lctx, issue(lctx, "ghost", [ROLE_ADMIN]))
    add_user(lctx, "ann")
    token = issue(lctx, "ann", [])
    with lctx.write_session() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        session.delete(user)
    reject(lctx, token)


@pytest.mark.req("AUTH-05")
def test_password_change_invalidates_older_tokens_but_not_newer(lctx: AppContext) -> None:
    add_user(lctx, "ann")
    old = issue(lctx, "ann", [], now=time.time() - 30)
    with lctx.write_session() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        users_db.set_password_hash(session, user, passwords.hash_password("a new password!"))
    reject(lctx, old)
    authenticate(issue(lctx, "ann", []), lctx)  # logged in after the change


@pytest.mark.req("AUTH-05")
def test_tokens_are_bound_to_the_credentials_version_not_to_the_clock(lctx: AppContext) -> None:
    add_user(lctx, "ann")
    # Many change/issue pairs back to back, inside the same second: a token issued before a
    # change is always rejected, one issued after it always accepted (no `iat` rounding).
    for i in range(15):
        before = issue(lctx, "ann", [])
        authenticate(before, lctx)
        with lctx.write_session() as session:
            user = users_db.get_user_by_username(session, "ann")
            assert user is not None
            users_db.set_password_hash(session, user, passwords.hash_password(f"new password {i}"))
        reject(lctx, before)
        authenticate(issue(lctx, "ann", []), lctx)


@pytest.mark.req("AUTH-05")
def test_credentials_version_is_exact_microseconds(lctx: AppContext) -> None:
    add_user(lctx, "ann")
    with lctx.sessionmaker() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        version = users_db.credentials_version(user)
        assert version > 1_700_000_000_000_000  # microseconds since the epoch, not seconds
    # A neighbouring version (one microsecond off) is not accepted.
    reject(lctx, issue(lctx, "ann", [], version=version + 1))
    reject(lctx, issue(lctx, "ann", [], version=version - 1))
    authenticate(issue(lctx, "ann", [], version=version), lctx)


@pytest.mark.req("AUTH-05")
def test_a_token_bound_to_another_user_id_or_without_the_binding_is_rejected(
    lctx: AppContext,
) -> None:
    add_user(lctx, "ann")
    add_user(lctx, "bob")
    with lctx.sessionmaker() as session:
        ann = users_db.get_user_by_username(session, "ann")
        bob = users_db.get_user_by_username(session, "bob")
        assert ann is not None and bob is not None
        bob_id = bob.id
    # sub says ann, but the id and version are bob's: the id decides, and it must match.
    reject(lctx, issue(lctx, "ann", [], user_id=bob_id))
    # A token signed with the right key but without uid/cv (an older format) is invalid.
    legacy = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "ann",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "jti": "x",
            "realm_access": {"roles": [ROLE_ADMIN]},
        },
        get_secret(lctx.settings),
        algorithm="HS256",
    )
    reject(lctx, legacy)
    # Non-integer or boolean bindings are invalid, not coerced.
    for bad in ("1", True, 1.5, None):
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "sub": "ann",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "jti": "x",
                "uid": bad,
                "cv": bad,
            },
            get_secret(lctx.settings),
            algorithm="HS256",
        )
        reject(lctx, forged)


@pytest.mark.req("AUTH-05")
def test_a_recreated_user_does_not_inherit_an_old_users_tokens(lctx: AppContext) -> None:
    add_user(lctx, "ann")
    old = issue(lctx, "ann", [], now=time.time() - 30)
    with lctx.write_session() as session:
        user = users_db.get_user_by_username(session, "ann")
        assert user is not None
        session.delete(user)
    add_user(lctx, "ann")
    reject(lctx, old)


# --- timing parity (AUTH-04) ----------------------------------------------------------


@pytest.mark.req("AUTH-04")
def test_unknown_user_costs_a_dummy_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = passwords.verify_password
    monkeypatch.setattr(passwords, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    passwords.verify_dummy("whatever")
    assert len(calls) == 1
    assert calls[0].startswith("$argon2id$")  # a real hash, so a real verification


@pytest.mark.req("AUTH-04")
def test_dummy_verification_takes_as_long_as_a_wrong_password() -> None:
    real_hash = passwords.hash_password("the real password")
    passwords.verify_dummy("warm up")  # builds the cached dummy hash

    def mean(fn: object, runs: int = 4) -> float:
        start = time.perf_counter()
        for _ in range(runs):
            fn()  # type: ignore[operator]
        return (time.perf_counter() - start) / runs

    wrong = mean(lambda: passwords.verify_password("not the password", real_hash))
    unknown = mean(lambda: passwords.verify_dummy("not the password"))
    assert 1 / 3 < unknown / wrong < 3
