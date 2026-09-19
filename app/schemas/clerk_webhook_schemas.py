from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, EmailStr


class ClerkEmailAddress(BaseModel):
    """Email address object from Clerk"""
    email_address: str
    id: str
    verification: Optional[Dict[str, Any]] = None


class ClerkUserData(BaseModel):
    """Raw user data from Clerk webhook"""
    id: str = Field(..., description="Clerk user ID")
    first_name: Optional[str] = Field(None, description="User's first name")
    last_name: Optional[str] = Field(None, description="User's last name")
    email_addresses: List[ClerkEmailAddress] = Field(default_factory=list, description="User's email addresses")
    primary_email_address_id: Optional[str] = Field(None, description="Primary email address ID")
    public_metadata: Dict[str, Any] = Field(default_factory=dict, description="Public metadata")
    unsafe_metadata: Dict[str, Any] = Field(default_factory=dict, description="Unsafe metadata")
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class ClerkWebhookUserData(BaseModel):
    """Processed user data for internal use"""
    clerkId: str = Field(..., description="Clerk user ID")
    firstName: str = Field(..., description="User's first name")
    lastName: str = Field(..., description="User's last name")
    email_address: EmailStr = Field(..., description="User's email address")

    # Public metadata fields
    organizationName: Optional[str] = Field(None, description="Organization/Tenant name")
    organizationSubdomain: Optional[str] = Field(None, description="Organization subdomain")
    isOrganizationOwner: Optional[bool] = Field(None, description="Whether user is organization owner")
    userRole: Optional[str] = Field(None, description="User role in organization")

    # Unsafe metadata fields (will be moved to public)
    unSafeOrganizationName: Optional[str] = Field(None, description="Unsafe organization name")
    unSafeOrganizationSubdomain: Optional[str] = Field(None, description="Unsafe organization subdomain")
    unSafeIsOrganizationOwner: Optional[bool] = Field(None, description="Unsafe organization owner flag")
    unSafeUserRole: Optional[str] = Field(None, description="Unsafe user role")


class ClerkWebhookRequest(BaseModel):
    """Request schema for Clerk webhook"""
    type: str = Field(..., description="Event type (e.g., user.created, user.updated)")
    data: ClerkUserData = Field(..., description="Raw user data from Clerk")


class ClerkWebhookResponse(BaseModel):
    """Response schema for Clerk webhook"""
    received: bool = Field(default=True, description="Whether webhook was received successfully")
    user_code: Optional[str] = Field(None, description="Created/updated user code")
    tenant_code: Optional[str] = Field(None, description="Associated tenant code")
    message: Optional[str] = Field(None, description="Additional message")
