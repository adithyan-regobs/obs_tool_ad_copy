"""
Resource variables endpoint.

Serves node variables and variable references grouped for the project canvas,
independent of the VPC/resources fetch. Separating this lets the frontend
refresh variables after a deployment (e.g. when a resource goes online and
backend auto-seeds ARN/URL variables) without rebuilding the whole canvas —
which would clobber open resource-detail form state.
"""

import logging
from collections import defaultdict
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.enum import EnvironmentEnum
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.repository.variable_mst_repository import VariableMstRepository
from app.schemas.variable_clone_schemas import (
    CloneSourceOptionsResponse,
    CloneSourceServicesRequest,
    CloneSourceServicesResponse,
    CloneSourceVariablesResponse,
)
from app.schemas.variable_schemas import (
    CloneVariablesRequest,
    CloneVariablesResponse,
    DeployVariablesRequest,
    DeployVariablesResponse,
    RevertVariablesRequest,
    RevertVariablesResponse,
    StageSyncRequest,
    StageSyncResponse,
    SaveVariableItem,
    SaveVariablesResponse,
)
from app.schemas.vpc_discovery_schemas import (
    CanvasNodeVariableItem,
    CanvasVariableRefItem,
    CanvasVariablesResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "",
    response_model=CanvasVariablesResponse,
    summary="Fetch canvas node variables and variable refs",
)
async def get_resource_variables(
    application_code: str = Query(..., description="Application code"),
    environment: EnvironmentEnum = Query(..., description="Environment (dev, stage, qa, prod)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> CanvasVariablesResponse:
    """Return `{nodeVariables, variableRefs}` keyed by `transaction_code`
    (= canvas node `settings.resourceDbCode`). The frontend remaps the outer
    key to the canvas `node.id` against its current nodes state.
    """
    user, tenant = user_and_tenant

    from app.services.workspace_service import WorkspaceService
    workspace_svc = WorkspaceService(db)
    if not await workspace_svc.verify_app_workspace_access(user.code, tenant.code, application_code):
        return CanvasVariablesResponse(nodeVariables={}, variableRefs={})

    variable_repo = VariableMstRepository(db)
    try:
        rows = await variable_repo.get_all_by_application_and_environment(
            application_code=application_code,
            environment=environment,
            tenant_code=tenant.code,
        )
    except Exception as exc:
        logger.error("Failed to load resource variables: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    node_variables: dict[str, list[CanvasNodeVariableItem]] = defaultdict(list)
    variable_refs: dict[str, list[CanvasVariableRefItem]] = defaultdict(list)

    for v in rows:
        if not v.transaction_code:
            continue
        if v.referenced_variable_id is not None:
            variable_refs[v.transaction_code].append(
                CanvasVariableRefItem(
                    dbId=v.id,
                    name=v.key,
                    refVariableId=v.referenced_variable_id,
                )
            )
        else:
            node_variables[v.transaction_code].append(
                CanvasNodeVariableItem(
                    id=v.id,
                    code=v.code,
                    name=v.key,
                    type=v.variable_type.value.lower() if v.variable_type else "secret",
                    secret_arn=v.variable_cloud_identifier,
                )
            )

    return CanvasVariablesResponse(
        nodeVariables=dict(node_variables),
        variableRefs=dict(variable_refs),
    )


@router.post(
    "/clone/source-services",
    response_model=CloneSourceServicesResponse,
    summary="List clone source services with suggested defaults",
)
async def get_clone_source_services(
    data: CloneSourceServicesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> CloneSourceServicesResponse:
    """Paginated + searchable source-service picker for the clone modal (pull
    flow — the caller is on the TARGET resource).

    With `include_recommendation=true` (first load) the response also carries a
    `recommendation` block preloading the target service itself as the default
    source (another environment/region) — including its full env/region options
    so the modal needs no second call. Null when the target service has no
    other env/region to clone from.
    """
    from app.services.permission_cache_service import PermissionCacheService
    from app.services.variable_clone_service import VariableCloneService
    from app.utils.permission_helper import PermissionHelper

    user, tenant = user_and_tenant

    perm_helper = PermissionHelper(db)
    if await perm_helper.is_org_owner(user):
        allowed_service_codes = None
    else:
        cache_service = PermissionCacheService(db)
        allowed_service_codes = await cache_service.get_allowed_services(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code,
        )

    service = VariableCloneService(db)
    try:
        return await service.get_source_services(
            request=data,
            tenant_code=tenant.code,
            allowed_service_codes=allowed_service_codes,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to load clone source services: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/clone/source-options",
    response_model=CloneSourceOptionsResponse,
    summary="Environments and regions available on a clone source service",
)
async def get_clone_source_options(
    service_code: str = Query(..., description="Selected source service code"),
    target_environment: EnvironmentEnum = Query(..., description="Environment being cloned into"),
    target_geo_loc_code: Optional[str] = Query(None, description="Region (geo_loc) being cloned into"),
    target_service_code: Optional[str] = Query(
        None,
        description="Target service code — excludes the target env/region combo when source == target",
    ),
    restrict_infra_type_ref_code: Optional[str] = Query(
        None,
        description="Settings clone only: restrict combos to this infrastructuretype_ref_code",
    ),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> CloneSourceOptionsResponse:
    """Environments (with nested regions) the source service is configured
    for, with `suggested`/`auto_select` flags for default selection. The
    env → region cascade is client-side; no further calls needed."""
    from app.services.variable_clone_service import VariableCloneService

    _, tenant = user_and_tenant
    service = VariableCloneService(db)
    try:
        return await service.get_source_options(
            service_code=service_code,
            target_environment=target_environment,
            target_geo_loc_code=target_geo_loc_code,
            target_service_code=target_service_code,
            tenant_code=tenant.code,
            restrict_infra_type_ref_code=restrict_infra_type_ref_code,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to load clone source options: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/clone/source-variables",
    response_model=CloneSourceVariablesResponse,
    summary="Deployed variables of a clone source resource",
)
async def get_clone_source_variables(
    config_code: str = Query(..., description="service_config code of the chosen source env/region combo"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> CloneSourceVariablesResponse:
    """Variables selectable in the clone modal. Deployed only — the clone
    reads the source's latest deployed value from the audit bucket, and
    un-deployed drafts are private to their author."""
    from app.services.variable_clone_service import VariableCloneService

    _, tenant = user_and_tenant
    service = VariableCloneService(db)
    try:
        return await service.get_source_variables(
            config_code=config_code,
            tenant_code=tenant.code,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to load clone source variables: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/save",
    response_model=SaveVariablesResponse,
    summary="Save variable/secret metadata and stage values in the temporary bucket",
)
async def save_resource_variables(
    items: List[SaveVariableItem],
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> SaveVariablesResponse:
    """Accepts a bare array of variable items. Metadata is stored in
    variable_mst (values are never persisted in DB); the value list is staged
    as one JSON file in the temporary bucket under the caller's email folder,
    KMS-encrypted when type=secret. Nothing touches the audit bucket or
    Secrets Manager/SSM until the deploy API is called."""
    from app.services.variable_service import VariableService

    user, tenant = user_and_tenant
    service = VariableService(db)
    try:
        results, staged_file = await service.save_variables(items, user, tenant)
    except Exception as exc:
        logger.error("Failed to save resource variables: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return SaveVariablesResponse(results=results, staged_file=staged_file)


# ── DISABLED IN OBS_TOOL ────────────────────────────────────────────────────
# Variable deploy executes only in devlift-secret-config-manager — this same
# route exists THERE (carbon copy) and the frontend's deployVariables calls it
# via the secrets client. VariableService.deploy_variables is commented out in
# this repo, so this route could only fail; removed from the surface instead.
# @router.post(
#     "/deploy",
#     response_model=DeployVariablesResponse,
#     summary="Deploy staged variables: write audit trail and push to Secrets Manager / SSM",
# )
# async def deploy_resource_variables(
#     request: DeployVariablesRequest,
#     user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
#     db: AsyncSession = Depends(get_db),
# ) -> DeployVariablesResponse:
#     """Reads the caller's staged file from the temporary bucket, journals each
#     change as versioned JSON in the audit bucket (update = delete entry + add
#     entry), pushes secrets to Secrets Manager and variables to SSM Parameter
#     Store, then deletes the staged file (kept if any item fails, for retry)."""
#     from app.services.variable_service import VariableService
#
#     user, tenant = user_and_tenant
#     service = VariableService(db)
#     try:
#         results, staged_file = await service.deploy_variables(
#             transaction_code=request.transaction_code,
#             environment=request.environment,
#             user=user,
#             tenant=tenant,
#             table_name=request.table_name,
#         )
#     except ValueError as exc:
#         raise HTTPException(status_code=400, detail=str(exc))
#     except Exception as exc:
#         logger.error("Failed to deploy resource variables: %s", exc)
#         raise HTTPException(status_code=500, detail=str(exc))
#     return DeployVariablesResponse(results=results, staged_file=staged_file)
#
#
@router.post(
    "/revert",
    response_model=RevertVariablesResponse,
    summary="Discard staged (un-deployed) changes for the given keys",
)
async def revert_resource_variables(
    request: RevertVariablesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> RevertVariablesResponse:
    """Removes the caller's un-deployed staged entries for the given keys from the
    temp bucket, discarding the pending change. Reverting a staged deletion
    restores the variable (its row/cloud value were never touched pre-deploy)."""
    from app.services.variable_service import VariableService

    user, tenant = user_and_tenant
    service = VariableService(db)
    try:
        reverted = await service.revert_staged(
            transaction_code=request.transaction_code,
            keys=request.keys,
            environment=request.environment,
            user=user,
            tenant=tenant,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to revert resource variables: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return RevertVariablesResponse(reverted=reverted)


@router.post(
    "/stage-sync",
    response_model=StageSyncResponse,
    summary="Stage a drift fix as a draft: restore the DevLift value or accept the AWS value",
)
async def stage_sync_resource_variables(
    request: StageSyncRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> StageSyncResponse:
    """For each drifted key, stages a normal draft entry in the caller's staged
    file — restore_devlift takes the last deployed value from the audit bucket,
    accept_aws reads the live AWS value. Nothing reaches AWS or the audit trail
    until the resource is deployed. Keys that already have a pending draft are
    rejected per-key."""
    from app.services.variable_service import VariableService

    user, tenant = user_and_tenant
    service = VariableService(db)
    try:
        results, staged_file = await service.stage_sync_values(request, user, tenant)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to stage drift sync: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return StageSyncResponse(results=results, staged_file=staged_file)


@router.post(
    "/clone",
    response_model=CloneVariablesResponse,
    summary="Clone variables/secrets from another resource's latest deployed values",
)
async def clone_resource_variables(
    request: CloneVariablesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> CloneVariablesResponse:
    """Clone (assisted manual entry): stages the values the user chose to copy
    against the TARGET resource and creates/updates the target's variable_mst
    rows. Secrets are KMS-encrypted at staging. Values reach SSM / Secrets
    Manager only when the target is deployed. Blocked for production targets."""
    from app.services.variable_service import VariableService

    user, tenant = user_and_tenant
    service = VariableService(db)
    try:
        results, staged_file = await service.clone_variables(request, user, tenant)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to clone resource variables: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return CloneVariablesResponse(results=results, staged_file=staged_file)
