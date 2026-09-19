"""
Tool Result Schema and Types.

Single responsibility: Define result types for tool outputs.
No logic - just type definitions.
"""

from typing import TypedDict, Optional, Any, List


class ToolResult(TypedDict, total=False):
    """
    Standard result type for all tools.

    Attributes:
        success: Whether the tool executed successfully
        data: The result data (type varies by tool)
        error: Error message if success=False
        suggestions: Alternative suggestions for low-confidence matches
        matched_services: Full service info for clickable buttons (when ambiguous)
    """

    success: bool
    data: Optional[Any]
    error: Optional[str]
    suggestions: Optional[List[str]]
    matched_services: Optional[List[dict]]  # For clickable buttons


class ServiceResolutionResult(TypedDict, total=False):
    """
    Result of resolving a service name.

    Attributes:
        resolved: Whether resolution was successful
        service_code: The resolved service code
        service_name: The resolved service name
        confidence: Similarity score (0.0 to 1.0)
        suggestions: Alternative service names if low confidence
        matched_services: Full service info for clickable buttons (when ambiguous)
        error: Error message if resolution failed
    """

    resolved: bool
    service_code: Optional[str]
    service_name: Optional[str]
    confidence: float
    suggestions: Optional[List[str]]
    matched_services: Optional[List[dict]]  # For clickable buttons
    error: Optional[str]


class ParameterResolutionResult(TypedDict, total=False):
    """
    Result of resolving a parameter name.

    Attributes:
        resolved: Whether resolution was successful
        canonical_name: The canonical parameter name
        confidence: Match confidence level
        score: Similarity score (0.0 to 1.0)
        suggestions: Alternative parameter names if low confidence
        error: Error message if resolution failed
    """

    resolved: bool
    canonical_name: Optional[str]
    confidence: str  # MatchConfidence enum value as string
    score: float
    suggestions: Optional[List[str]]
    error: Optional[str]


class AutofillCandidate(TypedDict, total=False):
    """
    Autofill candidate extracted from tool results.

    Attributes:
        field: Canonical parameter name
        value: Recommended value to fill
        source: Where the value came from (tool_result, recommendation, default)
        confidence: Confidence level (high, medium, low)
    """

    field: str
    value: Any
    source: str
    confidence: Optional[str]
