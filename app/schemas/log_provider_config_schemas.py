"""Schemas for log provider configuration endpoints."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.core.enum import LogProviderEnum


class CreateLogProviderConfigRequest(BaseModel):
    """Request to create a log provider config for the tenant."""
    provider: LogProviderEnum = Field(..., description="CLOUDWATCH or DATADOG")
    auth_config: dict = Field(
        ...,
        description=(
            "Provider credentials. "
            "CloudWatch: {assume_role_arn, region, external_id, session_name}. "
            "Datadog: {api_key_secret_arn, app_key_secret_arn, site}"
        ),
    )
    is_default: bool = Field(False, description="Set as the tenant's default log provider")
    name: str = Field("", description="Display name for this config")
    description: str = Field("", description="Optional description")


class UpdateLogProviderConfigRequest(BaseModel):
    """Request to update a log provider config."""
    auth_config: Optional[dict] = None
    is_default: Optional[bool] = None
    name: Optional[str] = None
    description: Optional[str] = None


class LogProviderConfigResponse(BaseModel):
    """Response for a log provider config."""
    id: int
    code: str
    provider: LogProviderEnum
    auth_config: dict
    is_default: bool
    name: str
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
