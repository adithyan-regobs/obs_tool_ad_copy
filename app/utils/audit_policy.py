"""
Audit payload policy — what may be stored from a request/response body.

The audit trail stores bodies for EVERY endpoint, because that detail is what
makes the trail useful to a customer reviewing activity. Two filters run on
the way in, and they cover different failure modes:

1. ROUTE-LEVEL (is_sensitive_route) — the secret/variable surface. Those
   bodies exist to carry the plain values a user types into Env & Permissions,
   so there is nothing worth keeping in them. The whole body is replaced with
   HIDDEN_PAYLOAD: a marker rather than NULL, so the audit UI can say
   "withheld by policy" instead of looking like the request had no body.

2. FIELD-LEVEL (sanitize_payload) — every OTHER endpoint. Bodies are stored,
   but any field whose NAME looks like a credential is replaced with REDACTED,
   at any depth. This is the backstop: the original leak happened because a
   sanitizer only checked five exact key names at the top level, so a field
   named `value` nested inside items[] walked straight past it.

Neither filter is sufficient alone. Route matching misses an endpoint nobody
remembered to list; field matching misses a credential in a field with an
innocent name. Together, a new endpoint has to defeat both to leak.

Route matching uses the segment AFTER `api/v1`, located anywhere in the path,
because the two services mount differently (obs_tool at /api/v1/..., the
secret service at /secret-config-manager/api/v1/...) and an index-0
assumption would silently match nothing in one of them.
"""
import re
from typing import Any, Optional

# ── route-level ──────────────────────────────────────────────────────────────

# First path segment after api/v1 for every route group whose request or
# response body can carry a secret/variable VALUE.
#
#   project-variables   canvas variables + /with-values (returns decrypted)
#   resource-variable   save / clone / deploy / revert / stage-sync
#   secrets, configs    the kind-split save / clone / with-values routes
#
# NOT included, deliberately: `internal` — the machine-to-machine
# /internal/variable-deploy route takes resource codes only; values are read
# from the staged S3 bucket and never travel in its body or response.
SENSITIVE_ROUTE_GROUPS = frozenset(
    {
        "project-variables",
        "resource-variable",
        "secrets",
        "configs",
    }
)

# Stored in place of the real body. An object, so the column stays valid JSONB
# and the UI can branch on `_withheld`.
HIDDEN_PAYLOAD = {
    "_withheld": True,
    "_reason": (
        "Body not recorded: this endpoint carries secret/variable values, "
        "which are never stored in the audit trail."
    ),
}


def route_key(path: str) -> Optional[str]:
    """The path after `api/v1`, e.g. 'secrets/CODE/save'.

    None when the path has no api/v1 segment (health checks, mounted
    sub-apps, MCP endpoints).
    """
    parts = path.strip("/").split("/")
    for i in range(len(parts) - 1):
        if parts[i] == "api" and parts[i + 1] == "v1":
            return "/".join(parts[i + 2:]) or None
    return None


def is_sensitive_route(path: str) -> bool:
    """True when this endpoint's bodies must not be stored at all."""
    key = route_key(path)
    if not key:
        return False
    return key.split("/")[0] in SENSITIVE_ROUTE_GROUPS


# ── field-level ──────────────────────────────────────────────────────────────

REDACTED = "***REDACTED***"

# Matched as a SUBSTRING of the field name, case-insensitively. These read as
# a credential wherever they appear, so token_hash, client_secret,
# script_access_key and auth_config are all caught.
_SENSITIVE_SUBSTRING = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|access_key"
    r"|private_key|credential|auth_config|authorization)",
    re.IGNORECASE,
)

# Matched EXACTLY (case-insensitively). Short, generic names whose substring
# form would over-redact innocent fields — 'value' as a substring would also
# hit threshold_value, which is real monitoring config worth auditing.
_SENSITIVE_EXACT = frozenset(
    {
        "value",
        "variable_value",
        "signature",
        "auth",
    }
)
# 'values' is deliberately NOT here. On /service-configs/eks/save-from-values
# it is the whole Helm values document — port, replicas, region — which is
# exactly the config detail the audit trail should show. Redacting the key
# wholesale threw all of it away. Recursion still reaches the `secret` block
# inside it, which the substring rule catches.

# Defensive bound; audit payloads are shallow in practice.
_MAX_DEPTH = 20


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_EXACT or bool(_SENSITIVE_SUBSTRING.search(lowered))


def sanitize_payload(payload: Any, _depth: int = 0) -> Any:
    """Return a copy of `payload` with credential-named fields redacted, at
    any nesting depth, through dicts AND lists.

    Safe on any JSON-shaped input (dict, list, scalar, None). The input is
    never mutated — the caller may still need the original.
    """
    if _depth > _MAX_DEPTH:
        # Too deep to inspect confidently -> refuse to store it.
        return REDACTED

    if isinstance(payload, dict):
        return {
            key: REDACTED
            if _is_sensitive_key(str(key))
            else sanitize_payload(item, _depth + 1)
            for key, item in payload.items()
        }

    if isinstance(payload, list):
        return [sanitize_payload(item, _depth + 1) for item in payload]

    return payload
