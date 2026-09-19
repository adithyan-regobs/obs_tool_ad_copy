"""
Pydantic schemas for ticket management
"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class GenerateTicketNumberRequest(BaseModel):
    """Request schema for generating new ticket number"""
    tenants_mst_code: Optional[str] = Field(None, description="Tenant code (auto-extracted from JWT)")
    user_mst_code: Optional[str] = Field(None, description="User code (auto-extracted from JWT)")
    name: str = Field(..., description="Ticket title/name")
    description: Optional[str] = Field(None, description="Ticket description")


class GenerateTicketNumberResponse(BaseModel):
    """Response schema for generated ticket"""
    success: bool
    message: str
    ticket_number: str
    ticket_code: str
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class TicketResponse(BaseModel):
    """Complete ticket information"""
    id: int
    code: str
    name: str
    description: Optional[str]
    ticket_number: str
    tenants_mst_code: str
    user_mst_code: str
    is_active: bool
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TicketHistoryResponse(BaseModel):
    """Response model for ticket history endpoint"""
    tickets: List[TicketResponse]
    total: int
