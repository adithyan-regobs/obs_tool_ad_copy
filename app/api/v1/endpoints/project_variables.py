"""
Project Variables API Endpoints

Canvas variable management — metadata stored in variable_mst,
values stored in AWS Secrets Manager (cross-account). Variable references are DB-only.
"""

from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.project_variables_service import ProjectVariablesService, _resolve_table_name
from app.schemas.secrets_parameters_schemas import (
    CreateCanvasVariableRequest,
    UpdateCanvasVariableRequest,
    CreateCanvasVariableRefRequest,
    CanvasVariableResponse,
    CanvasVariableWithValueResponse,
    CanvasVariableRefResponse,
    BulkCanvasVariablesResponse,
    BulkUpsertVariablesRequest,
    BulkUpsertVariablesResponse,
    BulkCreateRefsRequest,
    BulkCreateRefsResponse,
)

router = APIRouter()


@router.post(
    "/",
    response_model=CanvasVariableResponse,
    status_code=201,
    summary="Create a canvas variable",
)
async def create_variable(
    data: CreateCanvasVariableRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create a new canvas variable. Value is stored in AWS Secrets Manager, metadata in variable_mst."""
    user, tenant = user_and_tenant
    from app.services.applications_mst_service import ApplicationsMstService
    app_svc = ApplicationsMstService(db)
    if not await app_svc.can_write_for_app(user.code, tenant.code, data.application_code):
        raise HTTPException(status_code=403, detail="Access denied: read-only role cannot create variables")
    service = ProjectVariablesService(db)
    return await service.create_variable(request=data, tenant_code=tenant.code)


# DISABLED — PUT /project-variables/bulk
#
# Unrouted deliberately, not dead by accident. Two reasons:
#
#  1. Nothing calls it. The frontend wires onBulkSaveVariables all the way to
#     ResourceDetailPanel and then never invokes it; the Env tab goes through
#     /resource-variable/save instead. The MCP env-sync flow calls
#     bulk_create_referenced_variables directly, not this route.
#  2. It can strip the write-only flag off a secret. `variables` REPLACES the
#     resource's whole set, so an omitted key is soft-deleted; re-creating that
#     key later (here, or by the provider key sync) makes a fresh row with
#     is_write_only defaulting to false. The flag is meant to be one-way, so an
#     unused endpoint that quietly undoes it is not worth keeping reachable.
#
# Re-enabling means giving the create paths a way to inherit is_write_only from
# a soft-deleted row for the same owner+key. Until then, leave this off.
#
# @router.put(
#     "/bulk",
#     response_model=BulkUpsertVariablesResponse,
#     summary="Bulk upsert variables for a resource (1 AWS read + 1 write)",
# )
# async def bulk_upsert_variables(
#     data: BulkUpsertVariablesRequest,
#     user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
#     db: AsyncSession = Depends(get_db),
# ):
#     """Bulk create/update/delete variables for a single resource.
#     Optionally creates new refs and deletes existing refs in the same call."""
#     user, tenant = user_and_tenant
#
#     if data.application_code:
#         from app.services.workspace_service import WorkspaceService
#         workspace_svc = WorkspaceService(db)
#         if not await workspace_svc.verify_app_workspace_access(user.code, tenant.code, data.application_code):
#             raise HTTPException(status_code=403, detail="Access denied: application workspace is not accessible")
#         from app.services.applications_mst_service import ApplicationsMstService
#         app_svc = ApplicationsMstService(db)
#         if not await app_svc.can_write_for_app(user.code, tenant.code, data.application_code):
#             raise HTTPException(status_code=403, detail="Access denied: read-only role cannot create variables")
#
#     service = ProjectVariablesService(db)
#
#     # 1. Bulk upsert regular variables
#     result = await service.bulk_upsert_variables(request=data, tenant_code=tenant.code)
#
#     # 2. Bulk create new refs (if any)
#     if data.refs_to_add:
#         refs_request = BulkCreateRefsRequest(
#             application_code=data.application_code,
#             environment=data.environment,
#             transaction_code=data.resource_code,
#             table_name=data.resource_type_str,
#             resource_name=data.resource_name,
#             items=data.refs_to_add,
#         )
#         refs_result = await service.bulk_create_variable_refs(
#             request=refs_request, tenant_code=tenant.code
#         )
#         result.refs_created = refs_result.created
#         result.ref_variables = refs_result.variables
#
#     # 3. Bulk delete refs (1 AWS read + 1 write instead of N)
#     if data.refs_to_delete:
#         result.refs_deleted = await service.bulk_delete_variables(
#             variable_ids=data.refs_to_delete, tenant_code=tenant.code
#         )
#
#     return result


@router.post(
    "/bulk-refs",
    response_model=BulkCreateRefsResponse,
    status_code=201,
    summary="Bulk create variable references (same path as single create)",
)
async def bulk_create_variable_refs(
    data: BulkCreateRefsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Bulk create referenced variables — resolves each source value from AWS,
    writes all into the target's consolidated secret in 1 read + 1 write.
    Merges into existing keys (does not remove keys added by previous calls)."""
    user, tenant = user_and_tenant
    service = ProjectVariablesService(db)
    return await service.bulk_create_variable_refs(request=data, tenant_code=tenant.code)


@router.get(
    "/with-values",
    summary="Fetch a resource's variables with all value sources (cloud/devlift/pending) + sync state",
)
async def get_variables_with_values(
    resource_code: str = Query(..., description="Resource code (service_configs.code or infrastructure_mst.code)"),
    resource_type_str: str = Query(..., description="Resource type: SERVICE_CONFIG, INFRASTRUCTURE, etc."),
    environment: str = Query(..., description="Environment: dev, stage, qa, prod"),
    clone_fetch: bool = Query(
        False,
        description="Clone picker mode: exclude the caller's un-deployed drafts so only deployed values are listed",
    ),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Each item = {id, key, type, secret_provider, value} — ready to render directly."""
    user, tenant = user_and_tenant
    service = ProjectVariablesService(db)
    return await service.get_variables_with_values(
        resource_code=resource_code,
        resource_type_str=resource_type_str,
        environment=environment,
        tenant_code=tenant.code,
        user_email=user.email_id,
        user_code=user.code,
        clone_fetch=clone_fetch,
    )


@router.get(
    "/canvas",
    response_model=BulkCanvasVariablesResponse,
    summary="Bulk load all canvas variables for an application",
)
async def get_all_canvas_variables(
    application_code: str = Query(..., description="Application code"),
    environment: str = Query(..., description="Environment (dev, stage, qa, prod)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Bulk load all canvas variables and refs for an application + environment."""
    user, tenant = user_and_tenant

    from app.services.workspace_service import WorkspaceService
    workspace_svc = WorkspaceService(db)
    if not await workspace_svc.verify_app_workspace_access(user.code, tenant.code, application_code):
        return BulkCanvasVariablesResponse(nodeVariables={}, variableRefs={})

    service = ProjectVariablesService(db)
    return await service.get_all_canvas_variables(
        application_code=application_code,
        environment=environment,
        tenant_code=tenant.code,
    )


@router.get(
    "/node/{resource_code}",
    response_model=List[CanvasVariableResponse],
    summary="Get variables for a specific canvas node",
)
async def get_node_variables(
    resource_code: str,
    table_name: str = Query(..., alias="table_name", description="Canvas resource type: service, database, bucket, etc."),
    environment: str = Query(..., description="Environment (dev, stage, qa, prod)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Get all variables for a specific resource node on the canvas."""
    user, tenant = user_and_tenant
    service = ProjectVariablesService(db)
    from app.core.enum import EnvironmentEnum
    env_enum = EnvironmentEnum(environment)
    resolved_table = _resolve_table_name(table_name)
    records = await service.var_repo.get_all_by_transaction(
        table_name=resolved_table,
        transaction_code=resource_code,
        environment=env_enum,
    )
    return [service._to_response(r) for r in records]


@router.get(
    "/{variable_id}/value",
    response_model=CanvasVariableWithValueResponse,
    summary="Get variable value from AWS",
)
async def get_variable_value(
    variable_id: int,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a variable's value on-demand from AWS Secrets Manager."""
    user, tenant = user_and_tenant
    service = ProjectVariablesService(db)
    return await service.get_variable_value(variable_id=variable_id, tenant_code=tenant.code)


@router.put(
    "/{variable_id}",
    response_model=CanvasVariableResponse,
    summary="Update a canvas variable value",
)
async def update_variable(
    variable_id: int,
    data: UpdateCanvasVariableRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Update a variable's value in AWS Secrets Manager."""
    user, tenant = user_and_tenant
    service = ProjectVariablesService(db)
    return await service.update_variable(
        variable_id=variable_id, request=data, tenant_code=tenant.code
    )


@router.delete(
    "/{variable_id}",
    status_code=204,
    summary="Delete a canvas variable",
)
async def delete_variable(
    variable_id: int,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Schedule deletion of a variable in AWS and soft-delete in DB."""
    user, tenant = user_and_tenant
    service = ProjectVariablesService(db)
    await service.delete_variable(variable_id=variable_id, tenant_code=tenant.code)


@router.post(
    "/refs",
    response_model=CanvasVariableRefResponse,
    status_code=201,
    summary="Create a variable reference",
)
async def create_variable_ref(
    data: CreateCanvasVariableRefRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create a variable reference (interpolation link). DB only, no AWS call."""
    user, tenant = user_and_tenant
    service = ProjectVariablesService(db)
    return await service.create_variable_ref(request=data, tenant_code=tenant.code)


@router.delete(
    "/refs/{ref_id}",
    status_code=204,
    summary="Delete a variable reference",
)
async def delete_variable_ref(
    ref_id: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Delete a variable reference. DB only, no AWS call."""
    user, tenant = user_and_tenant
    service = ProjectVariablesService(db)
    await service.delete_variable_ref(ref_code=ref_id, tenant_code=tenant.code)
