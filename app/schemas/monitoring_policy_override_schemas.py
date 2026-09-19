from typing import Optional
from pydantic import BaseModel, Field, field_validator
from decimal import Decimal
from app.core.enum import ComparatorEnum, SeverityEnum, ThresholdUnitEnum


class CreateMonitoringPolicyOverrideRequest(BaseModel):
    """Request schema for creating a monitoring policy override"""

    name: str = Field(
        ...,
        description="Human-readable name for the override",
        min_length=1,
        max_length=255
    )

    description: Optional[str] = Field(
        default=None,
        description="Optional description of the override purpose",
        max_length=500
    )

    # UI sends infrastructure type and alert type - backend finds the default policy
    infrastructuretype_ref_code: str = Field(
        ...,
        description="Infrastructure type (e.g., ec2, rds, alb) - used to find the default policy",
        min_length=1,
        max_length=100
    )

    alerttype_ref_code: str = Field(
        ...,
        description="Alert type (e.g., cpu_util, memory_usage) - used to find the default policy",
        min_length=1,
        max_length=100
    )

    # Scoping fields - application and resource group optional
    # Note: tenants_mst_code is NOT included in request - it's automatically extracted
    # from JWT authentication and passed separately to the service layer.

    applications_mst_code: Optional[str] = Field(
        default=None,
        description="Application scope",
        max_length=100
    )

    resource_group_mst_code: Optional[str] = Field(
        default=None,
        description="Resource group scope (most specific)",
        max_length=100
    )

    # Alert configuration fields (from AlertBaseConfig)
    comparator: ComparatorEnum = Field(
        ...,
        description="Comparison operator (gt, lt, eq, gte, lte)"
    )

    threshold_value: Decimal = Field(
        ...,
        description="Threshold value for the alert",
        ge=0,
        le=10000
    )

    threshold_unit: ThresholdUnitEnum = Field(
        ...,
        description="Unit of measurement for threshold"
    )

    eval_window: int = Field(
        ...,
        description="Lookback window in minutes",
        gt=0
    )

    for_duration: int = Field(
        ...,
        description="Duration threshold must be breached in minutes",
        gt=0
    )

    no_data: bool = Field(
        default=True,
        description="Whether to alert on no data"
    )

    severity: SeverityEnum = Field(
        ...,
        description="Alert severity level (P0-P4)"
    )

    is_active: bool = Field(
        default=True,
        description="Whether this override is active"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name cannot be empty or whitespace")
        return v.strip()

    @field_validator("infrastructuretype_ref_code", "alerttype_ref_code")
    @classmethod
    def validate_required_codes(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()

    @field_validator(
        "applications_mst_code",
        "resource_group_mst_code"
    )
    @classmethod
    def validate_optional_codes(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Code cannot be empty string, use None instead")
            return v
        return None

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Strict CPU Policy",
                "description": "Lower CPU threshold for high-availability services",
                "infrastructuretype_ref_code": "ec2",
                "alerttype_ref_code": "cpu_util",
                "applications_mst_code": None,
                "resource_group_mst_code": None,
                "comparator": "gt",
                "threshold_value": 70.0,
                "threshold_unit": "percent",
                "eval_window": 5,
                "for_duration": 10,
                "no_data": True,
                "severity": "P1",
                "is_active": True
            }
        }


class MonitoringPolicyOverrideResponse(BaseModel):
    """Response schema for a monitoring policy override"""

    code: str = Field(..., description="Unique override code")
    name: str = Field(..., description="Override name")
    description: Optional[str] = Field(default=None, description="Override description")

    # Base policy reference
    monitoring_policy_defaults_ref_code: Optional[str] = Field(
        default=None,
        description="Code of the base default policy being overridden. NULL for standalone overrides without default policy."
    )

    # Scope information
    tenants_mst_code: Optional[str] = Field(default=None, description="Tenant scope")
    applications_mst_code: Optional[str] = Field(default=None, description="Application scope")
    resource_group_mst_code: Optional[str] = Field(default=None, description="Resource group scope")
    infrastructuretype_ref_code: str = Field(..., description="Infrastructure type")
    alerttype_ref_code: str = Field(..., description="Alert type")

    # Alert configuration
    comparator: ComparatorEnum
    threshold_value: Decimal
    threshold_unit: ThresholdUnitEnum
    eval_window: int
    for_duration: int
    no_data: bool
    severity: SeverityEnum
    is_active: bool

    # Metadata
    specificity_score: int = Field(..., description="Override specificity score")
    override_source: str = Field(..., description="Description of override scope")


class CreateMonitoringPolicyOverrideResponse(BaseModel):
    """Response schema for creating a monitoring policy override"""

    status: str = Field(default="success", description="Response status")
    message: str = Field(..., description="Human-readable message")
    override_code: str = Field(..., description="Unique code of the created override")
    override: MonitoringPolicyOverrideResponse = Field(
        ...,
        description="Full details of the created override"
    )


class UpdateMonitoringPolicyOverrideRequest(BaseModel):
    """
    Request schema for updating a monitoring policy override.

    All fields are optional to support partial updates.
    Only provided fields will be updated.

    IMMUTABLE FIELDS (cannot be updated):
    - code (unique identifier)
    - monitoring_policy_defaults_ref_code (base policy reference)
    - infrastructuretype_ref_code (infrastructure type)
    - alerttype_ref_code (alert type)
    - tenants_mst_code (tenant scope)
    - applications_mst_code (application scope)
    - resource_group_mst_code (resource group scope)

    Rationale: These fields define the override's scope and identity.
    Changing them would create a different override. User should delete and create new.
    """

    name: Optional[str] = Field(
        default=None,
        description="Human-readable name for the override",
        min_length=1,
        max_length=255
    )

    description: Optional[str] = Field(
        default=None,
        description="Optional description of the override purpose",
        max_length=500
    )

    # Alert configuration fields (from AlertBaseConfig)
    comparator: Optional[ComparatorEnum] = Field(
        default=None,
        description="Comparison operator (gt, lt, eq, gte, lte)"
    )

    threshold_value: Optional[Decimal] = Field(
        default=None,
        description="Threshold value for the alert",
        ge=0,
        le=10000
    )

    threshold_unit: Optional[ThresholdUnitEnum] = Field(
        default=None,
        description="Unit of measurement for threshold"
    )

    eval_window: Optional[int] = Field(
        default=None,
        description="Lookback window in minutes",
        gt=0
    )

    for_duration: Optional[int] = Field(
        default=None,
        description="Duration threshold must be breached in minutes",
        gt=0
    )

    no_data: Optional[bool] = Field(
        default=None,
        description="Whether to alert on no data"
    )

    severity: Optional[SeverityEnum] = Field(
        default=None,
        description="Alert severity level (P0-P4)"
    )

    is_active: Optional[bool] = Field(
        default=None,
        description="Whether this override is active"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            if not v.strip():
                raise ValueError("name cannot be empty or whitespace")
            return v.strip()
        return None

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Updated Strict CPU Policy",
                "threshold_value": 65.0,
                "eval_window": 10,
                "severity": "P2"
            }
        }


class UpdateMonitoringPolicyOverrideResponse(BaseModel):
    """Response schema for updating a monitoring policy override"""

    status: str = Field(default="success", description="Response status")
    message: str = Field(..., description="Human-readable message")
    override: MonitoringPolicyOverrideResponse = Field(
        ...,
        description="Full details of the updated override"
    )
