"""
Pydantic schemas for Service User Permissions
"""

from typing import List, Optional, Literal
from pydantic import BaseModel, Field, model_validator


class RebuildCacheRequest(BaseModel):
    """Request body for rebuild-cache endpoint"""
    user_mst_code: str = Field(..., description="User code to rebuild cache for")
    role_type_codes: List[str] = Field(default=[], description="List of user's role type codes (from role_type_ref)")
    environment: Optional[str] = Field(None, description="Environment filter (dev, staging, prod)")


class RebuildCacheResponse(BaseModel):
    """Response after cache rebuild"""
    user_mst_code: str = Field(..., description="User code")
    environment: Optional[str] = Field(None, description="Environment")
    permissions_count: int = Field(..., description="Number of permissions in cache")
    cache_id: int = Field(..., description="Cache row ID")


class CheckPermissionRequest(BaseModel):
    """Request body for check-permission endpoint"""
    user_mst_code: str = Field(..., description="User code to check")
    service_mst_code: str = Field(..., description="Service code to check permission for")
    policy_ref_code: str = Field(..., description="Policy code (service_admin, service_manager, service_support)")
    environment: Optional[str] = Field(None, description="Environment filter")


class CheckPermissionResponse(BaseModel):
    """Response for check-permission endpoint"""
    has_permission: bool = Field(..., description="True if user has permission, False otherwise")


class AllowedEnvironmentsResponse(BaseModel):
    """Response for get-allowed-environments endpoint"""
    service_mst_code: str = Field(..., description="Service code")
    environments: List[str] = Field(..., description="List of environments user has permission for (e.g. prod, stage, qa)")


# ============ CRUD Schemas for Permission Management UI ============

class ServicePermissionCreate(BaseModel):
    """Request body to create a new permission"""
    assignment_type: Literal["user", "role"] = Field(..., description="Assign to user or role")
    user_mst_code: Optional[str] = Field(None, description="User code (if assignment_type=user)")
    role_type_ref_code: Optional[str] = Field(None, description="Role code (if assignment_type=role)")
    policy_ref_code: str = Field(..., description="Policy: service_admin, service_manager, service_support")
    scope_type: Literal["service", "application"] = Field(..., description="Permission scope type")
    services_mst_code: Optional[str] = Field(None, description="Service code (if scope_type=service)")
    applications_mst_code: Optional[str] = Field(None, description="Application code (if scope_type=application)")
    environment: Optional[str] = Field(None, description="Environment (null = all environments)")

    @model_validator(mode="after")
    def validate_assignment(self):
        """Ensure user_mst_code or role_type_ref_code is provided based on assignment_type"""
        if self.assignment_type == "user" and not self.user_mst_code:
            raise ValueError("user_mst_code required when assignment_type is 'user'")
        if self.assignment_type == "role" and not self.role_type_ref_code:
            raise ValueError("role_type_ref_code required when assignment_type is 'role'")
        return self

    @model_validator(mode="after")
    def validate_scope(self):
        """Ensure scope code is provided based on scope_type"""
        if self.scope_type == "service" and not self.services_mst_code:
            raise ValueError("services_mst_code required when scope_type is 'service'")
        if self.scope_type == "application" and not self.applications_mst_code:
            raise ValueError("applications_mst_code required when scope_type is 'application'")
        return self


class ServicePermissionResponse(BaseModel):
    """Response for a single permission"""
    code: str = Field(..., description="Permission code")
    assignment_type: str = Field(..., description="user or role")
    assignee_name: str = Field(..., description="User name or Role name")
    assignee_code: str = Field(..., description="User code or Role code")
    policy_ref_code: str = Field(..., description="Policy code")
    policy_name: str = Field(..., description="Policy display name")
    scope_type: str = Field(..., description="service or application")
    scope_name: str = Field(..., description="Service name or Application name")
    scope_code: str = Field(..., description="Service code or Application code")
    environment: Optional[str] = Field(None, description="Environment (null = all)")
    can_read: bool = Field(..., description="Has read capability")
    can_write: bool = Field(..., description="Has write capability")
    can_manage: bool = Field(..., description="Has manage capability")


class PermissionListResponse(BaseModel):
    """Response for list of permissions"""
    permissions: List[ServicePermissionResponse] = Field(..., description="List of permissions")
    total: int = Field(..., description="Total count")


class ManageableScope(BaseModel):
    """A service or application that user can manage"""
    code: str = Field(..., description="Service or Application code")
    name: str = Field(..., description="Display name")
    scope_type: Literal["service", "application"] = Field(..., description="Type")


class ManageableScopesResponse(BaseModel):
    """Response for manageable services/applications"""
    scopes: List[ManageableScope] = Field(..., description="List of manageable scopes")
    total: int = Field(..., description="Total count")


class Assignee(BaseModel):
    """A user or role that can be assigned permission"""
    type: Literal["user", "role"] = Field(..., description="user or role")
    code: str = Field(..., description="User code or Role code")
    name: str = Field(..., description="Display name")
    email: Optional[str] = Field(None, description="Email (for users only)")


class AssigneesResponse(BaseModel):
    """Response for available assignees"""
    users: List[Assignee] = Field(..., description="Available users")
    roles: List[Assignee] = Field(..., description="Available roles")


class PolicyOption(BaseModel):
    """A policy option for dropdown"""
    code: str = Field(..., description="Policy code")
    name: str = Field(..., description="Display name")
    description: Optional[str] = Field(None, description="Policy description")
    can_read: bool = Field(..., description="Has read capability")
    can_write: bool = Field(..., description="Has write capability")
    can_manage: bool = Field(..., description="Has manage capability")


class PoliciesResponse(BaseModel):
    """Response for available policies"""
    policies: List[PolicyOption] = Field(..., description="Available policies")
