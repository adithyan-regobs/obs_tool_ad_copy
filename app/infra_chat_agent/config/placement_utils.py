"""
Shared placement-parameter utilities.

Provides helpers for resolving product display values to canonical
``applications_mst_code`` UUIDs via the resource-meta repository.
"""
import logging
from typing import Any

from app.infra_chat_agent.config.config_models import InfraTypeCode, TenantId
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo

logger = logging.getLogger(__name__)

# Maps logical tool / flow names → infra-type codes used by resource_meta_repo
TOOL_INFRA_TYPE_MAP: dict[str, InfraTypeCode] = {
    "CreateS3": InfraTypeCode("s3_infrastructuretype_ref"),
    "CreateSQS": InfraTypeCode("sqs_infrastructuretype_ref"),
    "CreateDynamoDB": InfraTypeCode("dynamodb_infrastructuretype_ref"),
    "CreateKongRoute": InfraTypeCode("kong_gateway"),
    "CreateDatabase": InfraTypeCode("database_infrastructuretype_ref"),
    "DatabaseUserManagement": InfraTypeCode("database_user_infrastructuretype_ref"),
}


def resolve_product_application_code(
    tenant_id: str,
    tool_name: str,
    product_value: Any,
) -> tuple[str | None, str | None]:
    """
    Resolve a product display value to its ``applications_mst_code`` UUID
    for the given tool / tenant.

    Returns:
        ``(applications_mst_code, product_label)`` when resolvable,
        otherwise ``(None, None)``.
    """
    if not tenant_id or not isinstance(product_value, str) or not product_value.strip():
        return None, None

    infra_type = TOOL_INFRA_TYPE_MAP.get(tool_name)
    if not infra_type:
        return None, None

    try:
        tid = TenantId(tenant_id)
        resolved_code = resource_meta_repo.resolve_placement_value(
            tid, infra_type, "applications_mst_code", product_value
        )
        options = resource_meta_repo.get_placement_options(
            tid, infra_type, "applications_mst_code"
        )
        if not options:
            return None, None

        matched = next(
            (
                opt
                for opt in options
                if str(opt.get("value", "")).strip().lower()
                == str(resolved_code).strip().lower()
            ),
            None,
        )
        if not matched:
            return None, None
        return str(matched["value"]), str(matched["label"])
    except Exception as exc:
        logger.warning(
            "[PLACEMENT_UTILS] Failed to resolve applications_mst_code from product: "
            f"tenant_id={tenant_id!r}, tool_name={tool_name!r}, "
            f"product_value={product_value!r}, error={exc}"
        )
        return None, None
