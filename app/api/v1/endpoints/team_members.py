"""
API endpoints for team member management
"""
from typing import List, Tuple
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.repository.user_mst_repository import UserMstRepository
from app.repository.invitation_mst_repository import InvitationMstRepository
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.invitation_schemas import PendingInvitation
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/team-members", tags=["team-members"])


class TeamMember(BaseModel):
    """Schema for team member"""
    id: str
    code: str
    name: str
    email: str
    firstName: str
    lastName: str
    role: str
    isOrgOwner: bool
    status: str
    clerkUserId: str
    createdAt: str
    updatedAt: str

    class Config:
        from_attributes = True


class TeamMembersResponse(BaseModel):
    """Response schema for team members list"""
    members: List[TeamMember]
    invitations: List[PendingInvitation]
    total: int


@router.get("", response_model=TeamMembersResponse)
async def get_team_members(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all team members and pending invitations for current organization

    Returns active team members from user_mst table and pending invitations
    from invitations_mst table for the current user's tenant.

    Args:
        user_and_tenant: Current authenticated user and tenant
        db: Database session

    Returns:
        List of team members and pending invitations

    Raises:
        500: Database error
    """
    user, tenant = user_and_tenant
    logger.info(f"[TEAM MEMBERS] Getting team members for tenant: {tenant.code}")

    try:
        user_repo = UserMstRepository(db)
        invitation_repo = InvitationMstRepository(db)

        # Get all active users in current tenant
        from sqlalchemy import select, and_

        stmt = select(UserMstModel).where(
            and_(
                UserMstModel.tenants_mst_code == tenant.code,
                UserMstModel.is_deleted == False,
                UserMstModel.is_active == True
            )
        ).order_by(UserMstModel.created_at.desc())

        result = await db.execute(stmt)
        users = list(result.scalars().all())

        # Get all pending invitations for current tenant
        invitations = await invitation_repo.get_pending_by_tenant(
            tenant.code
        )

        # Map users to TeamMember schema
        members = [
            TeamMember(
                id=str(user.id),
                code=user.code,
                name=f"{user.first_name} {user.last_name}",
                email=user.email_id,
                firstName=user.first_name,
                lastName=user.last_name,
                role="Admin" if user.is_org_owner else "User",  # Simplified for now
                isOrgOwner=user.is_org_owner,
                status="active" if user.is_active else "inactive",
                clerkUserId=user.auth_provider_id or "",
                createdAt=user.created_at.isoformat() if user.created_at else "",
                updatedAt=user.updated_at.isoformat() if user.updated_at else ""
            )
            for user in users
        ]

        # Map invitations to PendingInvitation schema
        pending_invitations = [
            PendingInvitation(
                id=str(invitation.id),
                code=invitation.code,
                email=invitation.email,
                role=invitation.role,
                is_org_owner=invitation.is_org_owner,
                status=invitation.status,
                invited_by=invitation.invited_by_user_code,
                invited_at=invitation.created_at,
                expires_at=invitation.expires_at
            )
            for invitation in invitations
        ]

        logger.info(
            f"[TEAM MEMBERS] Found {len(members)} active members "
            f"and {len(pending_invitations)} pending invitations"
        )

        return TeamMembersResponse(
            members=members,
            invitations=pending_invitations,
            total=len(members) + len(pending_invitations)
        )

    except Exception as e:
        logger.error(f"[TEAM MEMBERS] Error fetching team members: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch team members: {str(e)}"
        )


@router.delete("/{user_id}")
async def remove_team_member(
    user_id: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Remove team member from organization

    Only organization owners can remove members.
    Cannot remove yourself or other org owners.

    Args:
        user_id: Clerk user ID of member to remove
        user_and_tenant: Current authenticated user and tenant
        db: Database session

    Returns:
        Success message

    Raises:
        403: User is not org owner or trying to remove owner
        404: User not found
    """
    user, tenant = user_and_tenant
    logger.info(f"[TEAM MEMBERS] Removing user: {user_id}")

    # Verify current user is organization owner
    if not user.is_org_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organization owners can remove team members"
        )

    try:
        user_repo = UserMstRepository(db)

        # Get user to remove
        user_to_remove = await user_repo.get_by_auth_provider_id(user_id)

        if not user_to_remove:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Verify user belongs to same tenant
        if user_to_remove.tenants_mst_code != tenant.code:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot remove user from different organization"
            )

        # Cannot remove org owner
        if user_to_remove.is_org_owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot remove organization owner"
            )

        # Soft delete user
        await user_repo.soft_delete(user_to_remove.id)
        await db.commit()

        logger.info(f"[TEAM MEMBERS] Removed user: {user_to_remove.code}")

        return {
            "success": True,
            "message": "Team member removed successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TEAM MEMBERS] Error removing team member: {str(e)}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove team member: {str(e)}"
        )
