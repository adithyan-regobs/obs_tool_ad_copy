"""
Aspora Branch Helpers

Utility functions for branch validation and detection.
"""

from typing import List, Optional, NamedTuple
import logging

from app.strategies.aspora.branch_constants import (
    BLOCKED_PR_TARGETS,
    PR_TARGET_PREFERENCE,
)

logger = logging.getLogger(__name__)


class ValidationResult(NamedTuple):
    """Result of branch validation."""
    is_valid: bool
    error_message: Optional[str] = None


def detect_primary_target(available_branches: List[str]) -> Optional[str]:
    """
    Auto-detect the primary PR target branch from available branches.

    Prefers pre-prod, falls back to stage-env.

    Args:
        available_branches: List of branch names in the repository

    Returns:
        Primary target branch name or None if not found
    """
    for target in PR_TARGET_PREFERENCE:
        if target in available_branches:
            logger.info(f"Detected primary PR target: {target}")
            return target

    logger.warning(f"No preferred PR target found. Available: {available_branches}")
    return None


def validate_branch_for_pr(branch: str) -> ValidationResult:
    """
    Validate that a branch can be used as a direct PR target.

    main and master are never allowed as direct PR targets.

    Args:
        branch: Branch name to validate

    Returns:
        ValidationResult with is_valid and optional error_message
    """
    if branch in BLOCKED_PR_TARGETS:
        return ValidationResult(
            is_valid=False,
            error_message=f"Cannot create PR directly to '{branch}'. PRs must go through pre-prod or stage-env."
        )

    return ValidationResult(is_valid=True)


def get_branch_info_message(
    selected_branch: str,
    pr_target: str,
    secondary_pr_target: Optional[str] = None
) -> str:
    """
    Generate a user-friendly message about PR routing.

    Args:
        selected_branch: Branch user selected
        pr_target: Primary PR target
        secondary_pr_target: Secondary PR target if applicable

    Returns:
        Human-readable message about PR routing
    """
    if selected_branch == pr_target:
        base_msg = f"PR will be created to '{pr_target}'"
    else:
        base_msg = f"Selected branch '{selected_branch}' → PR will go to '{pr_target}'"

    if secondary_pr_target:
        base_msg += f" + secondary PR to '{secondary_pr_target}'"

    return base_msg
