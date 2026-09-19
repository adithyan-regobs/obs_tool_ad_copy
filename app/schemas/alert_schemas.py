from pyclbr import Class
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, ValidationError
from app.core.enum import ComparatorEnum, SeverityEnum, StatusEnum, ThresholdUnitEnum, DeploymentStatusEnum

class CreateAlert(BaseModel):
    
    services_code: str #Create alert against this Service
    monitoring_policy_code: Optional[str] = None  # Optional monitoring policy reference
    comparator: ComparatorEnum  # e.g. gt, lt
    threshold_value: int = Field(...,ge=0,le=10000)  # e.g. 0, 0.5, 60, 100 (>= 0 for event-based alerts)
    threshold_unit: ThresholdUnitEnum  # e.g. percent/count/ms
    eval_window: int = Field(..., gt=0) # lookback window
    for_duration: int = Field(..., gt=0)   # must persist this long
    severity: SeverityEnum  # (severity IN ('P1','P2','P3','P4'))
    status: StatusEnum
    no_data: bool = True

    @field_validator("services_code")
    @classmethod
    def validate_services_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("services_code cannot be empty or whitespace")
        # TODO: Add database check to verify service exists
        return v.strip()

class EnableAlertsForService(BaseModel):
    """Request schema for enabling all alerts for a service"""
    services_code: str

    @field_validator("services_code")
    @classmethod
    def validate_services_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("services_code cannot be empty or whitespace")
        return v.strip()


class BulkCreateOrUpdateAlerts(BaseModel):
    """Request schema for bulk create or update alerts"""
    alerts: List[CreateAlert] = Field(..., min_length=1, max_length=100)

    @field_validator("alerts")
    @classmethod
    def validate_alerts_list(cls, v: List[CreateAlert]) -> List[CreateAlert]:
        if not v:
            raise ValueError("alerts list cannot be empty")
        if len(v) > 100:
            raise ValueError("alerts list cannot exceed 100 items per request")
        return v


class GetAlertsForService(BaseModel):
    """Request schema for getting all alerts for a service"""
    services_code: str
    infrastructuretype_ref_code: str  # Required infrastructure type filter

    @field_validator("services_code")
    @classmethod
    def validate_services_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("services_code cannot be empty or whitespace")
        return v.strip()

    @field_validator("infrastructuretype_ref_code")
    @classmethod
    def validate_infrastructuretype_ref_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("infrastructuretype_ref_code cannot be empty or whitespace")
        return v.strip()


class AlertInfo(BaseModel):
    """Response schema for individual alert information"""
    # Configuration status
    is_configured: bool

    # Alert Config data (if configured)
    alert_id: Optional[int] = None
    alert_code: Optional[str] = None
    vendor_status: Optional[str] = None
    vendor_monitor_id: Optional[str] = None

    # Policy metadata
    monitoring_policy_code: str
    monitoring_policy_name: str
    infrastructuretype_ref_code: str
    alerttype_ref_code: str

    # Alert configuration values
    comparator: str
    threshold_value: int
    threshold_unit: str
    eval_window: int
    for_duration: int
    no_data: bool
    severity: str

    # Source of values
    policy_source: str  # "configured", "Override: RG+Tenant", "Default Policy", etc.


# ============================================================================
# Enable Alerts Response Schemas
# ============================================================================

class EnableAlertsResponse(BaseModel):
    """Simplified response schema for enable-alerts-for-service endpoint"""
    status: str = Field(
        ...,
        description="Overall operation status: 'success' if all succeeded, 'partial' if some failed"
    )
    message: str = Field(
        ...,
        description="Human-readable message describing the operation result"
    )
    alerts_created: int = Field(
        ...,
        description="Number of alerts successfully created"
    )
    alerts_failed: int = Field(
        ...,
        description="Number of alerts that failed to create"
    )


class CreateOrUpdateAlertResponse(BaseModel):
    """Simplified response schema for create-or-update-alert endpoint"""
    status: str = Field(
        ...,
        description="Operation status: 'success' or 'partial' (if vendor sync failed)"
    )
    message: str = Field(
        ...,
        description="Human-readable message describing the operation result"
    )
    operation: str = Field(
        ...,
        description="Operation performed: 'created' or 'updated'"
    )
    alert_code: str = Field(
        ...,
        description="Unique alert code"
    )
    # GitOps workflow tracking fields
    creation_status: Optional[str] = Field(
        None,
        description="Deployment workflow status (e.g., INITIATED, PR_CREATED, ACTIVE)"
    )
    pr_number: Optional[int] = Field(
        None,
        description="GitHub pull request number"
    )
    pr_url: Optional[str] = Field(
        None,
        description="Direct URL to pull request"
    )
    workflow_id: Optional[int] = Field(
        None,
        description="GitOps workflow detail ID"
    )


class BulkCreateOrUpdateAlertsResponse(BaseModel):
    """Simplified response schema for bulk-create-or-update-alerts endpoint"""
    status: str = Field(
        ...,
        description="Overall operation status: 'success' if all succeeded, 'partial' if some failed"
    )
    message: str = Field(
        ...,
        description="Human-readable message describing the operation result"
    )
    alerts_created: int = Field(
        ...,
        description="Number of alerts successfully created"
    )
    alerts_updated: int = Field(
        ...,
        description="Number of alerts successfully updated"
    )
    alerts_failed: int = Field(
        ...,
        description="Number of alerts that failed to create/update"
    )
    pr_number: Optional[int] = Field(
        None,
        description="Pull request number (when Terragrunt PR workflow is used)"
    )
    pr_url: Optional[str] = Field(
        None,
        description="Pull request URL (when Terragrunt PR workflow is used)"
    )
    workflow_id: Optional[int] = Field(
        None,
        description="GitOps workflow detail ID (shared by all alerts in bulk operation)"
    )
    creation_status: Optional[str] = Field(
        None,
        description="Deployment workflow status (e.g., INITIATED, PR_CREATED, ACTIVE)"
    )
