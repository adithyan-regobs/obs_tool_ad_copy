"""
Service for user self-service signup with organization creation

This service handles the signup flow where users:
1. Sign up with Clerk (get session)
2. Fill organization details form
3. Call API to create organization and user in database
4. Create Clerk Organization and add user as admin member
5. Update Clerk public metadata with subdomain
6. Provision infrastructure (VPC + EC2) via GitHub PR
"""
from typing import Dict, Any
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.user_mst_repository import UserMstRepository
from app.repository.tenants_mst_repository import TenantsMstRepository
from app.repository.role_mst_repository import RoleMstRepository
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.repository.resource_group_mst_repository import ResourceGroupMstRepository
from app.repository.geo_loc_mst_repository import GeoLocMstRepository
from app.repository.pipeline_vendor_mst_repository import PipelineVendorMstRepository
from app.domain.factories.applications_mst_factory import make_application
from app.domain.factories.resource_group_mst_factory import make_default_resource_group
from app.services.workspace_service import WorkspaceService
from app.schemas.workspace_schemas import CreateWorkspaceRequest
from app.core.enum import AuthProviderEnum, EnvironmentEnum, PipelineAgentEnum
from app.core.config import settings
from app.utils.tenant_config import set_clerk_organization_id
from clerk_backend_api import Clerk
from clerk_backend_api.models import CreateOrganizationRequestBody
import logging

logger = logging.getLogger(__name__)


class UserSignupService:
    """Service for handling user self-service signup with organization creation"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.user_repo = UserMstRepository(db)
        self.tenant_repo = TenantsMstRepository(db)
        self.role_repo = RoleMstRepository(db)
        self.applications_repo = ApplicationsMstRepository(db)
        self.resource_groups_repo = ResourceGroupMstRepository(db)
        self.geo_loc_repo = GeoLocMstRepository(db)
        self.pipeline_vendor_repo = PipelineVendorMstRepository(db)
        self.clerk = Clerk(bearer_auth=settings.clerk_secret_key)

    async def create_organization(
        self,
        clerk_user_id: str,
        email: str,
        first_name: str,
        last_name: str,
        organization_name: str,
        organization_subdomain: str,
        user_role: str,
        is_organization_owner: bool
    ) -> Dict[str, Any]:
        """
        Create organization and user in a single transaction

        This is called after user signs up with Clerk.
        User exists in Clerk but NOT in our database yet.

        Flow:
        1. Validate user doesn't already exist in DB (by auth_provider_id = clerk_user_id)
        2. Validate subdomain is available
        3. Create tenant (organization)
        4. Create user with tenant association
        5. Update Clerk public metadata with subdomain

        Args:
            clerk_user_id: Clerk user ID from session (stored as auth_provider_id)
            email: User email from Clerk
            first_name: User first name from Clerk
            last_name: User last name from Clerk
            organization_name: Organization name from form
            organization_subdomain: Unique subdomain from form
            user_role: User's role in organization
            is_organization_owner: Whether user is org owner

        Returns:
            Dict with success status, user, and tenant information

        Raises:
            HTTPException: If user already exists, subdomain taken, or other errors
        """
        logger.info(f"[SIGNUP] Starting organization creation for Clerk user: {clerk_user_id}")
        logger.info(f"[SIGNUP] Organization: {organization_name}, Subdomain: {organization_subdomain}")

        # Step 1: Check if user already exists in our database
        existing_user = await self.user_repo.get_by_auth_provider_id(clerk_user_id)
        if existing_user:
            logger.warning(f"[SIGNUP] User already exists: {existing_user.code}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User already has an organization. User code: {existing_user.code}"
            )

        # Step 2: Validate subdomain availability
        subdomain_lower = organization_subdomain.lower()

        # Check reserved subdomains (DNS records that exist but are not tenant orgs)
        if subdomain_lower in settings.reserved_subdomains_list:
            logger.warning(f"[SIGNUP] Reserved subdomain requested: {subdomain_lower}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Organization name '{subdomain_lower}' is reserved and cannot be used"
            )

        existing_tenant = await self.tenant_repo.get_by_subdomain(subdomain_lower)
        if existing_tenant:
            logger.warning(f"[SIGNUP] Subdomain already taken: {subdomain_lower}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Subdomain '{subdomain_lower}' is already taken"
            )

        logger.info(f"[SIGNUP] Subdomain available: {subdomain_lower}")

        # Step 3: Create tenant (organization)
        try:
            tenant = await self.tenant_repo.create_tenant(
                name=organization_name,
                subdomain=subdomain_lower,
                description=f"Organization for {organization_name}"
            )
            logger.info(f"[SIGNUP] Created tenant: {tenant.code} (subdomain: {tenant.subdomain})")

        except Exception as e:
            logger.error(f"[SIGNUP] Failed to create tenant: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create organization: {str(e)}"
            )

        # Step 4: Create user with tenant association
        try:
            user = await self.user_repo.create_user(
                auth_provider_id=clerk_user_id,  # Clerk user ID
                auth_provider=AuthProviderEnum.clerk,
                email=email,
                first_name=first_name,
                last_name=last_name,
                tenant_code=tenant.code,
                is_org_owner=is_organization_owner
            )
            logger.info(
                f"[SIGNUP] Created user: {user.code} "
                f"(email: {user.email_id}, tenant: {user.tenants_mst_code})"
            )

        except Exception as e:
            logger.error(f"[SIGNUP] Failed to create user: {str(e)}")
            # Rollback will happen automatically via SQLAlchemy
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create user: {str(e)}"
            )

        # Step 4.5: Create role in role_mst table
        try:
            role = await self.role_repo.create_role(
                user_mst_id=user.id,
                role_name=user_role,
                description=f"{user_role} role for {user.name}"
            )
            logger.info(
                f"[SIGNUP] Created role: {role.code} "
                f"(name: {role.name}, user_id: {role.user_mst_id})"
            )

        except Exception as e:
            logger.error(f"[SIGNUP] Failed to create role: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create user role: {str(e)}"
            )

        # Step 5: Create Clerk Organization and add user as admin member
        clerk_org_id = None
        try:
            clerk_org = self.clerk.organizations.create(
                request=CreateOrganizationRequestBody(
                    name=organization_name,
                    slug=subdomain_lower,
                    created_by=clerk_user_id,
                    public_metadata={
                        "subdomain": subdomain_lower,
                        "tenantCode": tenant.code,
                    },
                )
            )
            clerk_org_id = clerk_org.id if clerk_org else None
            logger.info(f"[SIGNUP] Created Clerk Organization: {clerk_org_id} (slug: {subdomain_lower})")

            # Add the user as admin of the Clerk Organization
            if clerk_org_id:
                self.clerk.organization_memberships.create(
                    organization_id=clerk_org_id,
                    user_id=clerk_user_id,
                    role="org:admin",
                )
                logger.info(f"[SIGNUP] Added user {clerk_user_id} as admin of Clerk org {clerk_org_id}")

        except Exception as e:
            logger.warning(
                f"[SIGNUP] Failed to create Clerk Organization (non-blocking): {str(e)}. "
                f"DB tenant/user already created — continuing with metadata update."
            )

        if clerk_org_id:
            set_clerk_organization_id(tenant, clerk_org_id, self.db)
            logger.info(f"[SIGNUP] Persisted Clerk org {clerk_org_id} to tenant {tenant.code} config")

        # Step 6: Update Clerk public metadata with organization subdomain
        # NOTE: User was just created in Clerk, there may be a propagation delay
        # Retry with exponential backoff to handle timing issues
        import asyncio

        max_retries = 5
        retry_delay = 0.5  # Start with 500ms

        for attempt in range(1, max_retries + 1):
            try:
                logger.info("=" * 60)
                logger.info(f"[SIGNUP] Attempt {attempt}/{max_retries}: Updating Clerk public metadata")
                logger.info(f"[SIGNUP] Clerk User ID: {clerk_user_id}")
                logger.info(f"[SIGNUP] Subdomain to set: {subdomain_lower}")
                logger.info(f"[SIGNUP] User role to set: {user_role}")
                logger.info("=" * 60)

                # Update metadata directly using update_metadata()
                # No need to verify user exists first - just update metadata
                # The JWT validator already confirmed the user ID is valid
                try:
                    logger.info(f"[SIGNUP] Updating Clerk metadata for user: {clerk_user_id}")
                    metadata = {
                            "organizationSubdomain": subdomain_lower,
                            "userRole": user_role,
                            "isOrganizationOwner": is_organization_owner,
                        }
                    if clerk_org_id:
                        metadata["clerkOrganizationId"] = clerk_org_id
                    self.clerk.users.update_metadata(
                        user_id=clerk_user_id,
                        public_metadata=metadata,
                    )
                    logger.info(f"[SIGNUP] ✓ Successfully updated Clerk metadata on attempt {attempt}")
                    break  # Success! Exit retry loop

                except Exception as update_error:
                    error_str = str(update_error)

                    # If user not found, retry (propagation delay)
                    if ("not found" in error_str.lower() or "No user was found" in error_str) and attempt < max_retries:
                        logger.warning(
                            f"[SIGNUP] Metadata update failed - user not available in Clerk API yet. "
                            f"Retrying in {retry_delay}s... (attempt {attempt}/{max_retries})"
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        continue
                    else:
                        # Max retries or different error
                        raise update_error

            except Exception as e:
                error_str = str(e)

                # Check if this is a "not found" error that we can retry
                if ("not found" in error_str.lower() or "No user was found" in error_str) and attempt < max_retries:
                    logger.warning(
                        f"[SIGNUP] Clerk API call failed (timing issue). "
                        f"Retrying in {retry_delay}s... (attempt {attempt}/{max_retries})"
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue

                # Max retries reached or different error type
                logger.error(f"[SIGNUP] Failed to update Clerk metadata: {error_str}")

                # Check if user doesn't exist after all retries (critical error)
                if "not found" in error_str.lower() or "No user was found" in error_str:
                    logger.error(
                        f"[SIGNUP] ❌ CLERK USER NOT FOUND after {max_retries} attempts: The user {clerk_user_id} doesn't exist in Clerk. "
                        f"This usually happens when:"
                        f"\n  1. User was deleted from Clerk dashboard but session still cached"
                        f"\n  2. Frontend and backend are using different Clerk environments"
                        f"\n  3. Clerk has an unusual propagation delay (>15 seconds)"
                    )
                    # Since this is critical, rollback the database transaction
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Clerk user not found after multiple retries. Your session may be stale. "
                            f"Please log out completely, clear your browser cache, and sign up again with a new account."
                        )
                    )

                # For other errors, just log and continue (non-critical)
                logger.warning(
                    f"[SIGNUP] Organization created successfully but Clerk metadata update failed. "
                    f"User: {user.code}, Tenant: {tenant.code}"
                )
                break  # Exit retry loop even if failed (non-critical error)

        logger.info(
            f"[SIGNUP] Organization creation completed successfully. "
            f"User: {user.code}, Tenant: {tenant.code}"
        )

        return {
            "success": True,
            "message": "Organization created successfully",
            "user": user,
            "tenant": tenant,
        }

    async def create_trail_application(self, subdomain: str, clerk_user_id: str) -> Dict[str, Any]:
        """
        Create a default workspace, trial application, and default resource group
        for a newly signed-up tenant.

        The application name is "{tenant_code}_trail" and is linked to the default workspace.

        Args:
            subdomain: Organization subdomain (equals tenant_code)
            clerk_user_id: Clerk user ID from JWT (used to fetch the org owner)

        Returns:
            Dict with workspace, application, and resource group details

        Raises:
            HTTPException: If tenant/user not found or creation fails
        """
        logger.info(f"[SIGNUP] Creating trail application for subdomain: {subdomain}")

        # Verify tenant exists
        tenant = await self.tenant_repo.get_by_subdomain(subdomain)
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Tenant with subdomain '{subdomain}' not found"
            )

        # Fetch the org owner user (created in create_organization step)
        user = await self.user_repo.get_by_auth_provider_id(clerk_user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User not found for the provided session"
            )

        # Create default workspace (also adds user as owner in workspace_user_mapping)
        try:
            workspace_service = WorkspaceService(self.db)
            workspace_result = await workspace_service.create_workspace(
                user=user,
                tenant=tenant,
                data=CreateWorkspaceRequest(workspace_name="Default Workspace"),
            )
            workspace_code = workspace_result["code"]
            logger.info(f"[SIGNUP] Created default workspace: {workspace_code}")
        except Exception as e:
            logger.error(f"[SIGNUP] Failed to create default workspace: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create default workspace: {str(e)}"
            )

        application_name = f"{tenant.code}_trail"

        # Create application (linked to the default workspace)
        try:
            application_data = make_application(
                tenant_code=tenant.code,
                application_name=application_name,
                description=f"Trail application for {tenant.name}",
                workspace_code=workspace_code,
            )
            created_app = await self.applications_repo.create(**application_data)
            logger.info(f"[SIGNUP] Created trail application: {created_app.code} ({created_app.name})")
        except Exception as e:
            logger.error(f"[SIGNUP] Failed to create trail application: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create trail application: {str(e)}"
            )

        # Create default resource group
        try:
            rg_data = make_default_resource_group(
                application_code=created_app.code,
                application_name=application_name,
                tenant_code=tenant.code
            )
            created_rg = await self.resource_groups_repo.create(**rg_data)
            logger.info(f"[SIGNUP] Created default resource group: {created_rg.code}")
        except Exception as e:
            logger.error(f"[SIGNUP] Failed to create resource group: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create resource group: {str(e)}"
            )

        # Create default geographic location (region)
        geo_loc_code = f"region-{tenant.code}-us"
        try:
            created_geo = await self.geo_loc_repo.create(
                code=geo_loc_code,
                name="US",
                description=f"us-east-1 region for {tenant.name}",
                tenants_mst_code=tenant.code,
            )
            logger.info(f"[SIGNUP] Created geo location: {created_geo.code}")
        except Exception as e:
            logger.error(f"[SIGNUP] Failed to create geo location: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create geo location: {str(e)}"
            )

        # Create default Jenkins pipeline vendor config (tenant-level, all environments)
        # Auth credentials come from env vars — this record is for DB tracking only
        try:
            for env in EnvironmentEnum:
                vendor_code = f"pv-jenkins-{tenant.code}-{env.value}"
                await self.pipeline_vendor_repo.create(
                    code=vendor_code,
                    name=f"Jenkins ({env.value})",
                    description=f"Jenkins CI/CD for {tenant.name} - {env.value}",
                    tenants_mst_code=tenant.code,
                    applications_mst_code=None,
                    resource_group_mst_code=None,
                    service_mst_code=None,
                    environment=env,
                    pipeline_agent_enum=PipelineAgentEnum.jenkins,
                    auth_config=None,
                    runner_info_config=None,
                )
            logger.info(f"[SIGNUP] Created Jenkins pipeline vendor configs for tenant: {tenant.code}")
        except Exception as e:
            logger.error(f"[SIGNUP] Failed to create pipeline vendor config: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create pipeline vendor config: {str(e)}"
            )

        return {
            "success": True,
            "message": "Trail application created successfully",
            "workspace_code": workspace_code,
            "workspace_name": workspace_result["name"],
            "application_code": created_app.code,
            "application_name": created_app.name,
            "resource_group_code": created_rg.code,
            "resource_group_name": created_rg.name,
            "geo_loc_code": created_geo.code,
        }
