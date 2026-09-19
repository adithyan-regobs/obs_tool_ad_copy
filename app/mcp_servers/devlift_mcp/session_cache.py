"""Redis-backed session cache for MCP draft state — v2 multi-draft.

Key shape (unchanged from v1):
    mcp:draft:{user_code}:{case_ref_code}  →  JSON list of draft entries

v2 change: the list now holds MULTIPLE entries (one per resource instance).
Each entry is identified by a server-generated `draft_id` that is returned to
Claude and echoed back on every subsequent call — same opaque-token pattern as
claude_session_id, but scoped per resource instance rather than per session.

Value entry shape (v2):
    {
        "draft_id":                "a3f9c2b1",   # 8-char hex, server-generated
        "project_id":              "proj-...",   # cross-session project anchor
        "ticket_code":             "T-...",
        "queue_code":              "Q-...",
        "queue_id":                12345,
        "transaction_code":        "INFRA-...",
        "transaction_table":       "infrastructure_mst",
        "last_updated":            "ISO-8601",
        "identifier":              "<resource name>",
        "resource_type":           "s3_bucket",
        "deployment_environment":  "stage",
        "status":                  "pending",    # pending | committed

        # Post-trigger additions (set by _execute_trigger_core on deploy):
        "deploy_in_progress":      True,         # cleared when pipeline terminal
        "pipeline_run_track_code": "...",        # PaaS — Jenkins; cleared on terminal
        "deploy_mode":             "temporal",   # Enterprise — "temporal" | "inline"
        "workflow_id":             "deploy-...", # Enterprise — Temporal DeploymentWorkflow id
        "deploy_final_status":     "COMPLETED",  # Enterprise — set when the workflow ends
        "pr_url":                  "...",        # Enterprise — GitOps PR URL

        # Env-sync additions (set post-trigger for resources that emit env vars,
        # e.g. postgres_server):
        "canonical_env_keys":      ["POSTGRES_HOST", ...],
        "env_synced":              False,        # True once wired into a service
    }

Removed in v2:
    - claude_session_id  (replaced by draft_id — per-resource, not per-session)
    - DraftBranch / decide_branch  (branching now driven by draft_id presence)
    - intent parameter  (draft_id IS the intent)
"""

import logging
import secrets
from typing import Optional

from app.integrations.redis_integration import RedisIntegration

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================
PREFIX_DRAFT = "mcp:draft:"
PREFIX_LOCK = "mcp:lock:"
DRAFT_TTL_SECONDS = 7 * 24 * 3600  # 7 days
LOCK_TTL_SECONDS = 30


# ============================================================
# draft_id generation
# ============================================================

def generate_draft_id() -> str:
    """Generate a short opaque draft ID (8-char hex).

    Example: "a3f9c2b1"
    Short enough to be readable in logs, long enough to avoid collisions
    across a user's drafts (2^32 possibilities per resource type).
    """
    return secrets.token_hex(4)


# ============================================================
# Key helpers
# ============================================================

def _draft_key(user_code: str, case_ref_code: str) -> str:
    return f"{PREFIX_DRAFT}{user_code}:{case_ref_code}"


def _lock_key(user_code: str, case_ref_code: str) -> str:
    return f"{PREFIX_LOCK}{user_code}:{case_ref_code}"


# ============================================================
# Async cache operations
# ============================================================

async def get_all_drafts(user_code: str, case_ref_code: str) -> list[dict]:
    """Return all draft entries for (user_code, case_ref_code).

    Returns an empty list if no drafts exist.
    """
    key = _draft_key(user_code, case_ref_code)
    blob = await RedisIntegration.get_json(key)
    if not blob:
        return []
    if isinstance(blob, list):
        return blob
    logger.warning(
        "mcp_session_cache: unexpected blob shape at key=%s type=%s — ignoring",
        key,
        type(blob).__name__,
    )
    return []


async def get_draft_by_id(
    user_code: str, case_ref_code: str, draft_id: str
) -> Optional[dict]:
    """Find a specific draft entry by draft_id. Returns None if not found."""
    drafts = await get_all_drafts(user_code, case_ref_code)
    for entry in drafts:
        if entry.get("draft_id") == draft_id:
            return entry
    return None


async def add_draft(user_code: str, case_ref_code: str, entry: dict) -> bool:
    """Append a new draft entry to the array. Refreshes the TTL."""
    key = _draft_key(user_code, case_ref_code)
    existing = await RedisIntegration.get_json(key)
    drafts = existing if isinstance(existing, list) else []
    drafts.append(entry)
    return await RedisIntegration.set_json(key, drafts, ttl=DRAFT_TTL_SECONDS)


async def update_draft_by_id(
    user_code: str, case_ref_code: str, draft_id: str, updated_entry: dict
) -> bool:
    """Replace the draft entry with the given draft_id in-place. Refreshes TTL.

    Returns False if draft_id is not found.
    """
    key = _draft_key(user_code, case_ref_code)
    blob = await RedisIntegration.get_json(key)
    drafts = blob if isinstance(blob, list) else []
    new_drafts = []
    found = False
    for d in drafts:
        if d.get("draft_id") == draft_id:
            new_drafts.append(updated_entry)
            found = True
        else:
            new_drafts.append(d)
    if not found:
        logger.warning(
            "mcp_session_cache: update_draft_by_id draft_id=%s not found in key=%s",
            draft_id, key,
        )
        return False
    return await RedisIntegration.set_json(key, new_drafts, ttl=DRAFT_TTL_SECONDS)


async def clear_draft(user_code: str, case_ref_code: str) -> bool:
    """Delete ALL draft entries for (user_code, case_ref_code)."""
    key = _draft_key(user_code, case_ref_code)
    return await RedisIntegration.delete(key)


async def scan_user_drafts(user_code: str) -> list[dict]:
    """Return all active draft entries across all resource types for the given user.

    Scans Redis for keys matching mcp:draft:{user_code}:* and returns every
    entry from each array. Attaches a '_case_ref_code' field so callers know
    which resource type each entry belongs to.
    """
    pattern = f"{PREFIX_DRAFT}{user_code}:*"
    try:
        client = await RedisIntegration.get_client_async()
        if not client:
            return []
        results = []
        async for key in client.scan_iter(match=pattern):
            raw_key = key if isinstance(key, str) else key.decode()
            blob = await RedisIntegration.get_json(raw_key)
            if not blob or not isinstance(blob, list):
                continue
            case_ref_suffix = raw_key.removeprefix(f"{PREFIX_DRAFT}{user_code}:")
            for entry in blob:
                if entry and isinstance(entry, dict):
                    results.append({**entry, "_case_ref_code": case_ref_suffix})
        return results
    except Exception:
        logger.exception("mcp_session_cache scan_user_drafts failed user=%s", user_code)
        return []


async def scan_available_env_sources(user_code: str, project_id: str) -> list[dict]:
    """Return committed infra drafts in the given project that can still be
    used as env-sync sources for a service deployment.

    A draft qualifies when:
      • it belongs to the given project_id (cross-session grouping)
      • status == "committed" (its env vars exist in AWS Secrets Manager)
      • it exposed canonical_env_keys (e.g. postgres emits POSTGRES_HOST etc.)
      • env_synced is False (hasn't already been wired into a service)

    If any of these filters doesn't hold, the draft isn't a syncable source —
    returning it would confuse the LLM or produce duplicate env wiring.
    """
    drafts = await scan_user_drafts(user_code)
    return [
        d for d in drafts
        if d.get("project_id") == project_id
        and d.get("status") == "committed"
        and d.get("canonical_env_keys")
        and not d.get("env_synced")
    ]


async def acquire_call_lock(user_code: str, case_ref_code: str) -> bool:
    """Try to acquire a short-lived lock to prevent concurrent tool calls from
    racing each other on the same draft array. Returns True if acquired.

    The lock is intentionally short (30s) to prevent stuck locks if a worker
    crashes mid-call.
    """
    lock_key = _lock_key(user_code, case_ref_code)
    return await RedisIntegration.acquire_lock(lock_key, ttl=LOCK_TTL_SECONDS)
