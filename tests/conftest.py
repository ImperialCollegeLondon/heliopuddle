"""Shared pytest setup: the ``req`` marker report (see scripts/req_matrix.py)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--req-report",
        default=None,
        help="Write a JSON report of requirement IDs -> test outcomes to this path",
    )


@pytest.fixture(autouse=True)
def _header_auth_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run tests in the pre-M5A ``header`` auth mode unless they ask for another.

    The product default is ``local`` (AUTH-01). Tests of auth pass
    ``auth_mode="local"`` or ``"oidc"`` to ``Settings`` explicitly (init values
    beat the environment); a test of the default calls
    ``monkeypatch.delenv("HELIOPUDDLE_AUTH_MODE")`` first.
    """
    monkeypatch.setenv("HELIOPUDDLE_AUTH_MODE", "header")


@pytest.fixture(autouse=True)
def _default_user_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give API test clients a user header unless a test passes ``headers=`` itself.

    ``header`` mode refuses a request without the header (AUTH-20), and most API
    tests run in that mode as one anonymous-but-present user. ``local`` and
    ``oidc`` modes ignore the header.
    """
    from starlette.testclient import TestClient

    original = TestClient.__init__

    def init(self: TestClient, *args: object, **kwargs: object) -> None:
        if kwargs.get("headers") is None:
            kwargs["headers"] = {"X-Forwarded-User": "tester"}
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(TestClient, "__init__", init)


@pytest.fixture(autouse=True)
def _fresh_login_throttle() -> Iterator[None]:
    """Each test starts with no failed-login counters (AUTH-18)."""
    from heliopuddle.server.auth.throttle import reset_throttle

    reset_throttle()
    yield
    reset_throttle()


@pytest.fixture(autouse=True)
def _fake_jwks_network() -> Iterator[None]:
    """Serve the JWKS of any ``FakeIssuer`` over ``urllib`` (see tests/fake_oidc.py)."""
    from tests.fake_oidc import install_jwks_network

    with install_jwks_network():
        yield


_results: dict[str, dict[str, str]] = {}


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:
    reqs = [rid for m in item.iter_markers("req") for rid in m.args]
    if not reqs:
        return
    if call.when == "setup" and call.excinfo is not None:
        outcome = "skipped" if call.excinfo.errisinstance(pytest.skip.Exception) else "failed"
    elif call.when == "call":
        if call.excinfo is None:
            outcome = "passed"
        elif call.excinfo.errisinstance(pytest.skip.Exception):
            outcome = "skipped"
        else:
            outcome = "failed"
    else:
        return
    for rid in reqs:
        prev = _results.setdefault(rid, {}).get(item.nodeid)
        if prev != "failed":
            _results[rid][item.nodeid] = outcome


def _merge(into: dict[str, dict[str, str]], other: dict[str, dict[str, str]]) -> None:
    """Merge ``other`` into ``into``; a failure for a test is never overwritten."""
    for rid, outcomes in other.items():
        for nodeid, outcome in outcomes.items():
            prev = into.setdefault(rid, {}).get(nodeid)
            if prev != "failed":
                into[rid][nodeid] = outcome


def pytest_sessionfinish(session: pytest.Session) -> None:
    # Under pytest-xdist a worker hands its results to the controller (see
    # ``pytest_testnodedown``); only the controller, or a plain run, writes.
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput["req_results"] = _results
        return
    path = session.config.getoption("--req-report")
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_results, indent=2, sort_keys=True))


def pytest_testnodedown(node: object, error: object) -> None:
    """Collect a finished xdist worker's requirement results."""
    output = getattr(node, "workeroutput", None) or {}
    _merge(_results, output.get("req_results", {}))
