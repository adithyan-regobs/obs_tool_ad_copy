from typing import Optional, Tuple
from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.github_mgmt_service import GitHubMgmtService
from app.domain.validators.github_rules import GitHubValidationError
from app.schemas.github_schemas import (
    RepositoriesListResponse,
    BranchesListResponse,
    PRStatusCheckResponse,
    BulkPRStatusSyncRequest,
    BulkPRStatusSyncResponse
)

router = APIRouter()


@router.get("/repositories", response_model=RepositoriesListResponse, summary="Get GitHub Repositories")
async def get_repositories(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all repositories for the authenticated GitHub user.

    Security:
        - JWT authentication required

    This endpoint fetches all repositories from GitHub API using the token configured
    in environment variables. Uses pagination internally to fetch all repos.
    No database interaction - pure API proxy.

    Response:
        - repositories: List of repository objects with:
            - id: GitHub repository ID
            - name: Repository name
            - full_name: Full name (owner/repo)
            - description: Repository description
            - private: Whether repository is private
            - default_branch: Default branch name
            - html_url: Repository URL
            - updated_at: Last updated timestamp
        - total_count: Total number of repositories returned

    Raises:
        401: Invalid or expired GitHub token
        403: GitHub API rate limit exceeded or insufficient permissions
        500: Internal server error or GitHub API error

    Examples:
        # Get all repositories
        GET /api/v1/github/repositories

        Response: {
            "repositories": [
                {
                    "id": 123456,
                    "name": "my-repo",
                    "full_name": "username/my-repo",
                    "description": "My awesome repository",
                    "private": false,
                    "default_branch": "main",
                    "html_url": "https://github.com/username/my-repo",
                    "updated_at": "2025-01-15T10:30:00Z"
                }
            ],
            "total_count": 1
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        service = GitHubMgmtService(db)
        result = await service.get_repositories_for_tenant(tenant.code)

        return result
    except HTTPException:
        raise
    except GitHubValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        error_message = str(e)

        # Map specific GitHub errors to appropriate HTTP status codes
        if "authentication failed" in error_message.lower() or "invalid" in error_message.lower():
            raise HTTPException(status_code=401, detail=error_message)
        elif "rate limit" in error_message.lower() or "insufficient permissions" in error_message.lower():
            raise HTTPException(status_code=403, detail=error_message)
        else:
            raise HTTPException(status_code=500, detail=f"Failed to fetch repositories: {error_message}")


@router.get("/repositories/{owner}/{repo}/branches", response_model=BranchesListResponse, summary="Get Repository Branches")
async def get_repository_branches(
    owner: str,
    repo: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all branches for a specific GitHub repository.

    Security:
        - JWT authentication required

    This endpoint fetches branches from GitHub API using the token configured
    in environment variables. No database interaction - pure API proxy.

    Path Parameters:
        - owner (required): Repository owner (username or organization)
        - repo (required): Repository name

    Response:
        - branches: List of branch objects with:
            - name: Branch name
            - commit: Latest commit information
                - sha: Commit SHA
            - protected: Whether branch is protected
        - total_count: Total number of branches

    Raises:
        400: Validation error (invalid owner/repo parameters)
        401: Invalid or expired GitHub token
        403: GitHub API rate limit exceeded or insufficient permissions
        404: Repository not found or no access
        500: Internal server error or GitHub API error

    Examples:
        # Get branches for a repository
        GET /api/v1/github/repositories/octocat/Hello-World/branches

        Response: {
            "branches": [
                {
                    "name": "main",
                    "commit": {
                        "sha": "abc123def456..."
                    },
                    "protected": true
                },
                {
                    "name": "develop",
                    "commit": {
                        "sha": "def456abc123..."
                    },
                    "protected": false
                }
            ],
            "total_count": 2
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        service = GitHubMgmtService(db)
        result = await service.get_repository_branches(owner=owner, repo=repo)

        return result
    except HTTPException:
        raise
    except GitHubValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        error_message = str(e)

        # Map specific GitHub errors to appropriate HTTP status codes
        if "authentication failed" in error_message.lower() or "invalid" in error_message.lower():
            raise HTTPException(status_code=401, detail=error_message)
        elif "rate limit" in error_message.lower() or "insufficient permissions" in error_message.lower():
            raise HTTPException(status_code=403, detail=error_message)
        elif "not found" in error_message.lower():
            raise HTTPException(status_code=404, detail=error_message)
        else:
            raise HTTPException(status_code=500, detail=f"Failed to fetch branches: {error_message}")


@router.get("/pr-status/{pr_number}", response_model=PRStatusCheckResponse, summary="Check PR Status")
async def check_pr_status(
    pr_number: int,
    repository: Optional[str] = Query(None, description="GitHub repository (owner/repo). Defaults to configured repo."),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Check and update PR status on-demand.

    Workflow:
    1. Lookup workflow by PR number and repository in database
    2. If pr_status is PR_OPEN: call GitHub API to check current status, update DB if changed
    3. If pr_status is PR_MERGED or PR_CLOSED: return cached status (no API call)

    Path Parameters:
        - pr_number: GitHub pull request number

    Query Parameters:
        - repository: GitHub repository (owner/repo). Defaults to configured datadog_terraform_repo.

    Response:
        - pr_number: PR number
        - pr_url: Direct URL to pull request
        - pr_status: Current status (PR_OPEN, PR_MERGED, PR_CLOSED)
        - pr_title: PR title (only when fetched from GitHub API)
        - git_branch: Feature branch name
        - atlantis_terragrunt_status: Atlantis plan status (PLAN_SUCCESS, PLAN_FAILED, or null)

    Raises:
        404: No workflow found for PR number
        401: Invalid or expired GitHub token
        500: Internal server error
    """
    try:
        user, tenant = user_and_tenant

        service = GitHubMgmtService(db)
        result = await service.check_and_update_pr_status(
            pr_number=pr_number,
            db=db,
            repository=repository
        )

        return result
    except HTTPException:
        raise
    except Exception as e:
        error_message = str(e)

        if "no workflow found" in error_message.lower():
            raise HTTPException(status_code=404, detail=error_message)
        elif "authentication failed" in error_message.lower():
            raise HTTPException(status_code=401, detail=error_message)
        elif "not found" in error_message.lower():
            raise HTTPException(status_code=404, detail=error_message)
        else:
            raise HTTPException(status_code=500, detail=f"Failed to check PR status: {error_message}")


@router.post("/sync-pr-status", response_model=BulkPRStatusSyncResponse, summary="Bulk Sync PR Status")
async def sync_pr_status(
    request: BulkPRStatusSyncRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Bulk sync PR statuses from GitHub and update database.

    For each workflow code:
    1. Fetch workflow from DB to get repo + PR number
    2. If PR is already merged in DB, skip (merged is final)
    3. Otherwise, fetch current status from GitHub API
    4. Update gitops_workflow_detail.pr_status and transaction_queue.status if changed

    Request Body:
        - workflow_codes: List of gitops_workflow_detail codes to sync

    Response:
        - results: Per-item sync results with previous/new status and update flag
        - total: Total items processed
        - updated_count: Number of items whose status changed
    """
    try:
        user, tenant = user_and_tenant

        service = GitHubMgmtService(db)
        result = await service.bulk_sync_pr_status(
            workflow_codes=request.workflow_codes,
            db=db
        )

        return result
    except HTTPException:
        raise
    except Exception as e:
        error_message = str(e)

        if "authentication failed" in error_message.lower():
            raise HTTPException(status_code=401, detail=error_message)
        else:
            raise HTTPException(status_code=500, detail=f"Failed to sync PR statuses: {error_message}")
