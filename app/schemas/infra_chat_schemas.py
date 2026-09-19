"""
Schemas for Infrastructure Chat Agent API.
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.core.enum import EnvironmentEnum, InfraVendorEnum


class InfraChatMessageSchema(BaseModel):
    """Schema for individual chat messages in history."""
    role: str  # "user" or "agent"
    message: str
    created_at: Optional[datetime] = None


class InfraChatContextSchema(BaseModel):
    """
    Schema for infra chat context from Infrastructure Studio UI dropdowns.

    Context includes: vendor + application + resource_group + service + environment + geo_loc + case_type

    Note: tenants_mst_code and user_mst_code are now automatically
    extracted from JWT authentication and should NOT be sent from frontend.
    """
    infra_vendor_enum: InfraVendorEnum = Field(..., description="Infrastructure vendor (required)")
    applications_mst_code: Optional[str] = Field(None, description="Application code (optional)")
    resource_group_mst_code: Optional[str] = Field(None, description="Resource group code (optional)")
    services_mst_code: Optional[str] = Field(None, description="Service code (optional)")
    environment_enum: Optional[EnvironmentEnum] = Field(None, description="Environment (optional)")
    geo_loc_mst_code: Optional[str] = Field(None, description="Geographic location code (optional)")
    case_type_ref_code: Optional[str] = Field(None, description="Case type reference code (optional)")

    # These fields are populated by the backend from JWT, not sent by frontend
    tenants_mst_code: Optional[str] = Field(None, description="Tenant code (auto-populated from JWT)")
    user_mst_code: Optional[str] = Field(None, description="User code (auto-populated from JWT)")


class InfraChatRequestSchema(BaseModel):
    """Schema for incoming infra chat request."""
    message: str = Field(..., min_length=1, description="User message")
    conversation_id: str = Field(..., min_length=1, description="Unique conversation identifier")

    # These fields are populated by the backend from JWT, not sent by frontend
    tenants_mst_code: Optional[str] = None
    user_mst_code: Optional[str] = None


class UpdatePlacementParamsRequestSchema(BaseModel):
    """Schema for updating placement parameters."""
    conversation_id: str = Field(..., min_length=1, description="Unique conversation identifier")
    placement_parameters: Dict[str, Any] = Field(..., description="Placement parameters to update")
    case_code: Optional[str] = Field(None, description="Optional case code for workflow context")

    # These fields are populated by the backend from JWT, not sent by frontend
    tenants_mst_code: Optional[str] = None
    user_mst_code: Optional[str] = None


class UpdatePlacementParamsResponseSchema(BaseModel):
    """Schema for placement parameter update response."""
    conversation_id: str
    status: str  # "success" or "error"
    collected_placement_parameters: Dict[str, Any]
    message: Optional[str] = None


class ServiceMatchSchema(BaseModel):
    """Schema for matched service results (compatible with service_config_chat)."""
    service_code: str
    service_name: str
    service_type: str  # API or BACKGROUND_SERVICE
    has_existing_config: bool = False


class InfraChatResponseSchema(BaseModel):
    """Schema for infra chat response."""
    conversation_id: str
    response: str
    intent: Optional[str] = None  # CREATE, REFERENCE, QA, UNSUPPORTED
    resource: Optional[str] = None  # Resource type for CREATE intent
    cases: Optional[List[str]] = None  # Cases/use-cases for this resource type (e.g., ["create_bucket"], ["create_queue"])
    attribute_parameters: Optional[Dict[str, Any]] = None  # Extracted attribute parameters
    placement_parameters: Optional[Dict[str, Any]] = None  # Placement parameters
    is_ready: bool = False  # True when all required parameters are collected and validated
    queue_status: Optional[str] = None  # Queue status for workflow (e.g., "draft", "approved")
    created_at: datetime

    # Structured parameter metadata for frontend rendering
    remaining_placement_parameters: Optional[Dict[str, Any]] = None  # Full placement param details with name, type, value_source, etc.
    remaining_attribute_parameters: Optional[Dict[str, Any]] = None  # Full attribute param details with name, type, value_source, etc.
    remaining_reference_parameters: Optional[Dict[str, Any]] = None  # For REFERENCE workflow - environment, geo_loc_code options

    # For REFERENCE list_services - service matches for clickable UI buttons
    matched_services: Optional[List[ServiceMatchSchema]] = None


class InfraChatHistorySchema(BaseModel):
    """Schema for conversation history response."""
    conversation_id: str
    messages: List[InfraChatMessageSchema]
    total_messages: int


class InfraChatStateSchema(BaseModel):
    """Schema for current conversation state."""
    conversation_id: str
    tenant_id: str
    user_id: str
    current_intent: Optional[str] = None
    current_resource: Optional[str] = None
    collected_parameters: Optional[Dict[str, Any]] = None
    remaining_parameters: Optional[Dict[str, Any]] = None


class ClearStateRequestSchema(BaseModel):
    """Schema for clearing conversation state (simulates resource creation completion)."""
    conversation_id: str = Field(..., min_length=1, description="Unique conversation identifier")

    # These fields are populated by the backend from JWT, not sent by frontend
    tenants_mst_code: Optional[str] = None
    user_mst_code: Optional[str] = None


class ClearStateResponseSchema(BaseModel):
    """Schema for clear state response."""
    conversation_id: str
    status: str  # "success" or "error"
    message: str
    cleared_items: List[str]  # List of cleared state items


class ChatHistoryRequestSchema(BaseModel):
    """Request schema for chat history retrieval."""
    conversation_id: str = Field(..., min_length=1, description="Conversation identifier")
    ticket_id: Optional[int] = Field(None, description="Optional ticket ID for history lookup")


class ChatHistoryMessageSchema(BaseModel):
    """Schema for individual chat history message."""
    id: str
    code: str
    role: str  # "user" or "agent"
    message: str
    intent: Optional[str] = None
    resource: Optional[str] = None
    placement_parameters: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ChatHistoryResponseSchema(BaseModel):
    """Response schema for chat history retrieval."""
    conversation_id: str
    thread_id: str
    total_messages: int
    messages: List[ChatHistoryMessageSchema] = Field(default_factory=list)
    placement_parameters: Optional[Dict[str, Any]] = None
