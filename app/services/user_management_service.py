"""
User Management Service.

Provides user lookup and management operations.
"""
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import logging

from app.db.models.user_mst_model import UserMstModel

logger = logging.getLogger(__name__)


class UserManagementService:
    """Service for user management operations."""

    def __init__(self, db: AsyncSession):
        """Initialize service with database session.

        Args:
            db: Database session
        """
        self.db = db

    async def get_user_by_email(self, email: str) -> Optional[UserMstModel]:
        """Get user details by email address.

        Args:
            email: User email address

        Returns:
            UserMstModel if found, None otherwise
        """
        try:
            # Query user by email
            stmt = select(UserMstModel).where(
                UserMstModel.email_id == email,
                UserMstModel.is_deleted == False
            )

            result = await self.db.execute(stmt)
            user = result.scalar_one_or_none()

            if user:
                logger.info(f"Found user with email {email}: code={user.code}, tenant={user.tenants_mst_code}")
            else:
                logger.warning(f"No user found with email {email}")

            return user

        except Exception as e:
            logger.error(f"Error fetching user by email {email}: {str(e)}", exc_info=True)
            raise
