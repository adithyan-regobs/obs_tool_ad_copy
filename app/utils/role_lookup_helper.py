"""
Role Lookup Helper

Helper function to get user's role type codes.
Used for permission cache rebuild.
"""

from typing import List
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.user_mst_model import RoleMst


async def get_user_role_type_codes(
    session: AsyncSession,
    user_id: int
) -> List[str]:
    """
    Get all role type codes for a user.

    Args:
        session: Database session
        user_id: User's ID

    Returns:
        List of role type codes (from role_type_ref) assigned to this user
    """
    # SELECT role_type_ref_code FROM role_mst WHERE user_mst_id = user_id
    stmt = select(RoleMst.role_type_ref_code).where(
        and_(
            RoleMst.user_mst_id == user_id,
            RoleMst.is_deleted == False,
            RoleMst.is_active == True,
            RoleMst.role_type_ref_code != None  # Only get those with role type set
        )
    )

    # Execute and extract codes
    result = await session.execute(stmt)
    return [row[0] for row in result.fetchall()]

