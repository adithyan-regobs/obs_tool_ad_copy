import difflib
import inspect
import logging
import re
from typing import Any, Literal, get_args, get_origin
from unittest import result
from pydantic import BaseModel, ValidationError
from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.config_models import TenantId, InfraTypeCode
from app.infra_chat_agent.config.master_data_config import MasterData, get_services_for_entry
from app.infra_chat_agent.config.placement_utils import (
    resolve_product_application_code as _resolve_product_application_code,
    TOOL_INFRA_TYPE_MAP as _TOOL_INFRA_TYPE_MAP,
)
from app.services.service_config_agent import state

logger = logging.getLogger(__name__)

_TRUE_WORDS = {
    "true", "yes", "y", "1",
    "enable", "enabled",
    "on", "active", "activate", "activated",
    "want", "need", "required", "with",
    "sure", "yeah", "yep", "yup", "correct", "right",
}
_FALSE_WORDS = {
    "false", "no", "n", "0",
    "disable", "disabled",
    "off", "inactive", "deactivate", "deactivated",
    "without", "none", "nope", "nah", "skip",
    "not", "dont", "don't", "no need",
}


def _build_master_data_reason(
    availability: list[dict], region: str | None, product: str | None, environment: str | None
) -> str:
    """Build a specific error message for invalid MasterData combinations."""
    if region and product and environment:
        region_entries = [e for e in availability if e["region"] == region]
        product_in_region = [e for e in region_entries if e["product"] == product]
        if not region_entries:
            available_regions = sorted({e["region"] for e in availability})
            return f"'{region}' is not an available region. Available regions: {', '.join(available_regions)}"
        if not product_in_region:
            available_products = sorted({e["product"] for e in region_entries})
            return f"'{product}' is not available in {region}. Available products in {region}: {', '.join(available_products)}"
        available_envs = sorted({env for e in product_in_region for env in e["environments"].keys()})
        return f"'{environment}' is not available for {product} in {region}. Available environments: {', '.join(available_envs)}"

    if region and product:
        region_entries = [e for e in availability if e["region"] == region]
        if not region_entries:
            available_regions = sorted({e["region"] for e in availability})
            return f"'{region}' is not an available region. Available regions: {', '.join(available_regions)}"
        available_products = sorted({e["product"] for e in region_entries})
        return f"'{product}' is not available in {region}. Available products in {region}: {', '.join(available_products)}"

    if region and environment:
        region_entries = [e for e in availability if e["region"] == region]
        if not region_entries:
            available_regions = sorted({e["region"] for e in availability})
            return f"'{region}' is not an available region. Available regions: {', '.join(available_regions)}"
        available_envs = sorted({env for e in region_entries for env in e["environments"].keys()})
        return f"'{environment}' is not available in {region}. Available environments in {region}: {', '.join(available_envs)}"

    if product and environment:
        product_entries = [e for e in availability if e["product"] == product]
        if not product_entries:
            available_products = sorted({e["product"] for e in availability})
            return f"'{product}' is not an available product. Available products: {', '.join(available_products)}"
        matching = [e for e in product_entries if environment in e["environments"]]
        if not matching:
            available_envs = sorted({env for e in product_entries for env in e["environments"].keys()})
            return f"'{environment}' is not available for {product}. Available environments for {product}: {', '.join(available_envs)}"
        available_regions = sorted({e["region"] for e in matching})
        return f"{product} with {environment} is only available in: {', '.join(available_regions)}"

    if region:
        available_regions = sorted({e["region"] for e in availability})
        return f"'{region}' is not an available region. Available regions: {', '.join(available_regions)}"
    if product:
        available_products = sorted({e["product"] for e in availability})
        return f"'{product}' is not an available product. Available products: {', '.join(available_products)}"
    if environment:
        available_envs = sorted({env for e in availability for env in e["environments"].keys()})
        return f"'{environment}' is not an available environment. Available environments: {', '.join(available_envs)}"

    return "This combination is not available"





# Maps model field names → (response key name, "attribute" | "placement")
FIELD_MAPPING = {
    "CreateS3": {
        "name": ("identifier", "attribute"),
        "version": ("versioning", "attribute"),
        "replication": ("enable_s3_replication", "attribute"),
        "crossAccountId": ("cross_account_account_id", "attribute"),
        "region": ("geo_loc_mst_code", "placement"),
        "product": ("applications_mst_code", "placement"),
        "environment": ("environment_enum", "placement"),
    },
    "CreateSQS": {
        "name": ("identifier", "attribute"),
        "fifo": ("fifo", "attribute"),
        "dlq": ("dlq", "attribute"),
        "max_receive_count": ("max_receive_count", "attribute"),
        "visibility_timeout_seconds": ("visibility_timeout_seconds", "attribute"),
        "main_queue_retention_seconds": ("main_queue_retention_seconds", "attribute"),
        "dlq_retention_seconds": ("dlq_retention_seconds", "attribute"),
        "cross_account_ids": ("cross_account_ids", "attribute"),
        "region": ("geo_loc_mst_code", "placement"),
        "product": ("applications_mst_code", "placement"),
        "environment": ("environment_enum", "placement"),
    },
    "CreateDynamoDB": {
        "identifier": ("identifier", "attribute"),
        "partition_key": ("partition_key", "attribute"),
        "partition_key_type": ("partition_key_type", "attribute"),
        "region": ("geo_loc_mst_code", "placement"),
        "product": ("applications_mst_code", "placement"),
        "environment": ("environment_enum", "placement"),
    },
    "CreateKongRoute": {
        "route": ("route", "attribute"),
        "method": ("method", "attribute"),
        "service": ("service", "attribute"),
        # Optional. Absent, the route joins the service's default group at
        # priority 0 — the executor says so in its reply rather than leaving it
        # implicit.
        "tag": ("route_group_key", "attribute"),
        "regex_priority": ("regex_priority", "attribute"),
        "region": ("geo_loc_mst_code", "placement"),
        "product": ("applications_mst_code", "placement"),
        "environment": ("environment_enum", "placement"),
    },
    "CreateDatabase": {
        "database_name": ("database_name", "attribute"),
        "database_server": ("db_server_name", "attribute"),
        "region": ("geo_loc_mst_code", "placement"),
        "product": ("applications_mst_code", "placement"),
        "environment": ("environment_enum", "placement"),
    },
}

# Static placement keys added to every response for a given tool
STATIC_PLACEMENT = {
    "CreateS3": {
        "infra_vendor_enum": "aws",
        "case_type_ref_code": "s3",
        "case_ref_code": "create_bucket",
    },
    "CreateSQS": {
        "infra_vendor_enum": "aws",
        "case_type_ref_code": "sqs",
        "case_ref_code": "create_queue",
    },
    "CreateDynamoDB": {
        "infra_vendor_enum": "aws",
        "case_type_ref_code": "dynamodb",
        "case_ref_code": "table_management",
    },
    "CreateKongRoute": {
        "infra_vendor_enum": "aws",
        "case_type_ref_code": "kong_gateway",
        "case_ref_code": "add_route",
    },
    "CreateDatabase": {
        "infra_vendor_enum": "aws",
        "case_type_ref_code": "database",
        "case_ref_code": "database_creation",
    },
}


def _default_conversation_state() -> dict[str, Any]:
    """Return an empty ValidateParams conversation state payload."""
    return {"tool_name": None, "valid": {}}


def _normalize_conversation_state(raw_state: Any) -> dict[str, Any]:
    """Normalize inbound conversation_state payload from caller."""
    logger.info(
    f"[VALIDATE_PARAMS] normalize function start {raw_state}"
    )
    if not isinstance(raw_state, dict):
        return _default_conversation_state()

    tool_name = raw_state.get("tool_name")
    if not isinstance(tool_name, str):
        tool_name = None

    valid = raw_state.get("valid")
    if not isinstance(valid, dict):
        valid = {}

    result = {"tool_name": tool_name, "valid": dict(valid)}
    logger.info(f"[VALIDATE_PARAMS] normalize function end {result}")

    return {
        "tool_name": tool_name,
        "valid": dict(valid),
    }


def _get_enum_values(annotation) -> list[str] | None:
    """Extract enum values from a Literal type annotation."""
    if get_origin(annotation) is Literal:
        args = get_args(annotation)
        if args and all(isinstance(a, str) for a in args):
            return list(args)
    return None


def _get_field_annotation(field_name: str, model: type[BaseModel]):
    """Get the resolved annotation for a field from Pydantic's model_fields.
    This correctly resolves Generic TypeVars (e.g. VersionT → Literal['v1','v2'])."""
    field_info = model.model_fields.get(field_name)
    if field_info is None:
        return None
    return field_info.annotation


def _is_bool_field(annotation) -> bool:
    """Check if the annotation is a bool type (including Optional[bool])."""
    if annotation is bool:
        return True
    # Handle Optional[bool] → Union[bool, None]
    return bool in get_args(annotation)


def _is_int_field(annotation) -> bool:
    """Check if the annotation is an int type (including Optional[int])."""
    if annotation is int:
        return True
    # Handle Optional[int] → Union[int, None]
    return int in get_args(annotation)


def _is_list_field(annotation) -> bool:
    """Check if the annotation is a list type (including Optional[list[...]])."""
    if get_origin(annotation) is list:
        return True
    # Handle Optional[list[...]] → Union[list[...], None]
    return any(get_origin(a) is list for a in get_args(annotation))


def _decode_pattern(pattern: str) -> str:
    """Convert a regex pattern into a human-readable description."""
    # Match common patterns like ^[a-zA-Z]{3,8}$, ^[a-z0-9]{4,10}$, etc.
    m = re.match(r'^\^?\[([^\]]+)\]\{(\d+),(\d+)\}\$?$', pattern)
    if m:
        charset, min_len, max_len = m.group(1), m.group(2), m.group(3)
        # Decode character class
        char_desc = []
        if 'a-z' in charset and 'A-Z' in charset:
            char_desc.append("letters")
        elif 'a-z' in charset:
            char_desc.append("lowercase letters")
        elif 'A-Z' in charset:
            char_desc.append("uppercase letters")
        if '0-9' in charset:
            char_desc.append("numbers")
        chars = " and ".join(char_desc) if char_desc else f"characters matching [{charset}]"
        return f"must be {min_len}-{max_len} {chars} only"

    # Match fixed length like ^[a-zA-Z]{5}$
    m = re.match(r'^\^?\[([^\]]+)\]\{(\d+)\}\$?$', pattern)
    if m:
        charset, length = m.group(1), m.group(2)
        char_desc = []
        if 'a-z' in charset and 'A-Z' in charset:
            char_desc.append("letters")
        if '0-9' in charset:
            char_desc.append("numbers")
        chars = " and ".join(char_desc) if char_desc else f"characters matching [{charset}]"
        return f"must be exactly {length} {chars}"

    # Fallback: skip raw pattern to avoid exposing regex to users
    # return f"must match pattern {pattern}"
    return ""


def _get_field_hint(field_name: str, model: type[BaseModel]) -> str:
    """Build a human-readable hint for a missing field."""
    field_info = model.model_fields.get(field_name)
    if not field_info:
        return ""

    hints = []
    if field_info.description:
        hints.append(field_info.description)

    annotation = _get_field_annotation(field_name, model)
    if annotation:
        enum_vals = _get_enum_values(annotation)
        if enum_vals:
            hints.append(f"Allowed options: {enum_vals}")

    if _is_bool_field(annotation):
        hints.append("yes/no")

    # Pattern hint (use JSON schema — reliable with Generic models)
    json_schema = model.model_json_schema()
    schema_props = json_schema.get("properties", {})
    if field_name in schema_props and "pattern" in schema_props[field_name]:
        decoded = _decode_pattern(schema_props[field_name]["pattern"])
        # Skip if description already covers the pattern info
        if decoded not in " ".join(hints).lower() and decoded.replace("must be ", "") not in " ".join(hints).lower():
            hints.append(decoded)

    return ". ".join(hints)


async def validate_params_structured(
    tool_name: str,
    new_params: dict,
    user_message: str,
    tenant_models: dict[str, type[BaseModel]],
    conversation_state: dict[str, Any],
    tenant_id: str = "",
    thread_id: str = "",
) -> tuple[dict, dict[str, Any]]:
    """Validate params with hallucination detection and caller-managed state."""
    state = _normalize_conversation_state(conversation_state)
    logger.info(
        "[VALIDATE_PARAMS] Incoming call: "
        f"thread_id={thread_id!r}, tool_name={tool_name!r}, tenant_id={tenant_id!r}, "
        f"new_param_keys={list(new_params.keys())}"
    )
    logger.info(
        "[VALIDATE_PARAMS] Incoming conversation_state: "
        f"tool_name={state['tool_name']!r}, valid_count={len(state['valid'])}"
    )

    model = tenant_models.get(tool_name)
    if model is None:
        error_response = {
            "error": f"Unknown tool '{tool_name}'. Available: {list(tenant_models.keys())}",
            "conversation_state": state,
        }
        return error_response, state

    # Reset accumulated values if tool changed across calls.
    if tool_name != state["tool_name"]:
        state = {"tool_name": tool_name, "valid": {}}
        logger.info(
            "[VALIDATE_PARAMS] Reset conversation_state because tool changed: "
            f"new_tool_name={tool_name!r}"
        )

    hallucinated = {}
    invalid = {}
    corrected = {}
    valid_new = {}
    msg_lower = user_message.lower()

    # Pre-compute JSON schema for reliable pattern lookup
    json_schema = model.model_json_schema()
    schema_props = json_schema.get("properties", {})

    # Precompute MasterData valid sets for per-field existence checks
    availability = MasterData.get(tenant_id, [])
    _md_valid_regions = {e["region"] for e in availability} if availability else set()
    _md_valid_products = {e["product"] for e in availability} if availability else set()
    _md_valid_envs = {env for e in availability for env in e["environments"].keys()} if availability else set()

    for pname, value in new_params.items():
        # Fuzzy match param names (e.g. "replicat" → "replication", "versio" → "version")
        if pname not in model.model_fields:
            field_matches = difflib.get_close_matches(pname, list(model.model_fields.keys()), n=1, cutoff=0.6)
            if field_matches:
                corrected[pname] = {"original": pname, "corrected_to": field_matches[0]}
                pname = field_matches[0]
            else:
                invalid[pname] = {"value": value, "reason": f"Unknown parameter. Available: {list(model.model_fields.keys())}"}
                continue

        field_info = model.model_fields[pname]
        annotation = _get_field_annotation(pname, model)

        # Hallucination check: value must appear in user message
        # Skip for booleans (user may say "yes"/"no"/"enable"/"disable")
        # Skip for ints (LLM converts units, e.g. "2 days" → 172800)
        # Skip if param already has a valid value in state (LLM re-sending old values)
        # Skip for fields validated by a custom VALIDATOR plugin (e.g. database_server, service)
        # These accept compound names like "common-pg" that users may type as "common pg"
        _VALIDATOR_CHECKED_FIELDS = {"database_server", "service"}
        is_bool = _is_bool_field(annotation)
        is_int = _is_int_field(annotation)
        skip_hallucination = pname in _VALIDATOR_CHECKED_FIELDS and getattr(model, "VALIDATOR", None) is not None
        val_str = str(value).lower()
        if not is_bool and not is_int and not skip_hallucination and val_str not in msg_lower:
            # Fuzzy fallback: allow LLM-corrected typos (e.g. user typed "londn", LLM sent "london")
            msg_words = msg_lower.split()
            fuzzy_hit = difflib.get_close_matches(val_str, msg_words, n=1, cutoff=0.6)
            if not fuzzy_hit:
                if pname not in state["valid"]:
                    hallucinated[pname] = {"value": value, "reason": "value not found in user message"}
                continue

        errors = []

        # Boolean normalization — supports natural language
        if is_bool:
            if isinstance(value, str):
                val_lower = value.lower()
                if val_lower in _TRUE_WORDS:
                    value = True
                elif val_lower in _FALSE_WORDS:
                    value = False
                else:
                    # Fuzzy match for typos (e.g. "enbale" → "enable")
                    all_words = list(_TRUE_WORDS) + list(_FALSE_WORDS)
                    matches = difflib.get_close_matches(val_lower, all_words, n=1, cutoff=0.7)
                    if matches:
                        matched = matches[0]
                        value = True if matched in _TRUE_WORDS else False
                        corrected[pname] = {"original": val_lower, "corrected_to": matched, "value": value}
                    else:
                        errors.append(f"'{value}' is not recognized. Use yes/no, true/false, enable/disable, or on/off")
            elif not isinstance(value, bool):
                errors.append(f"'{value}' is not recognized. Use yes/no, true/false, enable/disable, or on/off")

        # Clean value — strip leading/trailing punctuation (commas, spaces)
        if isinstance(value, str):
            value = value.strip(" ,;.'\"")

        # List normalization — LLM may send comma-separated string for list fields
        if _is_list_field(annotation) and isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]

        # Region alias normalization: allow short values like "mumbai" even if
        # model enum expects canonical codes like "region-aspora-mumbai".
        if pname == "region" and isinstance(value, str):
            enum_vals = _get_enum_values(annotation) or []
            normalized_region = normalize_geo_loc_code(value)
            if normalized_region in enum_vals and normalized_region != value:
                corrected[pname] = {"original": value, "corrected_to": normalized_region}
                value = normalized_region

        # Enum check via Literal (with fuzzy matching for typos)
        if not errors:
            enum_vals = _get_enum_values(annotation)
            if enum_vals and value not in enum_vals:
                enum_matches = difflib.get_close_matches(str(value).lower(), enum_vals, n=1, cutoff=0.6)
                if enum_matches:
                    corrected[pname] = {"original": value, "corrected_to": enum_matches[0]}
                    value = enum_matches[0]
                else:
                    errors.append(f"allowed options: {enum_vals}")

        # Pattern check (use JSON schema — reliable with Generic models).
        # For Optional[str], pattern may live under anyOf.
        if not errors and pname in schema_props:
            prop_schema = schema_props[pname]
            pattern = prop_schema.get("pattern")
            if not pattern:
                any_of = prop_schema.get("anyOf")
                if isinstance(any_of, list):
                    for item in any_of:
                        if isinstance(item, dict) and "pattern" in item:
                            pattern = item["pattern"]
                            break
            if pattern and not re.match(pattern, str(value)):
                errors.append(_decode_pattern(pattern))

        # Strict placement resolution: product must map to a canonical
        # applications_mst_code for the current tenant/tool.
        if not errors and pname == "product":
            app_code, _label = _resolve_product_application_code(tenant_id, tool_name, value)
            if not app_code:
                errors.append(
                    "Unknown product for this tenant/resource. Please choose a valid product option."
                )

        # MasterData per-field existence check for region/product/environment/service
        if not errors and availability:
            if pname == "region" and value not in _md_valid_regions:
                errors.append(f"'{value}' is not an available region. Available: {sorted(_md_valid_regions)}")
            elif pname == "product" and value not in _md_valid_products:
                errors.append(f"'{value}' is not an available product. Available: {sorted(_md_valid_products)}")
            elif pname == "environment" and value not in _md_valid_envs:
                errors.append(f"'{value}' is not an available environment. Available: {sorted(_md_valid_envs)}")
            # TODO need to uncomment after service master data append with word service 
            # elif pname == "service":
            #     # Merge accumulated + newly validated params to get placement values
            #     # (placement may have been validated in this call or a previous one)
            #     merged = {**state["valid"], **valid_new}
            #     svc_region = merged.get("region")
            #     svc_product = merged.get("product")
            #     svc_env = merged.get("environment")
            #     if all([svc_region, svc_product, svc_env]):
            #         valid_services = get_services_for_entry(tenant_id or "", svc_region, svc_product, svc_env)
            #         if valid_services and value not in valid_services:
            #             logger.info(
            #                 "[VALIDATE_PARAMS][master_data] service '%s' not in available services %s "
            #                 "(region=%r, product=%r, environment=%r)",
            #                 value, sorted(valid_services), svc_region, svc_product, svc_env,
            #             )
            #             errors.append(
            #                 f"'{value}' is not an available service for "
            #                 f"region='{svc_region}', product='{svc_product}', environment='{svc_env}'. "
            #                 f"Available services: {sorted(valid_services)}"
            #             )

        if errors:
            invalid[pname] = {"value": new_params[pname], "reason": "; ".join(errors)}
            state["valid"].pop(pname, None)
        else:
            valid_new[pname] = value

    # Run model's custom validator (complex cross-field / domain rules)
    # Supports both sync and async validators; async ones receive tenant_id
    logger.info(
    f"[VALIDATE_PARAMS] before invoking validator {state}"
    )
    print("before invoking validator {state}")
    validator = getattr(model, "VALIDATOR", None)
    if validator and valid_new:
        if inspect.iscoroutinefunction(validator):
            plugin_errors = await validator(valid_new, dict(state["valid"]), tenant_id=tenant_id)
        else:
            plugin_errors = validator(valid_new, dict(state["valid"]))
        if plugin_errors:
            for pname, err in plugin_errors.items():
                invalid[pname] = err
                valid_new.pop(pname, None)
    logger.info(
    f"[VALIDATE_PARAMS] after invoking validator {state}"
    )
    print("after invoking validator {state}")
    # Save previous placement values before merging (for rollback on combo failure)
    prev_valid = {k: v for k, v in state["valid"].items() if k in ("region", "product", "environment")}

    # Merge new valid values into accumulated state
    state["valid"].update(valid_new)

    # MasterData combination check: fields exist individually but combo may be invalid
    if availability:
        region_val = state["valid"].get("region")
        product_val = state["valid"].get("product")
        env_val = state["valid"].get("environment")
        if region_val or product_val or env_val:
            md_matches = list(availability)
            if region_val:
                md_matches = [e for e in md_matches if e["region"] == region_val]
            if product_val:
                md_matches = [e for e in md_matches if e["product"] == product_val]
            if env_val:
                md_matches = [e for e in md_matches if env_val in e["environments"]]
            if not md_matches:
                reason = _build_master_data_reason(availability, region_val, product_val, env_val)
                # Reject newly-added fields and restore previous values
                rejected = False
                for md_field in ("region", "product", "environment"):
                    if md_field in valid_new:
                        if md_field in prev_valid:
                            state["valid"][md_field] = prev_valid[md_field]
                            # Rolled back to previous value — record as informational
                            # rejection but don't block readiness
                            invalid[md_field] = {
                                "value": valid_new[md_field],
                                "reason": reason,
                                "rolled_back_to": prev_valid[md_field],
                            }
                        else:
                            state["valid"].pop(md_field, None)
                            invalid[md_field] = {"value": valid_new[md_field], "reason": reason}
                        rejected = True
                if not rejected:
                    invalid["_master_data"] = {"reason": reason}

    logger.info(
        "[VALIDATE_PARAMS] After merge: "
        f"thread_id={thread_id!r}, new_valid_keys={list(valid_new.keys())}, "
        f"accumulated_valid_keys={list(state['valid'].keys())}, "
        f"invalid_keys={list(invalid.keys())}, hallucinated_keys={list(hallucinated.keys())}"
    )

    # Build ordered lists (model field order) instead of dicts so LLM preserves order
    field_order = list(model.model_fields.keys())

    def _valid_list() -> list:
        result = []
        for k in field_order:
            if k in state["valid"]:
                result.append({"param": k, "value": state["valid"][k]})
            elif not model.model_fields[k].is_required() and model.model_fields[k].default is not None:
                # Show optional fields with their default values
                result.append({"param": k, "value": model.model_fields[k].default, "default": True})
        return result

    # Find missing params
    missing = []
    for pname in field_order:
        field_info = model.model_fields[pname]
        if pname in state["valid"]:
            continue
        if field_info.is_required():
            missing.append({"param": pname, "hint": _get_field_hint(pname, model), "required": True})
        elif field_info.default is None:
            # Optional with no default — show as not mandatory
            missing.append({"param": pname, "hint": _get_field_hint(pname, model), "required": False})

    # Check if all REQUIRED params are valid.
    # Optional params may be missing and should not block readiness.
    # Rolled-back rejections (value restored to previous) are informational — they
    # should not prevent readiness when the restored state is complete.
    blocking_invalid = {k: v for k, v in invalid.items() if "rolled_back_to" not in v}
    required_missing = [m for m in missing if m.get("required") is True]
    if not blocking_invalid and not required_missing and not hallucinated:
        # Final Pydantic validation (catches model_validator rules)
        try:
            model(**state["valid"])
        except ValidationError as e:
            state["valid"] = {k: v for k, v in state["valid"].items() if k in model.model_fields}
            validation_error_response = {
                "valid": _valid_list(),
                "invalid": {},
                "missing": [],
                "hallucinated": {},
                "validation_error": str(e),
                "conversation_state": state,
            }
            return validation_error_response, state

        ready_response = {
            "ready": True,
            "valid": _valid_list(),
            "invalid": {},
            "missing": [],
            "hallucinated": {},
            "conversation_state": state,
        }
        return ready_response, state

    response = {"valid": _valid_list(), "invalid": invalid, "missing": missing, "hallucinated": hallucinated}
    if corrected:
        response["corrected"] = corrected

    # Build attribute_parameters / placement_parameters from valid state
    # so response_handler can surface collected params to the frontend
    field_map = FIELD_MAPPING.get(tool_name)
    if field_map and state["valid"]:
        attribute_parameters = {}
        placement_parameters = {}
        # Add static placement keys (infra_vendor_enum, case_type_ref_code, etc.)
        static = STATIC_PLACEMENT.get(tool_name, {})
        placement_parameters.update(static)
        for model_field, value in state["valid"].items():
            mapping = field_map.get(model_field)
            if not mapping:
                continue
            service_key, param_type = mapping
            if param_type == "attribute":
                attribute_parameters[service_key] = value
            else:
                if model_field == "product":
                    app_code, _product_label = _resolve_product_application_code(
                        tenant_id, tool_name, value
                    )
                    # Always return canonical product key.
                    if app_code:
                        placement_parameters["applications_mst_code"] = app_code
                        placement_parameters["product_name"] = _product_label or value
                    elif value is not None:
                        placement_parameters["applications_mst_code"] = value
                        placement_parameters["product_name"] = value
                    continue
                if model_field == "region" and isinstance(value, str):
                    value = normalize_geo_loc_code(value)
                placement_parameters[service_key] = value
        if attribute_parameters:
            response["attribute_parameters"] = attribute_parameters
        if placement_parameters:
            response["placement_parameters"] = placement_parameters

    response["conversation_state"] = state
    return response, state

