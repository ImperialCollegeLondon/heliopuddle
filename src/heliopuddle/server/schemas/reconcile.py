"""Reconcile run summary (M5, SVC-26). See docs/design/rules.md#reconcile-m5."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReconcileFailure(BaseModel):
    """One file that failed in a rule pass, recorded so the pass can continue (SVC-28)."""

    rule: str
    path: str
    error: str


class ReconcilePass(BaseModel):
    """One pass of the rule phase: changes per rule, and the failures it recorded."""

    number: int
    changes: dict[str, int] = Field(default_factory=dict, description="Changes per rule name")
    failures: list[ReconcileFailure] = Field(default_factory=list)

    @property
    def changed(self) -> int:
        """Total changes made by every rule in this pass."""
        return sum(self.changes.values())


class ReconcileSummary(BaseModel):
    """What a reconcile did, logged at the end of the run (no artifact)."""

    project: str
    verify: bool = False
    rules: list[str] = Field(default_factory=list, description="Rules the rule phase ran")
    skipped_rules: list[str] = Field(default_factory=list, description="Disabled rules skipped")
    index_changes: dict[str, int] = Field(
        default_factory=dict, description="Changes per storage location in the index phase"
    )
    verify_changes: dict[str, int] = Field(
        default_factory=dict, description="Copies marked unknown per storage location"
    )
    passes: list[ReconcilePass] = Field(default_factory=list)

    @property
    def changed(self) -> int:
        """Total changes over every phase and pass."""
        return (
            sum(self.index_changes.values())
            + sum(self.verify_changes.values())
            + sum(p.changed for p in self.passes)
        )
