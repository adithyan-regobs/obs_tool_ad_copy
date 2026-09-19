"""
Dropdown Option Schemas

Standard `{label, value}` shape used by all "fetch options for a dropdown"
style endpoints (e.g. list services, list servers, list buckets) so the
frontend can render them uniformly.
"""
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class DropdownOption(BaseModel):
    """Standard option shape for dropdowns / select inputs."""

    label: str = Field(..., description="Human-readable display text")
    value: str = Field(..., description="Machine identifier (code/id)")
    extra_data: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Optional bag of additional fields the frontend may need alongside "
            "the option (e.g. environment, geo_loc, status). Keep keys camelCase "
            "or snake_case consistently per endpoint."
        ),
    )
