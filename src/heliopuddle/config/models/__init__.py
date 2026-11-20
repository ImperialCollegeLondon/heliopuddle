"""Pydantic models for project YAML files.

Only the parts needed by the current milestone are modelled; the top-level key
``analysis`` is accepted and ignored until its milestone.

Split by topic (every public name is re-exported here): :mod:`.common` (names,
sizes, run batch limits), :mod:`.versioning` (version rules), :mod:`.rules`
(rule models and age criteria) and :mod:`.project` (the project and its
storage locations, index and reconcile settings).
"""

from heliopuddle.config.models.common import BatchConfig, Size
from heliopuddle.config.models.project import (
    IndexConfig,
    ProjectConfig,
    ReconcileConfig,
    StorageLocationConfig,
)
from heliopuddle.config.models.rules import (
    AGE_FIELDS,
    AgeCriteria,
    AgeField,
    AgeLimit,
    AgeRange,
    RuleConfig,
    RuleMatch,
    RuleOptions,
    RuleSpec,
    age_cutoff,
)
from heliopuddle.config.models.versioning import (
    DEFAULT_VERSION_RULES,
    VersionRule,
    VersionType,
    template_fields,
)

__all__ = [
    "AGE_FIELDS",
    "DEFAULT_VERSION_RULES",
    "AgeCriteria",
    "AgeField",
    "AgeLimit",
    "AgeRange",
    "BatchConfig",
    "IndexConfig",
    "ProjectConfig",
    "ReconcileConfig",
    "RuleConfig",
    "RuleMatch",
    "RuleOptions",
    "RuleSpec",
    "Size",
    "StorageLocationConfig",
    "VersionRule",
    "VersionType",
    "age_cutoff",
    "template_fields",
]
