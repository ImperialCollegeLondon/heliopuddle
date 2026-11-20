"""Password hashing with argon2 (AUTH-02, AUTH-04)."""

from __future__ import annotations

from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

_hasher = PasswordHash((Argon2Hasher(),))


def hash_password(password: str) -> str:
    """Return the argon2id hash of ``password`` (salted; never the password itself)."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Whether ``password`` matches ``password_hash``; False for a malformed hash."""
    try:
        return _hasher.verify(password, password_hash)
    except Exception:
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash("heliopuddle-dummy-password")


def verify_dummy(password: str) -> None:
    """Spend the cost of a real verification against a throwaway hash.

    Used when the username is unknown, so that an unknown user and a wrong
    password take the same time (AUTH-04).
    """
    verify_password(password, _dummy_hash())
