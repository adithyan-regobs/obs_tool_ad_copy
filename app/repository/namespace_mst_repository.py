"""
Namespace Master Repository

Repository for namespace_mst database operations.
"""
from typing import Optional, List
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.namespace_mst_model import NamespaceMstModel
from app.repository.base_repository import BaseRepository


class NamespaceMstRepository(BaseRepository[NamespaceMstModel]):
    """Repository for Namespace Master operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(NamespaceMstModel, session)

    async def get_by_code(self, code: str) -> Optional[NamespaceMstModel]:
        """Get namespace by code"""
        stmt = select(self.model).where(
            and_(
                self.model.code == code,
                self.model.is_deleted == False
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_infrastructure(
        self,
        infrastructure_mst_code: str
    ) -> List[NamespaceMstModel]:
        """
        List all namespaces for a specific infrastructure (EKS cluster).

        Args:
            infrastructure_mst_code: Infrastructure code

        Returns:
            List of NamespaceMstModel instances
        """
        stmt = select(self.model).where(
            and_(
                self.model.infrastructure_mst_code == infrastructure_mst_code,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        ).order_by(self.model.namespace)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def check_namespace_exists(
        self,
        infrastructure_mst_code: str,
        namespace: str
    ) -> Optional[NamespaceMstModel]:
        """
        Check if a namespace already exists for an infrastructure.

        Args:
            infrastructure_mst_code: Infrastructure code
            namespace: Namespace name

        Returns:
            NamespaceMstModel if exists, None otherwise
        """
        stmt = select(self.model).where(
            and_(
                self.model.infrastructure_mst_code == infrastructure_mst_code,
                self.model.namespace == namespace,
                self.model.is_deleted == False
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_namespace(
        self,
        code: str,
        name: str,
        infrastructure_mst_code: str,
        namespace: str,
        description: Optional[str] = None
    ) -> NamespaceMstModel:
        """
        Create a new namespace for an infrastructure.

        Args:
            code: Unique code for the namespace record
            name: Display name
            infrastructure_mst_code: Infrastructure code
            namespace: Kubernetes namespace name
            description: Optional description

        Returns:
            Created NamespaceMstModel
        """
        return await self.create(
            code=code,
            name=name,
            infrastructure_mst_code=infrastructure_mst_code,
            namespace=namespace,
            description=description
        )

    async def delete_namespace(self, code: str) -> bool:
        """
        Soft delete a namespace.

        Args:
            code: Namespace code

        Returns:
            True if deleted, False if not found
        """
        namespace = await self.get_by_code(code)
        if namespace:
            namespace.soft_delete()
            self.session.add(namespace)
            await self.session.flush()
            return True
        return False
