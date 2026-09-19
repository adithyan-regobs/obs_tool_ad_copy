"""
PR Sync Helper Utilities

Smart PR replacement logic for GitHub PR operations.
Handles the 4 scenarios:
1. No existing PR → create new
2. Existing PR with same content → skip (return existing PR info)
3. Existing PR with different content → replace (close old, create new)
4. Existing PR was manually closed → create new replacement

Used by:
- pipeline_mgmt_service.py
- dockerfile_sync_service.py (optional refactor)
- terragrunt_sync_service.py (optional refactor)
"""

import logging
from typing import Dict, Optional, Any, Literal
from typing_extensions import TypedDict

from app.utils.github_sync_helpers import should_skip_commit

logger = logging.getLogger(__name__)


class PRActionResult(TypedDict, total=False):
    """Result of determine_pr_action helper."""
    action: Literal["skip", "create", "replace"]
    # For "skip" action - return existing PR info
    existing_pr_number: int
    existing_pr_url: str
    existing_branch: str
    existing_commit_sha: str
    # For "replace" action - info for cleanup
    old_pr_to_close: int
    old_branch_to_delete: str
    old_workflow_id: int


def determine_pr_action(
    existing_pr_info: Optional[Dict],
    existing_pr_content: Optional[str],
    new_content: str,
    owner: str = "",
    repo: str = ""
) -> PRActionResult:
    """
    Determine what PR action to take based on existing PR state and content comparison.

    Smart PR logic extracted from dockerfile_sync_service and terragrunt_sync_service.

    Scenarios:
    1. No existing PR (existing_pr_info is None) → action="create"
    2. Existing PR was manually closed (was_closed=True) → action="create" (with old_workflow_id for DB update)
    3. Existing PR + content matches → action="skip"
    4. Existing PR + content differs → action="replace"

    Args:
        existing_pr_info: Dict from DB lookup with keys:
            - git_branch: str
            - pr_number: int
            - workflow_id: int
            - commit_sha: str (optional)
            - was_closed: bool (optional, default False)
        existing_pr_content: Content fetched from existing PR's branch (None if not fetched)
        new_content: The new content to be committed
        owner: Repository owner (for constructing PR URL)
        repo: Repository name (for constructing PR URL)

    Returns:
        PRActionResult dict indicating the action to take
    """
    result: PRActionResult = {"action": "create"}

    # Scenario 1: No existing PR
    if not existing_pr_info:
        logger.info("No existing PR found - will create new")
        return result

    existing_branch = existing_pr_info.get("git_branch")
    existing_pr_number = existing_pr_info.get("pr_number")
    existing_workflow_id = existing_pr_info.get("workflow_id")
    pr_was_closed = existing_pr_info.get("was_closed", False)

    # Scenario 4: PR was manually closed - create replacement (skip content comparison)
    if pr_was_closed:
        logger.info(f"Previous PR #{existing_pr_number} was closed - creating replacement")
        result["action"] = "create"
        result["old_workflow_id"] = existing_workflow_id  # For DB status update
        return result

    # Scenario 2 & 3: PR is still open - check if content changed
    if existing_pr_content is not None:
        if should_skip_commit(existing_pr_content, new_content):
            # Scenario 2: No changes - skip, return existing PR info
            logger.info(f"No changes compared to existing PR #{existing_pr_number} - skipping")
            result["action"] = "skip"
            result["existing_pr_number"] = existing_pr_number
            result["existing_branch"] = existing_branch
            result["existing_commit_sha"] = existing_pr_info.get("commit_sha")
            if owner and repo:
                result["existing_pr_url"] = f"https://github.com/{owner}/{repo}/pull/{existing_pr_number}"
            return result
        else:
            # Scenario 3: Content changed - replace old PR
            logger.info(f"Content differs from existing PR #{existing_pr_number} - will replace")
            result["action"] = "replace"
            result["old_pr_to_close"] = existing_pr_number
            result["old_branch_to_delete"] = existing_branch
            result["old_workflow_id"] = existing_workflow_id
            return result

    # Couldn't fetch existing content - create new PR (conservative approach)
    logger.warning(f"Could not fetch content from existing PR #{existing_pr_number} branch - will create new")
    return result


def fetch_existing_pr_content(
    GitHubIntegration,
    github_token: str,
    github_base_url: str,
    owner: str,
    repo: str,
    file_path: str,
    branch: str
) -> Optional[str]:
    """
    Fetch file content from existing PR's branch for comparison.

    This is a convenience wrapper that handles errors gracefully.

    Args:
        GitHubIntegration: The GitHub integration class
        github_token: GitHub API token
        github_base_url: GitHub API base URL
        owner: Repository owner
        repo: Repository name
        file_path: Path to the file in repository
        branch: Branch to fetch from (existing PR's feature branch)

    Returns:
        File content as string, or None if not found/error
    """
    try:
        file_resp = GitHubIntegration.get_file_content(
            token=github_token,
            base_url=github_base_url,
            owner=owner,
            repo=repo,
            file_path=file_path,
            branch=branch
        )
        if file_resp and file_resp.get("exists"):
            return file_resp.get("content", "")
        return None
    except Exception as e:
        logger.warning(f"Could not fetch content from branch {branch}: {e}")
        return None


def cleanup_old_pr(
    GitHubIntegration,
    github_token: str,
    github_base_url: str,
    owner: str,
    repo: str,
    old_pr_number: int,
    old_branch: str,
    new_pr_number: int
) -> Dict[str, Any]:
    """
    Close old PR and delete its branch after creating replacement.

    Non-blocking: failures are logged but don't fail the operation.
    This ensures new PR success is not blocked by old PR/branch cleanup failures.

    Args:
        GitHubIntegration: The GitHub integration class
        github_token: GitHub API token
        github_base_url: GitHub API base URL
        owner: Repository owner
        repo: Repository name
        old_pr_number: PR number to close
        old_branch: Branch name to delete
        new_pr_number: New PR number (for supersede comment)

    Returns:
        Dict with cleanup results: {
            "pr_closed": bool,
            "branch_deleted": bool,
            "errors": List[str]
        }
    """
    result = {"pr_closed": False, "branch_deleted": False, "errors": []}

    # Close old PR with supersede comment
    try:
        GitHubIntegration.close_pull_request(
            token=github_token,
            base_url=github_base_url,
            owner=owner,
            repo=repo,
            pr_number=old_pr_number,
            comment=f"Superseded by PR #{new_pr_number}"
        )
        result["pr_closed"] = True
        logger.info(f"Closed old PR #{old_pr_number}")
    except Exception as e:
        result["errors"].append(f"Failed to close PR #{old_pr_number}: {e}")
        logger.warning(f"Failed to close old PR #{old_pr_number}: {e}")

    # Delete old branch
    try:
        GitHubIntegration.delete_branch(
            token=github_token,
            base_url=github_base_url,
            owner=owner,
            repo=repo,
            branch=old_branch
        )
        result["branch_deleted"] = True
        logger.info(f"Deleted old branch {old_branch}")
    except Exception as e:
        result["errors"].append(f"Failed to delete branch {old_branch}: {e}")
        logger.warning(f"Failed to delete old branch {old_branch}: {e}")

    return result
