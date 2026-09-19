"""
User repository with specific user operations
"""

from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy import select, or_, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.alert_config_model import AlertConfigModel
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.repository.base_repository import BaseRepository
from app.schemas.alert_schemas import CreateAlert
from app.core.enum import DeploymentStatusEnum
from app.domain.factories.infrastructure_transaction_factory import make_alert_config_transaction


class ObsAlertsRepository(BaseRepository[AlertConfigModel]):
    """Repository for AlertConfig operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(AlertConfigModel, session)

    async def check_alert_exists(
        self,
        services_code: str,
        monitoring_policy_code: str
    ) -> Optional[AlertConfigModel]:
        """
        Check if an alert already exists for a service + monitoring policy combination.

        Args:
            services_code: Service code to check
            monitoring_policy_code: Monitoring policy code to check

        Returns:
            AlertConfigModel if exists, None otherwise

        Example:
            existing = await repo.check_alert_exists("service_001", "ec2_cpu_default")
            if existing:
                print(f"Alert already exists with ID: {existing.id}")
        """
        stmt = select(self.model).where(
            self.model.services_mst_code == services_code,
            self.model.monitoring_policy_defaults_ref_code == monitoring_policy_code
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_creation_status(
        self,
        alert_id: int,
        status: DeploymentStatusEnum,
        updated_by: Optional[str] = None
    ) -> Optional[AlertConfigModel]:
        """
        Update creation_status and related tracking fields for an alert.

        Args:
            alert_id: Alert ID to update
            status: New deployment status
            updated_by: Email or username of updater (optional)

        Returns:
            Updated AlertConfigModel or None if not found

        Example:
            alert = await repo.update_creation_status(
                alert_id=123,
                status=DeploymentStatusEnum.PR_CREATED,
                updated_by="admin@example.com"
            )
        """
        alert = await self.get_by_id(alert_id)
        if not alert:
            return None

        updates = {
            "creation_status": status,
            "creation_status_updated_at": datetime.now(timezone.utc)
        }
        if updated_by:
            updates["creation_status_updated_by"] = updated_by

        return await self.update(alert, updates)

    async def bulk_update_creation_status(
        self,
        alert_ids: List[int],
        status: DeploymentStatusEnum,
        updated_by: Optional[str] = None
    ) -> None:
        """
        Bulk update creation_status for multiple alerts.

        Used in bulk operations where multiple alerts share the same workflow.

        Args:
            alert_ids: List of alert IDs to update
            status: New deployment status
            updated_by: Email or username of updater (optional)

        Example:
            # Update status for 25 alerts in bulk operation
            await repo.bulk_update_creation_status(
                alert_ids=[1, 2, 3, ..., 25],
                status=DeploymentStatusEnum.PR_CREATED,
                updated_by="admin@example.com"
            )
        """
        values = {
            "creation_status": status,
            "creation_status_updated_at": datetime.now(timezone.utc)
        }
        if updated_by:
            values["creation_status_updated_by"] = updated_by

        stmt = (
            update(self.model)
            .where(self.model.id.in_(alert_ids))
            .values(**values)
        )
        await self.session.execute(stmt)

    async def link_to_gitops_workflow(
        self,
        alert_ids: List[int],
        workflow_id: int
    ) -> None:
        """
        Link multiple alerts to a GitOps workflow.

        Used in bulk operations where multiple alerts share one PR/workflow.

        Args:
            alert_ids: List of alert IDs to link
            workflow_id: GitOps workflow detail ID

        Example:
            # Link 25 alerts to same workflow (bulk operation)
            await repo.link_to_gitops_workflow(
                alert_ids=[1, 2, 3, ..., 25],
                workflow_id=789
            )
        """
        stmt = (
            update(self.model)
            .where(self.model.id.in_(alert_ids))
            .values(gitops_workflow_id=workflow_id)
        )
        await self.session.execute(stmt)

    async def get_transactions_with_gitops(
        self,
        tenant_code: str,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        sort_order: str = "desc",
        paginate: bool = True
    ) -> Dict[str, Any]:
        """
        Get alert config transactions with LEFT JOIN to gitops_workflow_detail.

        Tenant isolation is achieved by joining through services_mst table.

        Args:
            tenant_code: Tenant code for isolation
            status: Optional filter by creation_status
            skip: Pagination offset
            limit: Page size
            sort_order: 'asc' or 'desc' by created_at
            paginate: If True, apply skip/limit in SQL. If False, return all for service-level pagination.

        Returns:
            Dict with 'total' count and 'data' list of transformed transactions
        """
        # Build query with LEFT JOIN to gitops_workflow_detail
        # Tenant isolation via JOIN to services_mst
        stmt = (
            select(
                # Alert config fields
                self.model.id,
                self.model.code,
                self.model.name,
                self.model.description,
                self.model.created_at,
                self.model.updated_at,
                self.model.is_active,
                self.model.is_deleted,
                self.model.obs_vendor_accounts_mst_code,
                self.model.services_mst_code,
                self.model.infrastructure_mst_code,
                self.model.signal_kind,
                self.model.comparator,
                self.model.threshold_value,
                self.model.threshold_unit,
                self.model.eval_window,
                self.model.for_duration,
                self.model.no_data,
                self.model.severity,
                self.model.monitoring_policy_defaults_ref_code,
                self.model.vendor_status,
                self.model.vendor_monitor_id,
                self.model.vendor_error,
                self.model.creation_status,
                self.model.creation_status_updated_by,
                self.model.creation_status_updated_at,
                self.model.resource_identifier,
                self.model.gitops_workflow_id,
                # Related entity names
                ServicesMstModel.name.label('service_name'),
                ApplicationsMstModel.name.label('application_name'),
                # GitOps workflow fields
                GitopsWorkflowDetailModel.id.label('workflow_id'),
                GitopsWorkflowDetailModel.code.label('workflow_code'),
                GitopsWorkflowDetailModel.name.label('workflow_name'),
                GitopsWorkflowDetailModel.git_repository,
                GitopsWorkflowDetailModel.git_branch,
                GitopsWorkflowDetailModel.git_commit_sha,
                GitopsWorkflowDetailModel.pr_number,
                GitopsWorkflowDetailModel.pr_url,
                GitopsWorkflowDetailModel.workflow_run_id,
                GitopsWorkflowDetailModel.workflow_run_url,
                GitopsWorkflowDetailModel.workflow_run_outputs,
                GitopsWorkflowDetailModel.run_initiated_at,
                GitopsWorkflowDetailModel.run_completed_at,
            )
            .select_from(self.model)
            .outerjoin(
                GitopsWorkflowDetailModel,
                self.model.gitops_workflow_id == GitopsWorkflowDetailModel.id
            )
            .join(
                ServicesMstModel,
                self.model.services_mst_code == ServicesMstModel.code
            )
            .join(
                ApplicationsMstModel,
                ServicesMstModel.applications_mst_code == ApplicationsMstModel.code
            )
            .where(self.model.is_deleted == False)
            .where(ServicesMstModel.tenants_mst_code == tenant_code)  # Tenant isolation via service
            .where(self.model.gitops_workflow_id.isnot(None))
        )

        # Apply status filter
        if status:
            stmt = stmt.where(self.model.creation_status == status)

        # Sort by created_at
        if sort_order == "desc":
            stmt = stmt.order_by(self.model.created_at.desc())
        else:
            stmt = stmt.order_by(self.model.created_at.asc())

        # If paginating at SQL level, get total count first
        if paginate:
            count_stmt = (
                select(func.count())
                .select_from(self.model)
                .join(
                    ServicesMstModel,
                    self.model.services_mst_code == ServicesMstModel.code
                )
                .where(self.model.is_deleted == False)
                .where(ServicesMstModel.tenants_mst_code == tenant_code)
                .where(self.model.gitops_workflow_id.isnot(None))
            )
            if status:
                count_stmt = count_stmt.where(self.model.creation_status == status)
            total_result = await self.session.execute(count_stmt)
            total = total_result.scalar() or 0

            # Apply pagination
            stmt = stmt.offset(skip).limit(limit)
        else:
            total = None  # Will be calculated from data length

        # Execute query
        result = await self.session.execute(stmt)
        rows = result.all()

        # Transform rows to response format
        data = [make_alert_config_transaction(row) for row in rows]

        return {
            "total": total if paginate else len(data),
            "data": data
        }

