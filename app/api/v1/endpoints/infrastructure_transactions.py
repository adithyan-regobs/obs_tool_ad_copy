"""
Infrastructure Transactions Endpoint

API endpoint for fetching unified infrastructure transactions from
infrastructure_mst, kong_route_configs, and alert_configs tables.
"""
from typing import Tuple
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.infrastructure_transaction_service import InfrastructureTransactionService
from app.domain.validators.infrastructure_transaction_rules import InfrastructureTransactionValidationError
from app.schemas.infrastructure_transaction_schemas import (
    GetInfraTransactionsRequest,
    InfraTransactionsListResponse,
    TransactionTabEnum
)
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

router = APIRouter()


@router.post(
    "/get-transactions",
    response_model=InfraTransactionsListResponse,
    summary="Get Infrastructure Transactions"
)
async def get_infrastructure_transactions(
    data: GetInfraTransactionsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get infrastructure transactions with tab-based filtering.

    Fetches transactions from:
    - infrastructure_mst (SQS, S3 resources)
    - kong_route_configs (Kong Gateway routes)
    - alert_configs (Datadog monitors)

    Security:
        - JWT authentication required
        - Tenant isolation enforced via service/infrastructure relationships

    Request Body:
        - tab: Filter by type (all, infrastructure, kong_route, alert_config)
        - status: Optional filter by deployment status
        - skip: Pagination offset (default: 0)
        - limit: Page size (default: 20, max: 100)
        - sort_order: 'asc' or 'desc' by created_at (default: desc)

    Response:
        - success: Boolean success flag
        - tab: Current tab filter
        - total: Total matching records
        - count: Records in this response
        - skip: Pagination offset
        - limit: Page size
        - data: List of transactions with gitops details

    Examples:
        # Get all transactions
        POST /api/v1/infrastructure-transactions/get-transactions
        Body: {"tab": "all"}

        # Get infrastructure-only transactions
        POST /api/v1/infrastructure-transactions/get-transactions
        Body: {"tab": "infrastructure", "skip": 0, "limit": 20}

        # Filter by status
        POST /api/v1/infrastructure-transactions/get-transactions
        Body: {"tab": "all", "status": "SUCCESS"}
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Initialize service layer
        service = InfrastructureTransactionService(db)

        # Call service layer with tenant_code from JWT
        result = await service.get_transactions(
            tenant_code=tenant.code,
            tab=data.tab,
            status=data.status,
            skip=data.skip,
            limit=data.limit,
            sort_order=data.sort_order
        )

        return result

    except InfrastructureTransactionValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
