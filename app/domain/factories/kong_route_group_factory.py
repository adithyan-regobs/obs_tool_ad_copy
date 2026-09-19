from typing import List, Optional
from uuid import uuid4

from app.core.enum import EnvironmentEnum


def make_kong_route_group(
    route_group_key: str,
    http_method: str,
    api_name: Optional[str] = None,
    services_mst_code: Optional[str] = None,
    environments_enum: Optional[EnvironmentEnum] = None,
    geo_loc_mst_code: Optional[str] = None,
    plugins: Optional[List[str]] = None,
    regex_priority: int = 0,
) -> dict:
    """
    Factory to build a KongRouteGroup domain object.

    A group is one Kong route object: the (service, env, region, group key,
    method) tuple that terragrunt renders as a single
    `kong_configs["<group>"].routes["<METHOD>"]` entry. Plugins live here
    because that is the grain Kong applies them at.

    Args:
        route_group_key: Terragrunt kong_configs group key
        http_method: HTTP method (normalized to uppercase)
        api_name: API identifier in kong_configs
        services_mst_code: Optional service code
        environments_enum: Optional environment (dev/staging/qa/prod)
        geo_loc_mst_code: Optional geographic location code
        plugins: Name-only Kong plugins for the whole group
        regex_priority: Kong regex_priority for the group

    Returns:
        Dictionary ready for repository.create()

    Mirrors make_kong_route_config so both tables are built the same way.
    """
    method = (http_method or "").upper()

    return {
        "code": f"KRG_{uuid4().hex[:8].upper()}",
        "name": f"Kong Route Group - {route_group_key} - {method}",
        "description": f"Kong route group {route_group_key} ({method})",
        "services_mst_code": services_mst_code,
        "geo_loc_mst_code": geo_loc_mst_code,
        "environments_enum": environments_enum,
        "route_group_key": route_group_key,
        "http_method": method,
        "api_name": api_name,
        "plugins": plugins or [],
        "regex_priority": regex_priority or 0,
    }
