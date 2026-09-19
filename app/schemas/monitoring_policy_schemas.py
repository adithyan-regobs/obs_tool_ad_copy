from typing import Optional
from pydantic import BaseModel, Field, field_validator
from decimal import Decimal
from app.core.enum import ComparatorEnum, SeverityEnum, ThresholdUnitEnum


class GetAllAlertPoliciesRequest(BaseModel):
    """
    Request schema for getting all alert policies for a tenant with optional filters

    Note: tenant_code is NOT included in request - it's automatically extracted
    from JWT authentication and passed separately to the service layer.
    """

    application_code: Optional[str] = Field(
        default=None,
        description="Optional: Filter by application code",
        max_length=100
    )

    resource_group_code: Optional[str] = Field(
        default=None,
        description="Optional: Filter by resource group code",
        max_length=100
    )

    infrastructuretype_code: Optional[str] = Field(
        default=None,
        description="Optional: Filter by infrastructure type (e.g., ec2, rds, vm)",
        max_length=100
    )

    alerttype_code: Optional[str] = Field(
        default=None,
        description="Optional: Filter by alert type (e.g., cpu_util, memory_usage)",
        max_length=100
    )

    skip: int = Field(
        default=0,
        ge=0,
        description="Number of records to skip for pagination"
    )

    limit: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Maximum number of records to return"
    )

    @field_validator("application_code", "resource_group_code", "infrastructuretype_code", "alerttype_code")
    @classmethod
    def validate_optional_codes(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Code cannot be empty string, use None instead")
            return v
        return None


class AlertPolicyResponse(BaseModel):
    """Response schema for individual alert policy with merged default and override values"""

    # Policy identification
    policy_code: str = Field(
        ...,
        description="Code of the policy (override code if exists, otherwise default code)"
    )
    policy_name: str = Field(
        ...,
        description="Name of the policy (override name if exists, otherwise default name)"
    )
    description: Optional[str] = Field(
        default=None,
        description="Description of the policy (override description if exists, otherwise default description)"
    )

    # Reference to default policy
    monitoring_policy_defaults_ref_code: Optional[str] = Field(
        default=None,
        description="Code of the base default policy. NULL for standalone overrides without default policy."
    )

    # Scope information (nullable for defaults)
    tenants_mst_code: Optional[str] = Field(
        default=None,
        description="Tenant scope - NULL means applies to all tenants"
    )
    applications_mst_code: Optional[str] = Field(
        default=None,
        description="Application scope - NULL means applies to all applications"
    )
    resource_group_mst_code: Optional[str] = Field(
        default=None,
        description="Resource group scope - NULL means applies to all resource groups"
    )
    infrastructuretype_ref_code: Optional[str] = Field(
        default=None,
        description="Infrastructure type - NULL means applies to all infrastructure types"
    )
    alerttype_ref_code: str = Field(
        ...,
        description="Alert type code (e.g., cpu_util, memory_usage)"
    )

    # Alert configuration (merged values)
    comparator: ComparatorEnum = Field(
        ...,
        description="Comparison operator (gt, lt, eq, gte, lte)"
    )
    threshold_value: Decimal = Field(
        ...,
        description="Threshold value for the alert"
    )
    threshold_unit: ThresholdUnitEnum = Field(
        ...,
        description="Unit of measurement for threshold"
    )
    eval_window: int = Field(
        ...,
        description="Lookback window in minutes"
    )
    for_duration: int = Field(
        ...,
        description="Duration threshold must be breached in minutes"
    )
    no_data: bool = Field(
        ...,
        description="Whether to alert on no data"
    )
    severity: SeverityEnum = Field(
        ...,
        description="Alert severity level (P0-P4)"
    )

    # Metadata
    is_override: bool = Field(
        ...,
        description="True if this policy has overrides applied, False if using default values only"
    )
    override_source: Optional[str] = Field(
        default=None,
        description="Description of override scope if applicable (e.g., 'Resource Group + Tenant')"
    )
    is_active: bool = Field(
        ...,
        description="Whether this policy is active"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "policy_code": "acme_cpu_override_01",
                "policy_name": "Acme Corp Strict CPU Policy",
                "monitoring_policy_defaults_ref_code": "ec2_cpu_default",
                "tenants_mst_code": "acme_corp",
                "applications_mst_code": None,
                "resource_group_mst_code": None,
                "infrastructuretype_ref_code": None,
                "alerttype_ref_code": "cpu_util",
                "comparator": "gt",
                "threshold_value": 70,
                "threshold_unit": "percent",
                "eval_window": 5,
                "for_duration": 10,
                "no_data": True,
                "severity": "P1",
                "is_override": True,
                "override_source": "Tenant-wide override",
                "is_active": True
            }
        }


class GetAllAlertPoliciesResponse(BaseModel):
    """Response schema for get all alert policies endpoint"""

    status: str = Field(
        default="success",
        description="Response status"
    )
    message: str = Field(
        ...,
        description="Human-readable message"
    )
    total: int = Field(
        ...,
        description="Total number of policies matching filters"
    )
    skip: int = Field(
        ...,
        description="Number of records skipped"
    )
    limit: int = Field(
        ...,
        description="Maximum records returned"
    )
    policies: list[AlertPolicyResponse] = Field(
        ...,
        description="List of alert policies with merged values"
    )