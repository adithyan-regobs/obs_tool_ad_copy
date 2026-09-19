"""
GitOps Workflow Service

Handles business logic for GitOps workflow operations including
fetching PR history for different entity types (service configs, Dockerfiles, pipelines).
"""
from typing import List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
from app.repository.service_config_dockerfile_workflow_repository import ServiceConfigDockerfileWorkflowRepository
from app.repository.pipeline_mst_repository import PipelineMstRepository
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.service_config_dockerfile_workflow_model import ServiceConfigDockerfileWorkflowModel
from app.db.models.pipeline_mst_model import PipelineMstModel
from app.core.enum import WorkflowSourceTableEnum


class GitopsWorkflowService:
    """Service for GitOps workflow business logic"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.workflow_repo = GitopsWorkflowDetailRepository(db)
        self.dockerfile_workflow_repo = ServiceConfigDockerfileWorkflowRepository(db)
        self.pipeline_repo = PipelineMstRepository(db)

    async def get_dockerfile_pr_history(
        self,
        service_config_id: int,
        tenant_code: str,
        page: int = 1,
        limit: int = 20
    ) -> Tuple[List[GitopsWorkflowDetailModel], int]:
        """
        Get Dockerfile PR history for a service config.

        Fetches all Dockerfile workflow PRs across all branches/repositories for a given service config.

        Args:
            service_config_id: Service config ID
            tenant_code: Tenant code for isolation
            page: Page number (1-indexed)
            limit: Items per page

        Returns:
            Tuple of (list of workflows, total count)

        Example:
            workflows, total = await service.get_dockerfile_pr_history(
                service_config_id=123,
                tenant_code="aspora",
                page=1,
                limit=20
            )
        """
        # Step 1: Get all dockerfile workflow entries for this service config
        dockerfile_workflows = await self.dockerfile_workflow_repo.get_workflows_for_config(
            service_config_id
        )

        if not dockerfile_workflows:
            return [], 0

        # Step 2: Extract workflow codes (transaction codes)
        workflow_codes = [dw.code for dw in dockerfile_workflows]

        # Step 3: Get all gitops workflow details for these codes
        workflows, total = await self.workflow_repo.get_by_transaction_codes_and_table(
            transaction_codes=workflow_codes,
            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
            tenant_code=tenant_code,
            page=page,
            limit=limit
        )

        return workflows, total

    async def get_pipeline_pr_history(
        self,
        transaction_code: str,
        table_name: str = "SERVICE_CONFIG",
        tenant_code: str = "",
        geo_loc_mst_code: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Tuple[List[GitopsWorkflowDetailModel], int]:
        """
        Get Pipeline PR history by transaction code and table name.

        Fetches all Pipeline workflow PRs matching the specified criteria.

        Args:
            transaction_code: Source entity code (service_config.code)
            table_name: Source table discriminator
            tenant_code: Tenant code for isolation
            geo_loc_mst_code: Optional geo location code
            page: Page number (1-indexed)
            limit: Items per page

        Returns:
            Tuple of (list of workflows, total count)

        Example:
            workflows, total = await service.get_pipeline_pr_history(
                transaction_code="svc-config-123",
                table_name="SERVICE_CONFIG",
                tenant_code="aspora",
                geo_loc_mst_code="london",
                page=1,
                limit=20
            )
        """
        # Step 1: Get all pipeline entries matching the filters
        pipelines = await self.pipeline_repo.get_by_filters(
            transaction_code=transaction_code,
            table_name=table_name,
            geo_loc_mst_code=geo_loc_mst_code
        )

        if not pipelines:
            return [], 0

        # Step 2: Extract pipeline codes (transaction codes)
        pipeline_codes = [p.code for p in pipelines]

        # Step 3: Get all gitops workflow details for these codes
        workflows, total = await self.workflow_repo.get_by_transaction_codes_and_table(
            transaction_codes=pipeline_codes,
            table_name=WorkflowSourceTableEnum.PIPELINE,
            tenant_code=tenant_code,
            page=page,
            limit=limit
        )

        return workflows, total
