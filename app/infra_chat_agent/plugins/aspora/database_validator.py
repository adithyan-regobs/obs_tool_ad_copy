# ============================================================================
# Database server validator for tenant_a
# ============================================================================
# Validates that the chosen database_server is available via the service layer
# (DatabaseCreationService.list_database_servers).
# ============================================================================

import json
import logging

from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code

logger = logging.getLogger(__name__)


async def validate(new_params: dict, current_valid: dict, *, tenant_id: str | None = None) -> dict | None:
    """Validate database_server against the service layer.

    Calls list_database_servers to fetch available servers from GitHub API,
    then checks if the user's chosen database_server is in the list.

    Args:
        new_params: params that passed individual field validation this round
        current_valid: accumulated valid state from previous rounds
        tenant_id: tenant identifier from the caller

    Returns None if valid, or {param_name: {"value": ..., "reason": ...}} for invalid params.
    """
    database_server = new_params.get("database_server")
    if not database_server:
        return None

    merged = {**current_valid, **new_params}
    region = merged.get("region")
    product = merged.get("product")
    environment = merged.get("environment")

    # Need all context params to query the service layer
    if not all([region, product, environment]):
        return None

    try:
        from app.infra_chat_agent.tools.database_tools import list_database_servers

        effective_tenant = tenant_id or "aspora"
        result_str = await list_database_servers({
            "tenant_code": effective_tenant,
            "product_name": product,
            "environment": environment,
            "geo_loc_code": normalize_geo_loc_code(region),
        })

        result = json.loads(result_str)

        if result.get("status") == "error":
            logger.warning(
                "[DB_VALIDATOR] Service layer error: %s", result.get("error")
            )
            return {
                "database_server": {
                    "value": database_server,
                    "reason": f"Failed to validate database server: {result.get('error')}",
                },
            }

        available_names = [s["server_name"] for s in result.get("servers", [])]
        if database_server not in available_names:
            return {
                "database_server": {
                    "value": database_server,
                    "reason": f"Database server '{database_server}' is not available",
                    "available": available_names,
                },
            }

        return None

    except Exception as exc:
        logger.error("[DB_VALIDATOR] Validation error: %s", exc, exc_info=True)
        return {
            "database_server": {
                "value": database_server,
                "reason": f"Failed to validate database server: {exc}",
            },
        }
