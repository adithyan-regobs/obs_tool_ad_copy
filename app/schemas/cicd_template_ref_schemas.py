"""
Pydantic schemas for CI/CD Template Reference operations.
"""
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class WorkflowStepRef(BaseModel):
    """Single workflow step configuration"""
    id: str
    name: str
    order: int
    mandatory: bool
    enabled: bool
    category: str
    description: str
    dependsOn: Optional[List[str]] = None


class CicdTemplateRefConfig(BaseModel):
    """CI/CD Template Reference configuration"""
    steps: List[WorkflowStepRef]


class CicdTemplateRefBase(BaseModel):
    """Base CI/CD Template Reference fields"""
    name: str
    config: Optional[CicdTemplateRefConfig] = None
    description: Optional[str] = None
    is_active: bool = True
    applications_mst_code: Optional[str] = None
    tenant_mst_code: Optional[str] = None


class CicdTemplateRefCreate(CicdTemplateRefBase):
    """Schema for creating a new CI/CD template reference"""
    pass


class CicdTemplateRefUpdate(BaseModel):
    """Schema for updating a CI/CD template reference"""
    name: Optional[str] = None
    config: Optional[CicdTemplateRefConfig] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    applications_mst_code: Optional[str] = None


class CicdTemplateRefResponse(CicdTemplateRefBase):
    """Schema for CI/CD Template Reference response"""
    id: int
    code: str
    tenant_mst_code: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CicdTemplateRefListResponse(BaseModel):
    """Schema for list of CI/CD template references"""
    templates: List[CicdTemplateRefResponse]
    total: int
