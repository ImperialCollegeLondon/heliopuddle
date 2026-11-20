"""Serve deployments from a Prefect runner in a background thread (SVC-01)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prefect.deployments.runner import RunnerDeployment
    from prefect.runner import Runner

logger = logging.getLogger("heliopuddle.server.services.deploy")

START_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 30.0


class ServicesRunner:
    """Serve deployments in a background thread.

    Args:
        deployments: deployments to register and serve.
        runner_kwargs: extra arguments for :class:`prefect.runner.Runner`.
    """

    def __init__(self, deployments: list[RunnerDeployment], **runner_kwargs: Any) -> None:
        self.deployments = deployments
        self.runner_kwargs: dict[str, Any] = {"pause_on_shutdown": False, **runner_kwargs}
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task[None] | None = None
        self._runner: Runner | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    @property
    def error(self) -> BaseException | None:
        """The exception that stopped the runner, if any."""
        return self._error

    def is_alive(self) -> bool:
        """True while the runner thread is running."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = START_TIMEOUT_S) -> None:
        """Register the deployments and start serving them.

        Returns once every deployment is registered with the Prefect API.

        Args:
            timeout: seconds to wait for the registration.

        Raises:
            RuntimeError: if registration fails or times out.
        """
        self._thread = threading.Thread(
            target=self._thread_main, name="heliopuddle-services", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise RuntimeError(f"Serving the deployments did not start within {timeout:.0f}s")
        if self._error is not None:
            raise RuntimeError(
                f"Serving the deployments failed: {type(self._error).__name__}: {self._error}"
            ) from self._error
        names = ", ".join(f"{d.flow_name}/{d.name}" for d in self.deployments)
        logger.info("Serving deployments: %s", names)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as e:
            self._error = e
            logger.exception("The services runner stopped with an error")
        finally:
            self._ready.set()

    async def _main(self) -> None:
        from prefect.runner import Runner

        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        runner = Runner(**self.runner_kwargs)
        for deployment in self.deployments:
            await runner.aadd_deployment(deployment)
        self._runner = runner
        self._ready.set()
        with contextlib.suppress(asyncio.CancelledError):
            await runner.start()

    async def _astop(self) -> None:
        runner = self._runner
        if runner is not None and runner.started:
            await runner.astop()
        elif self._main_task is not None:
            self._main_task.cancel()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Stop serving and wait for the thread to finish.

        Args:
            timeout: seconds to wait for the runner to stop.
        """
        thread, loop = self._thread, self._loop
        if thread is None:
            return
        if loop is not None and thread.is_alive():
            logger.info("Stopping the served deployments")
            with contextlib.suppress(RuntimeError):
                future = asyncio.run_coroutine_threadsafe(self._astop(), loop)
                with contextlib.suppress(Exception):
                    future.result(timeout)
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("The services runner did not stop within %.0fs", timeout)
        self._thread = None
