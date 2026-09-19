from typing import List, Optional
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from app.core.enum import SignalKindEnum


class CreateDatadogAlertQuery(BaseModel):
    """Schema for creating a new Datadog alert query template"""
    code: str = Field(..., min_length=1, max_length=100, description="Unique query code")
    name: str = Field(..., min_length=1, max_length=255, description="Query template name")
    infrastructuretype_ref_code: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Infrastructure type code (e.g., 'ec2', 'rds', 'lambda')"
    )
    alerttype_ref_code: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Alert type code (e.g., 'cpu_util', 'http_4xx_rate')"
    )
    signal_kind: SignalKindEnum = Field(
        ...,
        description="Signal kind: metric, log, trace, or event"
    )
    query_template: str = Field(
        ...,
        min_length=1,
        description="Datadog query template with placeholders (e.g., {{window}}, {{service_name}})"
    )
    description: Optional[str] = Field(
        None,
        max_length=500,
        description="Optional description of the query template"
    )
    is_active: bool = Field(default=True, description="Whether the query template is active")

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        """Validate code is not empty and trimmed"""
        if not v or not v.strip():
            raise ValueError("code is required and cannot be empty")
        return v.strip()

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate name is not empty and trimmed"""
        if not v or not v.strip():
            raise ValueError("name is required and cannot be empty")
        return v.strip()

    @field_validator("infrastructuretype_ref_code")
    @classmethod
    def validate_infrastructuretype_ref_code(cls, v: str) -> str:
        """Validate infrastructure type code is not empty and trimmed"""
        if not v or not v.strip():
            raise ValueError("infrastructuretype_ref_code is required and cannot be empty")
        return v.strip()

    @field_validator("alerttype_ref_code")
    @classmethod
    def validate_alerttype_ref_code(cls, v: str) -> str:
        """Validate alert type code is not empty and trimmed"""
        if not v or not v.strip():
            raise ValueError("alerttype_ref_code is required and cannot be empty")
        return v.strip()

    @field_validator("query_template")
    @classmethod
    def validate_query_template(cls, v: str) -> str:
        """Validate query template is not empty and trimmed"""
        if not v or not v.strip():
            raise ValueError("query_template is required and cannot be empty")
        return v.strip()

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        """Validate description is trimmed if provided"""
        if v is not None and not v.strip():
            return None  # Convert empty strings to None
        return v.strip() if v else None


class UpdateDatadogAlertQuery(BaseModel):
    """Schema for updating a Datadog alert query template"""
    code: Optional[str] = Field(None, min_length=1, max_length=100, description="Unique query code")
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Query template name")
    infrastructuretype_ref_code: Optional[str] = Field(
        None,
        min_length=1,
        max_length=100,
        description="Infrastructure type code (e.g., 'ec2', 'rds', 'lambda')"
    )
    alerttype_ref_code: Optional[str] = Field(
        None,
        min_length=1,
        max_length=100,
        description="Alert type code (e.g., 'cpu_util', 'http_4xx_rate')"
    )
    signal_kind: Optional[SignalKindEnum] = Field(
        None,
        description="Signal kind: metric, log, trace, or event"
    )
    query_template: Optional[str] = Field(
        None,
        min_length=1,
        description="Datadog query template with placeholders"
    )
    description: Optional[str] = Field(None, max_length=500, description="Description of the query template")
    is_active: Optional[bool] = Field(None, description="Whether the query template is active")

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: Optional[str]) -> Optional[str]:
        """Validate code is not empty and trimmed"""
        if v is not None and not v.strip():
            raise ValueError("code cannot be empty string")
        return v.strip() if v else None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        """Validate name is not empty and trimmed"""
        if v is not None and not v.strip():
            raise ValueError("name cannot be empty string")
        return v.strip() if v else None

    @field_validator("infrastructuretype_ref_code")
    @classmethod
    def validate_infrastructuretype_ref_code(cls, v: Optional[str]) -> Optional[str]:
        """Validate infrastructure type code is not empty and trimmed"""
        if v is not None and not v.strip():
            raise ValueError("infrastructuretype_ref_code cannot be empty string")
        return v.strip() if v else None

    @field_validator("alerttype_ref_code")
    @classmethod
    def validate_alerttype_ref_code(cls, v: Optional[str]) -> Optional[str]:
        """Validate alert type code is not empty and trimmed"""
        if v is not None and not v.strip():
            raise ValueError("alerttype_ref_code cannot be empty string")
        return v.strip() if v else None

    @field_validator("query_template")
    @classmethod
    def validate_query_template(cls, v: Optional[str]) -> Optional[str]:
        """Validate query template is not empty and trimmed"""
        if v is not None and not v.strip():
            raise ValueError("query_template cannot be empty string")
        return v.strip() if v else None

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        """Validate description is trimmed if provided"""
        if v is not None and not v.strip():
            return None  # Convert empty strings to None
        return v.strip() if v else None

class GetAllDatadogAlertQueriesRequest(BaseModel):
    """Request schema for getting all Datadog alert query templates"""
    infrastructuretype_ref_code: Optional[str] = Field(
        None,
        description="Filter by infrastructure type code"
    )
    alerttype_ref_code: Optional[str] = Field(
        None,
        description="Filter by alert type code"
    )
    signal_kind: Optional[str] = Field(
        None,
        description="Filter by signal kind (metric, log, trace, event)"
    )
    is_active: Optional[bool] = None
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("infrastructuretype_ref_code")
    @classmethod
    def validate_infrastructuretype_ref_code(cls, v: Optional[str]) -> Optional[str]:
        """Validate infrastructure type code is trimmed"""
        if v is not None and not v.strip():
            raise ValueError("infrastructuretype_ref_code cannot be empty string")
        return v.strip() if v else None

    @field_validator("alerttype_ref_code")
    @classmethod
    def validate_alerttype_ref_code(cls, v: Optional[str]) -> Optional[str]:
        """Validate alert type code is trimmed"""
        if v is not None and not v.strip():
            raise ValueError("alerttype_ref_code cannot be empty string")
        return v.strip() if v else None

    @field_validator("signal_kind")
    @classmethod
    def validate_signal_kind(cls, v: Optional[str]) -> Optional[str]:
        """Validate signal kind is valid"""
        if v is not None:
            v = v.strip().lower()
            valid_values = ['metric', 'log', 'trace', 'event']
            if v not in valid_values:
                raise ValueError(f"signal_kind must be one of: {', '.join(valid_values)}")
        return v


class DatadogAlertQueryItem(BaseModel):
    """Individual Datadog alert query item for list response"""
    id: int
    code: str
    name: str
    description: Optional[str] = None
    infrastructuretype_ref_code: str
    alerttype_ref_code: str
    signal_kind: str
    query_template: str
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None


class GetAllDatadogAlertQueriesResponse(BaseModel):
    """Response for getting all Datadog alert queries"""
    total: int = Field(..., description="Total number of Datadog alert query templates")
    skip: int = Field(default=0, description="Pagination offset")
    limit: int = Field(default=100, description="Page size")
    queries: List[DatadogAlertQueryItem] = Field(default=[], description="List of query templates")
