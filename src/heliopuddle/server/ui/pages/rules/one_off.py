"""Reading the Rules page's one-off run form into a rule spec."""

from __future__ import annotations

from typing import Any

from heliopuddle.config.models import AGE_FIELDS, RuleOptions, RuleSpec
from heliopuddle.server.ui.shared.errors import BadRequestError
from heliopuddle.server.ui.shared.forms import form_bool

AGE_BOUNDS = ("older_than", "younger_than")
"""The two bounds of each age criterion; the form names them ``age_<field>_<bound>``."""


def _age(form: dict[str, str]) -> dict[str, dict[str, str]]:
    """Read the age grid: one entry per date with at least one non-blank bound."""
    age: dict[str, dict[str, str]] = {}
    for field in AGE_FIELDS:
        bounds = {
            bound: value
            for bound in AGE_BOUNDS
            if (value := form.get(f"age_{field}_{bound}", "").strip())
        }
        if bounds:
            age[field] = bounds
    return age


def one_off_spec(form: dict[str, str]) -> RuleSpec:
    """Build the :class:`~heliopuddle.config.models.RuleSpec` of a one-off run.

    Age bounds arrive as ``age_<field>_older_than`` and
    ``age_<field>_younger_than`` for each of ``modified``, ``created``,
    ``content_date`` and ``arrived``; blank ones are left out.

    Args:
        form: the submitted fields.

    Returns:
        The validated spec.

    Raises:
        BadRequestError: a checkbox or ``max_files`` value is malformed.
        pydantic.ValidationError: the spec as a whole is invalid.
    """
    latest = form.get("is_latest", "")
    match: dict[str, Any] = {
        "paths": [line.strip() for line in form.get("paths", "").splitlines() if line.strip()],
        "exclude": [line.strip() for line in form.get("exclude", "").splitlines() if line.strip()],
        "is_latest": form_bool(latest, "is_latest") if latest else None,
        "age": _age(form),
    }
    options: dict[str, Any] = {
        field: form_bool(form.get(field, ""), field)
        for field in (
            "overwrite",
            "check_for_space",
            "update_only_if_newer",
            "dry_run",
            "retrigger",
        )
    }
    defaults = RuleOptions().model_dump(mode="json")
    for field in ("check_for_space_overhead", "pause_between_runs"):
        options[field] = form.get(field, defaults[field])
    max_files = form.get("max_files", str(defaults["batch"]["max_files"]))
    try:
        count = int(max_files) if max_files else None
    except ValueError:
        raise BadRequestError(f"Invalid value for max_files: {max_files!r}.") from None
    options["batch"] = {
        "max_files": count,
        "max_bytes": form.get("max_bytes", defaults["batch"]["max_bytes"]) or None,
        "max_duration": form.get("max_duration", defaults["batch"]["max_duration"]) or None,
    }
    return RuleSpec.model_validate(
        {
            "action": form.get("action"),
            "source": form.get("source"),
            "destination": form.get("destination") or None,
            "match": match,
            "options": options,
        }
    )
