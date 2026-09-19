"""describe_resource tool — DEPRECATED.

The schema source of truth has moved to chat-bot-POC. The chatbot drives the
field-by-field collection conversationally via the new `chat` tool, so the
LLM no longer needs an upfront schema.

The implementation below is preserved (commented out) for reference and easy
revert. The tool is no longer registered in server.py.
"""

import logging

from app.db.session import AsyncSessionLocal
from app.mcp_servers.devlift_mcp.auth import get_auth_context
from app.mcp_servers.devlift_mcp.meta_data import (
    get_resource_metadata,
    get_service_metadata,
)
from app.mcp_servers.devlift_mcp.geo_loc_mapping import (
    get_environment_options,
    get_geo_options,
)
from app.services.applications_mst_service import ApplicationsMstService

logger = logging.getLogger(__name__)


async def describe_resource_impl(resource_type: str) -> dict:
    auth_ctx = await get_auth_context()
    if auth_ctx is None:
        return {
            "status": "error",
            "message": "Authentication required — please run the 'authenticate' tool first.",
        }

    tenant_code = auth_ctx.tenant_code
    metadata = (
        get_resource_metadata(tenant_code, resource_type)
        or get_service_metadata(tenant_code, resource_type)
    )
    if not metadata:
        return {
            "status": "error",
            "message": (
                f"Resource type '{resource_type}' is not supported. "
                f"Call list_supported_resources to see what's available."
            ),
        }

    fields = []
    for f in metadata["fields"]:
        field_entry = {
            "name": f["name"],
            "type": f["type"],
            "required": f["required"],
            "description": f["description"],
        }
        if "options" in f:
            field_entry["options"] = f["options"]
        if "default" in f:
            field_entry["default"] = f["default"]
        fields.append(field_entry)

    product_options: list[dict] = []
    try:
        async with AsyncSessionLocal() as db:
            app_service = ApplicationsMstService(db)
            apps = await app_service.get_applications_dropdown(
                tenant_code=tenant_code,
                is_active=True,
            )
            product_options = [
                {"code": a.get("label"), "name": a.get("label")}
                for a in apps
                if a.get("label")
            ]
    except Exception:
        logger.warning("describe_resource: failed to fetch product options", exc_info=True)

    env_labels = get_environment_options(tenant_code)
    env_options = [{"code": e, "name": e} for e in env_labels]

    geo_options_by_env: dict[str, list[dict]] = {}
    for env_label in env_labels:
        env_value = env_label.lower()
        geo_options_by_env[env_label] = [
            {"code": g["label"], "name": g["label"]}
            for g in get_geo_options(tenant_code, env_value)
        ]

    product_field: dict = {
        "name": "product",
        "type": "string",
        "required": True,
        "description": "Deployment product (application).",
    }
    if product_options:
        product_field["options"] = product_options
    else:
        product_field["description"] += " Ask the user — valid values vary per tenant."

    environment_field: dict = {
        "name": "environment",
        "type": "string",
        "required": True,
        "description": "Deployment environment.",
        "options": env_options,
    }

    geo_field: dict = {
        "name": "geo_location",
        "type": "string",
        "required": True,
        "description": (
            "Deployment region. Valid values depend on the chosen environment — "
            "use `options_by_environment[<chosen environment>]`. Do NOT ask the "
            "user for this until product and environment are selected."
        ),
        "options_by_environment": geo_options_by_env,
    }

    return {
        "resource_type": resource_type,
        "label": metadata["label"],
        "description": metadata["description"],
        "fields": fields,
        "placement_order": ["product", "environment", "geo_location"],
        "placement_fields": [product_field, environment_field, geo_field],
    }
