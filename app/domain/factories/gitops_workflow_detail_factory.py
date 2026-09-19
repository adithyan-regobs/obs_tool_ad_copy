from uuid import uuid4
from typing import Dict, Any, Optional
from app.core.enum import PRStatusEnum, WorkflowSourceTableEnum


def make_gitops_workflow_detail(
    git_repository: str,
    git_branch: str,
    git_commit_sha: str,
    pr_number: int,
    pr_url: str,
    tenant_mst_code: str,
    user_mst_code: Optional[str] = None,
    workflow_name: Optional[str] = None,
    pr_status: PRStatusEnum = PRStatusEnum.PR_OPEN,
    transaction_code: Optional[str] = None,
    table_name: Optional[WorkflowSourceTableEnum] = None
) -> Dict[str, Any]:
    """
    Factory to create GitOps workflow detail domain object.

    Generates unique code and sets defaults for tracking PR and workflow execution.

    Args:
        git_repository: GitHub repository (e.g., "regobs/datadog-terraform-repo")
        git_branch: Feature branch name
        git_commit_sha: Git commit SHA
        pr_number: GitHub pull request number
        pr_url: Direct URL to pull request
        tenant_mst_code: Tenant code this workflow belongs to
        user_mst_code: User code who created this workflow (optional)
        workflow_name: Optional human-readable name for the workflow
        pr_status: PR status (defaults to PR_OPEN when PR is created)
        transaction_code: Code of the entity that created this workflow (e.g., service_config.code)
        table_name: Source table name for polymorphic reference (e.g., SERVICE_CONFIG)

    Returns:
        Dictionary with all fields for repository create method

    Example:
        >>> workflow_data = make_gitops_workflow_detail(
        ...     git_repository="regobs/datadog-terraform-repo",
        ...     git_branch="feature/payment-alerts",
        ...     git_commit_sha="a1b2c3d4",
        ...     pr_number=456,
        ...     pr_url="https://github.com/regobs/repo/pull/456",
        ...     workflow_name="Bulk Payment Alerts Deployment"
        ... )
        >>> workflow = await workflow_repository.create(**workflow_data)
    """
    workflow_code = f"GWD_PR{pr_number}_{str(uuid4())[:8]}"

    if not workflow_name:
        workflow_name = f"GitOps Workflow for PR #{pr_number}"

    return {
        "code": workflow_code,
        "name": workflow_name,
        "description": f"Deployment workflow tracking for PR #{pr_number}",
        "git_repository": git_repository,
        "git_branch": git_branch,
        "git_commit_sha": git_commit_sha,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "pr_status": pr_status,
        "tenant_mst_code": tenant_mst_code,
        "user_mst_code": user_mst_code,
        "transaction_code": transaction_code,
        "table_name": table_name,
        "is_deleted": False,
        "is_active": True
    }
