from pydantic import BaseModel

from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code
from app.infra_chat_agent.config.placement_utils import resolve_product_application_code as _resolve_product_application_code


async def execute_create_database(params: BaseModel, tenant_id: str) -> dict:
    d = params.model_dump()
    app_code, product_label = _resolve_product_application_code(
        tenant_id, "CreateDatabase", d.get("product")
    )
    if not app_code:
        raise ValueError(
            f"Unable to resolve product '{d.get('product')}' to applications_mst_code for tenant '{tenant_id}'"
        )
    product_name = product_label or d["product"]
    geo_loc_code = normalize_geo_loc_code(d["region"])

    attribute_parameters = {
        "database_name": d["database_name"],
        "db_server_name": d["database_server"],
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
        "case_type_ref_code": "database_creation",
    }

    return {
        "status": "success",
        "message": f"Database '{d['database_name']}' creation request validated for server '{d['database_server']}'",
        "attribute_parameters": attribute_parameters,
        "placement_parameters": placement_parameters,
        "is_ready": True,
    }
