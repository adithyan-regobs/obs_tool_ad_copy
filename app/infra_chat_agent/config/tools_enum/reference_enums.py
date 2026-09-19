"""
Enum definitions for REFERENCE tool parameters.

These enums define allowed values for tool parameters like environment
and geo_loc_code. Used in:
1. Tool definitions (mcp_tool_provider.py) - for LLM tool binding
2. Response formatting - to provide options to frontend

Single source of truth for environment values is ResourceMetaRepo.
"""
from enum import Enum
from typing import List

from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo


class Environment(str, Enum):
    """Deployment environment options."""
    QA = "qa"
    STAGE = "stage"
    PROD = "prod"


class GeoLocCode(str, Enum):
    """Geographic location code options."""
    MUMBAI = "region-aspora-mumbai"
    LONDON = "region-aspora-london"


# Export as lists for easy use in tool schemas — derived from ResourceMetaRepo
ENVIRONMENT_VALUES: List[str] = resource_meta_repo.get_all_environment_values()
GEO_LOC_CODE_VALUES: List[str] = [g.value for g in GeoLocCode]

# Mapping from variant environment names to canonical form
ENVIRONMENT_MAPPING = {
    "qa": "qa",
    "stage": "stage",
    "prod": "prod",
    "production": "prod",
    "dev": "qa",         # no dev in infra, maps to qa
    "development": "qa",
}


def normalize_environment(value: str) -> str:
    """Normalize environment aliases to canonical form. e.g. 'staging' → 'stage'"""
    if not value:
        return value
    return ENVIRONMENT_MAPPING.get(value.lower().strip(), value)


# Mapping from short/variant geo_loc_code names to canonical full form
GEO_LOC_CODE_MAPPING = {
    "mumbai": "region-aspora-mumbai",
    "london": "region-aspora-london",
    "region-aspora-mumbai": "region-aspora-mumbai",
    "region-aspora-london": "region-aspora-london",
}


def normalize_geo_loc_code(value: str) -> str:
    """Normalize short geo_loc_code to full form. e.g. 'mumbai' → 'region-aspora-mumbai'"""
    if not value:
        return value
    return GEO_LOC_CODE_MAPPING.get(value.lower().strip(), value)


def get_enum_options(enum_name: str) -> List[str]:
    """Get allowed values for an enum by name."""
    enums = {
        "environment": ENVIRONMENT_VALUES,
        "geo_loc_code": GEO_LOC_CODE_VALUES,
    }
    return enums.get(enum_name, [])
