"""
DynamoDB Ops API Endpoints
"""
from typing import Tuple

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.enum import EnvironmentEnum
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.schemas.validator_response_schemas import ValidationResult
from app.services.aws_ops.dynamodb_ops import DynamoDBOps

router = APIRouter()


class ValidateDuplicateTableRequest(BaseModel):
    product_code: str = Field(..., description="Application/product code")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    geo_loc: str = Field(..., description="Geographic location code")
    table_name: str = Field(..., description="DynamoDB table name to validate")


@router.post(
    "/validate-duplicate-table",
    response_model=ValidationResult,
    summary="Validate if a DynamoDB table name already exists",
)
async def validate_duplicate_table(
    payload: ValidateDuplicateTableRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> ValidationResult:
    """
    Check whether a DynamoDB table with the given name already exists for the
    (product_code, environment, geo_loc) combination.

    Returns a `ValidationResult` — the standard validator response format.
    See `app.schemas.validator_response_schemas.ValidationResult`.
    """
    _, tenant = user_and_tenant
    repo = InfrastructureMstRepository(db)
    ops = DynamoDBOps(infrastructure_repo=repo)
    return await ops.duplicate_table_validator(
        tenant_code=tenant.code,
        product_code=payload.product_code,
        environment=payload.environment,
        geo_loc=payload.geo_loc,
        table_name=payload.table_name,
    )
