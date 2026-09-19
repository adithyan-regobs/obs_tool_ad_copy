from pydantic import BaseModel

from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code
from app.infra_chat_agent.config.placement_utils import resolve_product_application_code as _resolve_product_application_code


async def execute_create_dynamodb(params: BaseModel, tenant_id: str) -> dict:
    from app.db.session import AsyncSessionLocal
    from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
    from app.services.dynamodb_creation_service import DynamoDbCreationService

    d = params.model_dump()
    app_code, product_label = _resolve_product_application_code(tenant_id, "CreateDynamoDB", d.get("product"))
    if not app_code:
        raise ValueError(
            f"Unable to resolve product '{d.get('product')}' to applications_mst_code for tenant '{tenant_id}'"
        )
    product_name = product_label or d.get("product", "")
    async with AsyncSessionLocal() as db:
        service = DynamoDbCreationService(db)
        return await service.create_dynamodb_table(
            identifier=d["identifier"],
            partition_key=d["partition_key"],
            partition_key_type=d["partition_key_type"],
            tenant_code=tenant_id,
            product_name=product_name,
            applications_mst_code=app_code,
            environment=Environment(d["environment"]),
            geo_loc_code=normalize_geo_loc_code(d["region"]),
            geo_loc=d["region"],
        )
