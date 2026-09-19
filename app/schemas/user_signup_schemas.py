"""
Schemas for organization creation and management
"""
from pydantic import BaseModel, Field, validator
from typing import Any, Dict, List, Optional
import re


class CreateOrganizationRequest(BaseModel):
    """
    Request schema for creating a new organization

    This endpoint is called after user signs up with Clerk.
    Frontend sends organization data. User identity comes from JWT token (secure).

    Security Note:
    - Clerk user ID is extracted from JWT token by backend (NOT from request body)
    - Email, firstName, lastName are optional - can be obtained from Clerk if needed
    """
    # User details (optional, for convenience - can be fetched from Clerk via JWT)
    email: str = Field(..., description="User email from Clerk session")
    firstName: str = Field(..., description="User first name from Clerk session")
    lastName: str = Field(..., description="User last name from Clerk session")

    # From organization form
    organizationName: str = Field(..., min_length=1, max_length=255, description="Organization name")
    organizationSubdomain: str = Field(..., min_length=3, max_length=63, description="Unique subdomain for organization")
    userRole: str = Field(..., min_length=1, max_length=100, description="User's role in organization")
    isOrganizationOwner: bool = Field(default=True, description="Whether user is organization owner")

    @validator('organizationSubdomain')
    def validate_subdomain(cls, v):
        """Validate subdomain format: lowercase, alphanumeric and hyphens only"""
        if not v:
            raise ValueError('Subdomain is required')

        v = v.lower().strip()

        # Check format: alphanumeric and hyphens only
        if not re.match(r'^[a-z0-9-]+$', v):
            raise ValueError('Subdomain must contain only lowercase letters, numbers, and hyphens')

        # Cannot start or end with hyphen
        if v.startswith('-') or v.endswith('-'):
            raise ValueError('Subdomain cannot start or end with a hyphen')

        # Cannot contain consecutive hyphens
        if '--' in v:
            raise ValueError('Subdomain cannot contain consecutive hyphens')

        return v

    @validator('email')
    def validate_email(cls, v):
        """Basic email validation"""
        if not v or '@' not in v:
            raise ValueError('Invalid email address')
        return v.lower().strip()


class UserResponse(BaseModel):
    """User information in response"""
    code: str
    email: str = Field(alias="email_id")
    first_name: str
    last_name: str
    is_org_owner: bool
    tenant_code: Optional[str] = Field(alias="tenants_mst_code")

    class Config:
        from_attributes = True
        populate_by_name = True


class TenantResponse(BaseModel):
    """Tenant/Organization information in response"""
    code: str
    name: str
    subdomain: str

    class Config:
        from_attributes = True


class CreateOrganizationResponse(BaseModel):
    """Response schema for organization creation"""
    success: bool
    message: str
    user: UserResponse
    tenant: TenantResponse


class CreateTrailApplicationRequest(BaseModel):
    """Request schema for creating a trail application during signup"""
    subdomain: str = Field(..., min_length=3, max_length=63, description="Organization subdomain (used as tenant_code)")

    @validator('subdomain')
    def validate_subdomain(cls, v):
        """Validate subdomain format"""
        v = v.lower().strip()
        if not re.match(r'^[a-z0-9-]+$', v):
            raise ValueError('Subdomain must contain only lowercase letters, numbers, and hyphens')
        return v


class CreateTrailApplicationResponse(BaseModel):
    """Response schema for trail application creation"""
    success: bool
    message: str
    workspace_code: str
    workspace_name: str
    application_code: str
    application_name: str
    resource_group_code: str
    resource_group_name: str
    geo_loc_code: str


class ProvisionSubdomainRequest(BaseModel):
    """Request schema for provisioning a subdomain (Vercel domain) for a new org"""
    subdomain: str = Field(..., min_length=2, max_length=63, description="Organization subdomain")

    @validator('subdomain')
    def validate_subdomain(cls, v):
        """Validate subdomain format"""
        v = v.lower().strip()
        if not re.match(r'^[a-z0-9-]+$', v):
            raise ValueError('Organization name must contain only letters, numbers, and hyphens')
        return v


class ProvisionSubdomainResponse(BaseModel):
    """Response schema for subdomain provisioning"""
    success: bool
    domain: str
    skipped: bool = False
    already_existed: bool = False
    verified: bool = False
    error: Optional[str] = None


class ProvisionInfrastructureRequest(BaseModel):
    """Request schema for provisioning infrastructure repo"""
    subdomain: str = Field(..., min_length=3, max_length=63, description="Organization subdomain")

    @validator('subdomain')
    def validate_subdomain(cls, v):
        """Validate subdomain format"""
        v = v.lower().strip()
        if not re.match(r'^[a-z0-9-]+$', v):
            raise ValueError('Subdomain must contain only lowercase letters, numbers, and hyphens')
        return v


class ProvisionInfrastructureResponse(BaseModel):
    """Response schema for infrastructure provisioning"""
    status: str
    repo_url: Optional[str] = None
    files_committed: Optional[List[str]] = None
    message: Optional[str] = None
    error: Optional[str] = None
