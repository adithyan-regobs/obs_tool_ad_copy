"""
Service Lookup Helper

Helper functions to lookup services by Resource Group or Application.
Used for permission expansion and other service queries.
"""

from typing import List
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.services_mst_model import ServicesMstModel


async def get_services_by_rg(
    session: AsyncSession,
    resource_group_mst_code: str,
    tenants_mst_code: str
) -> List[str]:
    """
    Get all service codes under a Resource Group.

    Args:
        session: Database session
        resource_group_mst_code: RG code to lookup
        tenants_mst_code: Tenant code for isolation

    Returns:
        List of service codes under this RG
    """
    # Build SELECT - only get 'code' column
    stmt = select(ServicesMstModel.code).where(
        and_(
            ServicesMstModel.resource_group_mst_code == resource_group_mst_code,
            ServicesMstModel.tenants_mst_code == tenants_mst_code,
            ServicesMstModel.is_deleted == False,
            ServicesMstModel.is_active == True
        )
    )

    # Execute and extract codes
    result = await session.execute(stmt)
    return [row[0] for row in result.fetchall()]


async def get_services_by_app(
    session: AsyncSession,
    applications_mst_code: str,
    tenants_mst_code: str
) -> List[str]:
    """
    Get all service codes under an Application.

    Args:
        session: Database session
        applications_mst_code: App code to lookup
        tenants_mst_code: Tenant code for isolation

    Returns:
        List of service codes under this Application
    """
    # Build SELECT - only get 'code' column
    stmt = select(ServicesMstModel.code).where(
        and_(
            ServicesMstModel.applications_mst_code == applications_mst_code,
            ServicesMstModel.tenants_mst_code == tenants_mst_code,
            ServicesMstModel.is_deleted == False,
            ServicesMstModel.is_active == True
        )
    )

    # Execute and extract codes
    result = await session.execute(stmt)
    return [row[0] for row in result.fetchall()]

