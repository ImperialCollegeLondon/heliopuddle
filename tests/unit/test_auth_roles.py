"""Role grammar and the authorization rule (AUTH-07, AUTH-08)."""

from __future__ import annotations

import pytest

from heliopuddle.server.auth.roles import (
    allowed,
    has_any_role_on,
    is_sensitive,
    parse_role,
    valid_roles,
    validate_roles,
)

STORAGE_ROLE = "hp-client-storage-read-write"


@pytest.mark.req("AUTH-08")
@pytest.mark.parametrize(
    ("role", "parsed"),
    [
        ("hp-admin", ("hp-admin", None)),
        ("hp-reader", ("hp-reader", None)),
        ("hp-writer:demo", ("hp-writer", "demo")),
        (STORAGE_ROLE, (STORAGE_ROLE, None)),
        (f"{STORAGE_ROLE}:demo", (STORAGE_ROLE, "demo")),
        (f"{STORAGE_ROLE}:", None),
        (f"{STORAGE_ROLE}:a b", None),
        ("hp-operator:a:b", ("hp-operator", "a:b")),
        ("hp-admin:demo", None),
        ("hp-reader:", None),
        ("hp-reader: x", None),
        ("hp-reader:a b", None),
        ("hp-owner", None),
        ("offline_access", None),
        ("", None),
    ],
)
def test_parse_role(role: str, parsed: tuple[str, str | None] | None) -> None:
    assert parse_role(role) == parsed


@pytest.mark.req("AUTH-08")
def test_validate_roles_dedupes_and_names_every_bad_role() -> None:
    assert validate_roles(
        ["hp-reader", "hp-reader", "hp-writer:demo", STORAGE_ROLE, f"{STORAGE_ROLE}:demo"]
    ) == [
        "hp-reader",
        "hp-writer:demo",
        STORAGE_ROLE,
        f"{STORAGE_ROLE}:demo",
    ]
    with pytest.raises(ValueError, match=r"'nope'.*'hp-admin:x'"):
        validate_roles(["hp-reader", "nope", "hp-admin:x"])


@pytest.mark.req("AUTH-09")
def test_valid_roles_drops_foreign_roles() -> None:
    assert valid_roles(
        ["offline_access", "hp-reader", "uma_authorization", "hp-reader", STORAGE_ROLE]
    ) == ["hp-reader", STORAGE_ROLE]


@pytest.mark.req("AUTH-07")
@pytest.mark.parametrize(
    ("roles", "needed", "project", "expected"),
    [
        (["hp-admin"], "hp-deleter", "demo", True),
        (["hp-admin"], "hp-admin", None, True),
        (["hp-reader"], "hp-reader", "demo", True),
        (["hp-reader"], "hp-writer", "demo", False),
        (["hp-writer"], "hp-writer", "demo", True),
        (["hp-writer"], "hp-reader", "demo", True),  # writer includes read
        (["hp-deleter"], "hp-reader", "demo", True),
        (["hp-operator"], "hp-reader", "demo", True),
        (["hp-admin"], STORAGE_ROLE, "demo", True),
        ([STORAGE_ROLE], STORAGE_ROLE, "demo", True),
        ([STORAGE_ROLE], "hp-reader", "demo", True),
        ([STORAGE_ROLE], "hp-writer", "demo", False),
        ([STORAGE_ROLE], "hp-deleter", "demo", False),
        ([STORAGE_ROLE], "hp-operator", "demo", False),
        ([STORAGE_ROLE], "hp-admin", None, False),
        (["hp-writer"], STORAGE_ROLE, "demo", False),
        (["hp-writer"], "hp-deleter", "demo", False),
        (["hp-writer"], "hp-admin", None, False),
        (["hp-reader", "hp-writer"], "hp-writer", "other", True),  # global
        ([], "hp-reader", "demo", False),
        (["nonsense"], "hp-reader", "demo", False),
    ],
)
def test_allowed_global(roles: list[str], needed: str, project: str | None, expected: bool) -> None:
    assert allowed(roles, needed, project) is expected


@pytest.mark.req("AUTH-08")
@pytest.mark.parametrize(
    ("roles", "needed", "project", "expected"),
    [
        (["hp-writer:demo"], "hp-writer", "demo", True),
        (["hp-writer:demo"], "hp-writer", "other", False),
        (["hp-writer:demo"], "hp-reader", "demo", True),
        (["hp-writer:demo"], "hp-reader", "other", False),
        (["hp-reader:a", "hp-writer:b"], "hp-writer", "a", False),
        (["hp-reader:a", "hp-writer:b"], "hp-writer", "b", True),
        (["hp-reader:a", "hp-writer:b"], "hp-reader", "b", True),
        (["hp-writer:demo"], "hp-writer", None, False),  # scoped roles never grant global
        (["hp-writer:demo"], "hp-admin", None, False),
        (["hp-reader:demo", "hp-writer"], "hp-writer", "other", True),
        ([f"{STORAGE_ROLE}:demo"], STORAGE_ROLE, "demo", True),
        ([f"{STORAGE_ROLE}:demo"], STORAGE_ROLE, "other", False),
        ([f"{STORAGE_ROLE}:demo"], STORAGE_ROLE, None, False),
        ([f"{STORAGE_ROLE}:demo"], "hp-reader", "demo", True),
        ([f"{STORAGE_ROLE}:demo"], "hp-reader", "other", False),
        ([f"{STORAGE_ROLE}:demo"], "hp-writer", "demo", False),
        ([f"{STORAGE_ROLE}:demo"], "hp-deleter", "demo", False),
        ([f"{STORAGE_ROLE}:demo"], "hp-operator", "demo", False),
        ([f"{STORAGE_ROLE}:demo"], "hp-admin", None, False),
    ],
)
def test_allowed_scoped(roles: list[str], needed: str, project: str | None, expected: bool) -> None:
    assert allowed(roles, needed, project) is expected


@pytest.mark.req("AUTH-08")
def test_has_any_role_on() -> None:
    assert has_any_role_on(["hp-operator:demo"], "demo")
    assert not has_any_role_on(["hp-operator:demo"], "other")
    assert has_any_role_on(["hp-reader"], "anything")
    assert has_any_role_on(["hp-admin"], "anything")
    assert has_any_role_on([STORAGE_ROLE], "anything")
    assert has_any_role_on([f"{STORAGE_ROLE}:demo"], "demo")
    assert not has_any_role_on([f"{STORAGE_ROLE}:demo"], "other")
    assert not has_any_role_on([], "demo")


@pytest.mark.req("AUTH-10")
def test_sensitive_roles() -> None:
    assert is_sensitive("hp-deleter") and is_sensitive("hp-admin")
    assert is_sensitive(STORAGE_ROLE)
    assert not is_sensitive("hp-writer") and not is_sensitive("hp-reader")
