"""
Namespace Master Schemas

Pydantic schemas for namespace_mst API endpoints.
"""
from pydantic import BaseModel, Field
from typing import List, Optional


class NamespaceMstResponse(BaseModel):
    """Single namespace response"""
    code: str
    name: str
    infrastructure_mst_code: str
    namespace: str
    description: Optional[str] = None

    class Config:
        from_attributes = True


class NamespaceMstListResponse(BaseModel):
    """List of namespaces response"""
    total: int
    namespaces: List[NamespaceMstResponse]


class NamespaceMstCreate(BaseModel):
    """Schema for creating a new namespace"""
    infrastructure_mst_code: str = Field(
        ...,
        description="Infrastructure code (EKS cluster)"
    )
    namespace: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Kubernetes namespace name (e.g., default, production)"
    )
    description: Optional[str] = Field(
        None,
        max_length=500,
        description="Optional description"
    )


class NamespaceDropdownItem(BaseModel):
    """Namespace item for dropdown selection"""
    code: str
    namespace: str

    class Config:
        from_attributes = True


class NamespaceDropdownResponse(BaseModel):
    """Namespace dropdown response"""
    namespaces: List[NamespaceDropdownItem]
