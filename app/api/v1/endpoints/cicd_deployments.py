from typing import Tuple, Optional, List
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.repository.geo_loc_mst_repository import GeoLocMstRepository
from app.services.gitops.github_component import GithubComponent
from app.services.script_pr_workflow_service import ScriptPRWorkflowService
from app.utils.workflow_file_helpers import resolve_eks_workflow

router = APIRouter()


class DeploymentEntry(BaseModel):
    run_id: str
    run_url: str
    status: str
    conclusion: Optional[str]
    workflow_name: str
    head_sha: str
    head_branch: str
    head_commit_message: str
    head_commit_author: str
    triggering_actor: str
    created_at: Optional[str]
    updated_at: Optional[str]
    run_started_at: Optional[str]
    is_live: bool = False


class DeploymentHistoryResponse(BaseModel):
    deployments: List[DeploymentEntry]
    total: int
    live_sha: Optional[str]


@router.get("/deployments", response_model=DeploymentHistoryResponse)
async def get_deployment_history(
    repository: str = Query(..., description="GitHub repo in owner/repo format (from service_config.config.repository)"),
    branch: Optional[str] = Query(None, description="Branch to filter runs by (from service_config.config.branches)"),
    service_name: Optional[str] = Query(None, description="Service name to scope to the obs_tool-created deploy workflow"),
    environment: Optional[str] = Query(None, description="Environment (e.g. stage) used in the workflow file name"),
    geo_loc: Optional[str] = Query(None, description="Geo-loc mst code used to resolve the workflow file name"),
    organization: Optional[str] = Query(None, description="shared-lib `organization` input (e.g. vance-core) — required to adopt a hand-named workflow"),
    status: Optional[str] = Query(None, description="GitHub Actions run status filter (e.g. success, failure, completed)"),
    limit: int = Query(20, ge=1, le=100),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    if "/" not in repository:
        raise HTTPException(status_code=422, detail="repository must be in owner/repo format")

    owner, repo_name = repository.split("/", 1)

    # Derive the specific workflow file obs_tool created for this service so we only
    # return runs for that workflow (avoids duplicates from other workflows on the same commit).
    workflow_file = None
    if service_name and environment and geo_loc:
        geo_loc_mst = await GeoLocMstRepository(db).get_by_code(geo_loc)
        geo_loc_label = geo_loc_mst.name.lower().replace(" ", "-") if geo_loc_mst else geo_loc.lower()
        svc_label = service_name.lower().replace("_", "-")
        if not svc_label.endswith("-service"):
            svc_label = f"{svc_label}-service"
        workflow_file = f"deploy-{svc_label}-eks-{environment.lower()}-{geo_loc_label}.yml"

        # A service onboarded before DevLift deploys through its own
        # hand-named workflow, and the file locator writes into THAT file
        # rather than creating a second one. Scoping the history to the
        # generated name would then hit a workflow that does not exist —
        # GitHub answers 404 and the tab renders empty. Resolved exactly the
        # way the locator resolves it, from the files' own inputs, so the two
        # can never disagree about which file is this service's pipeline.
        #
        # `organization` is required to verify a file, so without it this
        # falls straight back to the generated name. `branch` is optional:
        # unset, GitHub reads the repository's default branch, which is what
        # this tab wants anyway.
        component = GithubComponent(db)

        async def _fetch(name: str):
            result = await component.get_content(
                owner=owner, repo=repo_name,
                file_path=f".github/workflows/{name}", branch=branch,
            )
            return result.get("content") if result.get("exists") else None

        listing = await component.list_directory(
            owner=owner, repo=repo_name, path=".github/workflows", branch=branch,
        )
        adopted = await resolve_eks_workflow(
            file_names=[
                entry.get("name")
                for entry in (listing.get("entries") or [])
                if entry.get("type") == "file" and entry.get("name")
            ],
            env=environment.lower(),
            service_name=svc_label,
            canonical_name=workflow_file,
            fetch_content=_fetch,
            organization=organization,
        )
        if adopted:
            workflow_file = adopted

    github = GithubComponent(db)
    runs = await github.list_deployment_history(
        owner=owner,
        repo=repo_name,
        branch=branch,
        workflow_file=workflow_file,
        status=status,
        limit=limit,
    )

    entries = [DeploymentEntry(**run) for run in runs]

    return DeploymentHistoryResponse(
        deployments=entries,
        total=len(entries),
        live_sha=None,
    )


class RollbackRequest(BaseModel):
    queue_id: int


class RollbackResponse(BaseModel):
    commit_sha: Optional[str]
    env_file_path: str
    message: str


@router.post("/rollback", response_model=RollbackResponse)
async def trigger_rollback(
    request: RollbackRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    user, tenant = user_and_tenant

    pr_service = ScriptPRWorkflowService(db)
    try:
        result = await pr_service.trigger_rollback(
            queue_id=request.queue_id,
            tenant_code=tenant.code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {exc}")

    commit_result = result.get("commit_result") or {}
    return RollbackResponse(
        commit_sha=commit_result.get("commit_sha"),
        env_file_path=result.get("env_file_path", ""),
        message=f"Rolled back — committed to {result.get('k8s_branch')} ({commit_result.get('status')})",
    )
