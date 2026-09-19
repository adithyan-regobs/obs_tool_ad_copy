from pydantic import BaseModel

from app.domain.policies.kong_route_group_naming import describe_group_choice
from app.infra_chat_agent.config.master_data_config import ServiceMasterData
from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code
from app.infra_chat_agent.config.placement_utils import resolve_product_application_code as _resolve_product_application_code


async def execute_create_kong_route(params: BaseModel, tenant_id: str) -> dict:
    d = params.model_dump()
    app_code, product_label = _resolve_product_application_code(
        tenant_id, "CreateKongRoute", d.get("product")
    )
    if not app_code:
        raise ValueError(
            f"Unable to resolve product '{d.get('product')}' to applications_mst_code for tenant '{tenant_id}'"
        )
    product_name = product_label or d["product"]
    geo_loc_code = normalize_geo_loc_code(d["region"])

    service_name = d["service"]
    lookup_key = service_name.removesuffix("-service") if service_name else ""
    service_entry = ServiceMasterData.get(lookup_key, {})
    service_mst_code = service_entry.get("code", "")

    # Derive api_name: strip "-service" suffix (e.g. "goms-service" → "goms")
    api_name = service_name.removesuffix("-service") if service_name else ""

    # The route group and its priority. Both optional: no tag means the service's
    # default group, and priority is a GROUP-level value that only means anything
    # once a route shares its path with another group — Kong serves the higher and
    # shadows the other, and equal values have no tie-break at all.
    route_group_key = (d.get("tag") or "").strip() or None
    regex_priority = int(d.get("regex_priority") or 0)

    attribute_parameters = {
        "route": d["route"],
        "method": d["method"],
        "service": d["service"],
        "api_name": api_name,
        "route_group_key": route_group_key,
        "regex_priority": regex_priority,
    }

    placement_parameters = {
        "tenant_code": tenant_id,
        "product_name": product_name,
        "environment": d["environment"],
        "geo_loc": d["region"],
        "geo_loc_code": geo_loc_code,
        "infra_vendor": "aws",
        "applications_mst_code": app_code,
        "environment_enum": d["environment"],
        "geo_loc_mst_code": geo_loc_code,
        "infra_vendor_enum": "aws",
        "case_type_ref_code": "kong_gateway",
        "case_ref_code": "add_route",
        "service_mst_code": service_mst_code,
    }

    # Chat shows this string verbatim (see create_flow_v2_node), so it is the only
    # place the user learns which group the route went into. Silence here is how
    # every chat-created route ended up in the default group with nobody choosing.
    group_note = describe_group_choice(api_name, route_group_key)
    priority_note = f" Priority: {regex_priority}." if regex_priority else ""

    return {
        "status": "success",
        "message": (
            f"Kong route '{d['route']}' ({d['method']}) creation request validated. "
            f"{group_note}{priority_note}"
        ),
        "attribute_parameters": attribute_parameters,
        "placement_parameters": placement_parameters,
        "is_ready": True,
    }
