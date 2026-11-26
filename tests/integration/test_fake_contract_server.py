"""CLI-14: the fake's contract scenarios hold against a real ``heliopuddle server start``.

``tests/unit/test_testing_fake.py`` runs every scenario in
``tests/fake_contract.py`` against ``FakeHelioPuddleClient`` and against the
in-process API. Here the same scenarios run with ``HelioPuddleClient`` over
HTTP against the real server processes: the shared SQLite server with its
embedded Prefect, and the server on Postgres with an external Prefect (skipped
without Docker). Scenarios that need a second storage location are skipped,
since these servers have one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from heliopuddle.client import HelioPuddleClient
from tests.fake_contract import SCENARIOS, Harness, Scenario
from tests.fake_contract_sync import SYNC_SCENARIOS

from .conftest import PROJECT, Deployment


@pytest.fixture(params=["sqlite", "postgres"])
def h(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    fixture = "hp_server" if request.param == "sqlite" else "hp_postgres_server"
    deployment: Deployment = request.getfixturevalue(fixture)
    with HelioPuddleClient(deployment.api_url, PROJECT, user="alice") as client:
        yield Harness(client, deployment.store, tmp_path)


@pytest.mark.req("CLI-14")
@pytest.mark.parametrize("scenario", list(SCENARIOS.values()), ids=list(SCENARIOS))
def test_contract_against_a_real_server(h: Harness, scenario: Scenario) -> None:
    scenario(h)


@pytest.mark.req("CLI-14", "CLI-18")
@pytest.mark.parametrize("scenario", list(SYNC_SCENARIOS.values()), ids=list(SYNC_SCENARIOS))
def test_sync_contract_against_a_real_server(h: Harness, scenario: Scenario) -> None:
    scenario(h)
