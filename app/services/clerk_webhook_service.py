"""
Service for processing Clerk webhook events

Handles user deletion, and just-in-time provisioning of enterprise-SSO users
when JIT_TENANT_CODE is configured. Users who sign up through the normal
self-service flow are still created via /signup/create-organization.
"""
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from clerk_backend_api import Clerk
from app.core.config import settings
from app.core.enum import AuthProviderEnum, WorkspaceRoleEnum
from app.domain.factories.workspace_mst_factory import make_workspace_user_mapping
from app.repository.role_mst_repository import RoleMstRepository
from app.repository.tenants_mst_repository import TenantsMstRepository
from app.repository.user_mst_repository import UserMstRepository
from app.repository.workspace_mst_repository import WorkspaceMstRepository
from app.repository.workspace_user_mapping_repository import WorkspaceUserMappingRepository
from app.schemas.clerk_webhook_schemas import ClerkWebhookUserData, ClerkUserData
import logging

logger = logging.getLogger(__name__)


JIT_ROLE_NAME = "Admin"

# Signup names every tenant's first workspace this, and nothing marks a
# workspace as the default, so the name is the only handle JIT has.
JIT_WORKSPACE_NAME = "Default Workspace"
JIT_WORKSPACE_ROLE = WorkspaceRoleEnum.admin


class ClerkWebhookService:
    """Service for handling Clerk webhook events"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.user_repo = UserMstRepository(db)
        self.tenant_repo = TenantsMstRepository(db)
        self.role_repo = RoleMstRepository(db)
        self.workspace_repo = WorkspaceMstRepository(db)
        self.workspace_mapping_repo = WorkspaceUserMappingRepository(db)
        self.clerk = Clerk(bearer_auth=settings.clerk_secret_key)

    def _parse_clerk_data(self, raw_data: ClerkUserData) -> ClerkWebhookUserData:
        """Parse raw Clerk data into our internal format"""
        # Extract primary email
        primary_email = None
        for email_obj in raw_data.email_addresses:
            if email_obj.id == raw_data.primary_email_address_id:
                primary_email = email_obj.email_address
                break

        if not primary_email and raw_data.email_addresses:
            # Fallback to first email if primary not found
            primary_email = raw_data.email_addresses[0].email_address

        if not primary_email:
            primary_email = "unknown@email.com"

        public_metadata = raw_data.public_metadata or {}
        unsafe_metadata = raw_data.unsafe_metadata or {}

        return ClerkWebhookUserData(
            clerkId=raw_data.id,
            firstName=raw_data.first_name or "Unknown",
            lastName=raw_data.last_name or "User",
            email_address=primary_email,
            organizationName=public_metadata.get("organizationName"),
            organizationSubdomain=public_metadata.get("organizationSubdomain"),
            isOrganizationOwner=public_metadata.get("isOrganizationOwner"),
            userRole=public_metadata.get("userRole"),
            unSafeOrganizationName=unsafe_metadata.get("organizationName"),
            unSafeOrganizationSubdomain=unsafe_metadata.get("organizationSubdomain"),
            unSafeIsOrganizationOwner=unsafe_metadata.get("isOrganizationOwner"),
            unSafeUserRole=unsafe_metadata.get("userRole")
        )

    async def process_webhook(self, event_type: str, raw_data: ClerkUserData) -> Dict[str, Any]:
        """
        Process Clerk webhook events

        Handles user.deleted, and user.created when JIT is enabled.
        user.updated is ignored (profile updates go through the API).
        """
        logger.info(f"[WEBHOOK] Processing webhook event: {event_type}")
        logger.info(f"[WEBHOOK] User ID: {raw_data.id}")

        try:
            if event_type == "user.deleted":
                # Parse data
                data = self._parse_clerk_data(raw_data)
                result = await self._handle_user_deleted(data)
            elif event_type == "user.created":
                data = self._parse_clerk_data(raw_data)
                result = await self._handle_user_created(data)
            elif event_type == "user.updated":
                logger.info(f"[WEBHOOK] user.updated event ignored - profile updates handled via API")
                result = {
                    "received": True,
                    "message": "user.updated events are ignored"
                }
            else:
                logger.warning(f"[WEBHOOK] Unknown event type: {event_type}")
                result = {"received": True, "message": f"Unknown event type: {event_type}"}

            # Commit transaction
            await self.db.commit()
            logger.info(f"[WEBHOOK] Transaction committed successfully")
            return result

        except Exception as e:
            logger.error(f"[WEBHOOK] Error processing webhook: {str(e)}")
            await self.db.rollback()
            raise

    async def _handle_user_deleted(self, data: ClerkWebhookUserData) -> Dict[str, Any]:
        """
        Handle user deletion webhook

        When a user is deleted from Clerk (e.g., via Clerk dashboard),
        soft delete the corresponding user in our database.
        """
        logger.info(f"[WEBHOOK] Processing user.deleted for {data.clerkId}")

        # Get user
        user = await self.user_repo.get_by_auth_provider_id(data.clerkId)
        if not user:
            logger.warning(f"[WEBHOOK] User {data.clerkId} not found for deletion")
            return {
                "received": True,
                "message": "User not found (may have been already deleted)"
            }

        # Soft delete user
        await self.user_repo.soft_delete(user.id)

        logger.info(f"[WEBHOOK] Soft deleted user: {user.code} (email: {user.email_id})")

        return {
            "received": True,
            "message": "User deleted successfully",
            "user_code": user.code
        }


    async def _handle_user_created(self, data: ClerkWebhookUserData) -> Dict[str, Any]:
        """
        Just-in-time provisioning for enterprise-SSO users.

        Only runs on deployments that set JIT_TENANT_CODE, and only for users
        whose organizationSubdomain was stamped by the SAML connection's
        `public_metadata_organizationSubdomain` attribute mapping. Users
        arriving by any other route (Google, email) have no such claim and are
        ignored, so ordinary sign-ups cannot self-provision.
        """
        logger.info(f"[WEBHOOK] Processing user.created for {data.clerkId}")

        tenant_code = settings.jit_tenant_code
        if not tenant_code:
            logger.info("[WEBHOOK] JIT disabled (JIT_TENANT_CODE unset) - skipping")
            return {"received": True, "message": "JIT provisioning is not enabled"}

        tenant = await self.tenant_repo.get_by_code(tenant_code)
        if not tenant:
            logger.error(f"[WEBHOOK] JIT_TENANT_CODE '{tenant_code}' does not match any tenant")
            return {"received": True, "message": "Configured JIT tenant not found"}

        claimed_subdomain = data.organizationSubdomain or data.unSafeOrganizationSubdomain
        if claimed_subdomain != tenant.subdomain:
            logger.info(
                f"[WEBHOOK] Skipping JIT for {data.email_address}: "
                f"subdomain claim {claimed_subdomain!r} is not {tenant.subdomain!r}"
            )
            return {"received": True, "message": "Not an enterprise SSO user for this tenant"}

        existing_user = await self.user_repo.get_by_auth_provider_id(data.clerkId)
        if existing_user:
            logger.info(f"[WEBHOOK] User {data.clerkId} already provisioned as {existing_user.code}")
            return {
                "received": True,
                "message": "User already exists",
                "user_code": existing_user.code
            }

        user = await self.user_repo.create_user(
            auth_provider_id=data.clerkId,
            auth_provider=AuthProviderEnum.clerk,
            email=data.email_address,
            first_name=data.firstName,
            last_name=data.lastName,
            tenant_code=tenant.code,
            is_org_owner=True
        )
        logger.info(
            f"[WEBHOOK] JIT created user: {user.code} "
            f"(email: {user.email_id}, tenant: {user.tenants_mst_code})"
        )

        await self.role_repo.create_role(
            user_mst_id=user.id,
            role_name=JIT_ROLE_NAME,
            description=f"{JIT_ROLE_NAME} role for {user.name} (JIT provisioned)"
        )

        await self._add_to_default_workspace(user, tenant)

        # The attribute mapping should already have stamped this, but a
        # misconfigured IdP would otherwise leave the user with a token the
        # backend rejects and no way to recover.
        try:
            self.clerk.users.update_metadata(
                user_id=data.clerkId,
                public_metadata={
                    "organizationSubdomain": tenant.subdomain,
                    "userRole": JIT_ROLE_NAME,
                    "isOrganizationOwner": False
                }
            )
        except Exception as e:
            logger.warning(f"[WEBHOOK] Could not refresh Clerk metadata for {data.clerkId}: {str(e)}")

        return {
            "received": True,
            "message": "User provisioned successfully",
            "user_code": user.code,
            "tenant_code": tenant.code
        }

    async def _add_to_default_workspace(self, user, tenant) -> None:
        """Add a JIT-provisioned user to the tenant's default workspace.

        Never raises: a missing or renamed workspace should leave the user
        provisioned and able to sign in, not fail the whole webhook.
        """
        try:
            workspace = await self.workspace_repo.get_by_name_and_tenant(
                JIT_WORKSPACE_NAME, tenant.code
            )
            if not workspace:
                logger.warning(
                    f"[WEBHOOK] No '{JIT_WORKSPACE_NAME}' for tenant {tenant.code} - "
                    f"user {user.code} provisioned without a workspace"
                )
                return

            await self.workspace_mapping_repo.bulk_create([
                make_workspace_user_mapping(
                    workspace_code=workspace.code,
                    workspace_name=workspace.name,
                    user_mst_code=user.code,
                    user_email=user.email_id,
                    tenant_code=tenant.code,
                    role=JIT_WORKSPACE_ROLE,
                )
            ])
            logger.info(
                f"[WEBHOOK] Added {user.code} to workspace {workspace.code} "
                f"as {JIT_WORKSPACE_ROLE.value}"
            )
        except Exception as e:
            logger.warning(
                f"[WEBHOOK] Could not add {user.code} to default workspace: {str(e)}"
            )

