"""Policy statement builders for the per-tenant default IAM role.

Each supported resource kind has a fixed ``Sid`` and a fixed action set. The
runtime role keeps **one** statement per kind; multiple resources of the same
kind share that statement and live together in its ``Resource`` list. Adding a
resource appends its ARN (dedup). Removing drops the ARN; when the list empties
the whole statement goes away.
"""

from app.domain.policies.kinds import (
    SUPPORTED_KINDS,
    PolicyKind,
    build_statement,
    get_kind_for_resource,
)

__all__ = [
    "SUPPORTED_KINDS",
    "PolicyKind",
    "build_statement",
    "get_kind_for_resource",
]
