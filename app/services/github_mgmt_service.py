import logging
import re
from typing import Dict, Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.enum import PRStatusEnum
from app.integrations.github_integration import GitHubIntegration
from app.domain.validators.github_rules import GitHubValidator, GitHubValidationError
from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
from app.repository.transaction_queue_repository import TransactionQueueRepository
from app.repository.github_app_installation_mst_repository import GitHubAppInstallationMstRepository
from app.db.models.transaction_queue_model import TransactionQueueStatusEnum
from app.utils.github_app_token import get_token_for_org

logger = logging.getLogger(__name__)


def _parse_atlantis_terragrunt_status(comments: List[Dict[str, Any]]) -> Optional[str]:
    """
    Parse Atlantis terragrunt status from PR comments.

    Searches comments (newest first) for Atlantis plan/apply patterns.

    Pattern Detection:
    - Plan Success: "Plan: X to add, Y to change, Z to destroy"
    - Plan Failure: "Plan Error"

    Args:
        comments: List of comment dicts with 'body' field

    Returns:
        "PLAN_SUCCESS", "PLAN_FAILED", or None if no Atlantis comment found
    """
    plan_success_pattern = re.compile(r"Plan:\s*\d+\s*to add,\s*\d+\s*to change,\s*\d+\s*to destroy")
    plan_error_pattern = re.compile(r"Plan Error")

    for comment in reversed(comments):
        body = comment.get("body", "")

        if plan_error_pattern.search(body):
            return "PLAN_FAILED"

        if plan_success_pattern.search(body):
            return "PLAN_SUCCESS"

    return None


class GitHubMgmtService:
    """
    Service layer for GitHub integration operations.
    Uses GitHub App installation tokens (no PAT).
    """

    def __init__(self, db: AsyncSession):
        """Initialize GitHub service with database session for installation token resolution."""
        self.db = db
        self.github_base_url = settings.github_base_url

    async def _get_token_for_org(self, github_org: str) -> str:
        """Get GitHub App token for a specific org."""
        return await get_token_for_org(github_org, self.db)

    async def get_repositories_for_tenant(self, tenant_code: str) -> Dict[str, Any]:
        """
        Get all repositories accessible to the tenant's GitHub App installations.

        Fetches repos from each installation the tenant has linked.

        Args:
            tenant_code: Tenant code to look up installations for

        Returns:
            Dict with repositories list and metadata
        """
        repo = GitHubAppInstallationMstRepository(self.db)
        installations = await repo.get_by_tenant_code(tenant_code)

        if not installations:
            return {
                "repositories": [],
                "total_count": 0,
                "message": "No GitHub App installations found. Install the app on your GitHub org first."
            }

        all_repositories = []

        for inst in installations:
            try:
                token = await get_token_for_org(inst.github_org, self.db)
                result = await GitHubIntegration.fetch_installation_repositories(
                    token=token,
                    base_url=self.github_base_url,
                )

                repos_data = result.get("repositories", [])
                for r in repos_data:
                    all_repositories.append({
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "full_name": r.get("full_name"),
                        "description": r.get("description"),
                        "private": r.get("private", False),
                        "default_branch": r.get("default_branch", "main"),
                        "html_url": r.get("html_url"),
                        "updated_at": r.get("updated_at"),
                        "github_org": inst.github_org,
                    })
            except Exception as e:
                logger.warning(f"Failed to fetch repos for installation {inst.github_org}: {e}")

        return {
            "repositories": all_repositories,
            "total_count": len(all_repositories)
        }

    async def get_repository_branches(
        self,
        owner: str,
        repo: str
    ) -> Dict[str, Any]:
        """
        Get all branches for a specific GitHub repository.

        Args:
            owner: Repository owner (org name)
            repo: Repository name

        Returns:
            Dict with branches list and metadata
        """
        GitHubValidator.validate_repository_params(owner, repo)

        token = await self._get_token_for_org(owner)

        result = await GitHubIntegration.fetch_repository_branches(
            token=token,
            base_url=self.github_base_url,
            owner=owner,
            repo=repo
        )

        branches_data = result.get("branches", [])
        transformed_branches = []

        for branch in branches_data:
            transformed_branches.append({
                "name": branch.get("name"),
                "protected": branch.get("protected", False)
            })

        return {
            "branches": transformed_branches,
            "total_count": result.get("total_count", len(transformed_branches))
        }

    async def check_and_update_pr_status(
        self,
        pr_number: int,
        db: AsyncSession,
        repository: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Check PR status and update if changed.

        Workflow:
        1. Get latest workflow from DB by pr_number + repository
        2. If pr_status is PR_OPEN, call GitHub API to check current status
        3. Update ALL matching workflows in DB if status changed
        4. Return PR details including status
        """
        workflow_repo = GitopsWorkflowDetailRepository(db)

        if not repository:
            raise ValueError("repository parameter is required for check_and_update_pr_status")
        git_repository = repository

        workflow = await workflow_repo.get_latest_by_pr_and_repository(
            pr_number=pr_number,
            git_repository=git_repository
        )

        if not workflow:
            return await self.check_pr_status_direct(pr_number, git_repository)

        if workflow.pr_status != PRStatusEnum.PR_OPEN:
            return {
                "pr_number": workflow.pr_number,
                "pr_url": workflow.pr_url,
                "pr_status": workflow.pr_status.value if workflow.pr_status else None,
                "pr_title": None,
                "git_branch": workflow.git_branch,
                "atlantis_terragrunt_status": None
            }

        repo_parts = git_repository.split("/")
        if len(repo_parts) != 2:
            raise Exception(f"Invalid repository format: {git_repository}")

        owner, repo = repo_parts
        token = await self._get_token_for_org(owner)

        pr_details = await GitHubIntegration.get_pull_request(
            token=token,
            base_url=self.github_base_url,
            owner=owner,
            repo=repo,
            pr_number=pr_number
        )

        new_status = PRStatusEnum.PR_OPEN
        if pr_details.get("merged"):
            new_status = PRStatusEnum.PR_MERGED
        elif pr_details.get("state") == "closed":
            new_status = PRStatusEnum.PR_CLOSED

        if new_status != workflow.pr_status:
            all_workflows = await workflow_repo.get_all_by_pr_and_repository(
                pr_number=pr_number,
                git_repository=git_repository
            )
            for wf in all_workflows:
                wf.pr_status = new_status
            await db.commit()
            await db.refresh(workflow)

        atlantis_status = None
        if new_status == PRStatusEnum.PR_OPEN:
            comments = await GitHubIntegration.get_pr_comments(
                token=token,
                base_url=self.github_base_url,
                owner=owner,
                repo=repo,
                pr_number=pr_number
            )
            atlantis_status = _parse_atlantis_terragrunt_status(comments)

        return {
            "pr_number": workflow.pr_number,
            "pr_url": workflow.pr_url,
            "pr_status": new_status.value,
            "pr_title": pr_details.get("title"),
            "git_branch": workflow.git_branch,
            "atlantis_terragrunt_status": atlantis_status
        }

    async def check_pr_status_direct(
        self,
        pr_number: int,
        repository: str
    ) -> Dict[str, Any]:
        """Check PR status directly from GitHub API without requiring a workflow record."""
        repo_parts = repository.split("/")
        if len(repo_parts) != 2:
            raise Exception(f"Invalid repository format: {repository}")

        owner, repo = repo_parts
        token = await self._get_token_for_org(owner)

        pr_details = await GitHubIntegration.get_pull_request(
            token=token,
            base_url=self.github_base_url,
            owner=owner,
            repo=repo,
            pr_number=pr_number
        )

        pr_status = "PR_OPEN"
        if pr_details.get("merged"):
            pr_status = "PR_MERGED"
        elif pr_details.get("state") == "closed":
            pr_status = "PR_CLOSED"

        head_info = pr_details.get("head", {})
        commit_sha = head_info.get("sha") if isinstance(head_info, dict) else None

        return {
            "pr_number": pr_number,
            "pr_url": pr_details.get("html_url"),
            "pr_status": pr_status,
            "pr_title": pr_details.get("title"),
            "commit_sha": commit_sha,
            "git_branch": head_info.get("ref") if isinstance(head_info, dict) else None
        }

    async def bulk_sync_pr_status(
        self,
        workflow_codes: List[str],
        db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Bulk sync PR statuses from GitHub and update both gitops_workflow_detail
        and transaction_queue tables.
        """
        workflow_repo = GitopsWorkflowDetailRepository(db)
        queue_repo = TransactionQueueRepository(db)

        pr_to_queue_status = {
            PRStatusEnum.PR_MERGED: TransactionQueueStatusEnum.PR_MERGED,
            PRStatusEnum.PR_CLOSED: TransactionQueueStatusEnum.PR_REJECTED,
            PRStatusEnum.PR_OPEN: TransactionQueueStatusEnum.PR_RAISED,
        }

        workflows = await workflow_repo.get_by_codes(workflow_codes)

        results = []
        updated_count = 0

        for workflow in workflows:
            repository = workflow.git_repository
            pr_number = workflow.pr_number
            previous_status = workflow.pr_status.value if workflow.pr_status else None

            try:
                if not repository or not pr_number:
                    raise ValueError(f"Workflow {workflow.code} missing repository or pr_number")

                if workflow.pr_status == PRStatusEnum.PR_MERGED:
                    results.append({
                        "workflow_code": workflow.code,
                        "repository": repository,
                        "pr_number": pr_number,
                        "previous_status": previous_status,
                        "new_status": previous_status,
                        "updated": False,
                        "error": None
                    })
                    continue

                repo_parts = repository.split("/")
                if len(repo_parts) != 2:
                    raise ValueError(f"Invalid repository format: {repository}")

                owner, repo = repo_parts
                token = await self._get_token_for_org(owner)

                pr_details = await GitHubIntegration.get_pull_request(
                    token=token,
                    base_url=self.github_base_url,
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number
                )

                if pr_details.get("merged"):
                    new_pr_status = PRStatusEnum.PR_MERGED
                elif pr_details.get("state") == "closed":
                    new_pr_status = PRStatusEnum.PR_CLOSED
                else:
                    new_pr_status = PRStatusEnum.PR_OPEN

                was_updated = False

                if new_pr_status != workflow.pr_status:
                    await workflow_repo.update_pr_status_by_pr_and_repository(
                        pr_number=pr_number,
                        git_repository=repository,
                        new_status=new_pr_status
                    )

                    new_queue_status = pr_to_queue_status.get(new_pr_status)
                    if new_queue_status:
                        await queue_repo.update_status_by_pr_and_repository(
                            pr_number=pr_number,
                            git_repository=repository,
                            new_status=new_queue_status
                        )

                    was_updated = True
                    updated_count += 1
                    logger.info(
                        f"Synced PR #{pr_number} ({repository}): "
                        f"{previous_status} -> {new_pr_status.value}"
                    )

                results.append({
                    "workflow_code": workflow.code,
                    "repository": repository,
                    "pr_number": pr_number,
                    "previous_status": previous_status,
                    "new_status": new_pr_status.value,
                    "updated": was_updated,
                    "error": None
                })

            except Exception as e:
                logger.warning(f"Failed to sync workflow {workflow.code} (PR #{pr_number}, {repository}): {e}")
                results.append({
                    "workflow_code": workflow.code,
                    "repository": repository,
                    "pr_number": pr_number,
                    "previous_status": previous_status,
                    "new_status": None,
                    "updated": False,
                    "error": str(e)
                })

        await db.commit()

        return {
            "results": results,
            "total": len(results),
            "updated_count": updated_count
        }
