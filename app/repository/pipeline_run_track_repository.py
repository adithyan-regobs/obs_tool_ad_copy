"""
Repository for PipelineRunTrack operations.
Handles database operations for pipeline run tracking.
"""

from typing import Optional, List, Any, Dict
from sqlalchemy import select, update, and_, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.pipeline_run_track_model import PipelineRunTrackModel
from app.core.enum import PipelineRunStatusEnum
import logging

logger = logging.getLogger(__name__)


class PipelineRunTrackRepository:
    """Repository for pipeline run track operations"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        pipeline_mst_code: str,
        code: str,
        status: PipelineRunStatusEnum = PipelineRunStatusEnum.PENDING,
        log_url: Optional[str] = None,
        commit_sha: Optional[str] = None,
        github_run_id: Optional[str] = None,
        error_message: Optional[str] = None,
        build_number: Optional[int] = None,
        transaction_queue_code: Optional[List[str]] = None,
        build_stages: Optional[List[Any]] = None,
        vendor_deployment_id: Optional[str] = None,
    ) -> PipelineRunTrackModel:
        """
        Create a new pipeline run track record.

        Args:
            pipeline_mst_code: Code of the pipeline
            code: Unique code for this run
            status: Initial status (default: PENDING)
            log_url: URL to pipeline logs
            commit_sha: GitHub commit SHA
            github_run_id: GitHub Actions run ID
            error_message: Error message if any
            build_number: Jenkins build number for webhook lookup
            transaction_queue_code: List of transaction_queue codes for this run
            build_stages: Stage breakdown [{name, status, duration_secs, started_at}]

        Returns:
            Created PipelineRunTrackModel instance
        """
        run_track = PipelineRunTrackModel(
            pipeline_mst_code=pipeline_mst_code,
            code=code,
            status=status,
            log_url=log_url,
            commit_sha=commit_sha,
            github_run_id=github_run_id,
            error_message=error_message,
            build_number=build_number,
            transaction_queue_code=transaction_queue_code,
            build_stages=build_stages,
            vendor_deployment_id=vendor_deployment_id,
            is_active=True,
            is_deleted=False
        )

        self.db.add(run_track)
        await self.db.commit()
        await self.db.refresh(run_track)

        logger.info(f"Created pipeline run track: {code}")
        return run_track

    async def get_by_code(self, code: str) -> Optional[PipelineRunTrackModel]:
        """
        Get pipeline run track by code.

        Args:
            code: Unique run code

        Returns:
            PipelineRunTrackModel or None
        """
        stmt = select(PipelineRunTrackModel).where(
            PipelineRunTrackModel.code == code,
            PipelineRunTrackModel.is_deleted == False
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_pipeline_and_build_number(
        self,
        pipeline_mst_code: str,
        build_number: int,
    ) -> Optional[PipelineRunTrackModel]:
        """Find a run track by pipeline code + Jenkins build number."""
        stmt = select(PipelineRunTrackModel).where(
            and_(
                PipelineRunTrackModel.pipeline_mst_code == pipeline_mst_code,
                PipelineRunTrackModel.build_number == build_number,
                PipelineRunTrackModel.is_deleted == False,
            )
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_pipeline_code(
        self,
        pipeline_mst_code: str,
        limit: int = 10
    ) -> List[PipelineRunTrackModel]:
        """
        Get recent runs for a pipeline.

        Args:
            pipeline_mst_code: Pipeline code
            limit: Maximum number of runs to return

        Returns:
            List of PipelineRunTrackModel instances
        """
        stmt = (
            select(PipelineRunTrackModel)
            .where(
                PipelineRunTrackModel.pipeline_mst_code == pipeline_mst_code,
                PipelineRunTrackModel.is_deleted == False
            )
            .order_by(PipelineRunTrackModel.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def update(
        self,
        code: str,
        status: Optional[PipelineRunStatusEnum] = None,
        log_url: Optional[str] = None,
        commit_sha: Optional[str] = None,
        github_run_id: Optional[str] = None,
        error_message: Optional[str] = None,
        build_number: Optional[int] = None,
        build_stages: Optional[List[Any]] = None,
        vendor_deployment_id: Optional[str] = None,
        deploy_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[PipelineRunTrackModel]:
        """
        Update pipeline run track record.

        Returns:
            Updated PipelineRunTrackModel or None
        """
        # Build update dict with only provided values
        update_data = {}
        if status is not None:
            update_data["status"] = status
        if log_url is not None:
            update_data["log_url"] = log_url
        if commit_sha is not None:
            update_data["commit_sha"] = commit_sha
        if github_run_id is not None:
            update_data["github_run_id"] = github_run_id
        if error_message is not None:
            update_data["error_message"] = error_message
        if build_number is not None:
            update_data["build_number"] = build_number
        if build_stages is not None:
            update_data["build_stages"] = build_stages
        if vendor_deployment_id is not None:
            update_data["vendor_deployment_id"] = vendor_deployment_id
        if deploy_result is not None:
            update_data["deploy_result"] = deploy_result

        if not update_data:
            logger.warning(f"No fields to update for run track: {code}")
            return await self.get_by_code(code)

        stmt = (
            update(PipelineRunTrackModel)
            .where(
                PipelineRunTrackModel.code == code,
                PipelineRunTrackModel.is_deleted == False
            )
            .values(**update_data)
            .returning(PipelineRunTrackModel)
        )

        result = await self.db.execute(stmt)
        await self.db.commit()

        updated_run = result.scalar_one_or_none()
        if updated_run:
            logger.info(f"Updated pipeline run track: {code}")

        return updated_run

    async def get_by_vendor_deployment_id(
        self,
        vendor_deployment_id: str,
    ) -> List[PipelineRunTrackModel]:
        """Get all run_track rows for a given vendor_deployment_id (e.g. Temporal workflow_id)."""
        stmt = (
            select(PipelineRunTrackModel)
            .where(
                PipelineRunTrackModel.vendor_deployment_id == vendor_deployment_id,
                PipelineRunTrackModel.is_deleted == False,
            )
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def delete(self, code: str) -> bool:
        """
        Soft delete a pipeline run track record.

        Args:
            code: Unique run code

        Returns:
            True if deleted, False if not found
        """
        stmt = (
            update(PipelineRunTrackModel)
            .where(
                PipelineRunTrackModel.code == code,
                PipelineRunTrackModel.is_deleted == False
            )
            .values(is_deleted=True, is_active=False)
        )

        result = await self.db.execute(stmt)
        await self.db.commit()

        if result.rowcount > 0:
            logger.info(f"Deleted pipeline run track: {code}")
            return True
        return False

    async def get_run_history(
        self,
        pipeline_mst_code: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        resource_transaction_code: Optional[str] = None,
        resource_table_name: Optional[str] = None,
    ) -> dict:
        """
        Get pipeline run history with optional filtering by pipeline code.

        Args:
            pipeline_mst_code: Optional pipeline code to filter runs (None = all runs)
            skip: Number of records to skip for pagination
            limit: Maximum number of records to return
            resource_transaction_code: Optional — when set together with
                ``resource_table_name``, filters runs to only those whose
                ``transaction_queue_code`` JSONB array contains at least one
                queue item whose ``transaction_code`` matches. Used to narrow
                shared infra-apply pipeline runs to a specific resource.
            resource_table_name: Source table discriminator paired with
                ``resource_transaction_code`` (e.g. "INFRASTRUCTURE").

        Returns:
            Dict containing:
            - total: Total count of runs matching the filter
            - runs: List of PipelineRunTrackModel instances
        """
        from sqlalchemy import func, and_

        # Build base query with filters
        filters = [PipelineRunTrackModel.is_deleted == False]

        if pipeline_mst_code:
            filters.append(PipelineRunTrackModel.pipeline_mst_code == pipeline_mst_code)

        if resource_transaction_code and resource_table_name:
            # Restrict to runs whose transaction_queue_code JSONB array carries
            # at least one queue item that points at this resource. This narrows
            # the shared INFRA_APPLY pipeline's runs to a specific S3 bucket /
            # SQS queue / DynamoDB table / Redis cluster.
            # transaction_queue.table_name is a Postgres enum, so cast the bind
            # param explicitly — asyncpg won't coerce varchar→enum on its own.
            filters.append(
                text(
                    "EXISTS (SELECT 1 FROM jsonb_array_elements_text("
                    "pipeline_run_track.transaction_queue_code) AS q(code) "
                    "WHERE q.code IN (SELECT code FROM transaction_queue "
                    "WHERE transaction_code = :__rtc "
                    "AND table_name = CAST(:__rtn AS workflow_source_table_enum)))"
                ).bindparams(
                    __rtc=resource_transaction_code,
                    __rtn=resource_table_name,
                )
            )

        # Get total count
        count_stmt = select(func.count()).select_from(
            select(PipelineRunTrackModel.id)
            .select_from(PipelineRunTrackModel)
            .where(and_(*filters))
            .subquery()
        )
        total_result = await self.db.execute(count_stmt)
        total = total_result.scalar() or 0

        # Get paginated results
        stmt = (
            select(PipelineRunTrackModel)
            .where(and_(*filters))
            .order_by(PipelineRunTrackModel.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        result = await self.db.execute(stmt)
        runs = list(result.scalars().all())

        logger.info(f"Found {total} pipeline runs" + (f" for pipeline {pipeline_mst_code}" if pipeline_mst_code else ""))

        return {
            "total": total,
            "runs": runs
        }
