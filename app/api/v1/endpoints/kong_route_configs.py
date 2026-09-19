"""
Kong Route Config API Endpoints

Dedicated endpoint for creating/updating Kong Gateway routes.
Separate from the unified /infrastructures endpoint.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.authz.security import Authorization, SecureRouter
from app.repository.service_config_repository import ServiceConfigRepository
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.core.enum import EnvironmentEnum, WorkflowSourceTableEnum
from app.schemas.kong_route_schemas import (
    KongRouteConfigCreateRequest,
    KongRouteConfigCreateResponse,
    KongRouteConfigItem,
    KongRouteConfigListResponse,
    KongRouteConfigDeleteResponse,
    GatewayStateResponse,
    GatewaySaveRequest,
    GatewaySaveResponse,
    GatewayScopeItem,
    GatewayScopesResponse,
)
from app.services.kong_route_config_service import KongRouteConfigService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("", response_model=KongRouteConfigCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_kong_route_config(
    request: KongRouteConfigCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Create or update a Kong Gateway route config.

    Saves directly to kong_route_configs table.

    Supports UPSERT:
    - If request.code is provided: Updates existing record
    - If request.code is None: Creates new record

    Args:
        request: Kong route creation/update request
        db: Database session
        current_user_tenant: Current user and tenant from JWT

    Returns:
        KongRouteConfigCreateResponse with table_name=KONG_ROUTE and code

    Raises:
        HTTPException 400: Duplicate route (on create)
        HTTPException 403: Tenant isolation violation
        HTTPException 404: Service or route not found
        HTTPException 500: Internal server error

    Examples:
        Create route:
        POST /api/v1/kong-route-configs
        {
            "service_mst_code": "f7513b9e-c6ab-4a8e-a61b-b1afeb604829",
            "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
            "environment": "dev",
            "geo_loc_mst_code": "region-aspora-mumbai",
            "api_name": "user-api",
            "http_method": "GET",
            "route_path": "~/api/v1/users$"
        }

        Update route:
        POST /api/v1/kong-route-configs
        {
            "code": "KRC_DEF12345",
            "service_mst_code": "f7513b9e-c6ab-4a8e-a61b-b1afeb604829",
            "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
            "environment": "dev",
            "geo_loc_mst_code": "region-aspora-mumbai",
            "http_method": "POST",
            "route_path": "~/api/v1/users$"
        }
    """
    user, tenant = current_user_tenant

    is_update = request.code is not None
    logger.info(
        f"{'Updating' if is_update else 'Creating'} Kong route config: "
        f"service={request.service_mst_code}, method={request.http_method}, "
        f"tenant={tenant.code}, user={user.email_id}"
    )

    service = KongRouteConfigService(db)

    try:
        result = await service.create_route(
            tenant_code=tenant.code,
            user_code=user.code,
            request=request,
            user_email=user.email_id,
        )

        logger.info(f"Kong route config {'updated' if is_update else 'created'}: code={result.code}")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to {'update' if is_update else 'create'} Kong route config: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while processing Kong route config"
        )


@router.get("", response_model=KongRouteConfigListResponse)
async def list_kong_route_configs(
    service_mst_code: str,
    application_code: Optional[str] = None,
    environment: Optional[EnvironmentEnum] = None,
    geo_loc_mst_code: Optional[str] = None,
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    List a service's Kong Gateway routes, optionally scoped to an environment and
    region. Used by the Gateway tab to load existing routes.

    Query params:
        service_mst_code: Service to list routes for (required)
        application_code: Application code (for workspace access check)
        environment: Optional environment filter (dev/stage/qa/prod)
        geo_loc_mst_code: Optional region filter
        include_deleted: Also return soft-deleted rows, flagged pending_delete.
            Those are routes still live in the gateway that leave on the next
            deploy — needed to show a deletion as a pending change rather than
            having the route just disappear.

    Returns:
        KongRouteConfigListResponse with the routes and a total count.
    """
    user, tenant = current_user_tenant
    service = KongRouteConfigService(db)

    try:
        rows = await service.list_routes(
            tenant_code=tenant.code,
            user_code=user.code,
            service_mst_code=service_mst_code,
            application_code=application_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            include_deleted=include_deleted,
        )
        # plugins / regex_priority / route_group_key are group-level config and live
        # only on the linked kong_route_groups row. Built explicitly rather than via
        # model_validate, which would look for them on the route. Rows arrive with
        # route_group eager loaded — see KongRouteConfigsRepository.list_routes_for_service.
        items = [
            KongRouteConfigItem(
                code=r.code,
                api_name=r.api_name,
                http_method=r.http_method,
                route_path=r.route_path,
                plugins=list(r.route_group.plugins or []) if r.route_group else [],
                regex_priority=(r.route_group.regex_priority if r.route_group else 0) or 0,
                route_group_key=r.route_group.route_group_key if r.route_group else None,
                creation_status=r.creation_status,
                geo_loc_mst_code=r.geo_loc_mst_code,
                environments_enum=(
                    r.environments_enum.value if hasattr(r.environments_enum, "value")
                    else r.environments_enum
                ),
                pending_delete=bool(getattr(r, "is_deleted", False)),
            )
            for r in rows
        ]
        return KongRouteConfigListResponse(routes=items, total=len(items))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list Kong route configs: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while listing Kong route configs"
        )


@router.get("/gateway/scopes", response_model=GatewayScopesResponse)
async def get_gateway_scopes(
    service_mst_code: str,
    application_code: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Which environments and regions this service has a gateway in.

    GET /gateway needs both, and the Gateway tab normally takes the region from
    the canvas node — which reads it from service_configs. A service imported
    from terragrunt has Kong routes but no config row, so it has no region to
    send and cannot open its own tab. This answers that from the route groups
    themselves, which is the one place the information definitely exists.

    Call it only when the node has no region; then use a returned scope to make
    the real, properly scoped /gateway call.
    """
    user, tenant = current_user_tenant
    service = KongRouteConfigService(db)

    try:
        await service._guard_service_access(
            tenant.code, user.code, service_mst_code, application_code
        )
        scopes = await service.kong_route_group_repo.list_scopes(service_mst_code)
        return GatewayScopesResponse(
            service_mst_code=service_mst_code,
            scopes=[GatewayScopeItem(**s) for s in scopes],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list gateway scopes for {service_mst_code}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while listing gateway scopes"
        )


@router.get("/gateway", response_model=GatewayStateResponse)
async def get_gateway_state(
    service_mst_code: str,
    environment: EnvironmentEnum,
    geo_loc_mst_code: Optional[str] = None,
    application_code: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    A service's gateway for ONE environment and region, nested by route group.

    Replaces the flat GET "" list for the Gateway tab. Grouped because the group
    is now the unit of change: it owns the plugins, a queue row points at it, and
    its updated_at is the token the save path locks on.

    environment is REQUIRED — it is what separates a service's stage gateway from
    its prod one, and services_mst carries neither.

    geo_loc_mst_code is OPTIONAL. Send it when you know it; omit it and the server
    resolves it from the route groups. That is for services imported from
    terragrunt, which have routes but no service_configs row and so no region on
    the canvas node to send.

    The response ALWAYS covers one region, never several merged: `geo_loc_mst_code`
    comes back resolved. If the service has gateways in more than one region for
    this environment, `groups` is empty and `scopes` lists the choices — pick one
    and call again. Merging is refused rather than guessed because a save has to
    name a single region and a deploy writes a single region's terragrunt file.

    Each group carries `pending`: the undeployed change read straight off its
    queue row, or null. That IS the diff — nothing is recomputed to produce it.
    """
    user, tenant = current_user_tenant
    service = KongRouteConfigService(db)

    try:
        return await service.get_gateway_state(
            tenant_code=tenant.code,
            user_code=user.code,
            service_mst_code=service_mst_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            application_code=application_code,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to load gateway state for {service_mst_code}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while loading gateway state"
        )


# FGA-carded gateway fetch. Same SecureRouter pattern as service_config.py:
# the object id is the service_configs.code in the path, so the guard checks
# can_view_gateway on service:<code> before the handler runs. The gateway
# scope (service, environment, region) is then taken from the authorized row,
# never from the client — the caller can only see the gateway of the exact
# config they were authorized on.
secure_router = SecureRouter()


@secure_router.get(
    "/gateway/by-config/{service_config_code}",
    response_model=GatewayStateResponse,
    access=Authorization(
        permission="can_view_gateway",
        obj_type="service",
        param="service_config_code",
        deny_status=403,
    ),
)
async def get_gateway_state_by_config(
    service_config_code: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Gateway state scoped by a service config code instead of raw query params.

    The card already checked can_view_gateway on service:<service_config_code>.
    service_mst_code / environment / geo_loc_mst_code come from the fetched
    row (confused-deputy guard). application_code is not sent: the service
    resolves it from services_mst when it needs the legacy workspace check.
    """
    user, tenant = current_user_tenant

    config_repo = ServiceConfigRepository(db)
    row = await config_repo.get_by_code_and_tenant(service_config_code, tenant.code)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service configuration with code {service_config_code} not found"
        )

    service = KongRouteConfigService(db)
    try:
        return await service.get_gateway_state(
            tenant_code=tenant.code,
            user_code=user.code,
            service_mst_code=row.services_mst_code,
            environment=row.environment,
            geo_loc_mst_code=row.geo_loc_mst_code,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to load gateway state for config {service_config_code}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while loading gateway state"
        )


@secure_router.post(
    "/gateway/by-config/{service_config_code}/save",
    response_model=GatewaySaveResponse,
    access=Authorization(
        permission="can_write_gateway",
        obj_type="service",
        param="service_config_code",
        deny_status=403,
    ),
)
async def save_gateway_changes_by_config(
    service_config_code: str,
    request: GatewaySaveRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Carded save. The card already checked can_write_gateway on
    service:<service_config_code>; the save scope (service, environment,
    region) is then overwritten from the authorized row, so the body cannot
    re-point the write at another service's gateway (confused-deputy guard).
    Group payloads and 409 semantics are identical to the legacy /gateway/save.
    """
    user, tenant = current_user_tenant

    config_repo = ServiceConfigRepository(db)
    row = await config_repo.get_by_code_and_tenant(service_config_code, tenant.code)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service configuration with code {service_config_code} not found"
        )

    request.service_mst_code = row.services_mst_code
    request.environment = row.environment
    request.geo_loc_mst_code = row.geo_loc_mst_code
    # Not client-controlled either: the legacy workspace check inside the
    # service resolves the application from services_mst when this is None.
    request.application_code = None

    service = KongRouteConfigService(db)
    try:
        return await service.save_gateway_changes(
            tenant_code=tenant.code,
            user_code=user.code,
            request=request,
            user_email=user.email_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to save gateway changes for config {service_config_code}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while saving gateway changes"
        )


@router.post("/gateway/save", response_model=GatewaySaveResponse)
async def save_gateway_changes(
    request: GatewaySaveRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Save pending gateway changes for one service, environment and region.

    Per group: claim it on the `updated_at` returned by GET /gateway, reconcile
    its paths to the desired list, then store the change record on its queue row.
    Nothing deploys — the queue row is what a later deploy ships.

    Each group carries BOTH `paths` (the desired list, used to write the tables)
    and `delta` (the change record, stored as-is). Neither is redundant: the
    server cannot derive the delta, because that needs the deployed state, which
    it does not keep.

    409 when any group's `updated_at` no longer matches — somebody saved in
    between. The whole request is rejected, not the one group: committing the
    rest would leave the client holding stale tokens for them.
    """
    user, tenant = current_user_tenant
    service = KongRouteConfigService(db)

    try:
        return await service.save_gateway_changes(
            tenant_code=tenant.code,
            user_code=user.code,
            request=request,
            # Stamped onto the route rows at save. Same source create_route uses, so
            # both flows attribute a route the same way.
            user_email=user.email_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to save gateway changes for {request.service_mst_code}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while saving gateway changes"
        )


@router.delete("/{code}", response_model=KongRouteConfigDeleteResponse)
async def delete_kong_route_config(
    code: str,
    reconcile: bool = Query(
        True,
        description=(
            "Also take the route out of the caller's pending change set: the "
            "queue row's snapshot is rewritten without it, and the row is "
            "dropped if it held nothing else. That is what a delete a PERSON "
            "asked for means. Pass false for housekeeping deletes — the Gateway "
            "tab's duplicate self-heal runs on every load, and tidying a stale "
            "twin must not rewrite, let alone discard, a change set nobody "
            "touched."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Soft-delete a Kong Gateway route by code. Validates tenant ownership and
    workspace access via the route's service. Soft delete keeps history and drops
    the route from terragrunt generation.

    Path params:
        code: Route code (KRC_xxx) to delete.

    Query params:
        reconcile: Whether to also update the caller's pending change set.

    Returns:
        KongRouteConfigDeleteResponse.
    """
    user, tenant = current_user_tenant
    service = KongRouteConfigService(db)

    try:
        await service.delete_route(
            tenant_code=tenant.code, user_code=user.code, code=code,
            reconcile=reconcile,
        )
        return KongRouteConfigDeleteResponse(
            success=True, code=code, message="Kong route deleted",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete Kong route config {code}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while deleting Kong route config"
        )
