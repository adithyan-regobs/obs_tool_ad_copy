# ============================================================================
# Kong Route validator for tenant_a
# ============================================================================
# Complex validation for CreateKongRoute — runs after standard field validation
# (fuzzy matching, enum/pattern checks) and before values are added to valid state.
# Receives all new params + accumulated valid values for cross-field checks.
#
# Phase 1: Fast master data check (MasterData)
# Phase 2: Deep GitHub config validation (AsporaKongValidator)
# ============================================================================

import difflib
import logging

from app.infra_chat_agent.config.master_data_config import MasterData
from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code

logger = logging.getLogger(__name__)

TENANT_ID = "tenant_a"


def _get_kong_services(
    tenant_id: str,
    region: str | None = None,
    product: str | None = None,
    environment: str | None = None,
) -> list[str]:
    """Get services from MasterData for the given tenant, narrowed by region/product/environment."""
    entries = MasterData.get(tenant_id, [])
    if region:
        entries = [e for e in entries if e["region"] == region]
    if product:
        entries = [e for e in entries if e["product"] == product]
    if not entries:
        entries = MasterData.get(tenant_id, [])
    seen: set[str] = set()
    result: list[str] = []
    for entry in entries:
        if environment and environment in entry["environments"]:
            for svc in entry["environments"][environment]:
                if svc not in seen:
                    seen.add(svc)
                    result.append(svc)
        else:
            for env_value in entry["environments"].values():
                for svc in env_value:
                    if svc not in seen:
                        seen.add(svc)
                        result.append(svc)
    return result


async def validate(new_params: dict, current_valid: dict, *, tenant_id: str | None = None) -> dict | None:
    """Validate Kong route params for tenant_a.

    Two-phase validation:
      Phase 1: Check service against MasterData (fast).
      Phase 2: Check service exists in actual Kong HCL config in GitHub (deep).

    Args:
        new_params: params that passed individual field validation this round
        current_valid: accumulated valid state from previous rounds
        tenant_id: tenant identifier from the caller (e.g., "vance", "aspora")

    Returns None if valid, or {param_name: {"value": ..., "reason": ...}} for invalid params.
    """
    service = new_params.get("service")
    if not service:
        return None

    merged = {**current_valid, **new_params}
    region = merged.get("region")
    product = merged.get("product")
    environment = merged.get("environment")

    # --- Phase 1: Fast master data pre-check ---
    effective_tenant = tenant_id or TENANT_ID
    available = _get_kong_services(effective_tenant, region=region, product=product, environment=environment)
    if service not in available:
        # Try to resolve partial names: user may type "goms" meaning "goms-service"
        svc_lower = service.lower()
        # 1. Prefix / substring match (most reliable for short names)
        partial = [s for s in available if svc_lower in s.lower() or s.lower().startswith(svc_lower)]
        if len(partial) == 1:
            new_params["service"] = partial[0]
            service = partial[0]
        else:
            # 2. Fuzzy match fallback
            fuzzy = difflib.get_close_matches(service, available, n=1, cutoff=0.5)
            if fuzzy:
                new_params["service"] = fuzzy[0]
                service = fuzzy[0]
            else:
                return {
                    "service": {
                        "value": service,
                        "reason": f"Service '{service}' is not available through Kong gateway",
                        "available": available,
                    },
                }

    # --- Phase 2: Deep GitHub config validation ---
    # Only run when all required params are available
    if not all([region, product, environment]):
        return None

    try:
        from app.plugin.aspora.validator.aspora_kong_validator import AsporaKongValidator

        geo_loc_mst_code = normalize_geo_loc_code(region)
        tenant_code = tenant_id or "aspora"

        validator = AsporaKongValidator()
        result = await validator.validate(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            product_name=product,
            api_name=service,
        )

        if not result.validation_status:
            logger.warning(
                "[KONG_PLUGIN_VALIDATOR] GitHub config validation failed: "
                "error_code=%s, message=%s",
                result.error_code,
                result.error_message,
            )
            return {
                "service": {
                    "value": service,
                    "reason": result.error_message,
                    "error_code": result.error_code,
                },
            }

        logger.info("[KONG_PLUGIN_VALIDATOR] GitHub config validation passed for service=%s", service)
        return None

    except Exception as exc:
        logger.error("[KONG_PLUGIN_VALIDATOR] GitHub config validation error: %s", exc, exc_info=True)
        return {
            "service": {
                "value": service,
                "reason": f"Failed to validate service against Kong gateway config: {exc}",
            },
        }
