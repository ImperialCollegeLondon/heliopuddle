"""The run logger helper: Prefect run logger inside a run, standard logger outside (SVC-30)."""

from __future__ import annotations

import logging

import pytest
from prefect import flow

from heliopuddle.server.services.run_logging import run_logger


@pytest.mark.req("SVC-30")
def test_outside_a_run_it_uses_the_standard_logger(caplog: pytest.LogCaptureFixture) -> None:
    log = run_logger("heliopuddle.test.outside")

    with caplog.at_level(logging.INFO, logger="heliopuddle.test.outside"):
        log.info("hello %s", "world")

    assert [(r.name, r.getMessage()) for r in caplog.records] == [
        ("heliopuddle.test.outside", "hello world")
    ]


@pytest.mark.req("SVC-30")
@pytest.mark.usefixtures("prefect_harness")
def test_inside_a_flow_run_it_reaches_the_prefect_run_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = run_logger("heliopuddle.test.inside")

    @flow
    def demo() -> None:
        log.info("inside the run")

    with caplog.at_level(logging.INFO):
        demo()

    inside = [r for r in caplog.records if r.getMessage() == "inside the run"]
    assert inside and all(r.name.startswith("prefect.flow_runs") for r in inside)
