from datetime import datetime
from typing import Optional
from uuid import uuid4
from app.core.enum import DeploymentStatusEnum, EnvironmentEnum


def make_kong_route_config(
    api_name: str,
    http_method: str,
    route_path: str,
    services_mst_code: Optional[str] = None,
    environments_enum: Optional[EnvironmentEnum] = None,
    geo_loc_mst_code: Optional[str] = None,
    creation_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    creation_status_updated_by: Optional[str] = None,
    creation_error: Optional[str] = None,
    kong_route_group_id: Optional[int] = None
) -> dict:
    """
    Factory to build a KongRouteConfig domain object.

    Args:
        api_name: API identifier in kong_configs (e.g., "user_api")
        http_method: HTTP method (GET, POST, PUT, DELETE, etc.)
        route_path: Kong route pattern (e.g., "~/api/v1/users$")
        services_mst_code: Optional service code (None for plugin/global routes)
        environments_enum: Optional environment (dev/staging/prod) - matches infrastructure_mst
        geo_loc_mst_code: Optional geographic location code - matches infrastructure_mst
        creation_status: Initial deployment status (default: INITIATED)
        gitops_workflow_id: Optional workflow ID if linking to existing workflow
        resource_identifier: Optional Kong route ID (set after vendor creation)
        creation_status_updated_by: Optional user/system that created this route
        creation_error: Optional error message if creation/deployment failed

    Returns:
        Dictionary ready for repository.create()

    Example:
        route_data = make_kong_route_config(
            api_name="user_api",
            http_method="GET",
            route_path="~/api/v1/users$",
            services_mst_code="SVC_12345",
            environments_enum=EnvironmentEnum.DEV,
            geo_loc_mst_code="region-aspora-mumbai",
            creation_status=DeploymentStatusEnum.INITIATED
        )
        route = await repo.create(**route_data)

    This isolates object creation logic (like defaults, code generation, and derived fields)
    from the repository and service layers.
    """
    # Generate unique code for this route config
    route_code = f"KRC_{uuid4().hex[:8].upper()}"

    # Build descriptive name
    name = f"Kong Route - {api_name} - {http_method} {route_path[:50]}"

    # Return dictionary for repository create method
    route_data = {
        "code": route_code,
        "name": name,
        "description": f"Kong Gateway route for {api_name}: {http_method} {route_path}",
        "services_mst_code": services_mst_code,
        "geo_loc_mst_code": geo_loc_mst_code,
        "environments_enum": environments_enum,
        "api_name": api_name,
        "http_method": http_method.upper(),  # Normalize to uppercase
        "route_path": route_path,
        # PR Workflow Tracking
        "creation_status": creation_status,
        "creation_status_updated_by": creation_status_updated_by,
        "creation_status_updated_at": datetime.utcnow() if creation_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
        "creation_error": creation_error,
        # Dual-write link to kong_route_groups. Optional: callers that have not
        # been migrated yet leave it None and are picked up by the backfill.
        "kong_route_group_id": kong_route_group_id,
    }

    return route_data


# ── v2 (Gateway tab) ─────────────────────────────────────────────────────────
# Duplicated rather than shared with make_kong_route_config above: that one is
# also called by infrastructure_creation_service.py (Slack bot flow) and
# terragrunt_mgmt_service.py (POST /push-infra-config, a separate standalone
# REST API) — so v1 and v2 need to be able to change independently. Route
# codes still use the KRC_ prefix in both — that identifies the
# kong_route_configs ROW, not which flow wrote it; only route GROUP codes
# (KRG_ vs none) tell v1 and v2 items apart.
def make_kong_route_config_v2(
    api_name: str,
    http_method: str,
    route_path: str,
    services_mst_code: Optional[str] = None,
    environments_enum: Optional[EnvironmentEnum] = None,
    geo_loc_mst_code: Optional[str] = None,
    creation_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    creation_status_updated_by: Optional[str] = None,
    creation_error: Optional[str] = None,
    kong_route_group_id: Optional[int] = None
) -> dict:
    """
    Factory to build a KongRouteConfig domain object (Gateway tab / v2).

    Returns:
        Dictionary ready for repository.create()
    """
    route_code = f"KRC_{uuid4().hex[:8].upper()}"

    name = f"Kong Route - {api_name} - {http_method} {route_path[:50]}"

    route_data = {
        "code": route_code,
        "name": name,
        "description": f"Kong Gateway route for {api_name}: {http_method} {route_path}",
        "services_mst_code": services_mst_code,
        "geo_loc_mst_code": geo_loc_mst_code,
        "environments_enum": environments_enum,
        "api_name": api_name,
        "http_method": http_method.upper(),  # Normalize to uppercase
        "route_path": route_path,
        "creation_status": creation_status,
        "creation_status_updated_by": creation_status_updated_by,
        "creation_status_updated_at": datetime.utcnow() if creation_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
        "creation_error": creation_error,
        "kong_route_group_id": kong_route_group_id,
    }

    return route_data
