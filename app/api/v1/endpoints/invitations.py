"""
API endpoints for user invitation management
"""
from typing import Tuple
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.schemas.invitation_schemas import (
    InviteMemberRequest,
    InviteMemberResponse,
    VerifyInvitationRequest,
    VerifyInvitationResponse,
    AcceptInvitationRequest,
    AcceptInvitationResponse,
    CancelInvitationRequest,
    CancelInvitationResponse
)
from app.services.invitation_service import InvitationService
from app.api.dependencies import get_db, get_current_user_and_tenant, get_current_user_tenant_and_azp
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invitations", tags=["invitations"])


@router.post("/send", response_model=InviteMemberResponse)
async def send_invitation(
    request: InviteMemberRequest,
    user_tenant_azp: Tuple[UserMstModel, TenantsMstModel, str] = Depends(get_current_user_tenant_and_azp),
    db: AsyncSession = Depends(get_db)
):
    """
    Send invitation to new team member

    Only organization owners can send invitations.
    Clerk automatically sends the invitation email.

    Flow:
    1. Verify current user is org owner
    2. Create invitation record in database
    3. Send invitation via Clerk API (Clerk sends email)
    4. Return success with invitation details

    Args:
        request: Invitation details (email, role, is_org_owner)
        user_tenant_azp: Current authenticated user, tenant, and origin URL
        db: Database session

    Returns:
        Invitation details including redirect URL

    Raises:
        403: User is not org owner
        400: User already exists or pending invitation exists
        500: Failed to send invitation
    """
    user, tenant, azp = user_tenant_azp
    logger.info(f"[INVITE API] User {user.code} inviting {request.email} from origin {azp}")

    # Verify current user is organization owner
    if not user.is_org_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organization owners can send invitations"
        )

    # Initialize service
    invitation_service = InvitationService(db)

    try:
        # Send invitation with dynamic frontend URL from azp
        result = await invitation_service.send_invitation(
            inviter_user_code=user.code,
            inviter_tenant_code=tenant.code,
            email=request.email,
            role=request.role,
            is_org_owner=request.is_org_owner,
            frontend_url=azp
        )

        return InviteMemberResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[INVITE API] Error sending invitation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send invitation: {str(e)}"
        )


@router.get("/verify", response_model=VerifyInvitationResponse)
async def verify_invitation(
    token: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Verify invitation token is valid

    Public endpoint (no authentication required).
    Used by accept-invitation page to validate token before showing signup form.

    Args:
        token: Invitation token from URL query parameter
        db: Database session

    Returns:
        Invitation details if valid, error message if invalid
    """
    logger.info(f"[INVITE API] Verifying invitation token")

    invitation_service = InvitationService(db)

    try:
        result = await invitation_service.verify_invitation(token)
        return VerifyInvitationResponse(**result)

    except Exception as e:
        logger.error(f"[INVITE API] Error verifying invitation: {str(e)}")
        return VerifyInvitationResponse(
            valid=False,
            message=f"Failed to verify invitation: {str(e)}"
        )


@router.post("/accept", response_model=AcceptInvitationResponse)
async def accept_invitation(
    request: AcceptInvitationRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Accept invitation and create user account

    Public endpoint (no authentication required).
    Called after user completes Clerk signup via invitation link.

    Flow:
    1. User clicks invitation email link
    2. Redirected to /accept-invitation page
    3. User signs up with Clerk (gets clerk_user_id)
    4. Frontend calls this endpoint
    5. Backend creates user in database
    6. Backend updates Clerk metadata
    7. Frontend redirects to dashboard

    Args:
        request: Acceptance details (token, clerk_user_id, name)
        db: Database session

    Returns:
        User and tenant details including subdomain for redirect

    Raises:
        400: Invalid token or user already exists
        404: Organization not found
        500: Failed to create user
    """
    logger.info(f"[INVITE API] Accepting invitation for Clerk user: {request.clerk_user_id}")

    invitation_service = InvitationService(db)

    try:
        result = await invitation_service.accept_invitation(
            token=request.token,
            clerk_user_id=request.clerk_user_id,
            first_name=request.first_name,
            last_name=request.last_name
        )

        return AcceptInvitationResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[INVITE API] Error accepting invitation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to accept invitation: {str(e)}"
        )


@router.delete("/{invitation_id}/cancel", response_model=CancelInvitationResponse)
async def cancel_invitation(
    invitation_id: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Cancel pending invitation

    Only organization owners can cancel invitations.

    Args:
        invitation_id: Code of invitation to cancel
        user_and_tenant: Current authenticated user and tenant
        db: Database session

    Returns:
        Success message

    Raises:
        403: User is not org owner or invitation from different org
        404: Invitation not found
    """
    user, tenant = user_and_tenant
    logger.info(f"[INVITE API] Cancelling invitation: {invitation_id}")

    # Verify current user is organization owner
    if not user.is_org_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organization owners can cancel invitations"
        )

    invitation_service = InvitationService(db)

    try:
        result = await invitation_service.cancel_invitation(
            invitation_code=invitation_id,
            user_code=user.code
        )

        return CancelInvitationResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[INVITE API] Error cancelling invitation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel invitation: {str(e)}"
        )
