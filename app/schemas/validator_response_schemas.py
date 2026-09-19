"""
Validator Response Schemas

Shared schemas for validation layer responses.
"""
from pydantic import BaseModel, Field


class ValidatorResponse(BaseModel):
    """Standard response for validator results."""

    error_code: str = Field(..., description="Machine-readable error code")
    error_message: str = Field(..., description="Human-readable error message")
    validation_status: bool = Field(..., description="True if validation passed")
    component_name: str = Field(..., description="Validator component name")


class ValidationResult(BaseModel):
    """
    Standard response format for any validation check.

    This is the canonical shape every validator (duplicate checks, name format
    checks, quota checks, etc.) MUST return so the frontend can render the
    result uniformly.

    Example:
        {
            "type": "duplicateValidation",
            "description": "Table 'users' already exists for product 'myapp' in dev (mumbai).",
            "valid": false
        }

    Fields:
        type: Machine-readable validation category (camelCase).
              Examples: "duplicateValidation", "nameFormatValidation", "quotaValidation".
        description: Human-readable message describing the result. Should include
              the offending value and enough context for the user to act on it.
        valid: True if the input passed validation, False if it failed.
    """

    type: str = Field(..., description="Validation category, e.g. 'duplicateValidation'")
    description: str = Field(..., description="Human-readable result message")
    valid: bool = Field(..., description="True if validation passed, False otherwise")
