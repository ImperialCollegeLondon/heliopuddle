"""The state every page shares: settings, the client factory, templates and render helpers.

:func:`~heliopuddle.server.ui.app.create_ui` builds one :class:`UiContext` and
stores it on ``app.state.ui``. A route asks for it with the :data:`Ui`
dependency::

    @router.get("/projects/{project}/storage", name="ui_storage")
    def storage_page(request: Request, ui: Ui, project: str) -> Response:
        with ui.client_for(request, project) as client:
            ...
        return ui.render(request, "storage/storage.html", project=project)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from heliopuddle.client import HelioPuddleClient
from heliopuddle.server.schemas import ProjectSummary, RuleRead
from heliopuddle.server.ui.auth.session import LoginRequired, read_user
from heliopuddle.server.ui.shared.clients import ClientFactory
from heliopuddle.server.ui.shared.links import files_url
from heliopuddle.settings import Settings, prefect_ui_url

if TYPE_CHECKING:
    from heliopuddle.server.ui.auth.oidc import OidcProviderCache


def _allow(role: str) -> bool:
    return True


def _run_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


@dataclass
class UiContext:
    """Everything a page needs from the app, in one object.

    Attributes:
        settings: process settings.
        factory: builds the API client of a request.
        templates: the Jinja2 environment and response factory.
        root_path: the UI's root path (no trailing slash).
        login_mode: whether users log in (``local`` and ``oidc``), as opposed to
            ``header`` mode.
        oidc: the OIDC provider cache (``oidc`` mode only).
    """

    settings: Settings
    factory: ClientFactory
    templates: Jinja2Templates
    root_path: str
    login_mode: bool
    oidc: OidcProviderCache | None = None

    def can_for(self, request: Request, project: str | None) -> Callable[[str], bool]:
        """A ``can(role)`` check for the template: does the signed-in user hold ``role``?"""
        if not self.login_mode:  # in header mode the API decides alone, so every control is shown
            return _allow
        user = read_user(request)
        return lambda role: user is not None and user.can(role, project)

    def render(
        self, request: Request, name: str, status_code: int = 200, **context: Any
    ) -> Response:
        """Render a template with the shell's context (user, search, project switcher).

        Args:
            request: the incoming request.
            name: the template name, e.g. ``files/files.html``.
            status_code: the HTTP status.
            **context: the page's own template variables.

        Returns:
            The HTML response.
        """
        context.setdefault("prefect_url", prefect_ui_url(self.settings))
        user = read_user(request) if self.login_mode else None
        context["auth_user"] = user
        context["header_user"] = (
            None if self.login_mode else request.headers.get(self.settings.user_header) or None
        )
        context["can"] = self.can_for(request, context.get("project"))
        context["show_search"] = not self.login_mode or user is not None
        context.setdefault("project", None)
        context.setdefault("tab", None)
        context.setdefault("projects", [])
        context.setdefault("actions", None)
        context.setdefault("search_q", "")
        return self.templates.TemplateResponse(request, name, context, status_code=status_code)

    def error_page(self, request: Request, status_code: int, title: str, message: str) -> Response:
        """Render the error page."""
        return self.render(
            request, "error.html", status_code=status_code, title=title, message=message
        )

    def client_for(self, request: Request, project: str | None) -> HelioPuddleClient:
        """The API client for this request: the caller's user header, or their session token.

        Raises:
            LoginRequired: login mode and the session has no user.
        """
        if not self.login_mode:
            return self.factory(project, request.headers.get(self.settings.user_header))
        user = read_user(request)
        if user is None:  # the guard lets no such request through; a session expired mid-request
            raise LoginRequired("/")
        return self.factory(project, token=user.token)

    def run_notice(self, run_id: str | None, kind: str, **extra: Any) -> dict[str, Any] | None:
        """The "run submitted" notice shown after an action redirects back."""
        checked = _run_id(run_id)
        if checked is None:
            return None
        base = prefect_ui_url(self.settings)
        return {
            "kind": kind,
            "run_id": checked,
            "url": f"{base}/runs/flow-run/{checked}" if base else None,
            **extra,
        }

    @staticmethod
    def url(request: Request, name: str, /, **params: Any) -> str:
        """The root-relative path of a named UI route."""
        return request.url_for(name, **params).path

    def project_shell(
        self,
        request: Request,
        client: HelioPuddleClient,
        project: str,
        summary: ProjectSummary,
        rules: list[RuleRead] | None = None,
    ) -> dict[str, Any]:
        """The header of a project page: the switcher's projects and the Actions menu."""
        projects = [p.name for p in client.list_projects()]
        actions = None
        if self.can_for(request, project)("hp-operator"):
            if rules is None:
                rules = client.list_rules()
            actions = {
                "storage_locations": [s.name for s in summary.storage_locations if s.active],
                "rules": [r.name for r in rules],
            }
        return {"projects": projects, "actions": actions, "summary": summary}

    def files_link(self, request: Request, project: str) -> Callable[..., str]:
        """``link(view, storage_location, state)`` gives the Files URL of a project."""
        base = self.url(request, "ui_files", project=project)

        def link(view: str | None = None, storage_location: str = "", state: str = "") -> str:
            return files_url(
                base,
                [("view", view), ("storage_location", storage_location), ("state", state)],
            )

        return link


def get_ui(request: Request) -> UiContext:
    """FastAPI dependency: the app's :class:`UiContext`."""
    ui: UiContext = request.app.state.ui
    return ui


Ui = Annotated[UiContext, Depends(get_ui)]
