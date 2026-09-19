from __future__ import annotations

from typing import Any

_PARTITION_KEY_TYPE_ALIASES: dict[str, str] = {
    "STRING": "S",
    "STR": "S",
    "NUMBER": "N",
    "NUM": "N",
    "BINARY": "B",
    "BIN": "B",
}


def normalize_partition_key_type(value: Any) -> Any:
    """
    Normalize DynamoDB partition key type aliases to canonical forms.

    Examples:
    - string/str -> S
    - number/num -> N
    - binary/bin -> B
    """
    if not isinstance(value, str):
        return value

    normalized = value.strip().upper()
    return _PARTITION_KEY_TYPE_ALIASES.get(normalized, normalized)

