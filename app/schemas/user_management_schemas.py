"""
Schemas for User Management API.
"""
from pydantic import BaseModel, Field, EmailStr
from typing import Optional


class GetUserByEmailRequest(BaseModel):
    """Request schema for getting user details by email."""
    email: EmailStr = Field(..., description="User email address")


class GetUserByEmailResponse(BaseModel):
    """Response schema for user details."""
    user_mst_code: str = Field(..., description="User code")
    tenants_mst_code: str = Field(..., description="Tenant code")
    email_id: str = Field(..., description="User email address")
    first_name: str = Field(..., description="User first name")
    last_name: str = Field(..., description="User last name")
    is_org_owner: bool = Field(..., description="Whether user is organization owner")

    class Config:
        from_attributes = True
