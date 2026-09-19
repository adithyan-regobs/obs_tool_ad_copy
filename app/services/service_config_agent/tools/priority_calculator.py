"""
Priority Calculator for Listener Rule Priority.

Single responsibility: Calculate next available priority.
Pure functions - no I/O, no side effects.
"""

from typing import List, Optional, Tuple

# Valid range per AWS ALB rules
MIN_PRIORITY = 1
MAX_PRIORITY = 50000

# Default starting point (avoid 1-10 reserved for system rules)
DEFAULT_START = 100
# Increment step for readability
PRIORITY_STEP = 10


def find_next_available_priority(
    used_priorities: List[int],
    preferred_start: int = DEFAULT_START,
) -> int:
    """
    Find the next available listener priority.

    Strategy:
    1. Start from preferred_start (default 100)
    2. Find first gap in sequence using PRIORITY_STEP increments
    3. If no gaps, use max + PRIORITY_STEP

    Args:
        used_priorities: Sorted list of currently used priorities
        preferred_start: Where to start looking (default 100)

    Returns:
        Next available priority integer
    """
    if not used_priorities:
        return preferred_start

    used_set = set(used_priorities)

    # Try increments from preferred_start
    candidate = preferred_start
    while candidate <= MAX_PRIORITY:
        if candidate not in used_set:
            return candidate
        candidate += PRIORITY_STEP

    # Fallback: find any gap
    for i in range(MIN_PRIORITY, MAX_PRIORITY + 1):
        if i not in used_set:
            return i

    raise ValueError("No available listener priority in valid range")


def validate_priority_available(
    proposed_value: int,
    used_priorities: List[int],
) -> Tuple[bool, Optional[str]]:
    """
    Validate if a proposed priority is available.

    Args:
        proposed_value: The priority value to validate
        used_priorities: List of currently used priorities

    Returns:
        Tuple of (is_available, error_message)
    """
    # Range check
    if proposed_value < MIN_PRIORITY or proposed_value > MAX_PRIORITY:
        return False, f"Priority must be between {MIN_PRIORITY} and {MAX_PRIORITY}"

    # Uniqueness check
    if proposed_value in used_priorities:
        return False, f"Priority {proposed_value} is already in use"

    return True, None


def suggest_alternative(
    proposed_value: int,
    used_priorities: List[int],
) -> int:
    """
    Suggest an alternative priority close to the proposed value.

    Args:
        proposed_value: The conflicting priority
        used_priorities: List of currently used priorities

    Returns:
        Nearest available priority
    """
    used_set = set(used_priorities)

    # Try values close to proposed
    for offset in range(1, 100):
        # Try higher first
        if proposed_value + offset <= MAX_PRIORITY and proposed_value + offset not in used_set:
            return proposed_value + offset
        # Then try lower
        if proposed_value - offset >= MIN_PRIORITY and proposed_value - offset not in used_set:
            return proposed_value - offset

    # Fallback to any available
    return find_next_available_priority(used_priorities)
