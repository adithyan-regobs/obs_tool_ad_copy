"""
Service for managing user invitations

Handles invitation creation, validation, acceptance, and email sending via Clerk
"""
from typing import Dict, Any
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.invitation_mst_repository import InvitationMstRepository
from app.repository.user_mst_repository import UserMstRepository
from app.repository.tenants_mst_repository import TenantsMstRepository
from app.repository.role_mst_repository import RoleMstRepository
from app.core.enum import AuthProviderEnum
from app.core.config import settings
from app.utils.tenant_config import resolve_clerk_organization_id
from clerk_backend_api import Clerk
from clerk_backend_api.models import CreateInvitationRequestBody
import secrets
import logging

logger = logging.getLogger(__name__)


class InvitationService:
    """Service for handling user invitations"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.invitation_repo = InvitationMstRepository(db)
        self.user_repo = UserMstRepository(db)
        self.tenant_repo = TenantsMstRepository(db)
        self.role_repo = RoleMstRepository(db)
        self.clerk = Clerk(bearer_auth=settings.clerk_secret_key)

    async def send_invitation(
        self,
        inviter_user_code: str,
        inviter_tenant_code: str,
        email: str,
        role: str,
        is_org_owner: bool,
        frontend_url: str = None
    ) -> Dict[str, Any]:
        """
        Send invitation to new user

        Creates invitation record and sends email via Clerk.

        Args:
            inviter_user_code: Code of user sending invitation
            inviter_tenant_code: Tenant code of organization
            email: Email address to invite
            role: Role to assign (Admin/User)
            is_org_owner: Whether to make user org owner
            frontend_url: Frontend origin URL from JWT azp claim (e.g., "https://vance.devlift.ai")

        Returns:
            Dict with invitation details

        Raises:
            HTTPException: If validation fails
        """
        logger.info(f"[INVITE] Processing invitation for {email} to tenant {inviter_tenant_code}")

        # Step 1: Verify inviter exists and has permission
        inviter = await self.user_repo.get_by_code(inviter_user_code)
        if not inviter:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inviter user not found"
            )

        if not inviter.is_org_owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only organization owners can send invitations"
            )

        # Step 2: Get tenant details
        tenant = await self.tenant_repo.get_by_code(inviter_tenant_code)
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        # Step 3: Check if user already exists in this tenant
        existing_user = await self.user_repo.get_by_email(email)
        if existing_user and existing_user.tenants_mst_code == tenant.code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User with email {email} already exists in this organization"
            )

        # Step 4: Check if there's already a pending invitation
        existing_invitation = await self.invitation_repo.get_by_email_and_tenant(
            email,
            tenant.code
        )
        if existing_invitation:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Pending invitation already exists for {email}"
            )

        # Step 5: Generate unique secure token
        invitation_token = secrets.token_urlsafe(32)

        # Step 6: Create invitation record in database FIRST
        invitation = await self.invitation_repo.create_invitation(
            email=email,
            tenant_code=tenant.code,
            invited_by_user_code=inviter_user_code,
            role=role,
            is_org_owner=is_org_owner,
            token=invitation_token
        )

        logger.info(f"[INVITE] Created invitation record: {invitation.code}")

        # Step 7: Build invitation URL from frontend_url (from JWT azp claim)
        # User will be redirected to this URL after clicking email link
        # azp contains origin URL like "https://vance.devlift.ai" or "https://devlift.ai"
        if not frontend_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Frontend URL is required for sending invitations"
            )

        redirect_url = f"{frontend_url}/accept-invitation?token={invitation_token}"
        logger.info(f"[INVITE] Building redirect URL from azp: {redirect_url}")

        # Step 8: Send invitation email via Clerk
        try:
            logger.info(f"[INVITE] Sending invitation via Clerk to {email}")

            organization_id = resolve_clerk_organization_id(tenant)
            # Create invitation via Clerk API
            # Clerk will send the invitation email automatically
            # request_body = CreateInvitationRequestBody(
            #     email_address=email,
            #     redirect_url=redirect_url,
            #     notify=True,  # Explicitly enable email notification
            #     organization_id=organization_id,   # REQUIRED
            #     role=role,  
            #     public_metadata={
            #         "invitationToken": invitation_token,
            #         "tenantCode": tenant.code,
            #         "organizationSubdomain": tenant.subdomain,
            #         "organizationName": tenant.name,
            #         "userRole": role,
            #         "isOrganizationOwner": is_org_owner
            #     }
            # )
            

            clerk_invitation = self.clerk.organization_invitations.create(organization_id=organization_id,
                    email_address=email,
                    role='org:member',
                    public_metadata={ "invitationToken": invitation_token,
                    "tenantCode": tenant.code,
                    "organizationSubdomain": tenant.subdomain,
                    "organizationName": tenant.name,
                    "userRole": role,
                    "isOrganizationOwner": is_org_owner,
                    },
                    redirect_url=redirect_url, expires_in_days=1)

            # clerk_invitation = self.clerk.invitations.create(request=request_body)

            logger.info(f"[INVITE] Clerk invitation created: {clerk_invitation.id}")
            logger.info(f"[INVITE] Clerk will send email to {email} with redirect to {redirect_url}")

        except Exception as e:
            logger.error(f"[INVITE] Failed to send Clerk invitation: {str(e)}")
            # Rollback invitation creation if Clerk fails
            await self.invitation_repo.soft_delete(invitation.id)
            await self.db.commit()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to send invitation via Clerk: {str(e)}"
            )

        # Commit transaction
        await self.db.commit()

        return {
            "success": True,
            "message": f"Invitation sent to {email} via Clerk",
            "invitation_id": invitation.code,
            "clerk_invitation_id": clerk_invitation.id,
            "invitation_url": redirect_url
        }

    async def verify_invitation(self, token: str) -> Dict[str, Any]:
        """
        Verify invitation token is valid

        Args:
            token: Invitation token from URL

        Returns:
            Dict with invitation details if valid

        Raises:
            HTTPException: If token is invalid or expired
        """
        logger.info(f"[INVITE] Verifying invitation token")

        invitation = await self.invitation_repo.get_by_token(token)

        if not invitation:
            logger.warning(f"[INVITE] Invalid or expired invitation token")
            return {
                "valid": False,
                "message": "Invalid or expired invitation link"
            }

        # Get tenant details
        tenant = await self.tenant_repo.get_by_code(invitation.tenant_code)

        return {
            "valid": True,
            "email": invitation.email,
            "role": invitation.role,
            "is_org_owner": invitation.is_org_owner,
            "organization_name": tenant.name if tenant else None,
            "organization_subdomain": tenant.subdomain if tenant else None,
            "expires_at": invitation.expires_at
        }

    async def accept_invitation(
        self,
        token: str,
        clerk_user_id: str,
        first_name: str,
        last_name: str
    ) -> Dict[str, Any]:
        """
        Accept invitation and create user

        This is called after user completes Clerk signup.
        Creates user in database and updates Clerk metadata.

        Args:
            token: Invitation token from URL
            clerk_user_id: Clerk user ID from signup
            first_name: User's first name
            last_name: User's last name

        Returns:
            Dict with user and tenant information

        Raises:
            HTTPException: If validation fails
        """
        logger.info(f"[INVITE] Accepting invitation for Clerk user: {clerk_user_id}")

        # Step 1: Get and validate invitation
        invitation = await self.invitation_repo.get_by_token(token)
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired invitation link"
            )

        # Step 2: Check if user already exists
        existing_user = await self.user_repo.get_by_auth_provider_id(clerk_user_id)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User already exists. User code: {existing_user.code}"
            )

        # Step 3: Get tenant
        tenant = await self.tenant_repo.get_by_code(invitation.tenant_code)
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        # Step 4: Create user in database
        try:
            user = await self.user_repo.create_user(
                auth_provider_id=clerk_user_id,
                auth_provider=AuthProviderEnum.clerk,
                email=invitation.email,
                first_name=first_name,
                last_name=last_name,
                tenant_code=tenant.code,
                is_org_owner=invitation.is_org_owner
            )
            logger.info(
                f"[INVITE] Created user: {user.code} "
                f"(email: {user.email_id}, tenant: {user.tenants_mst_code})"
            )
        except Exception as e:
            logger.error(f"[INVITE] Failed to create user: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create user: {str(e)}"
            )

        # Step 4.5: Create role in role_mst table
        try:
            role = await self.role_repo.create_role(
                user_mst_id=user.id,
                role_name=invitation.role,
                description=f"{invitation.role} role for {user.name}"
            )
            logger.info(
                f"[INVITE] Created role: {role.code} "
                f"(name: {role.name}, user_id: {role.user_mst_id})"
            )
        except Exception as e:
            logger.error(f"[INVITE] Failed to create role: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create user role: {str(e)}"
            )

        # Step 5: Update Clerk public metadata (same pattern as signup)
        try:
            self.clerk.users.update_metadata(
                user_id=clerk_user_id,
                public_metadata={
                    "organizationSubdomain": tenant.subdomain,
                    "userRole": invitation.role,
                    "isOrganizationOwner": invitation.is_org_owner
                }
            )
            logger.info(f"[INVITE] Updated Clerk metadata for user {clerk_user_id}")
        except Exception as e:
            logger.error(f"[INVITE] Failed to update Clerk metadata: {str(e)}")
            # Don't fail the request - user is created, metadata update is non-critical

        # Step 6: Add user to Clerk Organization and revoke pending invitation
        try:
            organization_id = resolve_clerk_organization_id(tenant)

            # Add user as member of the organization
            role = 'org:admin' if invitation.is_org_owner else 'org:member'
            self.clerk.organization_memberships.create(
                organization_id=organization_id,
                user_id=clerk_user_id,
                role=role
            )
            logger.info(f"[INVITE] Added user {clerk_user_id} to Clerk Organization {organization_id} with role {role}")

            # Revoke the pending Clerk invitation
            try:
                # List pending invitations for this email
                pending_invitations = self.clerk.organization_invitations.list(
                    organization_id=organization_id,
                    status="pending"
                )
                # Find and revoke the invitation for this email
                for inv in pending_invitations.data:
                    if inv.email_address == invitation.email:
                        self.clerk.organization_invitations.revoke(
                            organization_id=organization_id,
                            invitation_id=inv.id
                        )
                        logger.info(f"[INVITE] Revoked Clerk invitation {inv.id} for {invitation.email}")
                        break
            except Exception as revoke_err:
                logger.warning(f"[INVITE] Could not revoke Clerk invitation: {str(revoke_err)}")

        except Exception as e:
            logger.error(f"[INVITE] Failed to add user to Clerk Organization: {str(e)}")
            # Don't fail - user is created in our DB, org membership is for Clerk sync

        # Step 7: Mark invitation as accepted
        await self.invitation_repo.mark_as_accepted(invitation)

        logger.info(
            f"[INVITE] Invitation accepted successfully. "
            f"User: {user.code}, Tenant: {tenant.code}"
        )

        # Commit transaction
        await self.db.commit()

        return {
            "success": True,
            "message": "Invitation accepted successfully",
            "user_code": user.code,
            "tenant_code": tenant.code,
            "subdomain": tenant.subdomain
        }

    async def cancel_invitation(
        self,
        invitation_code: str,
        user_code: str
    ) -> Dict[str, Any]:
        """
        Cancel pending invitation

        Args:
            invitation_code: Code of invitation to cancel
            user_code: Code of user cancelling (must be org owner)

        Returns:
            Dict with success message

        Raises:
            HTTPException: If validation fails
        """
        logger.info(f"[INVITE] Cancelling invitation: {invitation_code}")

        # Verify user is org owner
        user = await self.user_repo.get_by_code(user_code)
        if not user or not user.is_org_owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only organization owners can cancel invitations"
            )

        # Get invitation
        invitation = await self.invitation_repo.get_by_code(invitation_code)
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invitation not found"
            )

        # Verify invitation belongs to same tenant
        if invitation.tenant_code != user.tenants_mst_code:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot cancel invitation from different organization"
            )

        # Cancel invitation
        await self.invitation_repo.mark_as_cancelled(invitation)
        await self.db.commit()

        return {
            "success": True,
            "message": "Invitation cancelled successfully"
        }
