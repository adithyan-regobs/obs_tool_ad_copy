"""
Authentication Schemas

Defines request and response models for authentication endpoints.
"""
from pydantic import BaseModel, Field
from typing import Optional


class LoginResponse(BaseModel):
    """Response schema for login endpoint"""

    # User information
    user_id: str = Field(..., description="Internal user ID (UUID)")
    user_code: str = Field(..., description="User code")
    first_name: str = Field(..., description="User's first name")
    last_name: str = Field(..., description="User's last name")
    email: str = Field(..., description="User's email address")
    is_org_owner: bool = Field(..., description="Whether user is organization owner")

    # Tenant information
    tenant_id: str = Field(..., description="Internal tenant ID (UUID)")
    tenant_code: str = Field(..., description="Tenant code")
    tenant_name: str = Field(..., description="Organization/Tenant name")
    tenant_subdomain: Optional[str] = Field(None, description="Organization subdomain from Clerk")

    # Authentication info
    auth_provider: str = Field(..., description="Authentication provider (clerk, manual, etc.)")


class SubdomainValidationResponse(BaseModel):
    """Response schema for subdomain validation"""
    available: bool = Field(..., description="Whether the subdomain is available")
    subdomain: str = Field(..., description="The subdomain that was checked")
    message: str = Field(..., description="Human-readable message about availability")
