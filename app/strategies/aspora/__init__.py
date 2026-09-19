"""
Aspora Client Plugin

Client-specific PR strategy for GitHub workflow/Dockerfile updates.
Integrates with Aspora's existing infrastructure and branch flow.

Branch Flow (New):
    main → pre-prod → qa ← feature → sandbox

Branch Flow (Old):
    main → stage-env ← feature → stage-env-copy
"""

from app.strategies.aspora.pr_strategy import AsporaPRStrategy
from app.strategies.aspora.branch_constants import (
    PROTECTED_BRANCHES,
    BLOCKED_PR_TARGETS,
    PR_TARGET_PREFERENCE,
    SECONDARY_PR_ELIGIBLE,
    STAGE_ENV_FLOW_BRANCHES,
)

__all__ = [
    "AsporaPRStrategy",
    "PROTECTED_BRANCHES",
    "BLOCKED_PR_TARGETS",
    "PR_TARGET_PREFERENCE",
    "SECONDARY_PR_ELIGIBLE",
    "STAGE_ENV_FLOW_BRANCHES",
]
