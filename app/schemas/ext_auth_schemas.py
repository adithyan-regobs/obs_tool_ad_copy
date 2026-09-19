"""
Schemas for external client auth code flow
"""
from pydantic import BaseModel, Field
from typing import Optional


class ExtAuthExchangeRequest(BaseModel):
    code: str = Field(..., description="Short-lived auth code")
    client_id: str = Field(..., description="External client ID")
    state: Optional[str] = Field(None, description="State returned from authorize")


class ExtAuthUserInfo(BaseModel):
    user_id: str
    user_code: str
    first_name: str
    last_name: str
    email: str
    is_org_owner: bool
    auth_provider: str


class ExtAuthTenantInfo(BaseModel):
    tenant_id: str
    tenant_code: str
    tenant_name: str
    tenant_subdomain: Optional[str] = None


class ExtAuthExchangeResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: ExtAuthUserInfo
    tenant: ExtAuthTenantInfo


class ExtAuthAuthorizeResponse(BaseModel):
    code: str
    state: Optional[str] = None
