"""One-line descriptions of a rule, shown on the Overview, Rules and Storage pages."""

from __future__ import annotations

from datetime import datetime, timedelta

from heliopuddle.config.models import AgeCriteria, AgeField, AgeRange
from heliopuddle.server.schemas import RuleRead
from heliopuddle.server.ui.shared.format import format_date, format_duration, number


def rule_match_summary(rule: RuleRead) -> str:
    """A short description of a rule's match: ``data/** · latest · modified older than 30d``."""
    match = rule.match
    parts = [", ".join(match.paths) or "**"]
    if match.exclude:
        parts.append("not " + ", ".join(match.exclude))
    if match.is_latest is True:
        parts.append("latest")
    elif match.is_latest is False:
        parts.append("non-latest")
    parts.extend(age_descriptions(match.age))
    return " · ".join(parts)


def _config_size(size: int) -> str:
    """A byte count as written in config (decimal units): ``2GB``, ``1.5MB``."""
    for factor, unit in ((10**12, "TB"), (10**9, "GB"), (10**6, "MB"), (10**3, "KB")):
        if size >= factor:
            return f"{size / factor:g}{unit}"
    return f"{size}B"


_AGE_LABELS: dict[AgeField, str] = {
    "modified": "modified",
    "created": "created",
    "content_date": "content date",
    "arrived": "arrived",
}


def _bound(value: datetime | timedelta) -> str:
    return format_duration(value) if isinstance(value, timedelta) else format_date(value)


def _age_range_text(label: str, bounds: AgeRange) -> str:
    older, younger = bounds.older_than, bounds.younger_than
    if isinstance(older, datetime) and isinstance(younger, datetime):
        return f"{label} between {_bound(younger)} and {_bound(older)}"
    words = []
    if older is not None:
        words.append(f"older than {_bound(older)}")
    if younger is not None:
        words.append(f"younger than {_bound(younger)}")
    return f"{label} " + " and ".join(words)


def age_descriptions(age: AgeCriteria) -> list[str]:
    """Describe every age criterion of a rule, in a fixed order.

    Args:
        age: the rule's ``match.age``.

    Returns:
        One phrase per criterion, e.g. ``modified older than 30d`` or
        ``content date between 2024-01-01 and 2025-06-01``.
    """
    return [_age_range_text(_AGE_LABELS[field], bounds) for field, bounds in age.items()]


def rule_full_summary(rule: RuleRead) -> list[tuple[str, str]]:
    """Everything that decides what a rule does, as ``(label, text)`` lines.

    Unlike :func:`rule_match_summary` this is complete: the match (including
    every age criterion) and the options that change its effect
    (overwrite, space check, dry run, batch limits).
    """
    match, options = rule.match, rule.options
    selects = [", ".join(match.paths)]
    if match.exclude:
        selects.append("excluding " + ", ".join(match.exclude))
    if match.is_latest is True:
        selects.append("latest versions only")
    elif match.is_latest is False:
        selects.append("non-latest versions only")
    ages = age_descriptions(match.age)
    if ages:
        selects.append(", ".join(ages))
    opts = ["overwrite" if options.overwrite else "no overwrite"]
    if options.update_only_if_newer:
        opts.append("only if newer")
    if options.check_for_space:
        opts.append(f"space check (+{_config_size(int(options.check_for_space_overhead))})")
    if options.dry_run:
        opts.append("dry run")
    limits = []
    if options.batch.max_files is not None:
        limits.append(f"{number(options.batch.max_files)} files")
    if options.batch.max_bytes is not None:
        limits.append(_config_size(int(options.batch.max_bytes)))
    if options.batch.max_duration is not None:
        limits.append(format_duration(options.batch.max_duration))
    opts.append("batch of " + " / ".join(limits))
    return [("Selects", " · ".join(selects)), ("Options", " · ".join(opts))]


def rule_route(rule: RuleRead) -> str:
    """``copy primary → archive`` or ``delete from primary``."""
    if rule.destination:
        return f"{rule.action} {rule.source} → {rule.destination}"
    return f"{rule.action} from {rule.source}"
