from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
from datetime import datetime


class GetAllApplicationsRequest(BaseModel):
    """
    Request schema for getting all applications.

    Note: tenant_code is NOT included in request - it's automatically extracted
    from JWT authentication and passed separately to the service layer.
    """
    is_active: Optional[bool] = None
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    workspace_code: Optional[str] = Field(None, description="Filter by workspace. If omitted, returns all tenant applications.")


class ApplicationListItem(BaseModel):
    """Individual application item for list response"""
    id: int
    application_code: str
    application_name: str
    description: Optional[str] = None
    tenant_code: str
    tenant_name: str
    workspace_code: Optional[str] = None
    status: str  # "active" or "inactive"
    is_active: bool
    services_count: int = 0
    resource_groups_count: int = 0
    alerts_configured: int = 0
    alerts_total: int = 0
    infrastructure_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None


class ApplicationsListResponse(BaseModel):
    """Response for applications list endpoint"""
    total: int = Field(..., description="Total number of applications matching the query")
    skip: int = Field(default=0, description="Pagination offset")
    limit: int = Field(default=100, description="Page size")
    applications: List[ApplicationListItem] = Field(default=[], description="List of applications")


class CreateApplicationRequest(BaseModel):
    """
    Request schema for creating a new application.

    Note: tenant subdomain is NOT included in request - it's automatically extracted
    from JWT authentication and passed separately to the service layer.
    """
    application_name: str = Field(..., min_length=1, max_length=255, description="Application name")
    description: Optional[str] = Field(None, max_length=500, description="Optional application description")
    workspace_code: Optional[str] = Field(None, description="Workspace to assign this application to")

    @field_validator("application_name")
    @classmethod
    def validate_application_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("application_name cannot be empty")
        if len(v) > 255:
            raise ValueError("application_name cannot exceed 255 characters")
        return v.strip()

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip():
            return v.strip()
        return None


class ApplicationCreateResponse(BaseModel):
    """Response schema for created application"""
    # Application details
    id: int
    code: str
    name: str
    description: Optional[str] = None
    tenant_code: str
    tenant_name: str
    is_active: bool
    created_at: datetime

    workspace_code: Optional[str] = None

    # Default resource group details
    default_resource_group_code: str
    default_resource_group_name: str

    message: str = "Application created successfully with default resource group"
