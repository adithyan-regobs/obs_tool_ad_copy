"""
Repository for ServiceConfigDockerfileWorkflow junction table operations.
Handles linking service_configs to gitops_workflow_detail for Dockerfile PRs.
"""
from typing import List, Optional
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models.service_config_dockerfile_workflow_model import ServiceConfigDockerfileWorkflowModel
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel


class ServiceConfigDockerfileWorkflowRepository:
    """Repository for junction table operations"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.model = ServiceConfigDockerfileWorkflowModel

    async def get_workflows_for_config(
        self,
        service_config_id: int
    ) -> List[ServiceConfigDockerfileWorkflowModel]:
        """
        Get all Dockerfile workflow links for a service config.
        Includes eager loading of the gitops_workflow relationship.

        Args:
            service_config_id: Service config ID

        Returns:
            List of junction records with gitops_workflow loaded
        """
        stmt = (
            select(self.model)
            .where(self.model.service_config_id == service_config_id)
            .options(joinedload(self.model.gitops_workflow))
            .order_by(self.model.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_by_config_branch_repo(
        self,
        service_config_id: int,
        branch: str,
        repository: str
    ) -> Optional[ServiceConfigDockerfileWorkflowModel]:
        """
        Get a specific workflow link by config, branch, and repository.

        Args:
            service_config_id: Service config ID
            branch: Base branch name (main, stage, etc.)
            repository: GitHub repository (owner/repo)

        Returns:
            Junction record if found, None otherwise
        """
        stmt = (
            select(self.model)
            .where(
                self.model.service_config_id == service_config_id,
                self.model.branch == branch,
                self.model.repository == repository
            )
            .options(joinedload(self.model.gitops_workflow))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def link_workflow(
        self,
        service_config_id: int,
        gitops_workflow_id: int,
        branch: str,
        repository: str,
        code: str,
        name: str,
        description: str = None
    ) -> ServiceConfigDockerfileWorkflowModel:
        """
        Create or update a link between service config and gitops workflow.
        Uses upsert pattern - if (config, branch, repo) exists, updates the workflow_id.

        Args:
            service_config_id: Service config ID
            gitops_workflow_id: GitOps workflow detail ID
            branch: Base branch name (main, stage, etc.)
            repository: GitHub repository (owner/repo)
            code: Unique code for this dockerfile workflow record
            name: Name for this dockerfile workflow record
            description: Optional description

        Returns:
            Created or updated junction record
        """
        # Check if link already exists
        existing = await self.get_by_config_branch_repo(
            service_config_id, branch, repository
        )

        if existing:
            # Update existing link with new workflow
            existing.gitops_workflow_id = gitops_workflow_id
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            # Create new link
            new_link = self.model(
                service_config_id=service_config_id,
                gitops_workflow_id=gitops_workflow_id,
                branch=branch,
                repository=repository,
                code=code,
                name=name,
                description=description
            )
            self.session.add(new_link)
            await self.session.flush()
            await self.session.refresh(new_link)
            return new_link

    async def unlink_workflow(
        self,
        service_config_id: int,
        branch: str,
        repository: str
    ) -> bool:
        """
        Remove a workflow link for a specific branch and repository.

        Args:
            service_config_id: Service config ID
            branch: Base branch name
            repository: GitHub repository

        Returns:
            True if deleted, False if not found
        """
        stmt = (
            delete(self.model)
            .where(
                self.model.service_config_id == service_config_id,
                self.model.branch == branch,
                self.model.repository == repository
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount > 0

    async def unlink_all_for_config(
        self,
        service_config_id: int
    ) -> int:
        """
        Remove all workflow links for a service config.

        Args:
            service_config_id: Service config ID

        Returns:
            Number of deleted links
        """
        stmt = (
            delete(self.model)
            .where(self.model.service_config_id == service_config_id)
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def get_configs_for_workflow(
        self,
        gitops_workflow_id: int
    ) -> List[ServiceConfigDockerfileWorkflowModel]:
        """
        Get all service configs linked to a gitops workflow.
        Useful for finding which configs are affected by a PR status change.

        Args:
            gitops_workflow_id: GitOps workflow detail ID

        Returns:
            List of junction records
        """
        stmt = (
            select(self.model)
            .where(self.model.gitops_workflow_id == gitops_workflow_id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
