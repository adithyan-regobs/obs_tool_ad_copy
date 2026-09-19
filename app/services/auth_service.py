"""
Authentication Service

Handles authentication business logic including login and user validation.
"""
import logging
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.core.config import settings
from app.repository.user_mst_repository import UserMstRepository
from app.repository.tenants_mst_repository import TenantsMstRepository

logger = logging.getLogger(__name__)


class AuthService:
    """
    Service layer for authentication operations.
    Handles business logic for user login and validation.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.user_repository = UserMstRepository(session)
        self.tenant_repository = TenantsMstRepository(session)

    async def login(
        self,
        user_id: str,
        organization_subdomain: str
    ) -> Dict[str, Any]:
        """
        Handle user login with tenant validation.

        Business Logic:
        - Validates user exists and is active
        - Validates tenant exists and is active
        - Ensures user belongs to the tenant
        - Returns user and tenant information

        Args:
            user_id: Clerk user ID (auth_provider_id)
            organization_subdomain: Organization subdomain from Clerk JWT

        Returns:
            Dict containing user and tenant information

        Raises:
            HTTPException: If user/tenant not found or validation fails
        """
        logger.info(f"Login attempt for user_id: {user_id}, subdomain: {organization_subdomain}")

        # Get tenant by subdomain
        tenant = await self.tenant_repository.get_by_subdomain(organization_subdomain)
        if not tenant:
            logger.warning(f"Tenant not found for subdomain: {organization_subdomain}")
            raise HTTPException(
                status_code=403,
                detail=f"Organization '{organization_subdomain}' not found or inactive"
            )

        # Get user by auth_provider_id (Clerk ID)
        user = await self.user_repository.get_by_auth_provider_id(user_id)
        if not user:
            logger.warning(f"User not found for auth_provider_id: {user_id}")
            raise HTTPException(
                status_code=403,
                detail="User not found or inactive. Please contact your administrator."
            )

        # Business logic: Verify user belongs to tenant
        if user.tenants_mst_code != tenant.code:
            logger.error(
                f"User {user.code} (tenant: {user.tenants_mst_code}) "
                f"attempted to access tenant {tenant.code}"
            )
            raise HTTPException(
                status_code=403,
                detail="Access denied: You do not belong to this organization"
            )

        logger.info(
            f"Login successful: {user.code} ({user.email_id}) "
            f"for tenant: {tenant.code} ({tenant.name})"
        )

        # Return user and tenant information
        return {
            "user_id": str(user.id),
            "user_code": user.code,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email_id,
            "is_org_owner": user.is_org_owner,
            "tenant_id": str(tenant.id),
            "tenant_code": tenant.code,
            "tenant_name": tenant.name,
            "tenant_subdomain": tenant.subdomain,
            "auth_provider": user.auth_provider.value if user.auth_provider else "manual"
        }

    async def check_subdomain_availability(self, subdomain: str) -> Dict[str, Any]:
        """
        Check if a subdomain is available for organization creation.

        Business Logic:
        - Validates subdomain is not empty
        - Checks if subdomain already exists in database

        Args:
            subdomain: The subdomain to check

        Returns:
            Dict with:
            - available (bool): Whether subdomain is available
            - subdomain (str): The subdomain checked
            - message (str): Human-readable availability message

        Raises:
            ValueError: If subdomain is empty
        """
        if not subdomain or not subdomain.strip():
            raise ValueError("Subdomain cannot be empty")

        subdomain = subdomain.strip()

        # Check reserved subdomains (DNS records that exist but are not tenant orgs)
        if subdomain.lower() in settings.reserved_subdomains_list:
            return {
                "available": False,
                "subdomain": subdomain,
                "message": f"Organization name '{subdomain}' is reserved and cannot be used"
            }

        # Check database for existing tenant with this subdomain
        existing_tenant = await self.tenant_repository.get_by_subdomain(subdomain)

        if existing_tenant:
            return {
                "available": False,
                "subdomain": subdomain,
                "message": "Organization name is already taken"
            }

        # Subdomain is available
        return {
            "available": True,
            "subdomain": subdomain,
            "message": f"Subdomain '{subdomain}' is available"
        }
