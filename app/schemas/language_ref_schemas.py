"""
Language Reference Schemas

This module contains Pydantic schemas for language reference API endpoints.
These schemas are used for request validation and response serialization.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict
from datetime import datetime


class LanguageRefBase(BaseModel):
    """Base schema for language reference"""
    code: str = Field(..., description="Unique code identifier (e.g., 'PYTHON_3_12')")
    name: str = Field(..., description="Language name with version (e.g., 'Python 3.12')")
    version: str = Field(..., description="Language version (e.g., '3.12', '20.x')")
    yaml_templates: Dict[str, str] = Field(
        ...,
        description="YAML templates for different CI/CD platforms. "
                    "Keys: platform names (github_actions, gitlab_ci, etc.), "
                    "Values: template URLs"
    )

    @field_validator('yaml_templates')
    @classmethod
    def validate_yaml_templates_not_empty(cls, v):
        """Ensure yaml_templates is not empty"""
        if not v or len(v) == 0:
            raise ValueError("yaml_templates must contain at least one platform")
        return v


class LanguageRefResponse(LanguageRefBase):
    """Response schema for language reference"""
    id: int = Field(..., description="Database ID")
    description: Optional[str] = Field(None, description="Optional description")
    created_at: datetime = Field(..., description="Creation timestamp")
    is_active: bool = Field(..., description="Active status")
    is_deleted: bool = Field(..., description="Soft delete flag")

    class Config:
        from_attributes = True


class LanguageVersionsResponse(BaseModel):
    """Response schema for getting all language versions"""
    total: int = Field(..., description="Total number of language versions")
    languages: List[LanguageRefResponse] = Field(..., description="List of language versions")

    class Config:
        from_attributes = True


class LanguageRefGroupedResponse(BaseModel):
    """Grouped language response by language name"""
    language_name: str = Field(..., description="Base language name (e.g., 'Python', 'Node.js')")
    versions: List[LanguageRefResponse] = Field(..., description="Available versions for this language")


class LanguageVersionsGroupedResponse(BaseModel):
    """Response schema for grouped language versions"""
    total_languages: int = Field(..., description="Total number of distinct languages")
    total_versions: int = Field(..., description="Total number of language versions")
    languages: List[LanguageRefGroupedResponse] = Field(..., description="Languages grouped by name")

    class Config:
        from_attributes = True


class LanguageRefFilterRequest(BaseModel):
    """Request schema for filtering language versions"""
    language_name: Optional[str] = Field(None, description="Filter by language name (e.g., 'Python')")
    is_active: Optional[bool] = Field(True, description="Filter by active status")
    platform: Optional[str] = Field(
        None,
        description="Filter by supported platform (e.g., 'github_actions'). "
                    "Returns only languages that support this platform."
    )

    class Config:
        from_attributes = True
