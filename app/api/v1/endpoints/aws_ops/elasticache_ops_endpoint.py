"""
AWS ElastiCache Ops API Endpoints
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
from app.services.aws_ops.elasticache_ops import ElastiCacheOps

router = APIRouter()


class ValidateDuplicateClusterRequest(BaseModel):
    product_code: str = Field(..., description="Application/product code")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    geo_loc: str = Field(..., description="Geographic location code")
    identifier: str = Field(..., description="Redis cluster identifier to validate")


@router.post(
    "/validate-duplicate-cluster",
    response_model=ValidationResult,
    summary="Validate if an ElastiCache Redis cluster identifier already exists",
)
async def validate_duplicate_cluster(
    payload: ValidateDuplicateClusterRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> ValidationResult:
    """
    Check whether an ElastiCache Redis cluster with the given identifier
    already exists for the (product_code, environment, geo_loc) combination.

    Returns a `ValidationResult` — the standard validator response format.
    See `app.schemas.validator_response_schemas.ValidationResult`.
    """
    _, tenant = user_and_tenant
    repo = InfrastructureMstRepository(db)
    ops = ElastiCacheOps(infrastructure_repo=repo)
    return await ops.duplicate_cluster_validator(
        tenant_code=tenant.code,
        product_code=payload.product_code,
        environment=payload.environment,
        geo_loc=payload.geo_loc,
        identifier=payload.identifier,
    )
