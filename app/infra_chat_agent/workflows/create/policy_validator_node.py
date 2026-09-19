"""
Policy validator node for CREATE workflow.

Handles slot filling and validation of extracted attribute parameters.
Placement parameters are already validated before reaching the graph.
"""
import logging
from typing import Dict, Any, List, Tuple, Optional
from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.config_models import (
    TenantId, InfraTypeCode, StaticSource, ParamType, ConditionalRequirement, ParameterMeta
)

logger = logging.getLogger(__name__)


def _get_allowed_values(param) -> Optional[List[str]]:
    """
    Extract allowed values from a parameter's value_source.

    Args:
        param: ParameterMeta object

    Returns:
        List of allowed values or None
    """
    if param.value_source and isinstance(param.value_source, StaticSource):
        return [opt.value for opt in param.value_source.options]
    return None


def _is_conditionally_required(
    param: ParameterMeta,
    collected_params: Dict[str, Any]
) -> bool:
    """
    Check if a parameter is conditionally required based on other collected values.

    Evaluates the ConditionalRequirement on a parameter to determine if
    it should be treated as required given the current collected parameters.

    Args:
        param: The parameter metadata to check
        collected_params: All currently collected parameter values

    Returns:
        True if the parameter is conditionally required, False otherwise
    """
    if not param.conditional:
        return False

    # Get the value of the dependent parameter
    dep_value = collected_params.get(param.conditional.if_param)

    # Check if the dependent value matches any of the trigger values
    # Handle string/boolean type mismatch (LLM may return "true" string vs True boolean)
    for trigger_value in param.conditional.has_value:
        if isinstance(trigger_value, bool):
            # Normalize dep_value to boolean for comparison
            if isinstance(dep_value, str):
                normalized = dep_value.lower() in ("true", "yes", "1")
                if normalized == trigger_value:
                    return True
            elif dep_value == trigger_value:
                return True
        elif dep_value == trigger_value:
            return True

    return False


def _remove_orphaned_conditional_params(
    attributes: List[ParameterMeta],
    collected_params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Remove conditionally required parameters when their condition is no longer met.

    When a user changes a triggering parameter (e.g., enable_s3_replication=False),
    any conditionally required params that were collected should be removed since
    they're no longer relevant.

    Args:
        attributes: List of ParameterMeta from attributes group
        collected_params: Currently collected parameters

    Returns:
        Updated collected_params with orphaned conditional params removed
    """
    params = dict(collected_params)

    for param in attributes:
        # Only check params with conditional requirements
        if not param.conditional:
            continue

        # If param is collected but condition is NOT met, remove it
        if param.key in params and not _is_conditionally_required(param, params):
            removed_value = params.pop(param.key)
            logger.info(
                f"[POLICY_VALIDATOR] Removed orphaned conditional param {param.key}={removed_value} "
                f"(condition no longer met: {param.conditional.if_param} not in {param.conditional.has_value})"
            )

    return params


def _normalize_enum_value(value: Any, allowed_values: List[str]) -> Tuple[Any, bool]:
    """
    Normalize an ENUM value to match the canonical case from allowed_values.

    Performs case-insensitive matching and returns the canonical form.

    Args:
        value: The value to normalize
        allowed_values: List of allowed values in canonical form

    Returns:
        Tuple of (normalized_value, is_valid)
    """
    if not isinstance(value, str):
        return value, value in allowed_values

    # Build case-insensitive lookup map
    value_lower = value.lower()
    for allowed in allowed_values:
        if isinstance(allowed, str) and allowed.lower() == value_lower:
            return allowed, True  # Return canonical form

    return value, False  # No match found


def _validate_parameter(
    param_name: str,
    value: Any,
    allowed_values: Optional[List[str]],
    regex_pattern: Optional[str] = None,
    examples: Optional[List[str]] = None,
    param_type: Optional[str] = None,
    validate_as_regex: bool = False
) -> Tuple[Optional[Dict[str, str]], Any]:
    """
    Validate a parameter value against allowed values and regex pattern.

    Args:
        param_name: Name of the parameter
        value: Value to validate
        allowed_values: List of allowed values (None if no restriction)
        regex_pattern: Regex pattern to validate against (None if no regex validation)
        examples: Example values to show in error messages (None if no examples)
        param_type: Parameter type (e.g., "json" for list types)
        validate_as_regex: If True, validates that the value itself is a compilable regex

    Returns:
        Tuple of (error_dict or None, normalized_value)
        - error_dict: Error dict if invalid, None if valid
        - normalized_value: The value normalized to canonical form (for ENUMs)
    """
    import re

    normalized_value = value

    # Validate against allowed values (for ENUM types) - case-insensitive
    if allowed_values:
        normalized_value, is_valid = _normalize_enum_value(value, allowed_values)
        if not is_valid:
            label = param_name.replace("_", " ")
            return {
                "parameter": param_name,
                "message": f"Invalid {label} '{value}'. Allowed: {allowed_values}"
            }, value

    # Validate BOOL type parameters - only accept true/false
    if param_type in ("bool", ParamType.BOOL):
        valid_bool_values = {"true", "false"}
        str_value = str(value).lower().strip()
        if str_value not in valid_bool_values and not isinstance(value, bool):
            label = param_name.replace("_", " ")
            return {
                "parameter": param_name,
                "message": f"Invalid {label} '{value}'. Must be True or False."
            }, value

    # Validate INT type parameters
    if param_type in ("int", ParamType.INT):
        try:
            int(value)
        except (ValueError, TypeError):
            label = param_name.replace("_", " ")
            return {
                "parameter": param_name,
                "message": f"Invalid {label} '{value}'. Must be a number."
            }, value

    # Validate against regex pattern (for STRING types with regex validation)
    if regex_pattern and isinstance(value, str):
        # For JSON type parameters (lists), validate each value separately
        if param_type == "json":
            # Split by comma to get individual values
            values = [v.strip() for v in value.split(',') if v.strip()]
            invalid_values = []

            for v in values:
                if not re.match(regex_pattern, v):
                    invalid_values.append(v)

            if invalid_values:
                label = param_name.replace("_", " ")
                msg = f"Invalid {label}: {', '.join(invalid_values)}."

                # Include examples if available
                if examples:
                    msg += f" Examples: {examples}"
                else:
                    msg += f" Each value must match pattern: {regex_pattern}"

                return {
                    "parameter": param_name,
                    "message": msg
                }, normalized_value
        else:
            # Regular STRING type validation
            if not re.match(regex_pattern, normalized_value):
                label = param_name.replace("_", " ")
                msg = f"Invalid {label} '{normalized_value}'."

                # Include examples if available
                if examples:
                    msg += f" Examples: {examples}"
                else:
                    msg += f" Must match pattern: {regex_pattern}"

                return {
                    "parameter": param_name,
                    "message": msg
                }, normalized_value

    # Validate that the value itself is a compilable regex pattern
    if validate_as_regex and isinstance(value, str):
        try:
            # Extract the regex pattern from Kong route format (~/pattern$)
            test_pattern = value
            if test_pattern.startswith("~/"):
                test_pattern = test_pattern[2:]  # Remove ~/
            if test_pattern.endswith("$"):
                test_pattern = test_pattern[:-1]  # Remove trailing $

            # Convert Kong PCRE named groups (?<name>...) to Python syntax (?P<name>...)
            python_pattern = re.sub(r'\(\?<([^>]+)>', r'(?P<\1>', test_pattern)
            re.compile(python_pattern)
        except re.error as regex_error:
            label = param_name.replace("_", " ")
            msg = f"Invalid {label} regex syntax: {str(regex_error)}."
            if examples:
                msg += f" Examples: {examples}"
            return {
                "parameter": param_name,
                "message": msg
            }, normalized_value

    return None, normalized_value


def _apply_and_validate_updates(
    slot_updates: Dict[str, Any],
    param_definitions: Dict[str, Any],
    collected_params: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """
    Apply slot updates and validate against allowed_values.

    For JSON type parameters (used for lists), accumulates values instead of replacing.

    Args:
        slot_updates: New parameters to add/update
        param_definitions: Parameter definitions from resource_meta (attributes only)
        collected_params: Previously collected parameters

    Returns:
        Tuple of (updated_collected_params, errors)
    """
    params = dict(collected_params)
    errors = []

    for param_name, value in slot_updates.items():
        if param_name not in param_definitions:
            continue  # Skip unknown parameters

        param_def = param_definitions[param_name]

        # Get allowed values from value_source
        allowed_values = _get_allowed_values(param_def)

        # Get regex pattern from validation rule
        regex_pattern = None
        if hasattr(param_def, 'validation') and param_def.validation:
            regex_pattern = param_def.validation.regex

        # Get examples from parameter definition
        examples = param_def.examples if hasattr(param_def, 'examples') else None

        # Get parameter type
        param_type = param_def.type if hasattr(param_def, 'type') else None

        # Get validate_as_regex flag from validation rule
        validate_as_regex = False
        if hasattr(param_def, 'validation') and param_def.validation:
            validate_as_regex = getattr(param_def.validation, 'validate_as_regex', False)

        # Validate against allowed_values and regex pattern (examples used only for regex errors)
        # Returns (error, normalized_value) - normalized_value has canonical case for ENUMs
        error, normalized_value = _validate_parameter(
            param_name, value, allowed_values, regex_pattern, examples, param_type,
            validate_as_regex=validate_as_regex
        )
        if error:
            errors.append(error)
        else:
            # Handle JSON type parameters (used for lists) - accumulate instead of replace
            if hasattr(param_def, 'type') and param_def.type == "json":
                # Convert normalized_value to list if needed
                if isinstance(normalized_value, str):
                    # Split by comma if multiple values in one string
                    new_values = [v.strip() for v in normalized_value.split(',') if v.strip()]
                elif isinstance(normalized_value, list):
                    new_values = normalized_value
                else:
                    new_values = [normalized_value]

                # Get existing values (if any)
                existing = params.get(param_name, [])
                if isinstance(existing, str):
                    existing = [v.strip() for v in existing.split(',') if v.strip()]
                elif not isinstance(existing, list):
                    existing = [existing] if existing else []

                # Accumulate unique values
                combined = existing + [v for v in new_values if v not in existing]
                params[param_name] = combined
                logger.info(f"[POLICY_VALIDATOR] Accumulated list for {param_name}: {combined}")
            else:
                # Regular parameters - replace with normalized value
                params[param_name] = normalized_value

    return params, errors


def _apply_defaults(
    attributes: List[Any],
    collected_params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Apply default values for parameters not provided by user.

    Only applies defaults for parameters that are NOT already in collected_params.
    This ensures user-provided values are never overridden.

    Args:
        attributes: List of ParameterMeta from attributes group
        collected_params: Currently collected parameters

    Returns:
        Updated collected_params with defaults applied
    """
    params = dict(collected_params)

    for param in attributes:
        # Only apply default if user didn't provide it and default exists
        if param.key not in params and param.default is not None:
            params[param.key] = param.default
            logger.info(f"[POLICY_VALIDATOR] Applied default for {param.key}: {param.default}")

    return params


def _calculate_missing_params(
    attributes: List[Any],
    collected_params: Dict[str, Any]
) -> List[str]:
    """
    Calculate which required attribute parameters are still missing.

    Parameters with default values are NOT considered missing,
    since defaults will be applied automatically when ready.

    Also evaluates conditional requirements - parameters that become
    required based on other parameter values.

    Args:
        attributes: List of ParameterMeta from attributes group
        collected_params: Currently collected parameters

    Returns:
        List of missing required parameter keys
    """
    missing = []
    for p in attributes:
        # Skip if already collected
        if p.key in collected_params:
            continue

        # Skip if has default value (will be applied automatically)
        if p.default is not None:
            continue

        # Check if required (statically or conditionally)
        is_required = p.required or _is_conditionally_required(p, collected_params)

        if is_required:
            missing.append(p.key)

    return missing


def _build_user_response(
    display_name: str,
    collected_params: Dict[str, Any],
    errors: List[Dict[str, str]],
    missing_params: List[str]
) -> str:
    """
    Build user-friendly response based on validation results.

    Args:
        display_name: Resource display name for user-friendly messages
        collected_params: Collected parameters
        errors: Validation errors
        missing_params: Missing required parameters

    Returns:
        User response message
    """
    if errors:
        error_msgs = [e["message"] for e in errors]
        return "Please correct these errors:\n" + "\n".join(f"  - {msg}" for msg in error_msgs)

    if missing_params:
        missing_labels = [p.replace("_", " ") for p in missing_params]
        return f"I need a few more details for the {display_name.upper()}:\n" + \
               "\n".join(f"<&h><&b>{label}</&b>" for label in missing_labels) + \
               "\n\nPlease provide the missing values."

    # All parameters collected - format values properly
    formatted_params = []
    for k, v in collected_params.items():
        if isinstance(v, list):
            # Format list values as comma-separated
            formatted_value = ", ".join(str(item) for item in v)
            formatted_params.append(f"<&h><&b>{k}</&b>: [{formatted_value}]")
        else:
            formatted_params.append(f"<&h><&b>{k}</&b>: {v}")

    param_summary = "\n".join(formatted_params)
    return f"Ready to create {display_name.upper()} with:\n{param_summary}"


def policy_validator_node(state: ChatState) -> ChatState:
    """
    Policy validator node for CREATE workflow.

    This node:
    1. Applies slot_parameters from parameter extraction (attributes only)
    2. Validates attribute values against allowed_values from value_source
    3. Calculates missing required attribute parameters
    4. Builds user response

    Note: Placement parameters are already in state and don't need validation here.

    Args:
        state: Current chat state

    Returns:
        Updated state with:
        - collected_parameters: Updated with new validated values
        - remaining_parameters: Missing required parameters
        - slot_errors: Validation errors (if any)
        - turn_user_response: User-friendly message
    """
    tenant_id = get_tenant_id(state)
    resource = state.get("turn_resource") or ""
    user_message = state.get("user_message", "")

    # Debug logging
    previous_collected = state.get("collected_parameters") or {}
    slot_updates = state.get("slot_parameters") or {}
    print(f"[POLICY_VALIDATOR] Message: {user_message[:50]}")
    print(f"[POLICY_VALIDATOR] Previous collected: {previous_collected}")
    print(f"[POLICY_VALIDATOR] Slot updates: {slot_updates}")
    logger.info(f"[POLICY_VALIDATOR] Message: {user_message[:50]}")
    logger.info(f"[POLICY_VALIDATOR] Previous collected: {previous_collected}")
    logger.info(f"[POLICY_VALIDATOR] Slot updates: {slot_updates}")

    # Defensive check - should have resource set
    if not resource:
        return {
            "turn_user_response": "What would you like to create?",
            "collected_parameters": {},
            "remaining_parameters": {}
        }

    # Get resource meta from new repo
    resource_meta = resource_meta_repo.get(
        TenantId(tenant_id),
        InfraTypeCode(resource)
    )

    if not resource_meta:
        logger.error(f"[POLICY_VALIDATOR] ResourceMeta not found for {tenant_id}/{resource}")
        return {
            "turn_user_response": "Sorry, I couldn't find configuration for resource.",
            "collected_parameters": previous_collected,
            "remaining_parameters": {}
        }

    # Get display name for user-friendly messages
    display_name = resource_meta.infra_display_name

    # Only validate attributes (placement already validated)
    attributes = resource_meta.attributes.parameters
    param_definitions = {p.key: p for p in attributes}

    # Apply and validate slot updates
    collected_params, errors = _apply_and_validate_updates(
        slot_updates, param_definitions, previous_collected
    )

    # Remove orphaned conditional params (e.g., cross_account_id when replication disabled)
    collected_params = _remove_orphaned_conditional_params(attributes, collected_params)

    # Log validation errors
    if errors:
        logger.info(f"[POLICY_VALIDATOR] Validation errors: {errors}")
        print(f"[POLICY_VALIDATOR] Validation errors: {errors}")

    # Find missing required attributes (excluding those with defaults)
    missing_params = _calculate_missing_params(attributes, collected_params)

    # Check if all required parameters collected (ready for confirmation)
    all_params_collected = not missing_params and not errors

    # Apply defaults when ready for confirmation
    if all_params_collected:
        collected_params = _apply_defaults(attributes, collected_params)
        logger.info(f"[POLICY_VALIDATOR] After applying defaults: {collected_params}")

    # Build user response (after defaults applied)
    response = _build_user_response(display_name, collected_params, errors, missing_params)

    # Update state_hint running_phase based on collection status
    state_hint = state.get("state_hint", {})
    if all_params_collected:
        state_hint["running_phase"] = "confirmation"
    elif state_hint.get("running_intent") == "CREATE":
        state_hint["running_phase"] = "parameter_collection"

    # Return updated state
    result = {
        "collected_parameters": collected_params,
        "remaining_parameters": {p: None for p in missing_params},
        "slot_errors": errors,
        "turn_user_response": response,
        "state_hint": state_hint,
        "is_ready": all_params_collected,  # True when ready for confirmation
        "cases": resource_meta.cases  # Add cases from resource meta
    }

    logger.info(f"[POLICY_VALIDATOR] Returning collected: {collected_params}")
    logger.info(f"[POLICY_VALIDATOR] Returning remaining: {missing_params}")
    logger.info(f"[POLICY_VALIDATOR] State hint running_phase: {state_hint.get('running_phase')}")

    return result
