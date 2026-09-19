"""
Aspora PR Strategy

Client-specific PR strategy for workflow/Dockerfile commits.
Handles branch routing and secondary PR logic for Aspora's infrastructure.
"""

from typing import List, Optional, Dict, Any
import logging

from app.strategies.aspora.branch_constants import (
    PROTECTED_BRANCHES,
    BLOCKED_PR_TARGETS,
    PR_TARGET_PREFERENCE,
    SECONDARY_PR_ELIGIBLE,
    STAGE_ENV_FLOW_BRANCHES,
)

logger = logging.getLogger(__name__)


class AsporaPRStrategy:
    """
    Aspora-specific PR strategy for workflow/Dockerfile commits.

    Routing Rules:
    - main/master/pre-prod/qa/sandbox → PR to pre-prod (fallback: stage-env)
    - stage-env/stage-env-copy → PR to stage-env
    - Other feature branches → PR to same branch

    Secondary PR:
    - qa, sandbox, stage-env-copy can optionally have a secondary PR to themselves
    """

    def is_protected_branch(self, branch: str) -> bool:
        """
        Check if branch requires protected handling.

        Args:
            branch: Branch name to check

        Returns:
            True if branch is in the protected list
        """
        return branch in PROTECTED_BRANCHES

    def is_blocked_pr_target(self, branch: str) -> bool:
        """
        Check if branch can never be a direct PR target.

        Args:
            branch: Branch name to check

        Returns:
            True if branch is blocked (main/master)
        """
        return branch in BLOCKED_PR_TARGETS

    def is_stage_env_flow(self, branch: str) -> bool:
        """
        Check if branch uses stage-env flow.

        Args:
            branch: Branch name to check

        Returns:
            True if branch is stage-env or stage-env-copy
        """
        return branch in STAGE_ENV_FLOW_BRANCHES

    def detect_primary_target(self, available_branches: List[str]) -> Optional[str]:
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

        logger.warning(f"No preferred PR target found in branches: {available_branches}")
        return None

    def get_pr_target_branch(
        self,
        selected_branch: str,
        available_branches: List[str]
    ) -> str:
        """
        Determine PR target based on selected branch.

        Routing:
        - main/master/pre-prod/qa/sandbox → pre-prod (fallback stage-env)
        - stage-env/stage-env-copy → stage-env
        - Feature branches → same branch

        Args:
            selected_branch: Branch user selected in UI
            available_branches: List of branch names in the repository

        Returns:
            Target branch for the PR
        """
        # Stage-env flow branches always go to stage-env
        if self.is_stage_env_flow(selected_branch):
            logger.info(f"Branch {selected_branch} uses stage-env flow, PR target: stage-env")
            return "stage-env"

        # Protected branches (except stage-env flow) go to pre-prod or stage-env
        if self.is_protected_branch(selected_branch):
            primary_target = self.detect_primary_target(available_branches)
            if primary_target:
                logger.info(f"Protected branch {selected_branch}, PR target: {primary_target}")
                return primary_target
            else:
                # Fallback to selected branch if no pre-prod/stage-env exists
                logger.warning(f"No primary target found, using selected branch: {selected_branch}")
                return selected_branch

        # Feature branches go to themselves
        logger.info(f"Feature branch {selected_branch}, PR target: {selected_branch}")
        return selected_branch

    def get_feature_branch_source(
        self,
        selected_branch: str,
        available_branches: List[str]
    ) -> str:
        """
        Determine which branch to create feature branch FROM.

        Source:
        - main/master/pre-prod/qa/sandbox → pre-prod (fallback stage-env)
        - stage-env/stage-env-copy → stage-env
        - Feature branches → selected branch

        Args:
            selected_branch: Branch user selected in UI
            available_branches: List of branch names in the repository

        Returns:
            Branch to create feature branch from
        """
        # Stage-env flow branches create from stage-env
        if self.is_stage_env_flow(selected_branch):
            logger.info(f"Branch {selected_branch} uses stage-env flow, feature branch from: stage-env")
            return "stage-env"

        # Protected branches create from pre-prod or stage-env
        if self.is_protected_branch(selected_branch):
            primary_target = self.detect_primary_target(available_branches)
            if primary_target:
                logger.info(f"Protected branch {selected_branch}, feature branch from: {primary_target}")
                return primary_target
            else:
                logger.warning(f"No primary target found, creating from: {selected_branch}")
                return selected_branch

        # Feature branches create from themselves
        logger.info(f"Feature branch {selected_branch}, feature branch from: {selected_branch}")
        return selected_branch

    def should_allow_secondary_pr(self, selected_branch: str) -> bool:
        """
        Check if selected branch is eligible for secondary PR.

        Eligible branches: qa, sandbox, stage-env-copy

        Args:
            selected_branch: Branch user selected in UI

        Returns:
            True if secondary PR is allowed for this branch
        """
        return selected_branch in SECONDARY_PR_ELIGIBLE

    def get_secondary_pr_target(self, selected_branch: str) -> Optional[str]:
        """
        Return secondary PR target if eligible.

        The secondary PR target is the selected branch itself
        (e.g., qa selected → secondary PR to qa)

        Args:
            selected_branch: Branch user selected in UI

        Returns:
            Secondary PR target branch or None if not eligible
        """
        if self.should_allow_secondary_pr(selected_branch):
            return selected_branch
        return None

    def get_pr_routing_info(
        self,
        selected_branch: str,
        available_branches: List[str],
        create_secondary_pr: bool = False
    ) -> Dict[str, Any]:
        """
        Get complete PR routing information for a selected branch.

        Args:
            selected_branch: Branch user selected in UI
            available_branches: List of branch names in the repository
            create_secondary_pr: Whether to create secondary PR

        Returns:
            Dict with routing info:
            {
                "pr_target": str,
                "feature_branch_source": str,
                "is_protected": bool,
                "secondary_pr_target": Optional[str],
                "secondary_pr_enabled": bool
            }
        """
        pr_target = self.get_pr_target_branch(selected_branch, available_branches)
        feature_source = self.get_feature_branch_source(selected_branch, available_branches)

        secondary_target = None
        secondary_enabled = False

        if create_secondary_pr and self.should_allow_secondary_pr(selected_branch):
            secondary_target = self.get_secondary_pr_target(selected_branch)
            secondary_enabled = True

        return {
            "pr_target": pr_target,
            "feature_branch_source": feature_source,
            "is_protected": self.is_protected_branch(selected_branch),
            "secondary_pr_target": secondary_target,
            "secondary_pr_enabled": secondary_enabled,
        }
