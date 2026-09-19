"""
Service Config Chat Schemas

Pydantic schemas for service config assistance chat.
Separate from langchat schemas - different context (geo_loc + env + service).
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from app.core.enum import EnvironmentEnum


class ServiceConfigChatContextSchema(BaseModel):
    """
    Context for service config chat.

    Uses geo_loc + env + service as unique context hash.
    Different from langchat which uses vendor + app + resource_group + service + env.

    Note: tenants_mst_code and user_mst_code are automatically
    extracted from JWT authentication and injected by backend.
    """
    service_code: str = Field(..., min_length=1, description="Current service code being configured (required)")
    geo_loc_mst_code: str = Field(..., min_length=1, description="Geographic location code (required)")
    environment_enum: EnvironmentEnum = Field(..., description="Environment (required)")
    reference_service_name: Optional[str] = Field(None, description="Reference service name (set after selection)")

    # Infrastructure context (optional - for filtering stats and service lists)
    infra_vendor: Optional[str] = Field(None, description="Infrastructure vendor (aws, azure, gcp, on_prem)")
    infrastructure_type: Optional[str] = Field(None, description="Hosting type code (ECS, Lambda, EC2)")
    infrastructure_mst_code: Optional[str] = Field(None, description="Cluster/instance code")

    # Populated by backend from JWT
    tenants_mst_code: Optional[str] = None
    user_mst_code: Optional[str] = None


class ServiceConfigChatRequestSchema(BaseModel):
    """Request schema for service config chat."""
    message: str = Field(..., min_length=1, description="User message")
    context: ServiceConfigChatContextSchema
    case_code: str = Field(
        default="service_config_general",
        description="Case code for message filtering (defaults to service_config_general)"
    )


class ServiceMatchSchema(BaseModel):
    """Schema for fuzzy-matched service results."""
    service_code: str
    service_name: str
    service_type: str  # API or BACKGROUND_SERVICE
    similarity_score: float = Field(ge=0.0, le=1.0)
    has_existing_config: bool = Field(
        default=False,
        description="Whether config exists for this service in target env+geo_loc"
    )


class ReferenceConfigSchema(BaseModel):
    """Schema for reference configs from other environments."""
    environment: str
    geo_loc_mst_code: str
    config: Optional[Dict[str, Any]] = None
    language_ref_code: Optional[str] = None
    alb_selection: Optional[str] = None


class ValidationWarningSchema(BaseModel):
    """Schema for config validation warnings."""
    field: str
    message: str
    severity: str = Field(default="warning", description="warning or error")


class UpdationFieldSchema(BaseModel):
    """Schema for single field update when user asks about a parameter."""
    field: str = Field(..., description="Canonical parameter name")
    value: Any = Field(..., description="Recommended value (most common from stats)")


class ServiceConfigChatResponseSchema(BaseModel):
    """Response schema for service config chat."""
    chat_info_code: str
    response: str

    # AI-detected intent
    intent: Optional[str] = Field(
        None,
        description="Detected intent: list_services, select_service, provide_config, confirm, help"
    )

    # Reference service selection
    reference_service_code: Optional[str] = Field(None, description="Selected reference service code")
    reference_service_name: Optional[str] = Field(None, description="Selected reference service name")
    matched_services: Optional[List[ServiceMatchSchema]] = Field(
        None,
        description="Fuzzy-matched reference services when user mentions a service name"
    )

    # Reference configs from other envs
    reference_configs: Optional[List[ReferenceConfigSchema]] = Field(
        None,
        description="Existing configs from other envs as reference"
    )

    # Config building output
    config_json: Optional[Dict[str, Any]] = Field(
        None,
        description="Final config JSON for form autofill"
    )

    # Single field update (mutually exclusive with config_json)
    updation_field: Optional[UpdationFieldSchema] = Field(
        None,
        description="Single field update when user asks about a parameter (e.g., 'cpu?')"
    )

    is_ready: bool = Field(
        default=False,
        description="Whether config is ready for form submission"
    )
    validation_warnings: Optional[List[ValidationWarningSchema]] = Field(
        None,
        description="Config validation warnings"
    )

    created_at: datetime = Field(default_factory=datetime.now)


class GetServiceConfigChatByContextRequest(BaseModel):
    """
    Request schema for fetching service config chat messages by context.

    Note: tenants_mst_code and user_mst_code are extracted from JWT.
    """
    service_code: str = Field(..., description="Current service code being configured (required)")
    geo_loc_mst_code: str = Field(..., description="Geographic location code (required)")
    environment_enum: EnvironmentEnum = Field(..., description="Environment (required)")


class ServiceConfigChatMessageSchema(BaseModel):
    """Detailed schema for individual chat message."""
    id: int
    code: str
    role: str
    message: str
    created_at: datetime

    class Config:
        from_attributes = True


class GetServiceConfigChatByContextResponse(BaseModel):
    """Response schema for fetching service config chat messages by context."""
    chat_info_code: Optional[str] = Field(None, description="Chat session code (null if no chat exists)")
    chat_exists: bool = Field(..., description="Whether a chat session exists for this context")
    total_messages: int = Field(default=0, description="Total number of messages")
    messages: List[ServiceConfigChatMessageSchema] = Field(default=[], description="Chat messages")
    context: ServiceConfigChatContextSchema = Field(..., description="The context used for filtering")
