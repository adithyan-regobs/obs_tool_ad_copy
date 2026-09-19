"""
Service Configuration API Endpoints

API endpoints for managing service configurations.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from typing import List, Dict, Any

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.authz.security import AuthenticationOnly, Authorization, AuthorizationFromBody, SecureRouter
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.service_config_service import ServiceConfigService
from app.services.permission_cache_service import PermissionCacheService
from app.utils.permission_helper import PermissionHelper
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.repository.service_config_repository import ServiceConfigRepository
from app.schemas.service_config_schemas import (
    ServiceConfigCreate,
    ServiceConfigUpdate,
    ServiceConfigResponse,
    ServiceConfigEnvGeoOptionsResponse,
    ServiceNameResolveRequest,
    ServiceNameResolveResponse,
    SettingsDiffResponse,
    SidecarConfigResponse,
    ListenerPriorityValidationResponse,
    EKSYamlPreviewResponse,
    EKSDryRunRequest,
    EKSDryRunResponse,
    EKSValuesPreviewRequest,
    EKSValuesPreviewResponse,
    EKSSaveFromValuesRequest,
    CloneSettingsRequest,
    TerragruntPreviewRequest,
    TerragruntPreviewResponse,
    WorkflowPreviewRequest,
    WorkflowPreviewResponse,
    validate_service_path
)
from app.schemas.permission_schemas import AllowedEnvironmentsResponse
from app.core.enum import EnvironmentEnum, InfraVendorEnum
from typing import Optional
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sidecars/available", response_model=List[SidecarConfigResponse])
async def get_available_sidecars(
    app_code: str = Query(..., description="Application code"),
    rg_code: str = Query(..., description="Resource group code"),
    environment: EnvironmentEnum = Query(..., description="Environment (dev/staging/prod)"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get available sidecar configurations for dropdown selection.

    Args:
        app_code: Application code
        rg_code: Resource group code
        environment: Environment (dev/staging/prod)
        db: Database session
        current_user_tenant: Current user and tenant

    Returns:
        List of available sidecar configurations
    """
    service = ServiceConfigService(db)
    return await service.get_available_sidecars(
        app_code=app_code,
        rg_code=rg_code,
        environment=environment.value
    )


@router.get("/validate-listener-priority", response_model=ListenerPriorityValidationResponse)
async def validate_listener_priority(
    priority: str = Query(..., description="Listener rule priority to validate"),
    environment: str = Query(..., description="Environment (dev/stage/prod)"),
    application_code: str = Query(..., description="Application code for scope filtering"),
    geo_loc_mst_code: str = Query(..., description="Region code for scope filtering"),
    exclude_service_code: str = Query(None, description="Service code to exclude (for update validation)"),
    service_type: str = Query(None, description="Service type (API/BACKGROUND_SERVICE/OPS_TOOLS) for ALB scoping"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Validate if a listener rule priority is available.

    This endpoint checks if a listener_rule_priority is already in use by another service.
    Priority is scoped by tenant + environment + application + region + ALB type.
    - Prod: Check only against other prod configs (same application + region)
    - Dev/Stage: Share same pool within same region - can't have same priority in both
    - OPS_TOOLS: Uses separate ALB, only conflicts with other OPS_TOOLS
    - API/BACKGROUND_SERVICE: Share the same ALB

    Used by frontend for real-time inline validation.

    Args:
        priority: The listener rule priority to validate (1-50000)
        environment: Environment being validated (dev/stage/prod)
        application_code: Application code for scope filtering
        geo_loc_mst_code: Region code for scope filtering
        exclude_service_code: Service code to exclude from check (for update scenarios)
        service_type: Service type for ALB scoping (OPS_TOOLS uses different ALB)

    Returns:
        ListenerPriorityValidationResponse with valid status and message
    """
    _, tenant = current_user_tenant
    service = ServiceConfigService(db)
    result = await service.check_listener_priority_availability(
        tenant_code=tenant.code,
        listener_priority=priority,
        environment=environment,
        application_code=application_code,
        geo_loc_mst_code=geo_loc_mst_code,
        exclude_service_code=exclude_service_code,
        service_type=service_type
    )

    return ListenerPriorityValidationResponse(
        valid=result["valid"],
        priority=result["priority"],
        message=result["message"],
        used_by=result.get("used_by")
    )


@router.get("/allowed-environments/{service_code}", response_model=AllowedEnvironmentsResponse)
async def get_allowed_environments(
    service_code: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get list of environments user has permission for a specific service.

    Used by frontend to filter environment dropdown in Deployment tab.

    Args:
        service_code: Service code
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        AllowedEnvironmentsResponse with service_code and list of environments
    """
    user, tenant = current_user_tenant

    # Check if user is org_owner (has access to all environments)
    perm_helper = PermissionHelper(db)
    if await perm_helper.is_org_owner(user):
        environments = resource_meta_repo.get_all_environment_values()
    else:
        cache_service = PermissionCacheService(db)
        environments = await cache_service.get_allowed_environments(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code,
            service_mst_code=service_code
        )

    return AllowedEnvironmentsResponse(
        service_mst_code=service_code,
        environments=environments
    )


@router.get("/env-geo-options/{service_code}", response_model=ServiceConfigEnvGeoOptionsResponse)
async def get_service_config_env_geo_options(
    service_code: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    List every (environment, geo loc, cluster) combination a service has
    active service_config rows for.

    Used by the resource detail panel (EKS/ECS) context bar to build the
    environment / geo location / cluster dropdowns from the service's actual
    configurations instead of the full master lists.

    Args:
        service_code: services_mst.code (path param)
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        ServiceConfigEnvGeoOptionsResponse with one option per combination
    """
    _, tenant = current_user_tenant
    service = ServiceConfigService(db)
    return await service.get_env_geo_options(
        tenant_code=tenant.code,
        service_code=service_code,
    )


@router.post("/resolve-service-names", response_model=ServiceNameResolveResponse)
async def resolve_service_names(
    payload: ServiceNameResolveRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
) -> ServiceNameResolveResponse:
    """
    Resolve a batch of service_configs codes to their owning service's name.

    A literal join from service_configs to services_mst. The authz console calls
    it to label the store objects it lists: those objects ARE service_configs
    codes, and nothing in the code string is a reliable source for the name.

    Batched because the console needs every visible row named at once. Codes
    belonging to another tenant, or naming a row that no longer exists, come
    back under `unresolved` — not an error, just a fact about a store that is
    written independently of this database. Soft-deleted rows DO resolve, marked
    with is_deleted, since a tuple pointing at one is what needs revoking.

    Args:
        payload: the service_configs codes to resolve (max 2000)
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        ServiceNameResolveResponse with resolved names and unresolved codes
    """
    _, tenant = current_user_tenant
    service = ServiceConfigService(db)
    return await service.resolve_service_names(
        tenant_code=tenant.code,
        config_codes=payload.codes,
    )


@router.get("/{code}/settings-diff", response_model=SettingsDiffResponse)
async def get_service_config_settings_diff(
    code: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
) -> SettingsDiffResponse:
    """Settings config drift for the redeploy diff modal.

    Compares the service's current config against the config_snapshot of its
    latest DEPLOYED queue row. Empty items => nothing pending. The AWS live
    value is not read yet; the UI renders a placeholder for it."""
    _, tenant = current_user_tenant
    service = ServiceConfigService(db)
    return await service.get_settings_diff(code=code, tenant_code=tenant.code)


@router.get("/{service_code}", response_model=ServiceConfigResponse)
async def get_service_config(
    service_code: str,
    environment: EnvironmentEnum = Query(..., description="Environment (dev/staging/prod)"),
    geo_loc_code: str = Query(..., description="Geographic location code (e.g., mumbai, uk)"),
    alb_selection: str = Query("existing_alb", description="ALB type (no_alb/existing_alb/create_new_alb)"),
    infra_vendor: Optional[InfraVendorEnum] = Query(None, description="Infrastructure vendor (aws/azure/gcp/on_prem)"),
    infrastructure_type: Optional[str] = Query(None, description="Infrastructure type code (e.g., ecs_fargate_infrastructuretype_ref)"),
    infrastructure_mst_code: Optional[str] = Query(None, description="Infrastructure instance (cluster) code"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get service configuration by service code, environment, region, ALB type, and optionally vendor/infra type/cluster.

    Args:
        service_code: Service code
        environment: Environment (dev/staging/prod)
        geo_loc_code: Geographic location code
        alb_selection: ALB type (no_alb/existing_alb/create_new_alb)
        infra_vendor: Infrastructure vendor (aws/azure/gcp/on_prem) - optional filter
        infrastructure_type: Infrastructure type code - optional filter
        infrastructure_mst_code: Infrastructure instance (cluster) code - optional filter
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        Service configuration

    Raises:
        HTTPException 403: If user doesn't have permission for this service/environment
        HTTPException 404: If configuration not found
    """
    user, tenant = current_user_tenant

    # Check if user is org_owner (bypasses permission checks)
    perm_helper = PermissionHelper(db)
    if not await perm_helper.is_org_owner(user):
        # Not org_owner - check permission for service + environment
        cache_service = PermissionCacheService(db)
        allowed_environments = await cache_service.get_allowed_environments(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code,
            service_mst_code=service_code
        )

        if environment.value not in allowed_environments:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have permission to access service '{service_code}' in '{environment.value}' environment"
            )

    service = ServiceConfigService(db)
    config = await service.get_service_config(
        tenant_code=tenant.code,
        service_code=service_code,
        environment=environment.value,
        geo_loc_code=geo_loc_code,
        alb_selection=alb_selection,
        infra_vendor=infra_vendor.value if infra_vendor else None,
        infrastructure_type=infrastructure_type,
        infrastructure_mst_code=infrastructure_mst_code
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No configuration found for service {service_code} in {environment.value} environment, geo_loc {geo_loc_code}, ALB type {alb_selection}"
        )

    return config


@router.post("", response_model=ServiceConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_service_config(
    service_config_data: ServiceConfigCreate,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Create a new service configuration.

    Args:
        service_config_data: Service configuration data
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        Created service configuration

    Raises:
        HTTPException 403: If user doesn't have permission for this service/environment
        HTTPException 400: If configuration already exists or validation fails
    """
    user, tenant = current_user_tenant

    # Check if user is org_owner (bypasses permission checks)
    perm_helper = PermissionHelper(db)
    if not await perm_helper.is_org_owner(user):
        # Not org_owner - check can_write permission for service + environment
        has_write = await perm_helper.check_write_access(
            user, tenant,
            service_config_data.services_mst_code,
            service_config_data.environment
        )

        if not has_write:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have write permission for service '{service_config_data.services_mst_code}' in '{service_config_data.environment.value}' environment"
            )

    service = ServiceConfigService(db)
    return await service.create_service_config(
        tenant.code,
        service_config_data,
        user_email=user.email_id,
        user_code=user.code
    )


@router.post("/upsert-service-config", response_model=ServiceConfigResponse)
async def upsert_service_config(
    service_config_data: ServiceConfigCreate,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Save a service configuration (upsert - create if not exists, update if exists).

    This endpoint saves the configuration to database WITHOUT triggering:
    - GitHub operations (PR creation)
    - Terragrunt HCL generation
    - GitOps workflow records
    - Pipeline sync
    - Dockerfile workflow

    This is essentially a "Save Draft" functionality.

    Args:
        service_config_data: Service configuration data
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        Created or updated service configuration

    Raises:
        HTTPException 403: If user doesn't have permission for this service/environment
        HTTPException 400: If validation fails
    """
    user, tenant = current_user_tenant

    # Check if user is org_owner (bypasses permission checks)
    perm_helper = PermissionHelper(db)
    if not await perm_helper.is_org_owner(user):
        # Not org_owner - check can_write permission for service + environment
        has_write = await perm_helper.check_write_access(
            user, tenant,
            service_config_data.services_mst_code,
            service_config_data.environment
        )

        if not has_write:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have write permission for service '{service_config_data.services_mst_code}' in '{service_config_data.environment.value}' environment"
            )

    service = ServiceConfigService(db)
    return await service.upsert_service_config(
        tenant.code,
        service_config_data,
        user_email=user.email_id,
        user_code=user.code
    )


# ── FGA-era split of the upsert (ECS/EKS settings forms) ─────────────────────
# The upsert above stays untouched for legacy callers. These two routes are the
# same save split by intent, because an access card is fixed at import time and
# cannot branch on "did the config already exist?":
#   - create: no FGA check (per product decision) — the object does not exist
#     yet, so there is no tuple to check. Refuses to update (409) so it cannot
#     be used to bypass the update gate.
#   - update: keyed by the config code in the PATH so the stock Authorization
#     card can check can_write_settings on service:<code> BEFORE the handler
#     runs. Identity fields are then taken from the fetched row, not the body,
#     so the payload cannot re-point the write at a config the caller was not
#     authorized on (confused-deputy guard). The row's combination is what
#     upsert_service_config re-resolves, so it deterministically takes its
#     update branch — the battle-tested save path the settings forms already
#     exercise, unchanged.
secure_router = SecureRouter()


@secure_router.post(
    "/create-service-config",
    response_model=ServiceConfigResponse,
    access=AuthenticationOnly(
        reason="creation is deliberately not FGA-gated; the config does not "
        "exist yet, so there is no object to check — legacy PermissionHelper "
        "check kept for parity with the upsert"
    ),
)
async def create_service_config(
    service_config_data: ServiceConfigCreate,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Create-only half of the upsert. 409s if the combination already resolves
    to an existing config — updating is the carded route's job."""
    user, tenant = current_user_tenant

    perm_helper = PermissionHelper(db)
    if not await perm_helper.is_org_owner(user):
        has_write = await perm_helper.check_write_access(
            user, tenant,
            service_config_data.services_mst_code,
            service_config_data.environment
        )
        if not has_write:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have write permission for service '{service_config_data.services_mst_code}' in '{service_config_data.environment.value}' environment"
            )

    alb_selection = "existing_alb"
    if service_config_data.config:
        alb_selection = service_config_data.config.alb_selection or "existing_alb"
    config_repo = ServiceConfigRepository(db)
    existing = await config_repo.get_by_tenant_service_env_geo_loc(
        tenant.code,
        service_config_data.services_mst_code,
        service_config_data.environment.value,
        service_config_data.geo_loc_mst_code,
        alb_selection,
        infra_vendor=service_config_data.infra_vendor_enum.value if service_config_data.infra_vendor_enum else None,
        infrastructure_type=service_config_data.infrastructuretype_ref_code,
        infrastructure_mst_code=service_config_data.infrastructure_mst_code
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Service configuration already exists (code {existing.code}) — use the update endpoint"
        )

    service = ServiceConfigService(db)
    return await service.upsert_service_config(
        tenant.code,
        service_config_data,
        user_email=user.email_id,
        user_code=user.code
    )


@secure_router.get(
    "/by-code/{code}",
    response_model=ServiceConfigResponse,
    access=Authorization(
        permission="can_view_settings", obj_type="service", param="code", deny_status=403
    ),
)
async def get_service_config_by_code(
    code: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Fetch a service config by its own code — the card already checked
    can_view_settings on service:<code> before this ran. FGA only: the legacy
    PermissionCacheService env check stays on the combination route; this
    route is the new permission system, and per-environment granularity is
    inherent in the code itself (each config code IS env+geo specific).

    Delegates to the same get_service_config used by the combination route
    (sidecar enrichment included), so both return identical responses."""
    user, tenant = current_user_tenant

    config_repo = ServiceConfigRepository(db)
    row = await config_repo.get_by_code_and_tenant(code, tenant.code)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service configuration with code {code} not found"
        )

    service = ServiceConfigService(db)
    config = await service.get_service_config(
        tenant_code=tenant.code,
        service_code=row.services_mst_code,
        environment=row.environment.value if hasattr(row.environment, "value") else row.environment,
        geo_loc_code=row.geo_loc_mst_code,
        alb_selection=row.alb_selection,
        infra_vendor=row.infra_vendor_enum.value if row.infra_vendor_enum else None,
        infrastructure_type=row.infrastructuretype_ref_code,
        infrastructure_mst_code=row.infrastructure_mst_code,
    )
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service configuration with code {code} not found"
        )
    return config


@secure_router.put(
    "/update-service-config/{code}",
    response_model=ServiceConfigResponse,
    access=Authorization(
        permission="can_write_settings", obj_type="service", param="code", deny_status=403
    ),
)
async def update_service_config_by_code(
    code: str,
    service_config_data: ServiceConfigCreate,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Update-only half of the upsert. The card already checked
    can_write_settings on service:<code> before this ran."""
    user, tenant = current_user_tenant

    config_repo = ServiceConfigRepository(db)
    existing = await config_repo.get_by_code_and_tenant(code, tenant.code)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service configuration with code {code} not found"
        )
    if service_config_data.config is None:
        # Without a config block the upsert would default alb_selection to
        # existing_alb — for a row whose alb differs, the lookup would miss
        # and CREATE a duplicate instead of updating. Refuse instead.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="config is required when updating a service configuration"
        )

    # Identity comes from the authorized row, never the body — the payload
    # cannot re-point this write at a different config.
    service_config_data.services_mst_code = existing.services_mst_code
    service_config_data.environment = existing.environment
    service_config_data.geo_loc_mst_code = existing.geo_loc_mst_code
    service_config_data.infra_vendor_enum = existing.infra_vendor_enum
    service_config_data.infrastructuretype_ref_code = existing.infrastructuretype_ref_code
    service_config_data.infrastructure_mst_code = existing.infrastructure_mst_code
    service_config_data.config.alb_selection = existing.alb_selection

    service = ServiceConfigService(db)
    return await service.upsert_service_config(
        tenant.code,
        service_config_data,
        user_email=user.email_id,
        user_code=user.code
    )


@router.put("/{code}", response_model=ServiceConfigResponse)
async def update_service_config(
    code: str,
    service_config_data: ServiceConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Update an existing service configuration.

    Args:
        code: Service configuration code
        service_config_data: Service configuration update data
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        Updated service configuration

    Raises:
        HTTPException 403: If user doesn't have permission for this service/environment
        HTTPException 404: If configuration not found
        HTTPException 400: If validation fails or ALB selection change attempted
    """
    user, tenant = current_user_tenant

    # First fetch existing config to get service_mst_code and environment
    config_repo = ServiceConfigRepository(db)
    existing_config = await config_repo.get_by_code_and_tenant(code, tenant.code)

    if not existing_config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service configuration with code {code} not found"
        )

    # Check if user is org_owner (bypasses permission checks)
    perm_helper = PermissionHelper(db)
    if not await perm_helper.is_org_owner(user):
        # Not org_owner - check can_write permission for service + environment
        has_write = await perm_helper.check_write_access(
            user, tenant,
            existing_config.services_mst_code,
            existing_config.environment
        )

        if not has_write:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have write permission for service '{existing_config.services_mst_code}' in '{existing_config.environment.value}' environment"
            )

    service = ServiceConfigService(db)
    return await service.update_service_config(
        tenant.code,
        code,
        service_config_data,
        user_email=user.email_id,
        user_code=user.code
    )


@router.get("/eks/preview/{service_code}", response_model=EKSYamlPreviewResponse)
async def preview_eks_yaml(
    service_code: str,
    environment: EnvironmentEnum = Query(..., description="Environment (dev/staging/prod)"),
    geo_loc_code: str = Query(..., description="Geographic location code"),
    infrastructure_mst_code: Optional[str] = Query(None, description="Infrastructure instance (cluster) code"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Preview generated EKS YAML files before syncing to GitHub.

    Returns the generated config.yaml, workflow.yml, and deployment.yaml content
    along with the file paths where they would be pushed.

    This endpoint does NOT push to GitHub - use it to preview what will be generated.

    Args:
        service_code: Service code
        environment: Environment (dev/staging/prod)
        geo_loc_code: Geographic location code
        infrastructure_mst_code: Infrastructure instance (cluster) code - optional
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        EKSYamlPreviewResponse with generated YAML content and file paths

    Raises:
        HTTPException 403: If user doesn't have permission for this service/environment
        HTTPException 404: If configuration not found
        HTTPException 400: If service is not EKS type
    """
    user, tenant = current_user_tenant

    # Check if user is org_owner (bypasses permission checks)
    perm_helper = PermissionHelper(db)
    if not await perm_helper.is_org_owner(user):
        cache_service = PermissionCacheService(db)
        allowed_environments = await cache_service.get_allowed_environments(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code,
            service_mst_code=service_code
        )

        if environment.value not in allowed_environments:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have permission to access service '{service_code}' in '{environment.value}' environment"
            )

    # Get service config
    config_repo = ServiceConfigRepository(db)
    service_config = await config_repo.get_by_service_and_environment(
        tenant_code=tenant.code,
        service_code=service_code,
        environment=environment.value,
        geo_loc_code=geo_loc_code,
        infrastructure_mst_code=infrastructure_mst_code
    )

    if not service_config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No configuration found for service {service_code} in {environment.value} environment"
        )

    # Check if this is an EKS infrastructure type
    if not service_config.infrastructuretype_ref_code or "eks" not in service_config.infrastructuretype_ref_code.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Service {service_code} is not configured for EKS. Infrastructure type: {service_config.infrastructuretype_ref_code}"
        )

    # Get service model
    from app.repository.services_mst_repository import ServicesMstRepository
    services_repo = ServicesMstRepository(db)
    service = await services_repo.get_by_code(service_code)

    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service {service_code} not found"
        )

    # Generate preview using EKS pipeline service
    from app.services.eks_pipeline_service import EKSPipelineService
    eks_service = EKSPipelineService(db)

    result = await eks_service.preview_eks_yaml(
        service_config=service_config,
        service=service,
        environment=environment.value
    )

    return EKSYamlPreviewResponse(
        service_name=result["service_name"],
        environment=result["environment"],
        language=result["language"],
        config_file_path=result["config_file_path"],
        workflow_file_path=result["workflow_file_path"],
        deployment_file_path=result["deployment_file_path"],
        config_yaml=result["config_yaml"],
        workflow_yaml=result["workflow_yaml"],
        deployment_yaml=result["deployment_yaml"]
    )


@router.post("/eks/dry-run", response_model=EKSDryRunResponse)
async def eks_dry_run(
    request: EKSDryRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Generate EKS dry-run preview from form data without saving.

    This endpoint accepts the current form configuration and generates:
    - values.yaml: A Helm-style values file showing all configuration parameters
    - deployment.yaml: The generated Kubernetes manifests (Service, Deployment/Rollout, Ingress, HPA)

    Use this for real-time preview as the user fills the form (like Devtron's dry-run feature).

    Args:
        request: EKS configuration from the form
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        EKSDryRunResponse with values.yaml and deployment.yaml content
    """
    from app.services.eks_dry_run_service import EKSDryRunService

    user, tenant = current_user_tenant

    dry_run_service = EKSDryRunService(db)
    result = await dry_run_service.generate_dry_run(
        request=request,
        tenant_code=tenant.code
    )

    return EKSDryRunResponse(
        service_name=result["service_name"],
        environment=result["environment"],
        language=result["language"],
        values_yaml=result["values_yaml"],
        deployment_yaml=result["deployment_yaml"],
        config_yaml=result.get("config_yaml"),
        workflow_yaml=result.get("workflow_yaml")
    )


@router.post("/eks/values-preview", response_model=EKSValuesPreviewResponse)
async def eks_values_preview(
    request: EKSValuesPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Regenerate deployment.yaml from edited values.yaml content.

    This endpoint enables live preview - when the user edits the values.yaml in the UI,
    call this endpoint to regenerate the deployment.yaml in real-time.

    Args:
        request: Contains service_name, namespace, environment, and edited values_yaml
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        EKSValuesPreviewResponse with regenerated deployment.yaml content
    """
    from app.services.eks_dry_run_service import EKSDryRunService

    user, tenant = current_user_tenant

    dry_run_service = EKSDryRunService(db)

    try:
        deployment_strategy = None
        if request.deployment_strategy:
            deployment_strategy = request.deployment_strategy.model_dump()

        deployment_yaml = await dry_run_service.generate_deployment_from_values(
            service_name=request.service_name,
            namespace=request.namespace,
            environment=request.environment.value,
            values_yaml=request.values_yaml,
            deployment_strategy=deployment_strategy
        )

        return EKSValuesPreviewResponse(deployment_yaml=deployment_yaml)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/eks/save-from-values", response_model=ServiceConfigResponse)
async def eks_save_config_from_values(
    request: EKSSaveFromValuesRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Save edited values.yaml back to service configuration in DB.

    This enables bi-directional sync: when user edits values.yaml in the dry-run UI,
    this endpoint parses the YAML and updates the corresponding service_config record.

    Args:
        request: Contains code and values_yaml
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        Updated ServiceConfigResponse

    Raises:
        HTTPException 400: If YAML is invalid
        HTTPException 403: If user doesn't have write permission
        HTTPException 404: If configuration not found
    """
    import yaml
    from app.services.eks_dry_run_service import EKSDryRunService

    user, tenant = current_user_tenant

    # Find existing configuration with tenant filtering
    config_repo = ServiceConfigRepository(db)
    existing_config = await config_repo.get_by_code_and_tenant(request.code, tenant.code)

    if not existing_config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service configuration with code {code} not found"
        )

    # Check write permission
    perm_helper = PermissionHelper(db)
    if not await perm_helper.is_org_owner(user):
        has_write = await perm_helper.check_write_access(
            user, tenant,
            existing_config.services_mst_code,
            existing_config.environment
        )

        if not has_write:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have write permission for this service configuration"
            )

    # Parse values.yaml and extract config fields
    logger.info(f"📥 Received values.yaml (first 500 chars): {request.values_yaml[:500]}")

    try:
        values = yaml.safe_load(request.values_yaml)
    except yaml.YAMLError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid YAML format: {e}"
        )

    if not values:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty values.yaml content"
        )

    logger.info(f"📊 Parsed values dict: {values}")

    # Extract configuration from values.yaml
    resources = values.get("resources", {})
    autoscaling = values.get("autoscaling", {})
    liveness_probe = values.get("LivenessProbe", {})
    readiness_probe = values.get("ReadinessProbe", {})
    container_ports = values.get("ContainerPort", [])
    secret_config = values.get("secret", {})

    # Extract container port from ContainerPort array
    # Handle both new format (list of ints) and old format (list of dicts)
    container_port = None
    if container_ports and len(container_ports) > 0:
        if isinstance(container_ports[0], dict):
            container_port = str(container_ports[0].get("port")) if container_ports[0].get("port") else None
        elif isinstance(container_ports[0], int):
            container_port = str(container_ports[0])

    # Helper function to normalize resource values (add units if missing)
    def normalize_resource(value: str, resource_type: str) -> str:
        """Add units if missing: 'm' for CPU, 'Mi' for memory"""
        if not value:
            return value
        value_str = str(value).strip()

        if resource_type == "cpu":
            # If it's a plain number, add 'm' unit
            if value_str.isdigit():
                return f"{value_str}m"
            return value_str
        elif resource_type == "memory":
            # If it's a plain number, add 'Mi' unit
            if value_str.isdigit():
                return f"{value_str}Mi"
            return value_str
        return value_str

    # Update the config JSONB field
    if existing_config.config is None:
        existing_config.config = {}

    # Update resource allocation with normalization
    logger.info(f"Parsing resources from values.yaml: {resources}")

    if resources.get("requests", {}).get("cpu"):
        raw_value = str(resources["requests"]["cpu"])
        normalized = normalize_resource(raw_value, "cpu")
        existing_config.config["cpu_requested"] = normalized
        logger.info(f"CPU requested: {raw_value} → {normalized}")

    if resources.get("limits", {}).get("cpu"):
        raw_value = str(resources["limits"]["cpu"])
        normalized = normalize_resource(raw_value, "cpu")
        existing_config.config["cpu_limit"] = normalized
        logger.info(f"CPU limit: {raw_value} → {normalized}")

    if resources.get("requests", {}).get("memory"):
        raw_value = str(resources["requests"]["memory"])
        normalized = normalize_resource(raw_value, "memory")
        existing_config.config["memory_requested"] = normalized
        logger.info(f"Memory requested: {raw_value} → {normalized}")

    if resources.get("limits", {}).get("memory"):
        raw_value = str(resources["limits"]["memory"])
        normalized = normalize_resource(raw_value, "memory")
        existing_config.config["memory_limit"] = normalized
        logger.info(f"Memory limit: {raw_value} → {normalized}")

    # Update port and health
    if container_port:
        existing_config.config["port"] = container_port
    if liveness_probe and liveness_probe.get("Path"):
        existing_config.config["health"] = liveness_probe["Path"]

    # Update namespace (store in config JSONB)
    namespace = values.get("namespace")
    if namespace:
        existing_config.config["namespace"] = namespace
        logger.info(f"📝 Namespace saved to config JSONB: {namespace}")
    else:
        # Remove namespace from config if it was cleared
        if "namespace" in existing_config.config:
            del existing_config.config["namespace"]
            logger.info(f"📝 Namespace removed from config JSONB")

    # Update service_path (store in config JSONB) — only when the YAML carries
    # it, like every other field here. The old else-branch overwrote the stored
    # path with "/" whenever ServicePath was absent from the pasted YAML, so one
    # pass through the values-editor silently reset a saved "/api/v1" to "/".
    # A YAML that says nothing about ServicePath is not asking to change it;
    # clearing the path is the settings form's job.
    service_path = values.get("ServicePath")
    if service_path:
        try:
            # EKS route: a trailing /* is invalid on a Kubernetes ingress, so
            # this is one of the places the rule genuinely applies.
            service_path = validate_service_path(
                str(service_path), reject_slash_wildcard=True
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        existing_config.config["service_path"] = service_path
        logger.info(f"📝 ServicePath saved to config JSONB: {service_path}")

    # Update HPA settings
    if autoscaling.get("enabled"):
        if existing_config.config.get("hpa") is None:
            existing_config.config["hpa"] = {}
        existing_config.config["hpa"]["enabled"] = True
        if autoscaling.get("MinReplicas") is not None:
            existing_config.config["hpa"]["min_replicas"] = str(autoscaling["MinReplicas"])
        if autoscaling.get("MaxReplicas") is not None:
            existing_config.config["hpa"]["max_replicas"] = str(autoscaling["MaxReplicas"])
        if autoscaling.get("TargetCPUUtilizationPercentage") is not None:
            existing_config.config["hpa"]["cpu_threshold"] = str(autoscaling["TargetCPUUtilizationPercentage"])
        if autoscaling.get("TargetMemoryUtilizationPercentage") is not None:
            existing_config.config["hpa"]["memory_threshold"] = str(autoscaling["TargetMemoryUtilizationPercentage"])
    else:
        if existing_config.config.get("hpa"):
            existing_config.config["hpa"]["enabled"] = False
        # Update static replica count
        replica_count = values.get("replicaCount")
        if replica_count is not None:
            existing_config.config["replica_count"] = str(replica_count)

    # Update secrets configuration
    if secret_config.get("enabled") is not None:
        existing_config.config["secrets_enabled"] = secret_config["enabled"]
    if secret_config.get("data"):
        secret_keys = list(secret_config["data"].keys())
        existing_config.config["secret_keys"] = ",".join(secret_keys)

    # Update deployment strategy from values if present
    # The strategy can be in values.yaml in two formats:
    # 1. Nested under "deploymentStrategy" key
    # 2. At root level with "strategy" key and "canary"/"blueGreen"/"rolling" objects
    logger.info(f"🎯 Checking for deployment strategy in values.yaml...")
    logger.info(f"   - deploymentStrategy key: {values.get('deploymentStrategy')}")
    logger.info(f"   - strategy key: {values.get('strategy')}")
    logger.info(f"   - canary key: {values.get('canary')}")
    logger.info(f"   - blueGreen key: {values.get('blueGreen')}")

    deployment_strategy_values = values.get("deploymentStrategy")
    strategy_type = values.get("strategy", {}).get("type") if isinstance(values.get("strategy"), dict) else None

    if deployment_strategy_values:
        # Format 1: Nested under deploymentStrategy key
        if existing_config.deployment_strategy is None:
            existing_config.deployment_strategy = {}
        existing_config.deployment_strategy["strategy"] = deployment_strategy_values.get("type", "rolling")
        if deployment_strategy_values.get("canary"):
            existing_config.deployment_strategy["canary"] = deployment_strategy_values["canary"]
        if deployment_strategy_values.get("blueGreen"):
            existing_config.deployment_strategy["blueGreen"] = deployment_strategy_values["blueGreen"]
        if deployment_strategy_values.get("rolling"):
            existing_config.deployment_strategy["rolling"] = deployment_strategy_values["rolling"]
    elif strategy_type or values.get("canary") or values.get("blueGreen"):
        # Format 2: Root level strategy fields
        if existing_config.deployment_strategy is None:
            existing_config.deployment_strategy = {}

        # Get strategy type from strategy.type or infer from presence of canary/blueGreen
        if strategy_type:
            existing_config.deployment_strategy["strategy"] = strategy_type
        elif values.get("canary"):
            existing_config.deployment_strategy["strategy"] = "canary"
        elif values.get("blueGreen"):
            existing_config.deployment_strategy["strategy"] = "blueGreen"
        else:
            existing_config.deployment_strategy["strategy"] = "rolling"

        # Copy canary/blueGreen configuration
        if values.get("canary"):
            existing_config.deployment_strategy["canary"] = values["canary"]
        if values.get("blueGreen"):
            existing_config.deployment_strategy["blueGreen"] = values["blueGreen"]
    else:
        # Check for rolling strategy fields (MaxSurge, MaxUnavailable)
        max_surge = values.get("MaxSurge")
        max_unavailable = values.get("MaxUnavailable")
        if max_surge is not None or max_unavailable is not None:
            if existing_config.deployment_strategy is None:
                existing_config.deployment_strategy = {}
            existing_config.deployment_strategy["strategy"] = "rolling"
            if existing_config.deployment_strategy.get("rolling") is None:
                existing_config.deployment_strategy["rolling"] = {}
            if max_surge is not None:
                existing_config.deployment_strategy["rolling"]["maxSurge"] = max_surge
            if max_unavailable is not None:
                existing_config.deployment_strategy["rolling"]["maxUnavailable"] = max_unavailable

    # Mark as pending sync since we modified the config
    existing_config.sync_status = "PENDING_SYNC"

    # CRITICAL: Flag the JSONB column as modified so SQLAlchemy persists changes
    attributes.flag_modified(existing_config, "config")
    if existing_config.deployment_strategy:
        attributes.flag_modified(existing_config, "deployment_strategy")

    logger.info(f"🔄 Saving config to database...")
    logger.info(f"📝 Updated config fields: {existing_config.config}")
    logger.info(f"🏷️  Service config code: {existing_config.code}")

    # Save to database
    db.add(existing_config)
    await db.commit()
    await db.refresh(existing_config)

    logger.info(f"✅ Database save completed successfully")
    logger.info(f"📦 Namespace in config JSONB: {existing_config.config.get('namespace')}")
    logger.info(f"📦 Final config in DB: {existing_config.config}")

    # Return updated config using service
    service = ServiceConfigService(db)
    response = await service._build_service_config_response(existing_config)

    logger.info(f"📤 Returning response to frontend with namespace: {response.namespace}")
    logger.info(f"📤 Returning response to frontend with config: {response.config}")
    return response


# Clone copies the full source config "blueprint" onto the target EXCEPT the
# keys below, which are either identity the target owns or unique-constrained
# infra that would break/collide if overwritten:
#   - repo/branch: the target keeps its own source repo
#   - placement: env/geo/cluster is chosen by the target, not the source
#   - ALB/listener: listener_rule_priority must stay unique on the target's ALB
#   - endpoints: runtime-derived, per-instance
#   - service identity: name/type belong to the target (cross-type is blocked)
#   - env_variables: cloned via the dedicated /resource-variable/clone flow
_CLONE_EXCLUDED_CONFIG_KEYS = frozenset({
    # repo / branch
    "repository", "branches", "selected_branches",
    # placement — belongs to the target's env/geo/cluster
    "cluster_arn", "cluster_name", "cloud_region_id", "region",
    "subnet_ids", "vpc_id", "namespace",
    # ALB / listener — unique per target ALB
    "listener_rule_priority", "alb_url", "alb_selection", "alb_schema", "ingress_group_order",
    # runtime-derived endpoints + per-target routing (service_path is derived
    # from the target's own service name; cloning the source's would misroute it)
    "host_ip", "endpoint_url", "service_path",
    # service identity + DB identity — must never ride into the target's config,
    # or they surface as bogus diff rows and pollute the deploy baseline.
    "service_name", "service_type", "id", "code", "services_mst_code",
    # env vars have their own clone flow
    "env_variables",
    # backend-written at creation, never a user setting: launch_type is fixed
    # by the target's own cluster, the rest are model-serving artifacts derived
    # from the target's own model (and re-enriched from the target row by
    # add_item_to_queue anyway).
    "launch_type", "efs_path", "model_revision", "download_job_name", "ecr_registry",
})


@secure_router.post(
    "/clone-settings",
    response_model=ServiceConfigResponse,
    # Clone WRITES the target and READS the source, so both are gated here —
    # the same FGA relations a settings save (can_write_settings) and a settings
    # read (can_view_settings) use. Both object ids live in the body. Replaces
    # the legacy cache-based check that diverged from the approval flow and let a
    # user without can_write_settings write through clone.
    access=AuthorizationFromBody(checks=(
        ("can_write_settings", "service", "target_config_code"),
        ("can_view_settings", "service", "source_config_code"),
    )),
)
async def clone_service_config_settings(
    request: CloneSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Duplicate a source service_config's settings onto a target one, using the
    source as a blueprint: the full source config is copied EXCEPT the keys in
    _CLONE_EXCLUDED_CONFIG_KEYS (repo/branch, placement, ALB/listener, endpoints,
    service identity, env_variables), which the target owns or that would collide.
    Only what the settings UI shows is cloned — deployment_strategy and
    log_provider are deliberately NOT copied (no canvas field; the target keeps
    its own defaults). Fully draft-only: NOTHING is written to the live row —
    everything lands in the update_service queue draft and is applied to
    service_configs at deploy. Env variables are cloned separately via
    POST /resource-variable/clone."""
    user, tenant = current_user_tenant

    if request.source_config_code == request.target_config_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source and target configurations are the same"
        )

    config_repo = ServiceConfigRepository(db)
    source = await config_repo.get_by_code_and_tenant(request.source_config_code, tenant.code)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source configuration {request.source_config_code} not found"
        )
    target = await config_repo.get_by_code_and_tenant(request.target_config_code, tenant.code)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target configuration {request.target_config_code} not found"
        )

    # Portable keys are infra-type-shaped (EKS hpa/requests vs ECS cpu/ram) —
    # cross-type cloning would merge wrong-shaped settings.
    if source.infrastructuretype_ref_code != target.infrastructuretype_ref_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Cross-infrastructure clone is not supported: source is "
                f"{source.infrastructuretype_ref_code}, target is {target.infrastructuretype_ref_code}"
            )
        )

    # Authorization is done by the route card (can_write_settings on the target,
    # can_view_settings on the source) before this handler runs.

    # Lane-lock, like Save: one live request per service. If a change is already
    # submitted or approved, cloning would rewrite content under review — refuse
    # and let it finish rather than stack a second live request.
    from app.repository.approval_repository import ApprovalRepository

    live_request = await ApprovalRepository(db).find_live_for_resource(target.code, tenant.code)
    if live_request is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{target.code} already has a change "
                f"{getattr(live_request.status, 'value', live_request.status)} — finish its "
                f"review before cloning into this service."
            ),
        )

    portable = {
        k: v for k, v in dict(source.config or {}).items()
        if k not in _CLONE_EXCLUDED_CONFIG_KEYS
    }
    if not portable and source.language_ref_code is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The selected source has no configured settings to clone. Choose a configuration that has been set up."
        )
    # Draft-only, like Save: EVERYTHING is staged in the queue row and applied
    # to service_configs at DEPLOY, never here — so the live row stays the
    # deploy baseline the approval diff compares against. deployment_strategy,
    # log_provider and language_ref_code used to be written live right here,
    # which was a review bypass: they skipped the diff, took effect without
    # approval, and Discard could not undo them. They now ride in the snapshot
    # (columns at the root, language inside the nested payload where Save puts
    # it) and the settings-saver applies them at deploy.
    proposed_config = {**(target.config or {}), **portable}

    # Clone is a kind of save: enqueue the cloned config as a draft (same
    # nested snapshot shape the Settings-tab Save produces) so the settings
    # diff shows immediately and a redeploy can run — without a manual Save.
    try:
        from app.core.enum import WorkflowSourceTableEnum
        from app.repository.services_mst_repository import ServicesMstRepository
        from app.services.transaction_queue_service import TransactionQueueService

        svc_row = await ServicesMstRepository(db).get_by_code(target.services_mst_code)
        env_val = target.environment.value if hasattr(target.environment, "value") else target.environment
        service_type_val = None
        if svc_row and svc_row.service_type is not None:
            service_type_val = getattr(svc_row.service_type, "value", svc_row.service_type)

        # Nested shape, exactly like a Settings Save: real config under `config`
        # (so the approval diff compares only settings), identity at the root
        # (queue plumbing the diff ignores). A flat snapshot made the diff read
        # identity fields as changes.
        #
        # Strip using the diff's OWN exclude set (the single source of truth for
        # "not a user setting") plus service_path, which is a real diff field but
        # per-target routing that must not clone. The approval diff walks the
        # nested config with no exclusions of its own, so anything left here shows.
        from app.services.service_config_service import ServiceConfigService

        strip_keys = set(ServiceConfigService._DIFF_EXCLUDE_KEYS) | {"service_path"}
        nested_config = {
            k: v for k, v in proposed_config.items() if k not in strip_keys
        }
        # Language is a column, not config JSONB. Save carries the proposal
        # inside the nested payload (buildEksConfig does), and both the
        # approval diff's COLUMN_FIELDS lift and the settings-saver read it
        # from there — so the clone stages it in the same place. Added after
        # the strip: language_ref_code is in _DIFF_EXCLUDE_KEYS (handled
        # explicitly by the differ), which would silently drop it above.
        if source.language_ref_code is not None:
            nested_config["language_ref_code"] = source.language_ref_code

        # Display copies of the source language, for the form's dropdowns and
        # the Dockerfile generator — both read language_name at the snapshot
        # root, exactly where Save puts them.
        language_name = None
        language_version = None
        if source.language_ref_code is not None:
            from app.repository.language_ref_repository import LanguageRefRepository
            lang_ref = await LanguageRefRepository(db).get_by_code(source.language_ref_code)
            if lang_ref is not None:
                language_name = lang_ref.name
                language_version = lang_ref.version

        snapshot = {
            "config": nested_config,
            "services_mst_code": target.services_mst_code,
            "service_name": svc_row.name if svc_row else None,
            "service_type": service_type_val,
            "geo_loc_mst_code": target.geo_loc_mst_code,
            "environment": env_val,
            "infrastructuretype_ref_code": target.infrastructuretype_ref_code,
            "infrastructure_mst_code": target.infrastructure_mst_code,
            "applications_mst_code": svc_row.applications_mst_code if svc_row else None,
            **({"language_name": language_name} if language_name else {}),
            **({"language_version": language_version} if language_version else {}),
        }
        await TransactionQueueService(db).add_item_to_queue(
            user_code=user.code,
            tenant_code=tenant.code,
            transaction_code=target.code,
            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
            config_snapshot=snapshot,
            case_ref_code="update_service",
        )
    except HTTPException:
        # A deliberate refusal from the staging path — it already carries the
        # status and the sentence explaining what to do (e.g. "Secrets Manager
        # can't be turned off while this service still has secrets"). Wrapping
        # it in the 500 below would replace real guidance with "try again",
        # which is advice that cannot work.
        raise
    except Exception as exc:
        # The queue row is the ONLY carrier now — nothing was written live, so
        # a failed enqueue means the clone did not happen. Swallowing it here
        # (the old behaviour, from when the config was also written live)
        # returned 200 and toasted success over a no-op.
        logger.error("clone-settings: could not stage the clone for %s: %s", target.code, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The clone could not be staged as a draft — nothing was changed. Try again.",
        )

    service = ServiceConfigService(db)
    return await service._build_service_config_response(target)


@router.post("/reindex-vector-db", response_model=Dict[str, Any])
async def reindex_configs_to_vector_db(
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Reindex all service configs to Qdrant vector database.

    This endpoint triggers a batch reindex of all service configurations
    for the current tenant to enable semantic search functionality.

    Use this endpoint when:
    - Initial setup of semantic search
    - After bulk config imports
    - To rebuild the vector index after issues

    Note: Only org_owner can trigger reindex.

    Returns:
        Dict with total_configs, indexed_count, and status message
    """
    user, tenant = current_user_tenant

    # Only org_owner can trigger reindex
    perm_helper = PermissionHelper(db)
    if not await perm_helper.is_org_owner(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organization owners can trigger vector database reindex"
        )

    service = ServiceConfigService(db)
    result = await service.reindex_all_configs_to_vector_db(tenant.code)

    return result


@router.get("/datadog-status/{service_code}")
async def get_datadog_status(
    service_code: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
) -> Dict[str, Any]:
    """
    Check if Datadog is enabled for a service across any environment.

    Returns:
        Dict with datadog_enabled boolean and environments where it's enabled
    """
    _, tenant = current_user_tenant

    config_repo = ServiceConfigRepository(db)
    configs = await config_repo.get_all_configs_for_service(tenant.code, service_code)

    datadog_enabled = False
    enabled_environments = []

    for config in configs:
        if config.sidecar_config and isinstance(config.sidecar_config, list):
            for sidecar in config.sidecar_config:
                if not isinstance(sidecar, dict):
                    continue
                # Check if sidecar name contains 'datadog' and is enabled
                sidecar_name = str(sidecar.get('name', '') or sidecar.get('sidecar_config_code', '') or '')
                is_enabled = sidecar.get('enabled')
                # Explicitly check for True (not truthy) to handle string/boolean differences
                if 'datadog' in sidecar_name.lower() and is_enabled is True:
                    datadog_enabled = True
                    if config.environment not in enabled_environments:
                        enabled_environments.append(config.environment)

    return {
        "datadog_enabled": datadog_enabled,
        "enabled_environments": enabled_environments
    }


@router.post("/terragrunt/preview", response_model=TerragruntPreviewResponse)
async def preview_terragrunt_hcl(
    request: TerragruntPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Generate Terragrunt HCL preview from form data without saving.

    This endpoint accepts the current form configuration and generates the terragrunt.hcl content
    that would be created for an ECS service configuration. Useful for real-time preview as the
    user fills the form (similar to the Dockerfile and EKS dry-run features).

    Request schema is aligned with ServiceConfigCreate for consistency - uses same field names
    and types as the create/update endpoints.

    Args:
        request: Terragrunt configuration (services_mst_code, environment, config, etc.)
        db: Database session
        current_user_tenant: Current user and tenant (tenant from JWT)

    Returns:
        TerragruntPreviewResponse with generated HCL content, template used, and field mapping

    Example:
        POST /api/v1/service-configs/terragrunt/preview
        {
            "services_mst_code": "auth-service",
            "infrastructuretype_ref_code": "ecs",
            "infra_vendor_enum": "aws",
            "environment": "dev",
            "geo_loc_mst_code": "london",
            "config": {
                "cpu": "1.0",
                "ram": "2048",
                "port": "8080",
                "autoscaling": {"enabled": true, "min": "2", "max": "10", "desired": "4"}
            },
            "alb_selection": "existing_alb"
        }
    """
    from app.services.terragrunt_sync_service import TerragruntSyncService

    user, tenant = current_user_tenant

    try:
        # Convert config and sidecar_config to dict format
        config_dict = request.config.model_dump() if request.config else {}
        sidecar_config_list = [sc.model_dump() for sc in request.sidecar_config] if request.sidecar_config else []
        deployment_strategy_dict = request.deployment_strategy.model_dump() if request.deployment_strategy else None

        # Convert environment enum to string
        environment_str = request.environment.value if hasattr(request.environment, 'value') else str(request.environment)

        # Call service layer (follows layered architecture)
        terragrunt_service = TerragruntSyncService()

        result = await terragrunt_service.generate_hcl_preview(
            db=db,
            services_mst_code=request.services_mst_code,
            environment=environment_str,
            tenant_code=tenant.code,
            config=config_dict,
            geo_loc_mst_code=request.geo_loc_mst_code,
            alb_selection=request.alb_selection,
            language_ref_code=request.language_ref_code,
            sidecar_config=sidecar_config_list,
            deployment_strategy=deployment_strategy_dict
        )

        return TerragruntPreviewResponse(
            status="success",
            data=result
        )

    except ValueError as e:
        # Service not found or other value errors
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Failed to generate Terragrunt preview: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate Terragrunt preview: {str(e)}"
        )


@router.get("/{code}/refresh-sync-status")
async def refresh_sync_status(
    code: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Re-check and update sync_status for a service config.

    Checks if all PRs (Terragrunt, Dockerfile, and Pipeline) are merged,
    then updates sync_status to SYNCED or PENDING_SYNC accordingly.

    Args:
        code: Service configuration code
        db: Database session
        current_user_tenant: Current user and tenant

    Returns:
        Dictionary with updated sync_status
    """
    _, tenant = current_user_tenant
    service = ServiceConfigService(db)
    config_repo = ServiceConfigRepository(db)

    # Get service config by code and tenant (for security)
    service_config = await config_repo.get_by_code_and_tenant(code, tenant.code)
    if not service_config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service config with code '{code}' not found"
        )

    # Check if all PRs are merged (reuses existing correct logic)
    all_merged = await service._check_all_prs_merged(service_config)

    # Update sync_status based on PR states
    old_status = service_config.sync_status
    if all_merged:
        service_config.sync_status = "SYNCED"
    else:
        service_config.sync_status = "PENDING_SYNC"

    await db.commit()

    logger.info(f"Refreshed sync status for {code}: {old_status} → {service_config.sync_status}")

    return {
        "code": service_config.code,
        "sync_status": service_config.sync_status,
        "message": f"Sync status updated to {service_config.sync_status}"
    }


@router.post("/ecs-workflow-preview", summary="Preview ECS Workflow YAML")
async def preview_ecs_workflow_yaml(
    request: WorkflowPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Generate ECS GitHub Actions workflow YAML preview.

    This endpoint generates the exact same YAML that is created when saving
    a service config, but without any side effects (no database writes, no GitHub operations).

    **Request Body:**
    ```json
    {
        "infrastructure_ref_type": "ecs_ec2_infrastructuretype_ref",
        "infrastructure_mst_code": "infra-ecs-dev-mumbai-vance",
        "services_mst_code": "auth-service",
        "environment": "dev",
        "geo_loc_mst_code": "mumbai",
        "language_ref_code": "JAVA_GRADLE_17",
        "branch": "main",
        "build_path": "build",
        "dockerfile_path": "Dockerfile",
        "other_paths": ["shared/util", "aju/utils"],
        "wire_enabled": false,
        "wire_path": null,
        "go_use_aws_secrets": false,
        "build_args": [{"name": "NODE_ENV", "value": "production"}]
    }
    ```

    **Response:**
    ```json
    {
        "status": "success",
        "data": {
            "yaml_content": "name: Deploy auth-service to dev\\non:\\n  ...",
            "template_used": "templates/github-actions/java-gradle.yml",
            "infrastructure_type": "ecs",
            "language": "Java Gradle",
            "service_name": "auth-service",
            "environment": "dev",
            "derived_values": {
                "ecr_repository": "123456789.dkr.ecr.ap-south-1.amazonaws.com/org-dev-mumbai-auth-service",
                "aws_role_arn": "arn:aws:iam::123456789:role/OrganizationAccountAccessRole",
                "ecs_cluster": "ecs-dev-mumbai",
                "ecs_service": "org-dev-mumbai-auth-service-01",
                "aws_region": "ap-south-1",
                "geo_loc_name": "mumbai"
            }
        }
    }
    ```

    **Usage from Frontend:**
    When user selects a pipeline template, extract these fields from the pipeline record:
    - infrastructure_mst_code: From service_config.infrastructure_mst_code
    - services_mst_code: From service_config.services_mst_code (via pipeline.transaction_code)
    - environment: From service_config.environment (via pipeline.transaction_code)
    - geo_loc_mst_code: From pipeline.deployment_config.geo_loc_mst_code
    - language_ref_code: From pipeline.language_ref_code
    - branch: From pipeline.deployment_config.selected_branches[0]
    - build_path, dockerfile_path, other_paths: From pipeline.deployment_config
    """
    from app.services.workflow_preview_service import WorkflowPreviewService

    user, tenant = current_user_tenant

    try:
        # Convert environment enum to string
        environment_str = request.environment.value if hasattr(request.environment, 'value') else str(request.environment)

        # Initialize service
        workflow_service = WorkflowPreviewService(db)

        # Generate ECS workflow preview
        result = await workflow_service.generate_workflow_preview(
            infrastructure_ref_type="ecs_ec2_infrastructuretype_ref",
            infrastructure_mst_code=request.infrastructure_mst_code,
            services_mst_code=request.services_mst_code,
            environment=environment_str,
            geo_loc_mst_code=request.geo_loc_mst_code,
            branch=request.branch,
            language_ref_code=request.language_ref_code,
            build_path=request.build_path,
            dockerfile_path=request.dockerfile_path,
            other_paths=request.other_paths,
            wire_enabled=request.wire_enabled or False,
            wire_path=request.wire_path,
            go_use_aws_secrets=request.go_use_aws_secrets or False,
            build_args=request.build_args
        )

        return WorkflowPreviewResponse(
            status="success",
            data=result
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Failed to generate ECS workflow preview: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate ECS workflow preview: {str(e)}"
        )

