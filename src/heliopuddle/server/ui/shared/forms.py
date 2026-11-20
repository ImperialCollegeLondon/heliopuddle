"""Form posts: parsing bodies, the same-origin (CSRF) check and the Files return query.

Actions are plain HTML form posts (UI-04/UI-07/UI-08). Cross-site posts are
rejected: see :func:`check_same_origin`.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Depends, Request

from heliopuddle.server.ui.shared.errors import BadRequestError, CrossSiteError

TRUE_VALUES = frozenset({"1", "on", "true", "yes"})
RUN_MODES: tuple[Literal["full", "incremental"], ...] = ("full", "incremental")
"""The modes of an index or rule run."""
_FALSE_VALUES = frozenset({"0", "off", "false", "no", ""})
_RETURN_KEYS = frozenset(
    {
        "view",
        "path",
        "state",
        "storage_location",
        "content_from",
        "content_to",
        "latest_only",
        "include_deleted",
        "columns",
        "limit",
        "offset",
        "file",
    }
)


def _hosts(request: Request) -> set[str]:
    hosts = {request.headers.get("host", "").lower()}
    forwarded = request.headers.get("x-forwarded-host", "")
    hosts.update(h.strip().lower() for h in forwarded.split(",") if h.strip())
    hosts.discard("")
    return hosts


def check_same_origin(request: Request) -> None:
    """Reject a form post that a browser sent from another site (basic CSRF safety).

    In ``header`` mode the user is identified by a header set by an
    authenticating reverse proxy, which a browser sends on any request; in
    ``local`` and ``oidc`` mode the session cookie is ``SameSite=Lax``. Either way
    every state-changing UI route is a ``POST`` and must come from the UI's
    own pages. The check, in order:

    1. ``Sec-Fetch-Site: cross-site`` is rejected.
    2. If ``Origin`` is present, its host (with port) must equal the request's
       ``Host`` or one of the ``X-Forwarded-Host`` values; ``Origin: null`` is
       rejected.
    3. Otherwise, if ``Referer`` is present, the same applies to its host.

    A request with neither header (not a browser, or a browser that strips
    both) is allowed.

    Args:
        request: the incoming request.

    Raises:
        CrossSiteError: the post came from another site.
    """
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise CrossSiteError("cross-site request")
    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None:
        return
    host = urlsplit(source).netloc.lower() if source != "null" else ""
    if host not in _hosts(request):
        raise CrossSiteError(f"request from {source!r}")


async def form_data(request: Request) -> dict[str, str]:
    """Parse an ``application/x-www-form-urlencoded`` body (first value of each field).

    Args:
        request: the incoming request.

    Returns:
        Field names mapped to values.
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


async def form_lists(request: Request) -> dict[str, list[str]]:
    """Parse an urlencoded body keeping every value of repeated fields (multi-selects).

    Args:
        request: the incoming request.

    Returns:
        Field names mapped to all their values.
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    return parse_qs(body, keep_blank_values=True)


FormData = Depends(form_data)
FormLists = Depends(form_lists)
SameOrigin = Depends(check_same_origin)


def form_bool(value: str, field: str) -> bool:
    """Read a checkbox or select value as a bool.

    Args:
        value: the submitted text (``1``, ``on``, ``true``, ``yes`` and the false forms).
        field: the field's name, for the error message.

    Returns:
        The boolean.

    Raises:
        BadRequestError: the value is neither a true nor a false form.
    """
    lowered = value.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise BadRequestError(f"Invalid value for {field}: {value!r}.")


def return_query(raw: str, drop: str = "") -> str:
    """Keep only the Files page's own parameters from an untrusted query string."""
    pairs = [
        (k, v)
        for k, values in parse_qs(raw, keep_blank_values=False).items()
        if k in _RETURN_KEYS and k != drop
        for v in values
    ]
    return urlencode(pairs)
