"""Resolve a storage location's credentials from Prefect blocks (STO-18, STO-19).

A storage location may name a Prefect ``RemoteFileSystem`` block (``block:``)
and may hold ``{{ prefect.blocks.secret.<name> }}`` placeholders in its
``url`` and ``storage_options``. They are resolved here, lazily, whenever a
storage location is opened for use, through a :class:`BlockResolver` that the
server registers (:func:`set_block_resolver`); this module never imports
Prefect. Block lookups are cached per block for :data:`CACHE_TTL` seconds, so
a block created or changed after startup is picked up without a restart.

A block that is missing (or a Prefect that cannot be reached) makes the
storage location **incomplete**: :func:`resolve` and :func:`open_location`
raise :class:`StorageLocationIncomplete`, and :func:`location_problems`
returns the problems. Other modules can add problems for a resolved storage
location with :func:`register_problem_check`.

Resolved secret values are registered with
:func:`heliopuddle.storage.redact.register_secret`, so they are hidden in
error messages and logs. See docs/design/storage.md ("Credentials").
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from urllib.parse import urlsplit, urlunsplit

from heliopuddle.config.placeholders import PLACEHOLDER, is_whole_placeholder
from heliopuddle.storage.backends import server_only
from heliopuddle.storage.location import StorageLocation
from heliopuddle.storage.redact import redact_text, register_secret

if TYPE_CHECKING:
    from heliopuddle.config import StorageLocationConfig

__all__ = [
    "CACHE_TTL",
    "BlockResolver",
    "StorageLocationIncomplete",
    "clear_cache",
    "location_problems",
    "open_location",
    "register_problem_check",
    "resolve",
    "root_identity",
    "set_block_resolver",
    "set_clock",
]

#: Seconds a block lookup (found, missing or failed) is cached.
CACHE_TTL = 60.0

ProblemCheck = Callable[[str, dict[str, Any]], list[str]]
_S = TypeVar("_S", bound=StorageLocation)


class StorageLocationIncomplete(Exception):
    """A storage location cannot be used until its blocks exist (STO-18).

    Attributes:
        problems: one message per problem, naming the project file, field and block.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class BlockResolver(Protocol):
    """Looks up Prefect blocks. Both methods raise ``LookupError`` for a missing
    block and any other exception when Prefect cannot be reached."""

    def secret(self, name: str) -> Any:
        """The value of the Secret block ``name`` (a string or JSON value)."""
        ...

    def remote_file_system(self, name: str) -> tuple[str, dict[str, Any]]:
        """The ``basepath`` and ``settings`` of the RemoteFileSystem block ``name``."""
        ...


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.resolver: BlockResolver | None = None
        self.clock: Callable[[], float] = time.monotonic
        # (kind, name) -> (expires_at, value or the exception raised)
        self.cache: dict[tuple[str, str], tuple[float, Any, BaseException | None]] = {}
        self.checks: list[ProblemCheck] = []


_state = _State()


def set_block_resolver(resolver: BlockResolver | None) -> None:
    """Install the process's block resolver (None removes it) and clear the cache."""
    with _state.lock:
        _state.resolver = resolver
        _state.cache.clear()


def get_block_resolver() -> BlockResolver | None:
    """The installed block resolver, or None."""
    return _state.resolver


def set_clock(clock: Callable[[], float] | None) -> None:
    """Use ``clock`` (seconds, monotonic) for cache expiry; None restores the real clock."""
    with _state.lock:
        _state.clock = clock or time.monotonic


def clear_cache() -> None:
    """Forget every cached block lookup."""
    with _state.lock:
        _state.cache.clear()


def register_problem_check(check: ProblemCheck) -> None:
    """Add a check run on every resolved ``(url, storage_options)``.

    A check returns problems (empty when the storage location is usable); a
    non-empty result makes the storage location incomplete. Registering the
    same function twice has no effect.
    """
    with _state.lock:
        if check not in _state.checks:
            _state.checks.append(check)


def unregister_problem_check(check: ProblemCheck) -> None:
    """Remove a check added with :func:`register_problem_check` (no error if absent)."""
    with _state.lock:
        if check in _state.checks:
            _state.checks.remove(check)


class _MissingBlock(Exception):
    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


def _lookup(kind: str, name: str, what: str, where: str) -> Any:
    """A cached block lookup; raises :class:`_MissingBlock` with a problem message."""
    key = (kind, name)
    with _state.lock:
        now = _state.clock()
        cached = _state.cache.get(key)
        resolver = _state.resolver
    if cached is not None and cached[0] > now:
        value, error = cached[1], cached[2]
    elif resolver is None:
        raise _MissingBlock(
            f"{where}: cannot load Prefect {what} block {name!r}: Prefect is not "
            "connected in this process"
        )
    else:
        value, error = None, None
        try:
            value = resolver.secret(name) if kind == "secret" else resolver.remote_file_system(name)
        except Exception as e:  # LookupError: missing; anything else: unreachable
            error = e
        with _state.lock:
            _state.cache[key] = (now + CACHE_TTL, value, error)
    if isinstance(error, LookupError):
        raise _MissingBlock(f"{where}: Prefect {what} block {name!r} not found")
    if error is not None:
        detail = redact_text(f"{type(error).__name__}: {error}")
        raise _MissingBlock(f"{where}: cannot load Prefect {what} block {name!r}: {detail}")
    return value


def _register_secrets(value: Any) -> None:
    if isinstance(value, str):
        register_secret(value)
    elif isinstance(value, dict):
        for item in value.values():
            _register_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _register_secrets(item)


def _secret(name: str, where: str) -> Any:
    value = _lookup("secret", name, "Secret", where)
    _register_secrets(value)
    return value


def _whole_value(secret: Any) -> Any:
    """A whole-value placeholder takes the secret's JSON object or array when it holds one."""
    if isinstance(secret, str):
        try:
            parsed = json.loads(secret)
        except ValueError:
            return secret
        # Only objects and arrays: a password such as "12345" stays a string.
        if not isinstance(parsed, dict | list):
            return secret
        _register_secrets(parsed)  # each credential inside is redacted on its own
        return parsed
    return secret


def _as_text(secret: Any) -> str:
    return secret if isinstance(secret, str) else json.dumps(secret)


def _expand(value: Any, where: str, problems: list[str]) -> Any:
    """Expand the placeholders in a nested value, collecting problems."""
    if isinstance(value, dict):
        return {k: _expand(v, f"{where}.{k}", problems) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, f"{where}.{i}", problems) for i, v in enumerate(value)]
    if not isinstance(value, str) or PLACEHOLDER.search(value) is None:
        return value
    try:
        if is_whole_placeholder(value):
            match = PLACEHOLDER.search(value)
            assert match is not None
            return _whole_value(_secret(match.group(1), where))
        return PLACEHOLDER.sub(lambda m: _as_text(_secret(m.group(1), where)), value)
    except _MissingBlock as e:
        problems.append(e.problem)
        return value


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """``over`` merged over ``base``; nested mappings are merged key by key."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _options(cfg: StorageLocationConfig, problems: list[str]) -> dict[str, Any]:
    """The inline ``storage_options`` as a mapping (a whole placeholder expanded)."""
    where = f"{cfg.origin}.storage_options"
    options = cfg.storage_options
    if isinstance(options, dict):
        return options
    expanded = _expand(options, where, problems)
    if isinstance(expanded, dict):
        return expanded
    if expanded is not options:  # resolved, but not to a mapping
        problems.append(f"{where}: the Prefect Secret block must hold a JSON object")
    return {}


def resolve(cfg: StorageLocationConfig) -> tuple[str, dict[str, Any]]:
    """Resolve a storage location's URL and storage options from its blocks.

    The RemoteFileSystem block (``block:``) gives the URL and base options;
    the inline ``storage_options`` are merged over them; then every Secret
    placeholder is expanded. Block lookups are cached for :data:`CACHE_TTL`.

    Args:
        cfg: the storage location config.

    Returns:
        ``(url, storage_options)`` ready for :class:`StorageLocation`.

    Raises:
        StorageLocationIncomplete: a block is missing or cannot be loaded.
    """
    problems: list[str] = []
    url = cfg.url or ""
    base: dict[str, Any] = {}
    if cfg.block is not None:
        try:
            url, settings = _lookup("fs", cfg.block, "RemoteFileSystem", f"{cfg.origin}.block")
            base = dict(settings or {})
        except _MissingBlock as e:
            problems.append(e.problem)
    options = _merge(base, _options(cfg, problems))
    url = _expand(url, f"{cfg.origin}.url", problems)
    options = _expand(options, f"{cfg.origin}.storage_options", problems)
    if not problems and not (isinstance(url, str) and url):
        problems.append(f"{cfg.origin}.url: resolves to an empty URL")
    if not problems and cfg.is_first and server_only(url):
        # The literal URL is checked when the config loads; a block can only
        # be checked once it resolves (STO-05).
        problems.append(
            f"{cfg.origin}: the first storage location receives uploads and cannot be "
            "an rclone storage location; list it after another storage location"
        )
    if problems:
        raise StorageLocationIncomplete(problems)
    return url, options


def _check(url: str, options: dict[str, Any]) -> list[str]:
    with _state.lock:
        checks = list(_state.checks)
    problems: list[str] = []
    for check in checks:
        try:
            problems.extend(check(url, options))
        except Exception as e:
            problems.append(redact_text(f"{type(e).__name__}: {e}"))
    return problems


def location_problems(cfg: StorageLocationConfig) -> list[str]:
    """Why a storage location is incomplete; empty when it is usable. Never raises."""
    try:
        url, options = resolve(cfg)
    except StorageLocationIncomplete as e:
        return e.problems
    except Exception as e:  # defensive: a listing must never fail on one storage location
        return [redact_text(f"{cfg.origin}: {type(e).__name__}: {e}")]
    return _check(url, options)


def open_location(cfg: StorageLocationConfig, cls: type[_S] = StorageLocation) -> _S:
    """Open a configured storage location with its credentials resolved.

    Args:
        cfg: the storage location config.
        cls: the :class:`StorageLocation` class to build (a subclass for tests
            or counting stores).

    Raises:
        StorageLocationIncomplete: a block is missing or cannot be loaded, or
            a registered check reports a problem.
    """
    url, options = resolve(cfg)
    problems = _check(url, options)
    if problems:
        raise StorageLocationIncomplete(problems)
    return cls(url, options, cfg.create_root, cfg.path_prefix)


def root_identity(url: str) -> str:
    """A storage location root without credentials: scheme, host, port and path.

    User info (and any password), query and fragment are dropped, so the value
    is safe to store and compare (STO-19).

    Args:
        url: a resolved storage URL.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
