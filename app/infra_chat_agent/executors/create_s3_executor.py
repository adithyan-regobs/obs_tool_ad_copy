from pydantic import BaseModel

from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code
from app.infra_chat_agent.config.placement_utils import resolve_product_application_code as _resolve_product_application_code


async def execute_create_s3(params: BaseModel, tenant_id: str) -> dict:
    from app.db.session import AsyncSessionLocal
    from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
    from app.services.s3_creation_service import S3CreationService

    d = params.model_dump()
    app_code, product_label = _resolve_product_application_code(tenant_id, "CreateS3", d.get("product"))
    if not app_code:
        raise ValueError(
            f"Unable to resolve product '{d.get('product')}' to applications_mst_code for tenant '{tenant_id}'"
        )
    product_name = product_label or d["product"]
    async with AsyncSessionLocal() as db:
        service = S3CreationService(db)
        return await service.create_s3_bucket(
            identifier=d["name"],
            tenant_code=tenant_id,
            product_name=product_name,
            applications_mst_code=app_code,
            environment=Environment(d["environment"]),
            geo_loc=d["region"],
            geo_loc_code=normalize_geo_loc_code(d["region"]),
            infra_vendor="aws",
            versioning=d.get("version", False),
            enable_s3_replication=d.get("replication") or False,
            cross_account_account_id=d.get("crossAccountId"),
        )
