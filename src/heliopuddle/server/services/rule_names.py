"""Names shared by rule deployments, continuation runs and the control surface."""

RULE_ONCE_DEPLOYMENT = "run_rule_once/rule-once"
"""The shared deployment for validated, unsaved one-off rule specifications."""


def rule_deployment_name(project_id: int, project: str, rule: str) -> str:
    """Return a rule deployment's collision-free, human-readable name."""
    return f"rule-p{project_id}-{project}-{rule}"


def rule_deployment_full_name(project_id: int, project: str, rule: str) -> str:
    """Return ``run_rule/<deployment>`` for submissions to Prefect."""
    return f"run_rule/{rule_deployment_name(project_id, project, rule)}"


RECONCILE_FLOW_NAME = "reconcile"
"""The name of the reconcile flow, and so the first half of its deployments' names."""


def reconcile_deployment_name(project_id: int, project: str) -> str:
    """Return a project's collision-free reconcile deployment name."""
    return f"reconcile-p{project_id}-{project}"


def reconcile_deployment_full_name(project_id: int, project: str) -> str:
    """Return ``reconcile/<deployment>`` for submissions to Prefect."""
    return f"{RECONCILE_FLOW_NAME}/{reconcile_deployment_name(project_id, project)}"
