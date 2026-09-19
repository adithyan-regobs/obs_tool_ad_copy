"""
Recommendation Defaults Registry.

Single responsibility: Provide fallback values when data is insufficient.
Contains schema defaults and recommended values for parameters.
"""

from typing import Tuple, Any, Optional


# Minimum sample sizes for data-driven confidence
MIN_SAMPLE_HIGH_CONFIDENCE = 10
MIN_SAMPLE_MEDIUM_CONFIDENCE = 5


# Schema defaults (from MainConfigSchema and related schemas)
# These are the Pydantic default values used in config schemas
SCHEMA_DEFAULTS = {
    "java_version": "17",
    "alb_selection": "existing_alb",
    "enable_ulimits": True,
    "http_scaling_enabled": False,
    "ebs_enabled": False,
    "generate_dockerfile": False,
    "go_use_aws_secrets": False,
}


# Recommended values (from ConfigValidator)
# These are the values that pass validation without warnings
# Ordered by most common/recommended first
RECOMMENDED_VALUES = {
    "cpu": [512, 1024, 256, 2048, 4096],
    "memory": [1024, 2048, 512, 4096, 8192, 16384],
    "min_task_count": [1, 2, 3],
    "max_task_count": [5, 10, 20],
    "desired_count": [2, 3, 1, 5],
    "container_port": [8080, 3000, 8000, 80, 443],
    "xms": [512, 1024, 256, 2048],
    "xmx": [1024, 2048, 512, 4096],
}


# Common parameters to recommend by default
DEFAULT_RECOMMENDATION_PARAMETERS = [
    "cpu",
    "memory",
    "container_port",
    "health_check_path",
    "enable_autoscaling",
    "min_task_count",
    "max_task_count",
    "desired_count",
    "xms",
    "xmx",
    "java_version",
]


def get_fallback_value(parameter: str) -> Tuple[Optional[Any], str]:
    """
    Get fallback value and source for a parameter.

    Args:
        parameter: Canonical parameter name

    Returns:
        Tuple of (value, source) where source is one of:
        - "schema_default": From Pydantic schema defaults
        - "recommended_value": From ConfigValidator recommendations
        - "no_default": No fallback available
    """
    if parameter in SCHEMA_DEFAULTS:
        return SCHEMA_DEFAULTS[parameter], "schema_default"

    if parameter in RECOMMENDED_VALUES:
        # Return first (most recommended) value
        return RECOMMENDED_VALUES[parameter][0], "recommended_value"

    return None, "no_default"


def get_confidence_level(total_configs: int) -> str:
    """
    Determine confidence level based on sample size.

    Args:
        total_configs: Number of configs with this parameter

    Returns:
        "high", "medium", or "low"
    """
    if total_configs >= MIN_SAMPLE_HIGH_CONFIDENCE:
        return "high"
    elif total_configs >= MIN_SAMPLE_MEDIUM_CONFIDENCE:
        return "medium"
    else:
        return "low"
