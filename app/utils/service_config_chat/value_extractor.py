"""
Value extraction utilities for Service Config Chat.

Hybrid approach: regex first for common patterns, LLM fallback for complex expressions.
"""
import re
from typing import Optional


# Patterns for common value expressions
VALUE_PATTERNS = [
    # "fill with 7", "use 7", "set to 7", "set it to 7"
    r'(?:fill\s+with|use|set\s*(?:it)?\s*to)\s+(\d+)',
    # "no, 7" or "no. 7" or "no 7"
    r'^no[,.\s]+(\d+)$',
    # Just a number
    r'^(\d+)$',
]

# Boolean true values (excluding 'yes' which is a confirmation)
BOOLEAN_TRUE = {'true', 'enable', 'enabled', 'on'}

# Boolean false values (excluding 'no' which is a decline)
BOOLEAN_FALSE = {'false', 'disable', 'disabled', 'off'}

# Pure confirmation words (no custom value implied)
CONFIRMATIONS = {'yes', 'ok', 'sure', 'yep', 'yeah', 'y'}


def extract_value_regex(message: str) -> Optional[str]:
    """
    Try to extract custom value from message using regex patterns.

    Handles common patterns like:
    - "fill with 7", "use 7", "set to 7"
    - "no, 7", "no. 7"
    - Plain numbers: "7", "1024"
    - Booleans: "true", "enable", "false", "disable"

    Args:
        message: User's message

    Returns:
        Extracted value string, or None if no match found.
    """
    cleaned = message.lower().strip()

    # Check for boolean values first
    # Exclude standalone "yes"/"no" which are confirmations/declines
    if cleaned in BOOLEAN_TRUE:
        return 'true'
    if cleaned in BOOLEAN_FALSE:
        return 'false'

    # Try numeric patterns
    for pattern in VALUE_PATTERNS:
        match = re.search(pattern, cleaned)
        if match:
            return match.group(1)

    return None


def is_confirmation_only(message: str) -> bool:
    """
    Check if message is just a confirmation (yes/ok/sure) with no custom value.

    Args:
        message: User's message

    Returns:
        True if message is a pure confirmation, False otherwise.
    """
    cleaned = message.lower().strip()
    return cleaned in CONFIRMATIONS
