"""
Recommendation Builder for Service Config Agent.

Single responsibility: Build recommendations from statistics.
Pure functions - no I/O, no side effects.
"""

from typing import Any, Dict, List, Tuple

from app.services.service_config_agent.tools.recommendation_defaults import (
    MIN_SAMPLE_HIGH_CONFIDENCE,
    MIN_SAMPLE_MEDIUM_CONFIDENCE,
    get_confidence_level,
    get_fallback_value,
)


def build_recommendation(param: str, stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build recommendation for a single parameter from stats.

    Args:
        param: Parameter name
        stats: Stats dict from repository with keys:
            - values: {value: count}
            - most_common: str
            - total_configs: int
            - total_services: int

    Returns:
        Recommendation dict with keys:
            - recommended_value
            - confidence
            - source
            - usage_count (optional)
            - alternatives (optional)
            - note (optional)
    """
    total = stats.get("total_configs", 0)
    most_common = stats.get("most_common")
    values = stats.get("values", {})

    # Determine confidence based on sample size
    confidence = get_confidence_level(total)

    # Low confidence or no data - use fallback
    if confidence == "low" or most_common is None:
        return _build_fallback_recommendation(param, total)

    # Data-driven recommendation
    return _build_data_recommendation(stats, most_common, values, confidence)


def _build_fallback_recommendation(param: str, total: int) -> Dict[str, Any]:
    """Build recommendation using fallback defaults."""
    fallback_value, source = get_fallback_value(param)

    result = {
        "recommended_value": fallback_value,
        "confidence": "low",
        "source": source,
    }

    if total > 0:
        result["usage_count"] = total
        result["note"] = "Insufficient data for reliable recommendation"
    else:
        result["note"] = "No data available" if source == "no_default" else "Using fallback default"

    return result


def _build_data_recommendation(
    stats: Dict[str, Any],
    most_common: str,
    values: Dict[str, int],
    confidence: str,
) -> Dict[str, Any]:
    """Build recommendation from actual data."""
    # Build alternatives (top 3 excluding most common)
    sorted_values = sorted(values.items(), key=lambda x: x[1], reverse=True)
    alternatives = [
        {"value": v, "count": c}
        for v, c in sorted_values[1:4]  # Skip first (most common)
    ]

    total_configs = stats.get("total_configs", 0)
    total_services = stats.get("total_services", total_configs)
    usage_count = values.get(most_common, 0)

    # Calculate percentage based on total configs (handles multi-geo scenarios)
    usage_pct = int((usage_count / total_configs) * 100) if total_configs > 0 else 0

    return {
        "recommended_value": most_common,
        "confidence": confidence,
        "usage_count": usage_count,
        "usage_percentage": f"{usage_pct}%",
        "total_configs": total_configs,
        "total_services": total_services,
        "alternatives": alternatives,
        "source": "data",
    }


def build_recommendations_summary(
    recommendations: Dict[str, Dict],
    total_configs: int,
    service_type: str,
    environment: str,
) -> str:
    """
    Build summary string for recommendations.

    Args:
        recommendations: Dict of param -> recommendation
        total_configs: Total configs sampled
        service_type: Service type value string
        environment: Environment value string

    Returns:
        Human-readable summary string
    """
    high_confidence = sum(1 for r in recommendations.values() if r.get("confidence") == "high")
    medium_confidence = sum(1 for r in recommendations.values() if r.get("confidence") == "medium")
    low_confidence = sum(1 for r in recommendations.values() if r.get("confidence") == "low")

    summary_parts = [f"Based on {total_configs} {service_type} service configs in {environment}"]

    if high_confidence:
        summary_parts.append(f"{high_confidence} high-confidence recommendations")
    if medium_confidence:
        summary_parts.append(f"{medium_confidence} medium-confidence")
    if low_confidence:
        summary_parts.append(f"{low_confidence} using defaults (insufficient data)")

    return ". ".join(summary_parts)


def validate_value(param: str, user_value: Any, stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate a single parameter value against stats.

    Args:
        param: Parameter name
        user_value: User's value to validate
        stats: Stats dict from repository

    Returns:
        Validation result dict with keys:
            - status: "optimal" | "acceptable" | "unusual" | "unknown"
            - your_value
            - message
            - common_value (optional)
            - common_usage (optional)
    """
    most_common = stats.get("most_common")
    values = stats.get("values", {})
    total = stats.get("total_configs", 0)

    # Insufficient data for validation
    if total < MIN_SAMPLE_MEDIUM_CONFIDENCE:
        return {
            "status": "unknown",
            "your_value": user_value,
            "message": "Insufficient data to validate (fewer than 5 configs)",
        }

    user_str = str(user_value)
    common_count = values.get(most_common, 0)
    common_pct = int((common_count / total) * 100) if total > 0 else 0

    # User value matches most common
    if user_str == most_common:
        return {
            "status": "optimal",
            "your_value": user_value,
            "common_value": most_common,
            "common_usage": f"{common_pct}% of services",
            "message": "Matches the most common value",
        }

    # Check if user value exists in the data
    user_count = values.get(user_str, 0)
    user_pct = int((user_count / total) * 100) if total > 0 else 0

    if user_pct >= 10:  # At least 10% usage
        return {
            "status": "acceptable",
            "your_value": user_value,
            "common_value": most_common,
            "your_usage": f"{user_pct}% of services",
            "common_usage": f"{common_pct}% of services",
            "message": f"Used by some services, but {most_common} is more common",
        }

    # Unusual value
    return {
        "status": "unusual",
        "your_value": user_value,
        "common_value": most_common,
        "common_usage": f"{common_pct}% of services",
        "message": f"Uncommon choice. Most {param} values are {most_common}.",
    }


def calculate_validation_score(validation_results: Dict[str, Dict]) -> Tuple[int, str]:
    """
    Calculate overall validation score and summary.

    Args:
        validation_results: Dict of param -> validation result

    Returns:
        Tuple of (score_percent, summary_string)
    """
    if not validation_results:
        return 100, "No parameters to validate"

    optimal_count = 0
    acceptable_count = 0
    total_params = len(validation_results)

    for result in validation_results.values():
        status = result.get("status")
        if status == "optimal":
            optimal_count += 1
        elif status == "acceptable":
            acceptable_count += 1

    # Score: optimal = 100%, acceptable = 50%, others = 0%
    score = int(((optimal_count + acceptable_count * 0.5) / total_params) * 100)
    summary = f"{optimal_count} of {total_params} parameters match common patterns"

    return score, summary
