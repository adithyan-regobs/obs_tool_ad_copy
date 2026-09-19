"""
AWS Database Ops API Endpoints
"""
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.enum import EnvironmentEnum
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.repository.db_object_mst_repository import DbObjectMstRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.schemas.dropdown_option_schemas import DropdownOption
from app.schemas.validator_response_schemas import ValidationResult
from app.services.aws_ops.database_ops import DatabaseOps

router = APIRouter()


class FetchServersRequest(BaseModel):
    product_code: str = Field(..., description="Application/product code")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    geo_loc: str = Field(..., description="Geographic location code")


class ValidateDuplicateDatabaseRequest(BaseModel):
    server_code: str = Field(..., description="Server code (infrastructure_mst_code)")
    database_name: str = Field(..., description="Database name to validate")


@router.post(
    "/servers",
    response_model=List[DropdownOption],
    summary="Fetch AWS database servers for a product/env/geo",
)
async def fetch_servers(
    payload: FetchServersRequest,
    page: Optional[int] = Query(None, ge=1, description="1-indexed page number; omit for all results"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> List[DropdownOption]:
    """
    Return all AWS database servers (RDS / Aurora — MySQL & Postgres)
    for the (product_code, environment, geo_loc) combination as
    `[{label, value}]` dropdown options.

    If `page` query param is provided, results are paginated; otherwise all
    matching servers are returned.
    """
    _, tenant = user_and_tenant
    repo = InfrastructureMstRepository(db)
    ops = DatabaseOps(infrastructure_repo=repo)
    return await ops.fetch_servers(
        tenant_code=tenant.code,
        product_code=payload.product_code,
        environment=payload.environment,
        geo_loc=payload.geo_loc,
        page=page,
    )


@router.post(
    "/validate-duplicate-database",
    response_model=ValidationResult,
    summary="Validate if a database with the given name already exists on a server",
)
async def validate_duplicate_database(
    payload: ValidateDuplicateDatabaseRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> ValidationResult:
    """
    Check whether a database with the given name already exists on the given
    server (looked up in `db_object_mst` by infrastructure_mst_code).

    Returns a `ValidationResult` — the standard validator response format.
    """
    _, tenant = user_and_tenant
    infra_repo = InfrastructureMstRepository(db)
    db_object_repo = DbObjectMstRepository(db)
    ops = DatabaseOps(
        infrastructure_repo=infra_repo,
        db_object_mst_repo=db_object_repo,
    )
    return await ops.duplicate_database_validator(
        tenant_code=tenant.code,
        server_code=payload.server_code,
        database_name=payload.database_name,
    )
