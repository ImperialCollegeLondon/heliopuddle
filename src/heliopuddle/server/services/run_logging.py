"""Logging that reaches the Prefect UI when it runs inside a flow or task run.

Service code runs both inside Prefect runs (flows, tasks) and outside them (the
API calls a per-file task function inline). Inside a run, records must go
through Prefect's run logger so they are shipped to the Prefect API and show in
the flow-run log view; outside one they go to the module's standard logger
(the server's stdout). :func:`run_logger` returns a logger that decides on
every call, so a module can hold one at import time.
"""

from __future__ import annotations

import logging
from typing import Any

from prefect.exceptions import MissingContextError
from prefect.logging import get_run_logger


class RunLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """A logger that uses the Prefect run logger while a run context exists."""

    def __init__(self, name: str) -> None:
        super().__init__(logging.getLogger(name), {})

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log through the current run's logger, or the module logger outside a run."""
        target: Any
        try:
            target = get_run_logger()
        except MissingContextError:
            target = self.logger
        if target.isEnabledFor(level):
            target.log(level, msg, *args, **kwargs)


def run_logger(name: str) -> RunLogger:
    """Return a logger for a service module.

    Use it as ``logger = run_logger(__name__)``. Records are shipped to the
    Prefect API (and shown in the Prefect UI) when emitted inside a flow or
    task run, and go to the standard logger named ``name`` otherwise. It never
    raises ``MissingContextError``.

    Args:
        name: the standard logger name, normally ``__name__``.

    Returns:
        A logger with the usual ``debug``, ``info``, ``warning``, ``error`` and
        ``exception`` methods.
    """
    return RunLogger(name)
