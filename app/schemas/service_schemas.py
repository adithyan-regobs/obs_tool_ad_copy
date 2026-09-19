from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from app.core.enum import ServiceTypeEnum


class GetAllServicesRequest(BaseModel):
    """
    Request schema for getting all services.

    Note: tenant_code is automatically extracted from JWT authentication
    and should NOT be sent from frontend.
    """
    application_code: Optional[str] = None
    resource_group_mst_code: Optional[str] = None
    is_active: Optional[bool] = None
    search_query: Optional[str] = None
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("application_code")
    @classmethod
    def validate_application_code(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("application_code cannot be empty string")
        return v.strip() if v else None


class ServiceListItem(BaseModel):
    """Individual service item for list response"""
    id: int
    service_code: str
    service_name: str
    description: Optional[str] = None
    tenant_code: str
    tenant_name: str
    application_code: str
    application_name: str
    resource_group_code: str
    resource_group_name: str
    resource_group_kind: str
    status: str  # "active" or "inactive"
    is_public_facing: bool
    service_type: Optional[str] = None  # Service type: API or BACKGROUND_SERVICE
    # NOTE: infrastructuretype_ref_code and infra_vendor_enum moved to service_config
    alerts_configured: int = 0
    alerts_total: int = 0
    alerts_failed: int = 0
    infrastructure_count: int = 0
    dependency_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None


class ServicesListResponse(BaseModel):
    """Response for services list endpoint"""
    total: int = Field(..., description="Total number of services matching the query")
    skip: int = Field(default=0, description="Pagination offset")
    limit: int = Field(default=100, description="Page size")
    services: List[ServiceListItem] = Field(default=[], description="List of services")


class GetServiceByCodeRequest(BaseModel):
    """
    Request schema for getting service by code.

    Note: tenant_code is automatically extracted from JWT authentication
    and should NOT be sent from frontend.
    """
    service_code: str = Field(..., description="Auto-generated service code")

    @field_validator("service_code")
    @classmethod
    def validate_service_code(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("service_code is required and cannot be empty")
        return v.strip()


class ServiceDetailResponse(BaseModel):
    """Response schema for single service detail"""
    id: int
    service_code: str
    service_name: str
    description: Optional[str] = None
    tenant_code: str
    tenant_name: str
    application_code: str
    application_name: str
    resource_group_code: str
    resource_group_name: str
    resource_group_kind: str
    status: str  # "active" or "inactive"
    is_active: bool
    is_public_facing: bool
    service_type: Optional[str] = None  # Service type: API or BACKGROUND_SERVICE
    # NOTE: infrastructuretype_ref_code and infra_vendor_enum moved to service_config
    alerts_configured: int = 0
    alerts_total: int = 0
    alerts_failed: int = 0
    infrastructure_count: int = 0
    dependency_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None
    # Owner (point of contact) — null when no owner is assigned to the service.
    owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    owner_contact: Optional[str] = None  # reserved; user_mst has no phone column yet


class UpdateServiceOwnerRequest(BaseModel):
    """Request schema for changing a service's owner."""
    service_code: str = Field(..., description="Service code to update")
    owner_user_code: Optional[str] = Field(
        None,
        description="user_mst.code of the new owner, or null to unassign",
    )


class UpdateServiceOwnerResponse(BaseModel):
    """Response schema after changing a service's owner."""
    service_code: str
    owner_user_code: Optional[str] = None
    owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    message: str


class CreateServiceRequest(BaseModel):
    """
    Request schema for creating a new service.

    Note: tenant_code is NOT included in request - it's automatically extracted
    from JWT authentication and passed separately to the service layer.

    Note: infrastructuretype_ref_code and infra_vendor_enum have been moved to service_config
    to allow different configurations per environment/geo location.
    """
    application_code: str = Field(..., description="Application code this service belongs to")
    resource_group_code: str = Field(..., description="Resource group code this service belongs to")
    service_name: str = Field(..., min_length=1, max_length=255, description="Service name")
    service_type: ServiceTypeEnum = Field(..., description="Service type: API or Background Service")
    is_active: bool = Field(default=True, description="Active status (default: True)")
    is_public_facing: bool = Field(default=False, description="Whether service is publicly accessible (has public endpoints)")

    @field_validator("application_code")
    @classmethod
    def validate_application_code(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("application_code is required and cannot be empty")
        return v.strip()

    @field_validator("resource_group_code")
    @classmethod
    def validate_resource_group_code(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("resource_group_code is required and cannot be empty")
        return v.strip()

    @field_validator("service_name")
    @classmethod
    def validate_service_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("service_name is required and cannot be empty")
        return v.strip()


class DeleteServiceRequest(BaseModel):
    service_code: str = Field(..., description="Service code to delete")


class DeleteServiceResponse(BaseModel):
    service_code: str
    service_name: str
    is_deleted: bool
    is_active: bool
    message: str = Field(default="Service deleted successfully")


class CreateServiceResponse(BaseModel):
    """Response schema for created service"""
    id: int
    service_code: str = Field(..., description="Auto-generated service code")
    service_name: str
    tenant_code: str
    application_code: str
    resource_group_code: str
    service_type: ServiceTypeEnum
    # NOTE: infra_vendor_enum moved to service_config
    is_active: bool
    is_public_facing: bool
    created_at: datetime
    message: str = Field(default="Service created successfully")
