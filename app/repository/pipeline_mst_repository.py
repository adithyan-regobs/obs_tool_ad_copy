"""
Pipeline Master Repository
Handles data access operations for pipeline_mst table
"""
from typing import Optional, Dict, Any
from sqlalchemy import select, and_, func, literal_column, distinct, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from app.db.models.pipeline_mst_model import PipelineMstModel
from app.db.models.pipeline_vendor_mst_model import PipelineVendorMstModel
from app.db.models.language_ref_model import LanguageRefModel
from app.db.models.pipeline_run_track_model import PipelineRunTrackModel
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.core.enum import WorkflowSourceTableEnum
from app.repository.base_repository import BaseRepository


class PipelineMstRepository(BaseRepository[PipelineMstModel]):
    """Repository for PipelineMst operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(PipelineMstModel, session)

    async def check_pipeline_exists(
        self,
        transaction_code: str,
        repo_url: str,
        branch: str,
        table_name: str = "SERVICE_CONFIG",
        workflow_file_path: Optional[str] = None
    ) -> Optional[PipelineMstModel]:
        """
        Check if a pipeline already exists for the given configuration.

        A pipeline is considered duplicate if it has the same:
        - transaction_code + table_name
        - repo_url
        - branch
        - workflow_file_path (optional, checked in deployment_config JSONB)

        Args:
            transaction_code: Source entity code (service_config.code or infrastructure_mst.code)
            repo_url: GitHub repository URL
            branch: Git branch name
            table_name: Source table discriminator
            workflow_file_path: Path to workflow file (optional, for save_pipeline)

        Returns:
            Existing pipeline if found, None otherwise
        """
        conditions = [
            self.model.transaction_code == transaction_code,
            self.model.table_name == table_name,
            self.model.repo_url == repo_url,
            self.model.repo_branch == branch,
        ]

        # Add workflow_file_path check if provided (stored in deployment_config JSONB)
        if workflow_file_path:
            conditions.append(
                self.model.deployment_config['workflow_file_path'].astext == workflow_file_path
            )

        stmt = select(self.model).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[PipelineMstModel]:
        """
        Get pipeline by code.

        Args:
            code: Pipeline code

        Returns:
            Pipeline if found, None otherwise
        """
        return await self.get_by(code=code)

    async def get_all_by_transaction_code(
        self,
        transaction_code: str,
        table_name: str = "SERVICE_CONFIG",
        skip: int = 0,
        limit: int = 100
    ) -> list[PipelineMstModel]:
        """
        Get all pipelines for a source entity with pagination.

        Args:
            transaction_code: Source entity code (service_config.code or infrastructure_mst.code)
            table_name: Source table discriminator
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of pipelines
        """
        filters = [
            self.model.transaction_code == transaction_code,
            self.model.table_name == table_name
        ]
        return await self.get_multi(skip=skip, limit=limit, filters=filters)

    async def get_pipelines_by_transaction_code(
        self,
        transaction_code: str,
        table_name: str = "SERVICE_CONFIG",
        skip: int = 0,
        limit: int = 100,
        geo_loc_mst_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get all pipelines for a source entity with related data from joined tables.

        This method performs JOINs with:
        - pipeline_vendor_mst: to get pipeline_agent_enum
        - language_ref: to get language name
        - pipeline_run_track: to get last deploy time and status (LATERAL JOIN)

        Args:
            transaction_code: Source entity code to filter pipelines
            table_name: Source table discriminator
            skip: Number of records to skip for pagination
            limit: Maximum number of records to return
            geo_loc_mst_code: Optional filter by geographic location

        Returns:
            Dict containing:
            - total: Total count of pipelines
            - pipelines: List of pipeline dictionaries with all requested fields
        """
        # Create LATERAL subquery to get the most recent pipeline run
        latest_run_lateral = (
            select(
                PipelineRunTrackModel.created_at.label('last_deploy_time'),
                PipelineRunTrackModel.status.label('last_deploy_status')
            )
            .where(
                and_(
                    PipelineRunTrackModel.pipeline_mst_code == PipelineMstModel.code,
                    PipelineRunTrackModel.is_deleted == False
                )
            )
            .order_by(PipelineRunTrackModel.created_at.desc())
            .limit(1)
            .lateral('latest_run')
        )

        # Build filter conditions
        # Default: match pipelines whose own transaction_code equals the resource code.
        transaction_match = PipelineMstModel.transaction_code == transaction_code

        if table_name == "INFRASTRUCTURE":
            # Infra-apply creates a single shared pipeline_mst per tenant+env keyed by
            # "INFRA_APPLY_<tenant>_<env>", so the direct transaction_code match fails for
            # individual infra resources (S3, SQS, DynamoDB, Redis). Widen the filter to
            # also include any pipeline whose runs carry a transaction_queue entry that
            # points at this resource's infrastructure_mst code.
            linked_pipeline_subq = (
                select(distinct(PipelineRunTrackModel.pipeline_mst_code))
                .select_from(PipelineRunTrackModel)
                .join(
                    TransactionQueueModel,
                    text("transaction_queue.code = ANY(SELECT jsonb_array_elements_text(pipeline_run_track.transaction_queue_code))"),
                )
                .where(
                    TransactionQueueModel.transaction_code == transaction_code,
                    TransactionQueueModel.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE,
                    PipelineRunTrackModel.is_deleted == False,
                )
                .scalar_subquery()
            )
            transaction_match = transaction_match | PipelineMstModel.code.in_(linked_pipeline_subq)

        filter_conditions = [
            transaction_match,
            PipelineMstModel.table_name == table_name,
            PipelineMstModel.is_deleted == False
        ]

        # Add optional filters
        if geo_loc_mst_code:
            filter_conditions.append(
                PipelineMstModel.deployment_config['geo_loc_mst_code'].astext == geo_loc_mst_code
            )

        # Build the main query with JOINs
        stmt = (
            select(
                PipelineMstModel.table_name,
                PipelineMstModel.created_at,
                PipelineMstModel.name,
                PipelineMstModel.code,
                PipelineMstModel.description,
                PipelineMstModel.repo_url,
                PipelineMstModel.repo_branch,
                PipelineMstModel.deployment_config,
                PipelineMstModel.transaction_code,
                PipelineVendorMstModel.pipeline_agent_enum,
                LanguageRefModel.name.label('language_name'),
                latest_run_lateral.c.last_deploy_time,
                latest_run_lateral.c.last_deploy_status,
                # GitOps workflow detail columns
                GitopsWorkflowDetailModel.id.label('gitops_workflow_id'),
                GitopsWorkflowDetailModel.code.label('gitops_workflow_code'),
                GitopsWorkflowDetailModel.name.label('gitops_workflow_name'),
                GitopsWorkflowDetailModel.git_repository,
                GitopsWorkflowDetailModel.git_branch,
                GitopsWorkflowDetailModel.git_commit_sha,
                GitopsWorkflowDetailModel.pr_number,
                GitopsWorkflowDetailModel.pr_url,
                GitopsWorkflowDetailModel.pr_status,
                GitopsWorkflowDetailModel.workflow_run_id,
                GitopsWorkflowDetailModel.workflow_run_url,
                GitopsWorkflowDetailModel.run_initiated_at,
                GitopsWorkflowDetailModel.run_completed_at,
                GitopsWorkflowDetailModel.created_at.label('gitops_workflow_created_at')
            )
            .select_from(PipelineMstModel)
            .outerjoin(
                PipelineVendorMstModel,
                PipelineMstModel.pipeline_vendor_mst_code == PipelineVendorMstModel.code
            )
            .outerjoin(
                LanguageRefModel,
                PipelineMstModel.language_ref_code == LanguageRefModel.code
            )
            .outerjoin(latest_run_lateral, literal_column('true'))  # LEFT JOIN LATERAL
            .outerjoin(
                GitopsWorkflowDetailModel,
                PipelineMstModel.gitops_workflow_id == GitopsWorkflowDetailModel.id
            )
            .where(and_(*filter_conditions))
            .order_by(PipelineMstModel.created_at.desc())
        )

        # Get total count before pagination (using same filter conditions)
        count_stmt = select(func.count()).select_from(
            select(PipelineMstModel.id)
            .select_from(PipelineMstModel)
            .where(and_(*filter_conditions))
            .subquery()
        )
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0

        # Apply pagination
        stmt = stmt.offset(skip).limit(limit)

        # Execute query
        result = await self.session.execute(stmt)
        rows = result.all()

        # Transform results to dictionaries
        pipelines = []
        for row in rows:
            # Extract geo_loc_mst_code and workflow_file_path from deployment_config JSONB
            deployment_config = row.deployment_config or {}
            geo_loc_mst_code = deployment_config.get("geo_loc_mst_code") if isinstance(deployment_config, dict) else None
            workflow_file_path = deployment_config.get("workflow_file_path") if isinstance(deployment_config, dict) else None

            # Build gitops_workflow object if workflow exists
            gitops_workflow = None
            if row.gitops_workflow_id:
                gitops_workflow = {
                    "id": row.gitops_workflow_id,
                    "code": row.gitops_workflow_code,
                    "name": row.gitops_workflow_name,
                    "git_repository": row.git_repository,
                    "git_branch": row.git_branch,
                    "git_commit_sha": row.git_commit_sha,
                    "pr_number": row.pr_number,
                    "pr_url": row.pr_url,
                    "pr_status": row.pr_status.value if row.pr_status and hasattr(row.pr_status, 'value') else row.pr_status,
                    "workflow_run_id": row.workflow_run_id,
                    "workflow_run_url": row.workflow_run_url,
                    "run_initiated_at": row.run_initiated_at,
                    "run_completed_at": row.run_completed_at,
                    "created_at": row.gitops_workflow_created_at
                }

            pipelines.append({
                "table_name": row.table_name,
                "created_at": row.created_at,
                "name": row.name,
                "code": row.code,
                "description": row.description,
                "repo_url": row.repo_url,
                "repo_branch": row.repo_branch,
                "geo_loc_mst_code": geo_loc_mst_code,
                "transaction_code": row.transaction_code,
                "workflow_file_path": workflow_file_path,
                "pipeline_agent": row.pipeline_agent_enum.value if row.pipeline_agent_enum and hasattr(row.pipeline_agent_enum, 'value') else (row.pipeline_agent_enum or ""),
                "language": row.language_name or "",
                "last_deploy_time": row.last_deploy_time,
                "last_deploy_status": row.last_deploy_status.value if row.last_deploy_status and hasattr(row.last_deploy_status, 'value') else row.last_deploy_status,
                "gitops_workflow": gitops_workflow
            })

        return {
            "total": total,
            "pipelines": pipelines
        }

    async def get_by_filters(
        self,
        transaction_code: str,
        table_name: str = "SERVICE_CONFIG",
        geo_loc_mst_code: Optional[str] = None,
        pipeline_vendor_mst_code: Optional[str] = None
    ) -> list[PipelineMstModel]:
        """
        Get pipelines matching the specified filters.

        Args:
            transaction_code: Source entity code (service_config.code or infrastructure_mst.code)
            table_name: Source table discriminator
            geo_loc_mst_code: Optional geo location code
            pipeline_vendor_mst_code: Optional pipeline vendor code

        Returns:
            List of PipelineMstModel matching the filters
        """
        filters = [
            self.model.transaction_code == transaction_code,
            self.model.table_name == table_name,
            self.model.is_deleted == False
        ]

        if pipeline_vendor_mst_code:
            filters.append(self.model.pipeline_vendor_mst_code == pipeline_vendor_mst_code)

        stmt = select(self.model).where(*filters)

        if geo_loc_mst_code:
            stmt = stmt.where(
                self.model.deployment_config['geo_loc_mst_code'].astext == geo_loc_mst_code
            )

        stmt = stmt.order_by(self.model.code)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())
