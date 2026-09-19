"""
Service Reference Tools API Schemas

Pydantic schemas for MCP service reference tool endpoints.
"""
from pydantic import BaseModel, Field
from typing import List, Optional
from app.core.enum import EnvironmentEnum


class ListServicesRequestSchema(BaseModel):
    """Request schema for list_services endpoint."""
    environment: EnvironmentEnum = Field(..., description="Environment (dev, staging, prod)")
    geo_loc_code: str = Field(..., min_length=1, description="Geographic location code")
    infra_vendor: Optional[str] = Field(None, description="Infrastructure vendor (aws, azure)")
    infrastructure_type: Optional[str] = Field(None, description="Infrastructure type code")


class ServiceItemSchema(BaseModel):
    """Schema for individual service in response."""
    service_code: str
    service_name: str
    service_type: str  # API or BACKGROUND_SERVICE
    has_existing_config: bool = False


class ListServicesResponseSchema(BaseModel):
    """Response schema for list_services endpoint."""
    services: List[ServiceItemSchema]
    count: int
    infra_suffix: str = Field(default="", description="Formatted infra context")

