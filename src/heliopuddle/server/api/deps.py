"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from heliopuddle.config import ProjectConfig
from heliopuddle.server.auth.errors import AuthError
from heliopuddle.server.auth.principal import Principal, header_principal
from heliopuddle.server.auth.roles import ROLE_ADMIN, is_sensitive
from heliopuddle.server.context import AppContext, get_context
from heliopuddle.server.models.files import project_name_of_file, project_name_of_series
from heliopuddle.server.models.uploads import project_name_of_upload
from heliopuddle.server.schemas.auth import Forbidden


def get_ctx(request: Request) -> AppContext:
    """Return the app's context, or the process context when none was given."""
    ctx: AppContext | None = getattr(request.app.state, "ctx", None)
    return ctx if ctx is not None else get_context()


Ctx = Annotated[AppContext, Depends(get_ctx)]


def _unauthorized(error: AuthError) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
    return HTTPException(status_code=error.status_code, detail=error.detail, headers=headers)


def get_principal(request: Request, ctx: Ctx) -> Principal:
    """Authenticate the request (AUTH-01, AUTH-05, AUTH-09); cached per request.

    ``header`` mode trusts ``settings.user_header`` and rejects a request without
    it (AUTH-20). ``local`` and ``oidc`` modes
    require ``Authorization: Bearer <token>`` and ignore that header.

    Raises:
        HTTPException: 401 (``WWW-Authenticate: Bearer``) for missing or invalid
            credentials; 502 when the identity provider cannot be reached.
    """
    cached: Principal | None = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    mode = ctx.settings.auth_mode
    if mode == "header":
        username = (request.headers.get(ctx.settings.user_header) or "").strip()
        if not username:  # the trusted proxy always sets it: its absence means a bypass (AUTH-20)
            raise _unauthorized(AuthError("not authenticated"))
        principal = header_principal(username)
    else:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise _unauthorized(AuthError("not authenticated"))
        try:
            if mode == "local":
                from heliopuddle.server.auth import local

                principal = local.authenticate(token, ctx)
            else:
                if ctx.oidc is None:
                    raise AuthError("the OIDC provider is not initialised", 502)
                principal = ctx.oidc.authenticate(token)
        except AuthError as error:
            raise _unauthorized(error) from None
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def get_user(principal: CurrentPrincipal) -> str | None:
    """Return the authenticated username (``header`` mode: the header value)."""
    return principal.username


User = Annotated[str | None, Depends(get_user)]


def require_project(ctx: AppContext, project: str) -> ProjectConfig:
    """Return the project's config.

    Raises:
        HTTPException: 404 if the project is not configured.
    """
    cfg = ctx.projects.get(project)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"project {project!r} not found")
    return cfg


ProjectResolver = Callable[[Request, AppContext], str | None]
"""Maps a request to the name of the project its row belongs to (None: no such row)."""


class InsufficientRole(Exception):
    """The caller has some role on the project, but not the needed one: HTTP 403 (AUTH-07).

    Args:
        needed: the role the route needs, or None for "any role".
        project: the project the route is about, or None for a project-agnostic route.
    """

    def __init__(self, needed: str | None, project: str | None) -> None:
        roles = [ROLE_ADMIN] if needed in (None, ROLE_ADMIN) else [needed]
        if needed not in (None, ROLE_ADMIN):
            if project is not None:
                roles.append(f"{needed}:{project}")
            roles.append(ROLE_ADMIN)
        self.body = Forbidden(
            detail="insufficient role: one of " + ", ".join(roles) + " is required",
            required_roles=roles,
        )
        super().__init__(self.body.detail)


def _forbidden_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, InsufficientRole)
    return JSONResponse(status_code=403, content=exc.body.model_dump())


def install_auth_handlers(app: FastAPI) -> None:
    """Make :class:`InsufficientRole` answer ``403 {detail, required_roles}``."""
    app.add_exception_handler(InsufficientRole, _forbidden_handler)


def _hidden(what: str = "not found") -> HTTPException:
    return HTTPException(status_code=404, detail=what)


def _introspect(
    request: Request, ctx: AppContext, principal: Principal, needed: str | None
) -> None:
    """Ask the identity provider whether the token is still active, when configured (AUTH-10)."""
    settings = ctx.settings
    if settings.auth_mode != "oidc" or settings.oidc_introspect == "off":
        return
    if settings.oidc_introspect == "sensitive" and (needed is None or not is_sensitive(needed)):
        return
    if getattr(request.state, "introspected", False):
        return
    try:
        if principal.token is None:
            raise AuthError("not authenticated")
        if ctx.oidc is None:
            raise AuthError("the OIDC provider is not initialised", 502)
        ctx.oidc.introspect(principal.token)
    except AuthError as error:
        raise _unauthorized(error) from None
    request.state.introspected = True


def requires(
    role: str | None, *, project_of: ProjectResolver | None = None
) -> Callable[..., Principal]:
    """Build the dependency that authenticates a request and authorizes it for ``role``.

    The project the route is about is ``project_of(request, ctx)`` when given (for
    routes addressed by id), else the ``project`` path parameter, else there is none
    and only global roles count. Every route needs one of these, or an explicit
    entry in the route-table test's public list (AUTH-07).

    A project that is not in the config (unknown, or removed with its rows left in the
    DB) is reachable only with a global role: scoped roles grant nothing on it.

    Outcomes: no or invalid credentials ``401``; no role at all on the project
    ``404`` (existence hidden, AUTH-08); some role but not ``role`` ``403``
    with ``required_roles``; a project-agnostic route without the role ``403``.
    When ``project_of`` finds no row, the caller needs ``role`` globally to reach the
    route's own ``404``, and gets ``404`` otherwise, so a missing id and a forbidden
    one look alike to an unprivileged caller.

    Args:
        role: a role from ``auth.roles.ALL_ROLES``;
            None means "any valid principal" (the route filters its own output).
        project_of: resolves the owning project's name from the row a route is addressed by.

    Returns:
        A sync FastAPI dependency returning the :class:`Principal`.
    """

    def dependency(
        request: Request, ctx: Ctx, principal: Annotated[Principal, Depends(get_principal)]
    ) -> Principal:
        _introspect(request, ctx, principal, role)
        if role is None:
            return principal
        if project_of is not None:
            project = project_of(request, ctx)
            if project is None:
                if principal.can(role):
                    return principal  # the route answers its own 404
                raise _hidden()
        else:
            project = request.path_params.get("project")
        if project is None:
            if principal.can(role):
                return principal
            raise InsufficientRole(role, None)
        if project not in ctx.projects:
            # A project removed from the config keeps its DB rows, but scoped roles grant
            # nothing on it (AUTH-08): only a global role gets through (to the route's 404
            # or its history); everyone else sees "not found".
            if principal.can(role):
                return principal
            raise _hidden()
        if principal.can(role, project):
            return principal
        if principal.has_any_role_on(project):
            raise InsufficientRole(role, project)
        raise _hidden()

    dependency.hp_requires = (role, project_of)  # type: ignore[attr-defined]
    return dependency


def _resolve_id(
    request: Request, ctx: AppContext, param: str, lookup: Callable[..., str | None]
) -> str | None:
    """Look up a project by an integer id path parameter (None if not an id, or unknown)."""
    try:
        key = int(request.path_params[param])
    except ValueError:
        return None
    if not 0 < key < 2**63:
        return None
    with ctx.sessionmaker() as session:
        return lookup(session, key)


def file_project(request: Request, ctx: AppContext) -> str | None:
    """The project of the ``file_id`` path parameter's file (None if there is none)."""
    return _resolve_id(request, ctx, "file_id", project_name_of_file)


def series_project(request: Request, ctx: AppContext) -> str | None:
    """The project of the ``series_id`` path parameter's series (None if there is none)."""
    return _resolve_id(request, ctx, "series_id", project_name_of_series)


def upload_project(request: Request, ctx: AppContext) -> str | None:
    """The project of the ``token`` path parameter's upload (None if there is none)."""
    with ctx.sessionmaker() as session:
        return project_name_of_upload(session, str(request.path_params["token"]))
