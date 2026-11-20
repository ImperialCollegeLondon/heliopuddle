"""``reconcile:`` project config validation (DEP-14)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from heliopuddle.config.models import ProjectConfig

pytestmark = pytest.mark.req("DEP-14")


def data(**reconcile: Any) -> dict[str, Any]:
    return {
        "project": "demo",
        "storage_locations": [{"name": "primary", "url": "memory://demo"}],
        "rules": [
            {
                "name": "archive",
                "action": "delete",
                "source": "primary",
                "match": {"paths": ["**"]},
            }
        ],
        **({"reconcile": reconcile} if reconcile else {}),
    }


def test_a_minimal_project_needs_no_reconcile_key_and_gets_defaults() -> None:
    minimal = {
        "project": "demo",
        "storage_locations": [{"name": "primary", "url": "memory://demo"}],
    }
    cfg = ProjectConfig.model_validate(minimal)
    assert cfg.reconcile.schedule is None
    assert cfg.reconcile.verify is False
    assert cfg.reconcile.rules is None
    assert (cfg.reconcile.max_rule_passes, cfg.reconcile.max_failures) == (3, 5)


def test_every_key_is_optional_and_accepted() -> None:
    cfg = ProjectConfig.model_validate(
        data(
            schedule="0 3 * * *",
            verify=True,
            rules=["archive"],
            max_rule_passes=2,
            max_failures=1,
        )
    )
    assert cfg.reconcile.schedule == "0 3 * * *"
    assert cfg.reconcile.rules == ["archive"]
    assert ProjectConfig.model_validate(data(rules=[])).reconcile.rules == []


@pytest.mark.parametrize(
    ("reconcile", "message"),
    [
        ({"schedule": "not a cron"}, "schedule"),
        ({"schedule": "* * *"}, "schedule"),
        ({"max_rule_passes": 0}, "max_rule_passes"),
        ({"max_rule_passes": -1}, "max_rule_passes"),
        ({"max_failures": 0}, "max_failures"),
        ({"max_failures": -3}, "max_failures"),
        ({"rules": ["archive", "missing"]}, r"reconcile\.rules: unknown rule\(s\): missing"),
        ({"rules": ["b", "a"]}, r"unknown rule\(s\): a, b"),
        ({"unknown_key": 1}, "unknown_key"),
    ],
)
def test_invalid_reconcile_config_is_rejected_naming_the_field(
    reconcile: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ProjectConfig.model_validate(data(**reconcile))
