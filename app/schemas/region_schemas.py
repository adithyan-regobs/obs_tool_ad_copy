"""
Pydantic schemas for Region Reference
"""
from typing import List, Optional
from pydantic import BaseModel, Field
from datetime import datetime

from app.core.enum import InfraVendorEnum


class RegionResponse(BaseModel):
    """Individual region item for API responses"""
    id: int
    code: str = Field(..., description="Region reference code (e.g., 'aws-us-east-1')")
    name: str = Field(..., description="Human-readable region name")
    description: Optional[str] = Field(None, description="Region description")
    infra_vendor_enum: str = Field(..., description="Infrastructure vendor")
    region_identifier: str = Field(..., description="Actual region code (e.g., 'us-east-1')")
    display_order: int = Field(..., description="Sort order for UI dropdowns")
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True  # For Pydantic v2 (was orm_mode in v1)


class RegionsListResponse(BaseModel):
    """Response for regions list endpoint (cascading dropdown)"""
    vendor: str = Field(..., description="Infrastructure vendor")
    supports_custom: bool = Field(..., description="Whether vendor supports custom region input")
    regions: List[RegionResponse] = Field(default=[], description="List of available regions")
    total: int = Field(..., description="Total number of regions")
