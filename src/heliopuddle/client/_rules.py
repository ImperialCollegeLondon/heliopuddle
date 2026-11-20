"""Rules and runs."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from heliopuddle.client._base import ClientBase
from heliopuddle.config.models import RuleSpec
from heliopuddle.server.schemas import (
    ReconcileRunRequest,
    RuleOnceRunRequest,
    RuleRead,
    RuleRunRequest,
    RuleTriggerResult,
    RunInfo,
    RunRequest,
)


class RulesMixin(ClientBase):
    """Rule controls and run submission of :class:`HelioPuddleClient`."""

    def list_rules(self) -> list[RuleRead]:
        """Return configured rules with defaults, effective enablement and progress."""
        rows = self._request("GET", f"/projects/{self._project()}/rules").json()
        return [RuleRead.model_validate(row) for row in rows]

    def get_rule(self, name: str) -> RuleRead:
        """Return a configured rule and its runtime state."""
        return RuleRead.model_validate(self._request("GET", self._rule_url(name)).json())

    def _rule_url(self, name: str) -> str:
        return f"/projects/{self._project()}/rules/{quote(name, safe='')}"

    def enable_rule(self, name: str) -> RuleRead:
        """Enable a rule's runtime switch; YAML must also allow it."""
        return RuleRead.model_validate(
            self._request("POST", f"{self._rule_url(name)}/enable").json()
        )

    def disable_rule(self, name: str) -> RuleRead:
        """Disable a rule, pausing its schedule and cancelling future runs."""
        return RuleRead.model_validate(
            self._request("POST", f"{self._rule_url(name)}/disable").json()
        )

    def trigger_rule(self, name: str) -> RuleTriggerResult:
        """Start incremental work only if no run already exists; disabled rules raise 409."""
        return RuleTriggerResult.model_validate(
            self._request("POST", f"{self._rule_url(name)}/trigger").json()
        )

    def run_rule(
        self,
        name: str,
        mode: Literal["full", "incremental"] = "incremental",
        dry_run: bool = False,
    ) -> RunInfo:
        """Submit a configured rule, changing only mode and dry run for this run."""
        body = RuleRunRequest(kind="rule", rule=name, mode=mode, dry_run=dry_run)
        response = self._request(
            "POST", f"/projects/{self._project()}/runs", json=body.model_dump(mode="json")
        )
        return RunInfo.model_validate(response.json())

    def run_once(self, spec: RuleSpec | dict[str, Any]) -> RunInfo:
        """Submit a complete unsaved specification for exactly one full batch."""
        body = RuleOnceRunRequest(kind="rule_once", spec=RuleSpec.model_validate(spec))
        response = self._request(
            "POST", f"/projects/{self._project()}/runs", json=body.model_dump(mode="json")
        )
        return RunInfo.model_validate(response.json())

    def reconcile(self, verify: bool = False, rules: list[str] | None = None) -> RunInfo:
        """Submit a reconcile of the project (``POST /projects/{p}/runs``) and return at once.

        The run indexes every storage location, optionally re-hashes available
        copies, then applies the rules until nothing changes. Follow it in the
        Prefect UI at ``flow_run_url``.

        Args:
            verify: also re-hash every available copy.
            rules: only these configured rules in the rule phase; None means all.

        Returns:
            The flow run id, its Prefect UI URL and the deployment name.

        Raises:
            HelioPuddleError: ``409`` if a reconcile is unfinished (the detail
                names its run), ``422`` for an unknown rule name, ``503`` if the
                deployment is missing or Prefect is down.
        """
        body = ReconcileRunRequest(kind="reconcile", verify=verify, rules=rules)
        response = self._request(
            "POST", f"/projects/{self._project()}/runs", json=body.model_dump(mode="json")
        )
        return RunInfo.model_validate(response.json())

    def trigger_index(
        self,
        storage_location: str | None = None,
        mode: Literal["full", "incremental"] = "full",
    ) -> RunInfo:
        """Start an index run of a storage location (``POST /projects/{p}/runs``).

        The run is submitted to Prefect and this returns at once; follow it in
        the Prefect UI at ``flow_run_url``.

        Args:
            storage_location: storage location name. None means the project's
                first storage location.
            mode: ``full`` (also detects removals) or ``incremental``.

        Returns:
            The flow run id, its Prefect UI URL and the deployment name.

        Raises:
            NotFoundError: unknown project or storage location.
            HelioPuddleError: ``409`` for an inactive storage location; ``503``
                if the index deployment is not registered or Prefect is down.
        """
        body = RunRequest(kind="index", storage_location=storage_location, mode=mode)
        response = self._request(
            "POST",
            f"/projects/{self._project()}/runs",
            json=body.model_dump(exclude_none=True),
        )
        return RunInfo.model_validate(response.json())
