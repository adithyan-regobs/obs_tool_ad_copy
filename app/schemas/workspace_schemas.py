from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

from app.core.enum import WorkspaceStatusEnum, WorkspaceRoleEnum


# ─── Create Workspace ──────────────────────────────────────────────────────
class CreateWorkspaceRequest(BaseModel):
    """
    Request schema for creating a workspace.

    Note: tenant is taken from JWT, never from request body.
    """
    workspace_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=500)

    @field_validator("workspace_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("workspace_name cannot be empty")
        return v.strip()

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip():
            return v.strip()
        return None


class WorkspaceCreateResponse(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    tenant_code: str
    status: WorkspaceStatusEnum
    is_active: bool
    created_at: datetime
    creator_user_code: str
    creator_role: WorkspaceRoleEnum
    message: str = "Workspace created successfully and creator added as owner"


# ─── Add Users to Workspace ────────────────────────────────────────────────
class AddUserItem(BaseModel):
    user_code: str = Field(..., min_length=1, max_length=100)
    role: WorkspaceRoleEnum

    @field_validator("user_code")
    @classmethod
    def _strip_user_code(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("user_code cannot be empty")
        return v.strip()


class AddUsersToWorkspaceRequest(BaseModel):
    users: List[AddUserItem] = Field(..., min_length=1)


class AddUserResultItem(BaseModel):
    user_code: str
    role: Optional[WorkspaceRoleEnum] = None


class FailedUserItem(BaseModel):
    user_code: str
    reason: str


class AddUsersToWorkspaceResponse(BaseModel):
    workspace_code: str
    added: List[AddUserResultItem] = []
    skipped: List[AddUserResultItem] = []
    failed: List[FailedUserItem] = []
    total_requested: int
    total_added: int
    total_skipped: int
    total_failed: int


# ─── Get All Workspaces ────────────────────────────────────────────────────
class GetAllWorkspacesRequest(BaseModel):
    status: Optional[WorkspaceStatusEnum] = None
    is_active: Optional[bool] = None
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class WorkspaceListItem(BaseModel):
    """
    Unified shape used by both the user-scoped listing (populates `user_role`)
    and the tenant-scoped listing (populates `tenant_code`, `is_active`,
    `member_count`). Fields not produced by a given repository call default
    to None / 0.
    """
    id: int
    code: str
    name: str
    description: Optional[str] = None
    tenant_code: Optional[str] = None
    status: WorkspaceStatusEnum
    is_active: Optional[bool] = None
    user_role: Optional[str] = None
    member_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None


class WorkspacesListResponse(BaseModel):
    total: int
    skip: int = 0
    limit: int = 100
    workspaces: List[WorkspaceListItem] = []


# ─── Workspace Members ─────────────────────────────────────────────────────
class WorkspaceMemberItem(BaseModel):
    id: int
    mapping_code: str
    user_code: str
    first_name: str
    last_name: str
    email_id: str
    role: WorkspaceRoleEnum
    is_org_owner: bool
    is_active: bool
    created_at: datetime


class WorkspaceMembersResponse(BaseModel):
    workspace_code: str
    total: int
    members: List[WorkspaceMemberItem] = []


class RemoveWorkspaceMemberResponse(BaseModel):
    workspace_code: str
    user_code: str
    removed: bool = True


# ─── Eligible users (not yet in workspace) ────────────────────────────────
class EligibleUserItem(BaseModel):
    user_code: str
    first_name: str
    last_name: str
    email_id: str
    is_org_owner: bool


class EligibleUsersResponse(BaseModel):
    workspace_code: str
    total: int
    users: List[EligibleUserItem] = []
