"""
Placement Parameter Validator Node.

This node validates placement parameters from collected_parameters against the resource's
placement group config. It ensures all required placement parameters are present before
proceeding to parameter extraction.

Runs after intent detection (so we have turn_resource) and before parameter extraction.
"""
import importlib
import logging
import re
from typing import Dict, Any, Optional
from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.config_models import TenantId, InfraTypeCode, ValueSourceType
from app.db.session import AsyncSessionLocal
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.repository.services_mst_repository import ServicesMstRepository

logger = logging.getLogger(__name__)


async def _lookup_name_from_code(
    code: str,
    lookup_type: str
) -> Optional[str]:
    """
    Lookup display name from database using code.

    Args:
        code: The UUID code to lookup
        lookup_type: Either 'application' or 'service'

    Returns:
        Display name if found, None otherwise
    """
    if not code:
        return None

    try:
        async with AsyncSessionLocal() as session:
            if lookup_type == 'application':
                repo = ApplicationsMstRepository(session)
                record = await repo.get_by_code(code)
                return record.name if record else None
            elif lookup_type == 'service':
                repo = ServicesMstRepository(session)
                record = await repo.get_by_code(code)
                return record.name if record else None
    except Exception as e:
        logger.warning(f"[PLACEMENT_VALIDATOR] Failed to lookup {lookup_type} name for code {code}: {e}")
    return None


def _camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


async def placement_param_validator_node(state: ChatState) -> ChatState:
    """
    Validate placement parameters against resource config.

    Checks collected_placement_parameters against the resource's placement group
    configuration and calculates which required parameters are missing.

    Args:
        state: Current chat state

    Returns:
        Updated state with remaining_placement_parameters filled
    """
    tenant_id = get_tenant_id(state)
    turn_intent = state.get("turn_intent", "")
    turn_resource = state.get("turn_resource", "")
    state_hint = state.get("state_hint", {})
    collected_placement_parameters = state.get("collected_placement_parameters", {})

    # Only process for CREATE intent (other intents don't use placement params)
    if turn_intent != "CREATE":
        logger.debug(f"[PLACEMENT_VALIDATOR] Skipping validation for intent={turn_intent}")
        return {}

    # If no resource type from turn, fall back to state_hint.running_resource
    if not turn_resource:
        turn_resource = state_hint.get("running_resource", "")
        if turn_resource:
            logger.info(f"[PLACEMENT_VALIDATOR] Using running_resource from state_hint: {turn_resource}")
        else:
            logger.warning("[PLACEMENT_VALIDATOR] No turn_resource set, skipping validation")
            return {}

    # Load resource metadata
    resource_meta = resource_meta_repo.get(
        tenant_id=TenantId(tenant_id),
        infra_type=InfraTypeCode(turn_resource)
    )

    if not resource_meta:
        logger.warning(
            f"[PLACEMENT_VALIDATOR] ResourceMeta not found for "
            f"tenant={tenant_id}, infra_type={turn_resource}"
        )
        return {}

    # Get placement parameters from config
    placement_params = resource_meta.placement.parameters

    logger.info(
        f"[PLACEMENT_VALIDATOR] Resource: {turn_resource}, "
        f"Placement params in config: {[p.key for p in placement_params]}"
    )
    logger.info(
        f"[PLACEMENT_VALIDATOR] Collected placement params: {collected_placement_parameters}"
    )

    # Calculate missing required placement parameters (just keys)
    # Check for missing keys, null values, AND empty strings
    remaining_placement = {}
    for param in placement_params:
        if param.required:
            value = collected_placement_parameters.get(param.key)
            # Check for missing, null, or empty values
            if param.key not in collected_placement_parameters or value is None or value == "":
                remaining_placement[param.key] = None

    logger.info(
        f"[PLACEMENT_VALIDATOR] Missing required placement params: "
        f"{list(remaining_placement.keys())}"
    )

    # Run pre-attributes validators if all placement params are collected
    logger.info(
        f"[PLACEMENT_VALIDATOR] Check validators: remaining_placement={remaining_placement}, "
        f"has_validators={bool(resource_meta.pre_attributes_collect_validators)}, "
        f"validators_count={len(resource_meta.pre_attributes_collect_validators)}"
    )

    if not remaining_placement and resource_meta.pre_attributes_collect_validators:
        logger.info(f"[PLACEMENT_VALIDATOR] Running pre-attributes validators")

        for idx, validator_source in enumerate(resource_meta.pre_attributes_collect_validators):
            logger.info(
                f"[PLACEMENT_VALIDATOR] Validator[{idx}]: type={validator_source.type}, "
                f"class_name={validator_source.class_name}"
            )
            if validator_source.type == ValueSourceType.INTERNAL:
                try:
                    # Dynamic import of validator class
                    # Extract tenant prefix from class name (e.g., "AsporaKongValidator" -> "aspora")
                    module_name = _camel_to_snake(validator_source.class_name)
                    # Use tenant prefix from class name for module path (shared validators)
                    class_tenant = module_name.split("_")[0]  # "aspora_kong_validator" -> "aspora"
                    module_path = f"app.plugin.{class_tenant}.validator.{module_name}"
                    logger.info(f"[PLACEMENT_VALIDATOR] Importing validator: {module_path}")
                    module = importlib.import_module(module_path)
                    validator_class = getattr(module, validator_source.class_name)
                    validator = validator_class()

                    # Get values from collected placement parameters
                    env_str = collected_placement_parameters.get("environment_enum", "prod")
                    geo_loc = collected_placement_parameters.get("geo_loc_mst_code", "")
                    logger.info(f"[PLACEMENT_VALIDATOR] env_str={env_str}, geo_loc={geo_loc}")

                    # Get product_name: try display name first, fallback to DB lookup
                    product_name = collected_placement_parameters.get("_applications_mst_name")
                    logger.info(f"[PLACEMENT_VALIDATOR] _applications_mst_name={product_name}")
                    if not product_name:
                        app_code = collected_placement_parameters.get("applications_mst_code")
                        logger.info(f"[PLACEMENT_VALIDATOR] Looking up application name for code={app_code}")
                        product_name = await _lookup_name_from_code(app_code, "application") or ""
                        logger.info(f"[PLACEMENT_VALIDATOR] DB lookup result: product_name={product_name}")

                    # Get service_name: try display name first, fallback to DB lookup
                    service_name = collected_placement_parameters.get("_service_mst_name")
                    logger.info(f"[PLACEMENT_VALIDATOR] _service_mst_name={service_name}")
                    if not service_name:
                        svc_code = collected_placement_parameters.get("service_mst_code")
                        logger.info(f"[PLACEMENT_VALIDATOR] Looking up service name for code={svc_code}")
                        service_name = await _lookup_name_from_code(svc_code, "service") or ""
                        logger.info(f"[PLACEMENT_VALIDATOR] DB lookup result: service_name={service_name}")

                    # Derive api_name from service name
                    api_name = service_name.lower().replace(" ", "-").replace("_", "-") if service_name else ""
                    if api_name and not api_name.endswith("-service"):
                        api_name = f"{api_name}-service"
                    logger.info(f"[PLACEMENT_VALIDATOR] Derived api_name={api_name}")

                    logger.info(
                        f"[PLACEMENT_VALIDATOR] Calling validator with: "
                        f"tenant={tenant_id}, env={env_str}, geo_loc={geo_loc}, "
                        f"product={product_name}, api={api_name}"
                    )

                    # Run validator
                    logger.info(f"[PLACEMENT_VALIDATOR] Invoking validator.validate()...")
                    result = await validator.validate(
                        tenant_code=tenant_id,
                        environment=env_str,
                        geo_loc_mst_code=geo_loc,
                        product_name=product_name,
                        api_name=api_name
                    )
                    logger.info(
                        f"[PLACEMENT_VALIDATOR] Validator result: "
                        f"status={result.validation_status}, error={result.error_message}"
                    )

                    if not result.validation_status:
                        logger.warning(
                            f"[PLACEMENT_VALIDATOR] Validation failed: {result.error_message}"
                        )
                        return {
                            "remaining_placement_parameters": {},
                            "turn_user_response": f"Validation failed: {result.error_message}",
                            "turn_requires_user_input": True
                        }

                    logger.info(f"[PLACEMENT_VALIDATOR] Validation passed")

                except Exception as e:
                    logger.error(f"[PLACEMENT_VALIDATOR] Validator error: {e}", exc_info=True)
                    return {
                        "remaining_placement_parameters": {},
                        "turn_user_response": f"Validation error: {str(e)}",
                        "turn_requires_user_input": True
                    }

    return {
        "remaining_placement_parameters": remaining_placement
    }
