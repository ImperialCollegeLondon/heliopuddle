"""Auth API types, shared by the server and :mod:`heliopuddle.client` (M5A)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from heliopuddle.server.auth.roles import allowed, validate_roles
from heliopuddle.settings import MIN_PASSWORD_LENGTH, USERNAME_PATTERN

AuthModeName = Literal["local", "oidc", "header"]


class TokenResponse(BaseModel):
    """``POST /auth/token`` result (OAuth2 access token response, AUTH-04)."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class OAuthErrorResponse(BaseModel):
    """OAuth2 error body: ``invalid_grant``, ``unsupported_grant_type``, ..."""

    error: str
    error_description: str | None = None


class AuthConfig(BaseModel):
    """``GET /auth/config`` (public): how clients should authenticate (AUTH-11)."""

    mode: AuthModeName
    token_url: str | None = None
    authorization_url: str | None = None


class Me(BaseModel):
    """``GET /auth/me``: the caller (AUTH-11)."""

    username: str | None
    roles: list[str]
    mode: AuthModeName

    def can(self, role: str, project: str | None = None) -> bool:
        """Whether the roles grant ``role`` (on ``project``), by the server's own rule."""
        return allowed(self.roles, role, project)


class Forbidden(BaseModel):
    """The ``403`` body: what was missing (AUTH-07)."""

    detail: str
    required_roles: list[str]


def _check_password(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return value


class UserRead(BaseModel):
    """A local user; never includes the password hash (AUTH-13)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    roles: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    """``POST /users`` body."""

    username: str = Field(min_length=1, max_length=255, pattern=USERNAME_PATTERN)
    password: str
    roles: list[str] = Field(default_factory=list)
    is_active: bool = True

    _pw = field_validator("password")(_check_password)
    _roles = field_validator("roles")(validate_roles)


class UserUpdate(BaseModel):
    """``PATCH /users/{username}`` body; omitted fields are unchanged."""

    password: str | None = None
    roles: list[str] | None = None
    is_active: bool | None = None

    @field_validator("password")
    @classmethod
    def _pw(cls, value: str | None) -> str | None:
        return None if value is None else _check_password(value)

    @field_validator("roles")
    @classmethod
    def _roles(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else validate_roles(value)


class PasswordChange(BaseModel):
    """``POST /auth/me/password`` body."""

    current_password: str
    new_password: str

    _pw = field_validator("new_password")(_check_password)
