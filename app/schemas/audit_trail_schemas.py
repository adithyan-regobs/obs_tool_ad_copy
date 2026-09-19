"""
Audit Trail API Schemas

Pydantic schemas for audit trail API endpoints.
"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field

from app.core.enum import AuditActionEnum


class AuditActorResponse(BaseModel):
    """Actor information for audit events"""
    actor_id: int
    username: Optional[str] = None
    role: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    class Config:
        from_attributes = True


class AuditFieldChangeResponse(BaseModel):
    """Individual field change information"""
    id: int
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None

    class Config:
        from_attributes = True


class AuditEventResponse(BaseModel):
    """Audit event with actor and field changes"""
    event_id: int
    event_time: datetime
    event_name: AuditActionEnum
    event_source: Optional[str] = None
    resource_type: str
    resource_id: Optional[str] = None
    status: Optional[str] = None
    correlation_id: Optional[str] = None

    # Actor information (from join)
    username: Optional[str] = None
    role: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    # Field changes (from join)
    field_changes: List[AuditFieldChangeResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class AuditTrailListResponse(BaseModel):
    """Paginated list of audit events"""
    events: List[AuditEventResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class AuditEventDetailResponse(BaseModel):
    """Detailed view of a single audit event"""
    event_id: int
    event_time: datetime
    event_name: AuditActionEnum
    event_source: Optional[str] = None
    resource_type: str
    resource_id: Optional[str] = None
    status: Optional[str] = None
    correlation_id: Optional[str] = None

    # Full actor details
    actor: AuditActorResponse

    # All field changes
    field_changes: List[AuditFieldChangeResponse] = Field(default_factory=list)

    # Request/response payloads (optional, can be large)
    request_payload: Optional[dict] = None
    response_payload: Optional[dict] = None

    class Config:
        from_attributes = True
