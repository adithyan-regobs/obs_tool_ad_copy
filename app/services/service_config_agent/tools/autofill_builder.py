"""
Autofill Response Builder for Service Config Agent.

Single responsibility: Build autofill offer text from candidate.
Pure functions - no I/O, no side effects.
"""

from typing import Optional

from app.services.service_config_agent.tools.base import AutofillCandidate


def build_autofill_offer(candidate: AutofillCandidate) -> str:
    """
    Build autofill offer text from candidate.

    Args:
        candidate: Autofill candidate with field and value

    Returns:
        Formatted autofill offer string
    """
    field = candidate.get("field", "")
    value = candidate.get("value", "")

    return f"Would you like me to fill '{field}' with value '{value}'?"


def append_autofill_offer(
    response: str,
    candidate: Optional[AutofillCandidate],
) -> str:
    """
    Append autofill offer to response if candidate exists.

    Args:
        response: Original response text
        candidate: Autofill candidate or None

    Returns:
        Response with autofill offer appended (if applicable)
    """
    if not candidate:
        return response

    offer = build_autofill_offer(candidate)
    return f"{response}\n\n{offer}"
