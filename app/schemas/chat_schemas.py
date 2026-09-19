from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from app.core.enum import EnvironmentEnum, InfraVendorEnum


class ChatMessageSchema(BaseModel):
    """Schema for individual chat messages."""
    role: str  # "user" or "agent"
    message: str
    created_at: Optional[datetime] = None


class ChatContextSchema(BaseModel):
    """
    Schema for chat context from Infrastructure Studio UI dropdowns.

    Context includes: geo_loc + case_type + product + env + vendor + service + user + tenant

    Note: tenants_mst_code and user_mst_code are now automatically
    extracted from JWT authentication and should NOT be sent from frontend.
    """
    infra_vendor_enum: InfraVendorEnum
    applications_mst_code: Optional[str] = None
    resource_group_mst_code: Optional[str] = None
    services_mst_code: Optional[str] = None
    environment_enum: Optional[EnvironmentEnum] = None
    geo_loc_mst_code: Optional[str] = None
    case_type_ref_code: Optional[str] = None

    # These fields are populated by the backend from JWT, not sent by frontend
    tenants_mst_code: Optional[str] = None
    user_mst_code: Optional[str] = None


class ChatRequestSchema(BaseModel):
    """Schema for incoming chat request."""
    message: str = Field(..., min_length=1, description="User message")
    context: ChatContextSchema
    case_type_code: str = Field(..., min_length=1, description="Case type code from case_type_ref table (required)")
    case_code: str = Field(..., min_length=1, description="Case code from case_ref table (required)")


class ChatResponseSchema(BaseModel):
    """Schema for chat response."""
    chat_info_code: str
    response: str
    terraform_code: Optional[str] = None  # Extracted Terraform code if any
    service_type: Optional[str] = None  # "s3", "sqs", or "gateway"
    is_ready: bool = False  # Whether configuration is ready to generate
    parameters: Optional[Dict[str, Any]] = None  # Service-specific extracted parameters
    created_at: datetime


class ChatHistorySchema(BaseModel):
    """Schema for chat history response."""
    chat_info_code: str
    messages: List[ChatMessageSchema]
    summary: Optional[str] = None
    context: ChatContextSchema


class GetChatByContextRequest(BaseModel):
    """
    Request schema for fetching chat messages by context combination.

    Context includes: geo_loc + case_type + product + env + vendor + service + user + tenant

    Note: tenants_mst_code and user_mst_code are NOT included in request -
    they are automatically extracted from JWT authentication and injected by backend.
    """
    infra_vendor_enum: InfraVendorEnum = Field(..., description="Infrastructure vendor (required)")
    applications_mst_code: Optional[str] = Field(None, description="Application code (optional)")
    resource_group_mst_code: Optional[str] = Field(None, description="Resource group code (optional)")
    services_mst_code: Optional[str] = Field(None, description="Service code (optional)")
    environment_enum: Optional[EnvironmentEnum] = Field(None, description="Environment (optional)")
    geo_loc_mst_code: Optional[str] = Field(None, description="Geographic location code (optional)")
    case_type_ref_code: Optional[str] = Field(None, description="Case type reference code (optional)")


class ChatMessageDetailSchema(BaseModel):
    """Detailed schema for individual chat message including metadata."""
    id: int
    code: str
    role: str
    message: str
    summary_status: bool
    created_at: datetime

    class Config:
        from_attributes = True


class GetChatByContextResponse(BaseModel):
    """Response schema for fetching chat messages by context."""
    chat_info_code: Optional[str] = Field(None, description="Chat session code (null if no chat exists)")
    chat_exists: bool = Field(..., description="Whether a chat session exists for this context")
    total_messages: int = Field(default=0, description="Total number of messages in the chat")
    messages: List[ChatMessageDetailSchema] = Field(default=[], description="List of chat messages")
    context: ChatContextSchema = Field(..., description="The context used for filtering")


# =========================================================================
# Chat History Schemas (for sidebar display)
# =========================================================================

class ChatHistoryItemSchema(BaseModel):
    """
    Schema for individual chat history item in sidebar.

    Display format: {case_type_name} - {application_name} ({env}, {geo_loc_name}) - {service_name}
    Example: "Kong Gateway - core (dev, Mumbai)" or "S3 - core (staging, London) - payment-service"
    """
    chat_info_code: str = Field(..., description="Unique chat session code")
    name: str = Field(..., description="Display name for the chat session")
    geo_loc_mst_code: Optional[str] = Field(None, description="Geographic location code")
    geo_loc_name: Optional[str] = Field(None, description="Geographic location name (joined)")
    case_type_ref_code: Optional[str] = Field(None, description="Case type reference code")
    case_type_name: Optional[str] = Field(None, description="Case type name (joined)")
    applications_mst_code: Optional[str] = Field(None, description="Application/product code")
    application_name: Optional[str] = Field(None, description="Application name (joined)")
    environment_enum: Optional[EnvironmentEnum] = Field(None, description="Environment")
    infra_vendor_enum: InfraVendorEnum = Field(..., description="Infrastructure vendor")
    services_mst_code: Optional[str] = Field(None, description="Service code")
    service_name: Optional[str] = Field(None, description="Service name (joined)")
    resource_group_mst_code: Optional[str] = Field(None, description="Resource group code")
    last_message_at: Optional[datetime] = Field(None, description="Timestamp of last message")
    message_count: int = Field(default=0, description="Total number of messages in the chat")

    class Config:
        from_attributes = True


class ChatHistoryListResponse(BaseModel):
    """Response schema for chat history list endpoint."""
    chats: List[ChatHistoryItemSchema] = Field(default=[], description="List of chat history items")
    total: int = Field(default=0, description="Total number of chat sessions")