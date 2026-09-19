"""
Infrastructure Schemas

Pydantic schemas for unified infrastructure creation (regular infrastructure + Kong routes).
"""
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, model_validator
from app.core.enum import EnvironmentEnum, WorkflowSourceTableEnum


class InfrastructureDetailRequest(BaseModel):
    code: str


class CloneInfraSettingsRequest(BaseModel):
    """Clone the settings (locator config) of one infra resource into another of the same type."""
    source_code: str = Field(..., description="infrastructure_mst.code to copy settings from")
    target_code: str = Field(..., description="infrastructure_mst.code to copy settings into")


class InfrastructureDetailResponse(BaseModel):
    code: str
    name: Optional[str] = None
    infrastructuretype_ref_code: Optional[str] = None
    environment: Optional[str] = None
    geo_loc_mst_code: Optional[str] = None
    infra_status: Optional[str] = None
    locator: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class InfrastructureStatusResponse(BaseModel):
    """Response schema for infrastructure status check"""
    code: str
    infra_status: Optional[str] = None
    infra_status_updated_by: Optional[str] = None
    infra_status_updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class InfrastructureCreateRequest(BaseModel):
    """
    Unified infrastructure creation request.

    Uses infrastructuretype_ref_code to determine routing:
    - kong_infrastructuretype_ref → saves to kong_route_configs table
    - Any other infrastructure type → saves to infrastructure_mst table

    Supports upsert:
    - If code is provided: Updates existing record
    - If code is None: Creates new record
    """

    # Optional: Provide code to update existing record (for upsert)
    code: Optional[str] = Field(
        None,
        description="Infrastructure/Kong route code to UPDATE. If not provided, creates NEW record"
    )

    # Infrastructure type (determines routing and target table)
    infrastructuretype_ref_code: str = Field(
        ...,
        description="Infrastructure type code (e.g., s3_infrastructuretype_ref, kong_infrastructuretype_ref)"
    )

    # Common required fields
    application_code: str = Field(..., description="Application code")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    geo_loc_mst_code: str = Field(..., description="Geographic location code")

    # Type-specific configuration (required)
    type_specific_config: Dict[str, Any] = Field(
        ...,
        description="""Type-specific configuration:
        - For INFRASTRUCTURE (S3): {identifier, region?}
        - For INFRASTRUCTURE (SQS): {identifier, region?}
        - For INFRASTRUCTURE (DynamoDB): {identifier, partition_key, partition_key_type, region?}
        - For KONG_ROUTE: {method, route, api_name?}
        Note: region is optional and will be auto-derived from geo_loc_mst_code if not provided.
        Note: api_name is optional and will default to service.name if not provided
        """
    )

    # Infrastructure-specific fields
    resource_group_mst_code: Optional[str] = Field(
        None,
        description="Optional resource group code"
    )

    # Service association (required for KONG_ROUTE, optional for INFRASTRUCTURE)
    service_mst_code: Optional[str] = Field(
        None,
        description="Service code - REQUIRED for KONG_ROUTE, optional for INFRASTRUCTURE"
    )

    # Optional tracking
    created_by: Optional[str] = Field(None, description="User email for tracking")

    # @model_validator(mode='after')
    # def validate_fields(self):
    #     """Validate fields based on infrastructuretype_ref_code"""
    #
    #     # Check if infrastructuretype_ref_code is 'kong_gateway' for Kong routes
    #     if self.infrastructuretype_ref_code in ('kong_gateway', 'kong_gateway_infrastructuretype_ref'):
    #         # Validate Kong route-specific fields
    #         if not self.service_mst_code:
    #             raise ValueError("service_mst_code is required for KONG_ROUTE")
    #
    #         # Validate type_specific_config has required Kong fields
    #         required_kong_fields = ["method", "route"]
    #         missing_fields = [f for f in required_kong_fields if f not in self.type_specific_config]
    #         if missing_fields:
    #             raise ValueError(f"type_specific_config missing required Kong fields: {', '.join(missing_fields)}")
    #
    #         # Validate HTTP method
    #         http_method = self.type_specific_config.get("method", "").upper()
    #         valid_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]
    #         if http_method not in valid_methods:
    #             raise ValueError(f"method must be one of: {', '.join(valid_methods)}")
    #         # Normalize to uppercase
    #         self.type_specific_config["method"] = http_method
    #
    #     return self

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "infrastructuretype_ref_code": "s3_infrastructuretype_ref",
                    "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
                    "environment": "dev",
                    "geo_loc_mst_code": "region-aspora-mumbai",
                    "type_specific_config": {
                        "identifier": "my-app-logs-bucket",
                        "region": "ap-south-1"
                    }
                },
                {
                    "infrastructuretype_ref_code": "kong_gateway",
                    "service_mst_code": "f7513b9e-c6ab-4a8e-a61b-b1afeb604829",
                    "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
                    "environment": "dev",
                    "geo_loc_mst_code": "region-aspora-mumbai",
                    "type_specific_config": {
                        "method": "GET",
                        "route": "~/api/v1/users$"
                    }
                },
                {
                    "infrastructuretype_ref_code": "kong_gateway",
                    "service_mst_code": "f7513b9e-c6ab-4a8e-a61b-b1afeb604829",
                    "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
                    "environment": "dev",
                    "geo_loc_mst_code": "region-aspora-mumbai",
                    "type_specific_config": {
                        "api_name": "custom-api-name",
                        "method": "POST",
                        "route": "~/api/v1/users$"
                    }
                },
                {
                    "code": "INFRA_S3_ABC123",
                    "infrastructuretype_ref_code": "s3_infrastructuretype_ref",
                    "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
                    "environment": "dev",
                    "geo_loc_mst_code": "region-aspora-mumbai",
                    "type_specific_config": {
                        "identifier": "my-updated-bucket",
                        "region": "ap-south-1"
                    }
                }
            ]
        }


class InfrastructureCreateResponse(BaseModel):
    """
    Unified infrastructure creation response.

    Returns table_name (as enum) and generated code.
    """

    table_name: WorkflowSourceTableEnum = Field(
        ...,
        description="Target table enum (INFRASTRUCTURE or KONG_ROUTE)"
    )
    code: str = Field(..., description="Generated resource code (INFRA_xxx or KRC_xxx)")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "examples": [
                {
                    "table_name": "INFRASTRUCTURE",
                    "code": "INFRA_S3_A1B2C3D4"
                },
                {
                    "table_name": "KONG_ROUTE",
                    "code": "KRC_DEF12345"
                }
            ]
        }
