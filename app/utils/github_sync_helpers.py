"""
GitHub Sync Helper Utilities

Shared helper functions for GitHub sync operations.
Used by both dockerfile_sync_service.py and terragrunt_sync_service.py
to follow DRY (Don't Repeat Yourself) principle.
"""

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def normalize_file_content(content: str) -> str:
    """Normalize file content for comparison.

    Strips leading/trailing whitespace from each line, removes empty lines,
    and joins with single newline. This ensures that semantically identical
    files compare equal even if they have different whitespace.

    Args:
        content: The file content to normalize

    Returns:
        Normalized content string
    """
    lines = content.split('\n')
    # Strip each line and filter out empty lines
    normalized_lines = [line.strip() for line in lines if line.strip()]
    return '\n'.join(normalized_lines)


def create_or_get_feature_branch(
    GitHubIntegration,
    github_token: str,
    github_base_url: str,
    owner: str,
    repo: str,
    feature_branch: str,
    base_branch: str
) -> Tuple[bool, Optional[str]]:
    """Create a feature branch or get existing one.

    Args:
        GitHubIntegration: The GitHub integration class
        github_token: GitHub API token
        github_base_url: GitHub API base URL
        owner: Repository owner
        repo: Repository name
        feature_branch: Name of the feature branch to create
        base_branch: Name of the base branch to create from

    Returns:
        Tuple of (branch_already_exists: bool, error: Optional[str])
    """
    try:
        branch_result = GitHubIntegration.create_branch(
            token=github_token,
            base_url=github_base_url,
            owner=owner,
            repo=repo,
            branch_name=feature_branch,
            from_branch=base_branch
        )
        branch_already_exists = branch_result.get("already_exists", False)

        if branch_already_exists:
            logger.info(f"Branch {feature_branch} already exists, will compare with it")
        else:
            logger.info(f"Created new branch: {feature_branch}")

        return branch_already_exists, None
    except Exception as e:
        logger.error(f"Failed to create/get branch {feature_branch}: {e}")
        return False, str(e)


def get_file_from_appropriate_branch(
    GitHubIntegration,
    github_token: str,
    github_base_url: str,
    owner: str,
    repo: str,
    file_path: str,
    feature_branch: str,
    base_branch: str,
    branch_already_exists: bool
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch file content from the appropriate branch.

    If feature branch already exists, fetch from feature branch (has latest changes).
    If feature branch is new, fetch from base branch.

    This is important to avoid creating duplicate commits when content hasn't changed.

    Args:
        GitHubIntegration: The GitHub integration class
        github_token: GitHub API token
        github_base_url: GitHub API base URL
        owner: Repository owner
        repo: Repository name
        file_path: Path to the file
        feature_branch: Name of the feature branch
        base_branch: Name of the base branch
        branch_already_exists: Whether the feature branch already exists

    Returns:
        Tuple of (content: Optional[str], error: Optional[str])
    """
    # Choose the right branch to fetch from
    # - If branch exists → get from FEATURE branch (has latest changes from previous updates)
    # - If new branch → get from BASE branch
    content_branch = feature_branch if branch_already_exists else base_branch

    logger.info(f"Fetching {file_path} from branch: {content_branch} (branch_exists: {branch_already_exists})")

    try:
        file_resp = GitHubIntegration.get_file_content(
            token=github_token,
            base_url=github_base_url,
            owner=owner,
            repo=repo,
            file_path=file_path,
            branch=content_branch
        )

        # If file not found on feature branch, fallback to base branch
        if (not file_resp or not file_resp.get("content")) and branch_already_exists:
            logger.info(f"File {file_path} not found on feature branch, falling back to base branch: {base_branch}")
            file_resp = GitHubIntegration.get_file_content(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo,
                file_path=file_path,
                branch=base_branch
            )

        if not file_resp or not file_resp.get("content"):
            return None, f"File not found at {file_path}"

        return file_resp["content"], None
    except Exception as e:
        logger.error(f"Failed to fetch {file_path} from {content_branch}: {e}")
        return None, str(e)


def should_skip_commit(original_content: str, new_content: str) -> bool:
    """Check if commit should be skipped due to no changes.

    Uses normalized comparison to ignore whitespace differences.

    Args:
        original_content: The original file content
        new_content: The new/transformed file content

    Returns:
        True if content is the same (skip commit), False otherwise
    """
    return normalize_file_content(original_content) == normalize_file_content(new_content)
