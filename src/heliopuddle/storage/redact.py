"""Hide credentials in storage URLs before they reach an error message or a log (STO-16).

Credentials belong in a storage location's ``storage_options`` (``${ENV}``
references), not in its URL, but a URL such as ``scheme://user:secret@host/path``
is valid and may be configured. Every place that formats a storage URL into an
exception message or log line uses :func:`redact_url`, and text that may quote
one (a backend's exception message) goes through :func:`redact_text`.

Values resolved from Prefect blocks (STO-18) are registered with
:func:`register_secret`; both functions then hide every occurrence of them.
"""

from __future__ import annotations

import re
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = ["REDACTED", "redact_text", "redact_url", "register_secret"]

REDACTED = "***"

_SECRET_QUERY_KEY = re.compile(r"sig|token|secret|key|password|passwd|credential", re.IGNORECASE)
_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^\s/:@]*):[^\s/@]*@")

#: Shorter values are not registered: hiding them would mangle ordinary text.
MIN_SECRET_LENGTH = 4
_MAX_SECRETS = 1024
_secrets: dict[str, None] = {}  # insertion-ordered set; the oldest are dropped first
_secrets_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Hide ``value`` wherever :func:`redact_text` or :func:`redact_url` formats it.

    Values shorter than :data:`MIN_SECRET_LENGTH` are ignored. At most 1024
    values are kept (the oldest are forgotten first).
    """
    if len(value) < MIN_SECRET_LENGTH:
        return
    with _secrets_lock:
        _secrets.pop(value, None)
        _secrets[value] = None
        while len(_secrets) > _MAX_SECRETS:
            del _secrets[next(iter(_secrets))]


def _hide_secrets(text: str) -> str:
    with _secrets_lock:
        values = sorted(_secrets, key=len, reverse=True)
    for value in values:
        if value in text:
            text = text.replace(value, REDACTED)
    return text


def redact_url(url: str) -> str:
    """Return ``url`` with the password in its user info and secret query values hidden.

    ``scheme://user:secret@host/path`` becomes ``scheme://user:***@host/path``. A
    query value whose key looks like a secret (``sig``, ``token``, ``key``, ...,
    as in a signed URL) becomes ``***``. A URL with neither is returned as given.

    Args:
        url: a storage URL, possibly with credentials.
    """
    url = _hide_secrets(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, host = netloc.rpartition("@")
        user, colon, _password = userinfo.partition(":")
        if colon:
            netloc = f"{user}:{REDACTED}@{host}"
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(_SECRET_QUERY_KEY.search(key) and value for key, value in pairs):
            query = urlencode(
                [(k, REDACTED if _SECRET_QUERY_KEY.search(k) and v else v) for k, v in pairs],
                safe="*",
            )
    if netloc == parts.netloc and query == parts.query:
        return url
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_text(text: str) -> str:
    """Hide registered secrets and the password of every ``scheme://user:password@host`` URL."""
    text = _hide_secrets(text)
    return _USERINFO.sub(lambda m: f"{m['scheme']}{m['user']}:{REDACTED}@", text)
