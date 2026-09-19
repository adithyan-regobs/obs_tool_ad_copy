from pydantic import BaseModel

from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code
from app.infra_chat_agent.config.placement_utils import resolve_product_application_code as _resolve_product_application_code


async def execute_create_sqs(params: BaseModel, tenant_id: str) -> dict:
    from app.db.session import AsyncSessionLocal
    from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
    from app.services.sqs_creation_service import SqsCreationService

    d = params.model_dump()
    app_code, product_label = _resolve_product_application_code(tenant_id, "CreateSQS", d.get("product"))
    if not app_code:
        raise ValueError(
            f"Unable to resolve product '{d.get('product')}' to applications_mst_code for tenant '{tenant_id}'"
        )
    product_name = product_label or d.get("product", "")
    async with AsyncSessionLocal() as db:
        service = SqsCreationService(db)
        return await service.create_sqs_queue(
            identifier=d["name"],
            tenant_code=tenant_id,
            product_name=product_name,
            applications_mst_code=app_code,
            environment=Environment(d["environment"]),
            geo_loc_code=normalize_geo_loc_code(d["region"]),
            geo_loc=d["region"],
            fifo_queue=d.get("fifo", True),
            create_dlq=d.get("dlq", True),
            max_receive_count=d.get("max_receive_count"),
            visibility_timeout_seconds=d.get("visibility_timeout_seconds"),
            message_retention_seconds=d.get("main_queue_retention_seconds"),
            dlq_message_retention_seconds=d.get("dlq_retention_seconds"),
            cross_account_ids=d.get("cross_account_ids"),
        )
