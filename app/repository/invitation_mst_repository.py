"""
Repository for invitations_mst table operations
"""
from typing import Optional, List
from datetime import datetime, timedelta
import uuid
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.base_repository import BaseRepository
from app.db.models.invitation_mst_model import InvitationMstModel


class InvitationMstRepository(BaseRepository[InvitationMstModel]):
    """Repository for invitations_mst table"""

    def __init__(self, session: AsyncSession):
        super().__init__(InvitationMstModel, session)

    async def get_by_code(self, code: str) -> Optional[InvitationMstModel]:
        """Get invitation by code (only active, non-deleted invitations)"""
        stmt = select(InvitationMstModel).where(
            InvitationMstModel.code == code,
            InvitationMstModel.is_deleted == False
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_invitation(
        self,
        email: str,
        tenant_code: str,
        invited_by_user_code: str,
        role: str,
        is_org_owner: bool,
        token: str,
        expiry_days: int = 7
    ) -> InvitationMstModel:
        """
        Create new invitation

        Args:
            email: Email address to invite
            tenant_code: Tenant/organization code
            invited_by_user_code: Code of user sending invitation
            role: Role to assign (Admin/User)
            is_org_owner: Whether to make user org owner
            token: Unique secure token for invitation URL
            expiry_days: Number of days until invitation expires

        Returns:
            Created invitation model
        """
        invitation_code = str(uuid.uuid4())
        expires_at = datetime.utcnow() + timedelta(days=expiry_days)

        return await self.create(
            code=invitation_code,
            name=f"Invitation for {email}",
            description=f"Invitation to join as {role}",
            email=email,
            token=token,
            tenant_code=tenant_code,
            invited_by_user_code=invited_by_user_code,
            role=role,
            is_org_owner=is_org_owner,
            status="pending",
            expires_at=expires_at,
            is_active=True,
            is_deleted=False
        )

    async def get_by_token(self, token: str) -> Optional[InvitationMstModel]:
        """
        Get invitation by token

        Only returns active, non-expired, pending invitations
        """
        stmt = select(InvitationMstModel).where(
            and_(
                InvitationMstModel.token == token,
                InvitationMstModel.status == "pending",
                InvitationMstModel.expires_at > datetime.utcnow(),
                InvitationMstModel.is_deleted == False
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email_and_tenant(
        self,
        email: str,
        tenant_code: str
    ) -> Optional[InvitationMstModel]:
        """
        Get pending invitation for email in specific tenant

        Returns the most recent pending invitation if exists
        """
        stmt = select(InvitationMstModel).where(
            and_(
                InvitationMstModel.email == email,
                InvitationMstModel.tenant_code == tenant_code,
                InvitationMstModel.status == "pending",
                InvitationMstModel.is_deleted == False
            )
        ).order_by(InvitationMstModel.created_at.desc())

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_pending_by_tenant(self, tenant_code: str) -> List[InvitationMstModel]:
        """
        Get all pending invitations for a tenant

        Returns list of active, non-expired, pending invitations
        """
        stmt = select(InvitationMstModel).where(
            and_(
                InvitationMstModel.tenant_code == tenant_code,
                InvitationMstModel.status == "pending",
                InvitationMstModel.expires_at > datetime.utcnow(),
                InvitationMstModel.is_deleted == False
            )
        ).order_by(InvitationMstModel.created_at.desc())

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def mark_as_accepted(
        self,
        invitation: InvitationMstModel
    ) -> InvitationMstModel:
        """Mark invitation as accepted"""
        return await self.update(invitation, {
            "status": "accepted",
            "accepted_at": datetime.utcnow()
        })

    async def mark_as_cancelled(
        self,
        invitation: InvitationMstModel
    ) -> InvitationMstModel:
        """Mark invitation as cancelled"""
        return await self.update(invitation, {
            "status": "cancelled"
        })

    async def mark_as_expired(
        self,
        invitation: InvitationMstModel
    ) -> InvitationMstModel:
        """Mark invitation as expired"""
        return await self.update(invitation, {
            "status": "expired"
        })
