"""
Pydantic schemas for invitation management
"""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field


class InviteMemberRequest(BaseModel):
    """Request schema for sending invitation"""
    email: EmailStr = Field(..., description="Email address to invite")
    role: str = Field(..., description="User role (Admin/User)")
    is_org_owner: bool = Field(default=False, description="Whether user should be org owner")


class InviteMemberResponse(BaseModel):
    """Response schema for sending invitation"""
    success: bool
    message: str
    invitation_id: str
    invitation_url: Optional[str] = None


class VerifyInvitationRequest(BaseModel):
    """Request schema for verifying invitation token"""
    token: str = Field(..., description="Invitation token from URL")


class VerifyInvitationResponse(BaseModel):
    """Response schema for invitation verification"""
    valid: bool
    email: Optional[str] = None
    role: Optional[str] = None
    is_org_owner: Optional[bool] = None
    organization_name: Optional[str] = None
    organization_subdomain: Optional[str] = None
    expires_at: Optional[datetime] = None
    message: Optional[str] = None


class AcceptInvitationRequest(BaseModel):
    """Request schema for accepting invitation"""
    token: str = Field(..., description="Invitation token from URL")
    clerk_user_id: str = Field(..., description="Clerk user ID after signup")
    first_name: str = Field(..., description="User's first name")
    last_name: str = Field(..., description="User's last name")


class AcceptInvitationResponse(BaseModel):
    """Response schema for accepting invitation"""
    success: bool
    message: str
    user_code: Optional[str] = None
    tenant_code: Optional[str] = None
    subdomain: Optional[str] = None


class CancelInvitationRequest(BaseModel):
    """Request schema for cancelling invitation"""
    invitation_id: str = Field(..., description="Invitation code to cancel")


class CancelInvitationResponse(BaseModel):
    """Response schema for cancelling invitation"""
    success: bool
    message: str


class ResendInvitationRequest(BaseModel):
    """Request schema for resending invitation"""
    invitation_id: str = Field(..., description="Invitation code to resend")


class ResendInvitationResponse(BaseModel):
    """Response schema for resending invitation"""
    success: bool
    message: str
    invitation_url: Optional[str] = None


class PendingInvitation(BaseModel):
    """Schema for pending invitation in team members list"""
    id: str
    code: str
    email: str
    role: str
    is_org_owner: bool
    status: str
    invited_by: Optional[str] = None
    invited_at: datetime
    expires_at: datetime

    class Config:
        from_attributes = True
