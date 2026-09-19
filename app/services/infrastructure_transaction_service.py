"""
Infrastructure Transaction Service

Service layer for fetching unified infrastructure transactions.
Handles tab-based filtering, merging from multiple tables, and pagination.
"""
from typing import Dict, Any, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.kong_route_configs_repository import KongRouteConfigsRepository
from app.repository.alert_configs_repository import ObsAlertsRepository
from app.domain.validators.infrastructure_transaction_rules import InfrastructureTransactionValidator
from app.schemas.infrastructure_transaction_schemas import TransactionTabEnum


class InfrastructureTransactionService:
    """
    Service layer for Infrastructure Transaction operations.

    Handles fetching transactions from:
    - infrastructure_mst (SQS, S3)
    - kong_route_configs (Kong Gateway routes)
    - alert_configs (Datadog monitors)

    Supports tab-based filtering:
    - all: Merge from all tables, sort and paginate in service layer
    - infrastructure: Direct fetch with SQL pagination
    - kong_route: Direct fetch with SQL pagination
    - alert_config: Direct fetch with SQL pagination
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.infrastructure_repo = InfrastructureMstRepository(session)
        self.kong_route_repo = KongRouteConfigsRepository(session)
        self.alert_config_repo = ObsAlertsRepository(session)

    async def get_transactions(
        self,
        tenant_code: str,
        tab: TransactionTabEnum = TransactionTabEnum.ALL,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
        sort_order: str = "desc"
    ) -> Dict[str, Any]:
        """
        Get infrastructure transactions with tab-based filtering.

        Args:
            tenant_code: Tenant code for isolation
            tab: Tab filter (all, infrastructure, kong_route, alert_config)
            status: Optional status filter
            skip: Pagination offset
            limit: Page size
            sort_order: 'asc' or 'desc' by created_at

        Returns:
            Dict with success, tab, total, count, skip, limit, and data list
        """
        # Validate request parameters
        InfrastructureTransactionValidator.validate_request(
            tenant_code=tenant_code,
            skip=skip,
            limit=limit,
            sort_order=sort_order
        )

        if tab == TransactionTabEnum.ALL:
            result = await self._get_all_transactions(
                tenant_code=tenant_code,
                status=status,
                skip=skip,
                limit=limit,
                sort_order=sort_order
            )
        elif tab == TransactionTabEnum.INFRASTRUCTURE:
            result = await self.infrastructure_repo.get_transactions_with_gitops(
                tenant_code=tenant_code,
                status=status,
                skip=skip,
                limit=limit,
                sort_order=sort_order,
                paginate=True
            )
        elif tab == TransactionTabEnum.KONG_ROUTE:
            result = await self.kong_route_repo.get_transactions_with_gitops(
                tenant_code=tenant_code,
                status=status,
                skip=skip,
                limit=limit,
                sort_order=sort_order,
                paginate=True
            )
        elif tab == TransactionTabEnum.ALERT_CONFIG:
            result = await self.alert_config_repo.get_transactions_with_gitops(
                tenant_code=tenant_code,
                status=status,
                skip=skip,
                limit=limit,
                sort_order=sort_order,
                paginate=True
            )
        else:
            result = {"total": 0, "data": []}

        return {
            "success": True,
            "tab": tab.value,
            "total": result["total"],
            "count": len(result["data"]),
            "skip": skip,
            "limit": limit,
            "data": result["data"]
        }

    async def _get_all_transactions(
        self,
        tenant_code: str,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
        sort_order: str = "desc"
    ) -> Dict[str, Any]:
        """
        Fetch from all three tables, merge, sort, and paginate in service layer.

        Args:
            tenant_code: Tenant code for isolation
            status: Optional status filter
            skip: Pagination offset
            limit: Page size
            sort_order: 'asc' or 'desc' by created_at

        Returns:
            Dict with total count and paginated data list
        """
        # Fetch all records without SQL-level pagination
        infra_result = await self.infrastructure_repo.get_transactions_with_gitops(
            tenant_code=tenant_code,
            status=status,
            paginate=False,
            sort_order=sort_order
        )
        kong_result = await self.kong_route_repo.get_transactions_with_gitops(
            tenant_code=tenant_code,
            status=status,
            paginate=False,
            sort_order=sort_order
        )
        alert_result = await self.alert_config_repo.get_transactions_with_gitops(
            tenant_code=tenant_code,
            status=status,
            paginate=False,
            sort_order=sort_order
        )

        # Merge all data
        all_data: List[Dict[str, Any]] = []
        all_data.extend(infra_result["data"])
        all_data.extend(kong_result["data"])
        all_data.extend(alert_result["data"])

        # Sort by createdAt
        reverse = sort_order == "desc"
        all_data.sort(key=lambda x: x.get("createdAt", ""), reverse=reverse)

        # Calculate total and apply pagination
        total = len(all_data)
        paginated_data = all_data[skip:skip + limit]

        return {
            "total": total,
            "data": paginated_data
        }
