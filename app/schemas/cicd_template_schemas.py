from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class WorkflowStepConfig(BaseModel):
    """Single workflow step configuration"""
    id: str
    name: str
    order: int
    mandatory: bool
    enabled: bool
    category: str
    description: Optional[str] = None


class WorkflowTriggerConfig(BaseModel):
    """Workflow trigger configuration"""
    push_branches: List[str] = ["main"]
    manual_trigger: bool = True
    pull_request: bool = False


class CicdTemplateConfig(BaseModel):
    """CI/CD Template configuration"""
    steps: Optional[List[WorkflowStepConfig]] = None
    workflow_triggers: Optional[WorkflowTriggerConfig] = None


class CicdTemplateBase(BaseModel):
    """Base CI/CD Template fields"""
    name: str
    config: Optional[CicdTemplateConfig] = None
    description: Optional[str] = None
    is_active: bool = True
    applications_mst_code: Optional[str] = None
    tenant_mst_code: Optional[str] = None


class CicdTemplateCreate(CicdTemplateBase):
    """Schema for creating a new CI/CD template"""
    pass


class CicdTemplateUpdate(BaseModel):
    """Schema for updating a CI/CD template"""
    name: Optional[str] = None
    config: Optional[CicdTemplateConfig] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    applications_mst_code: Optional[str] = None


class CicdTemplateResponse(CicdTemplateBase):
    """Schema for CI/CD Template response"""
    id: int
    code: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CicdTemplateListResponse(BaseModel):
    """Schema for list of CI/CD templates"""
    templates: List[CicdTemplateResponse]
    total: int


class WorkflowInstanceCreate(BaseModel):
    """Schema for creating a workflow instance from template"""
    template_id: int
    service_config_code: str
    name: str
    customizations: Optional[Dict[str, Any]] = None


class WorkflowInstanceResponse(BaseModel):
    """Schema for workflow instance response"""
    id: int
    code: str
    name: str
    service_config_code: str
    template_code: Optional[str] = None
    config: Dict[str, Any]
    generated_workflow_yaml: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
