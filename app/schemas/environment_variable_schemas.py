"""
Environment Variable Management Schemas

This module contains Pydantic schemas for environment variable management API endpoints.
These schemas are used for request validation and response serialization.
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum


class VariableType(str, Enum):
    """Variable type enumeration"""
    SECRET = "secret"
    VARIABLE = "variable"


class GetAllVariablesRequest(BaseModel):
    """Request schema for getting all environment variables"""
    skip: int = Field(default=0, ge=0, description="Number of records to skip")
    limit: int = Field(default=100, ge=1, le=500, description="Maximum number of records to return")
    type: Optional[VariableType] = Field(None, description="Filter by variable type (secret or variable)")


class VariableListItem(BaseModel):
    """Individual environment variable item"""
    id: str = Field(..., description="Variable ID")
    key: str = Field(..., description="Variable key/name")
    value: str = Field(..., description="Variable value (masked for secrets)")
    type: str = Field(..., description="Variable type (secret or variable)")
    last_updated: str = Field(..., description="Last updated date (YYYY-MM-DD)")


class GetAllVariablesResponse(BaseModel):
    """Response schema for getting all environment variables"""
    total: int = Field(..., description="Total number of variables")
    skip: int = Field(..., description="Number of records skipped")
    limit: int = Field(..., description="Maximum number of records returned")
    variables: List[VariableListItem] = Field(default=[], description="List of environment variables")
