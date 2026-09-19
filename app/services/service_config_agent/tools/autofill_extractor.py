"""
Autofill Extractor for Service Config Agent.

Single responsibility: Extract autofill candidates from tool results.
Pure functions - no I/O, no side effects.
"""

from typing import Dict, Any, List, Optional

from app.services.service_config_agent.tools.base import AutofillCandidate
from app.utils.service_config_chat.prompts.parameter_definitions import is_autofill_allowed


def extract_autofill_candidate(
    tool_results: List[Dict[str, Any]],
    infrastructure_type: Optional[str] = None,
) -> Optional[AutofillCandidate]:
    """
    Extract autofill candidate from tool results.

    Supports extraction from:
    - get_parameter_value: Direct parameter value lookup
    - get_recommendations: Recommended values (high/medium confidence)
    - semantic_parameter_search: Parameter definitions with defaults
    - search_services_by_parameter: Most common value from statistics

    Args:
        tool_results: List of tool execution results
        infrastructure_type: Current infrastructure type (ECS/EKS) for validation

    Returns:
        AutofillCandidate if applicable, None otherwise
    """
    # Only offer autofill for single successful result
    successful_results = [
        r for r in tool_results
        if r.get("result", {}).get("success", False)
    ]

    if len(successful_results) != 1:
        return None  # Multiple results = complex query, no autofill

    result = successful_results[0]
    tool_name = result.get("tool", "")
    data = result.get("result", {}).get("data", {})

    # Dispatch to tool-specific extractor
    extractors = {
        "get_parameter_value": _extract_from_parameter_value,
        "get_recommendations": _extract_from_recommendations,
        "semantic_parameter_search": _extract_from_parameter_search,
        "search_services_by_parameter": _extract_from_search_statistics,
        "get_available_listener_priority": _extract_from_listener_priority,
    }

    extractor = extractors.get(tool_name)
    if not extractor:
        return None

    return extractor(data, infrastructure_type)


def _extract_from_parameter_value(
    data: Dict[str, Any],
    infrastructure_type: Optional[str],
) -> Optional[AutofillCandidate]:
    """Extract from get_parameter_value result."""
    param = data.get("parameter")
    value = data.get("value")

    if not param or value is None:
        return None

    if not is_autofill_allowed(param, infrastructure_type):
        return None

    return AutofillCandidate(
        field=param,
        value=value,
        source="tool_result",
        confidence="high",
    )


def _extract_from_recommendations(
    data: Dict[str, Any],
    infrastructure_type: Optional[str],
) -> Optional[AutofillCandidate]:
    """Extract from get_recommendations result (high or medium confidence)."""
    recommendations = data.get("recommendations", {})

    # Only autofill if exactly one parameter was requested
    if len(recommendations) != 1:
        return None

    param, rec = list(recommendations.items())[0]
    confidence = rec.get("confidence", "low")

    # Accept high and medium confidence recommendations
    if confidence not in ("high", "medium"):
        return None

    if not is_autofill_allowed(param, infrastructure_type):
        return None

    return AutofillCandidate(
        field=param,
        value=rec.get("recommended_value"),
        source="recommendation",
        confidence=confidence,
    )


def _extract_from_parameter_search(
    data: Dict[str, Any],
    infrastructure_type: Optional[str],
) -> Optional[AutofillCandidate]:
    """Extract from semantic_parameter_search result."""
    param = data.get("canonical_name") or data.get("parameter")
    definition = data.get("definition", {})

    if not param:
        return None

    # For parameter definition queries, use default from definition
    default_value = definition.get("default")
    if default_value is None:
        return None

    if not is_autofill_allowed(param, infrastructure_type):
        return None

    return AutofillCandidate(
        field=param,
        value=default_value,
        source="default",
        confidence="medium",
    )


# Minimum configs required to consider statistics reliable
MIN_CONFIGS_FOR_AUTOFILL = 3


def _extract_from_search_statistics(
    data: Dict[str, Any],
    infrastructure_type: Optional[str],
) -> Optional[AutofillCandidate]:
    """
    Extract from search_services_by_parameter result.

    Uses the most common value from statistics when sufficient data exists.
    """
    param = data.get("parameter")
    most_common_value = data.get("most_common_value")
    total_configs = data.get("total_configs", 0)

    if not param or most_common_value is None:
        return None

    # Require minimum sample size for reliable autofill
    if total_configs < MIN_CONFIGS_FOR_AUTOFILL:
        return None

    if not is_autofill_allowed(param, infrastructure_type):
        return None

    # Determine confidence based on sample size
    confidence = "high" if total_configs >= 10 else "medium"

    return AutofillCandidate(
        field=param,
        value=most_common_value,
        source="statistics",
        confidence=confidence,
    )


def _extract_from_listener_priority(
    data: Dict[str, Any],
    infrastructure_type: Optional[str],
) -> Optional[AutofillCandidate]:
    """
    Extract from get_available_listener_priority result.

    Always offers autofill since the tool guarantees a valid, unique value.
    Note: Bypasses is_autofill_allowed() because this tool explicitly
    calculates an available value for the unique-constraint parameter.
    """
    suggested_value = data.get("suggested_value")
    status = data.get("status")

    if suggested_value is None:
        return None

    # Determine confidence based on status
    # - "available": user's proposed value is valid (high confidence)
    # - "suggested": tool found next available (high confidence)
    # - "conflict": alternative suggested (medium confidence)
    confidence = "high" if status in ("available", "suggested") else "medium"

    return AutofillCandidate(
        field="listener_rule_priority",
        value=suggested_value,
        source="calculated",
        confidence=confidence,
    )
