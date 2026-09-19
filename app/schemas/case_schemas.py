from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime


class CaseTypeRefItem(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    vendor_provider: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_deleted: Optional[bool] = None
    is_active: Optional[bool] = None

    class Config:
        from_attributes = True


class CaseRefItem(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    case_type_ref_code: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_deleted: Optional[bool] = None
    is_active: Optional[bool] = None

    class Config:
        from_attributes = True


class SearchCaseRefsRequest(BaseModel):
    """Request schema for searching case references"""
    search_query: str = Field("", description="Space-separated search terms (empty returns all)")


class CaseRefWithTypeItem(BaseModel):
    """Case ref with joined case_type_ref data"""
    id: int
    code: str
    name: str
    case_type_ref_code: Optional[str] = None
    case_type_name: Optional[str] = None

    class Config:
        from_attributes = True