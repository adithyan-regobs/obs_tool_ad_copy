"""Cache for placement parameter collection - supports Redis (multi-worker) or in-memory (single worker).

Stores placement parameters during UI collection to avoid multiple infraChat calls.
Only calls infraChat once all placement params are collected.

When REDIS_ENABLED=true:
- Uses Redis for distributed state across multiple workers
- Automatic TTLs prevent memory leaks
- Distributed locks prevent race conditions

When REDIS_ENABLED=false:
- Uses in-memory dictionaries (original behavior)
- Works only with single worker deployments
"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import logging

from app.core.config import settings
from app.integrations.redis_integration import RedisIntegration

logger = logging.getLogger(__name__)

# TTL for cache entries (1 day)
ONE_DAY_TTL = 86400
CACHE_TTL_SECONDS = ONE_DAY_TTL
DEPLOYMENT_MSG_TTL = ONE_DAY_TTL
PLACEMENT_MSG_TTL = ONE_DAY_TTL
REMINDER_MSG_TTL = ONE_DAY_TTL
WAIT_MSG_TTL = ONE_DAY_TTL
LOCK_TTL = 600  # 10 minutes for locks (prevent stuck locks)
PREVIEW_STATE_TTL = ONE_DAY_TTL

# Redis key prefixes
PREFIX_PLACEMENT = "placement:cache:"
PREFIX_DEPLOYMENT_MSG = "deployment:msg:"
PREFIX_PLACEMENT_MSG = "placement:msg:"
PREFIX_REMINDER_MSG = "reminder:msg:"
PREFIX_WAIT_MSG = "wait:msg:"
PREFIX_LOCK = "lock:"
PREFIX_PREVIEW = "preview:state:"

# In-memory fallback caches (used when REDIS_ENABLED=false)
_placement_cache: Dict[str, Dict[str, Any]] = {}
_pending_deployment_messages: Dict[str, Dict[str, str]] = {}
_pending_placement_messages: Dict[str, Dict[str, str]] = {}
_pending_reminder_messages: Dict[str, Dict[str, str]] = {}
_pending_wait_messages: Dict[str, Dict[str, str]] = {}
_conversation_locks: Dict[str, Dict[str, Any]] = {}
_preview_states: Dict[str, Dict[str, Any]] = {}


# ============================================================
# PLACEMENT CACHE FUNCTIONS
# ============================================================

async def store_placement_state(
    conversation_id: str,
    remaining_params: Dict[str, Any],
    user_mst_code: str,
    tenant_code: str
) -> None:
    """Store placement parameters contract after initial infraChat call.

    Args:
        conversation_id: Ticket code
        remaining_params: The remaining_placement_parameters from infraChat
        user_mst_code: User code
        tenant_code: Tenant code
    """
    state = {
        "collected": {},
        "remaining": remaining_params,
        "user_mst_code": user_mst_code,
        "tenant_code": tenant_code,
        "created_at": datetime.utcnow().isoformat()
    }

    if settings.redis_enabled:
        key = f"{PREFIX_PLACEMENT}{conversation_id}"
        await RedisIntegration.set_json(key, state, ttl=CACHE_TTL_SECONDS)
    else:
        _placement_cache[conversation_id] = {
            **state,
            "created_at": datetime.utcnow()  # Keep datetime object for in-memory
        }

    logger.info(f"Stored placement state for {conversation_id}, {len(remaining_params)} params remaining")


async def get_placement_state(conversation_id: str) -> Optional[Dict[str, Any]]:
    """Get placement state for a conversation.

    Args:
        conversation_id: Ticket code

    Returns:
        State dict or None if not found/expired
    """
    if settings.redis_enabled:
        key = f"{PREFIX_PLACEMENT}{conversation_id}"
        state = await RedisIntegration.get_json(key)
        if state:
            # Convert created_at back to datetime if needed for compatibility
            if isinstance(state.get("created_at"), str):
                state["created_at"] = datetime.fromisoformat(state["created_at"])
        return state
    else:
        # In-memory fallback with TTL check
        state = _placement_cache.get(conversation_id)

        if not state:
            return None

        # Check TTL
        created_at = state.get("created_at")
        if created_at and datetime.utcnow() - created_at > timedelta(seconds=CACHE_TTL_SECONDS):
            logger.info(f"Cache expired for {conversation_id}")
            del _placement_cache[conversation_id]
            return None

        return state


async def update_placement_selection(
    conversation_id: str,
    param_name: str,
    selected_value: str,
    selected_label: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update collected params with new selection.

    Args:
        conversation_id: Ticket code
        param_name: Parameter name (e.g., "infra_vendor_enum")
        selected_value: Selected value (e.g., "aws")
        selected_label: Display label for the selected option (optional)

    Returns:
        Updated state or None if not found
    """
    state = await get_placement_state(conversation_id)

    if not state:
        logger.warning(f"No placement state found for {conversation_id}")
        return None

    # Add to collected
    state["collected"][param_name] = selected_value

    # Store label for service_mst_code to derive api_name (Kong Gateway)
    if param_name == "service_mst_code" and selected_label:
        state["collected"]["_service_mst_name"] = selected_label
        logger.info(f"[PLACEMENT_CACHE] Stored _service_mst_name={selected_label} for {conversation_id}")

    # Store label for applications_mst_code to derive product_name (Atlantis naming)
    logger.info(f"[PLACEMENT_CACHE] param_name='{param_name}', selected_label='{selected_label}', is_applications_mst_code={param_name == 'applications_mst_code'}")
    if param_name == "applications_mst_code" and selected_label:
        state["collected"]["_applications_mst_name"] = selected_label
        logger.info(f"[PLACEMENT_CACHE] Stored _applications_mst_name={selected_label} for {conversation_id}")
    elif param_name == "applications_mst_code" and not selected_label:
        logger.warning(f"[PLACEMENT_CACHE] applications_mst_code selected but NO selected_label provided! Cannot store _applications_mst_name")

    # Remove from remaining
    if param_name in state["remaining"]:
        del state["remaining"][param_name]

    # Save updated state back
    if settings.redis_enabled:
        key = f"{PREFIX_PLACEMENT}{conversation_id}"
        # Convert datetime to ISO string for JSON serialization
        if isinstance(state.get("created_at"), datetime):
            state["created_at"] = state["created_at"].isoformat()
        await RedisIntegration.set_json(key, state, ttl=CACHE_TTL_SECONDS)
    # In-memory: state is already updated in place

    logger.info(f"Updated {conversation_id}: collected {param_name}={selected_value}, "
                f"{len(state['remaining'])} params remaining")

    return state


async def is_placement_complete(conversation_id: str) -> bool:
    """Check if all placement params have been collected.

    Args:
        conversation_id: Ticket code

    Returns:
        True if all params collected (remaining is empty)
    """
    state = await get_placement_state(conversation_id)

    if not state:
        return False

    return len(state.get("remaining", {})) == 0


async def get_collected_params(conversation_id: str) -> Dict[str, str]:
    """Get all collected placement params.

    Args:
        conversation_id: Ticket code

    Returns:
        Dict of collected params
    """
    state = await get_placement_state(conversation_id)

    if not state:
        return {}

    return state.get("collected", {})


async def get_next_param(conversation_id: str) -> Optional[tuple]:
    """Get the next parameter to collect (sorted by order).

    Args:
        conversation_id: Ticket code

    Returns:
        Tuple of (param_name, param_meta) or None if all collected
    """
    state = await get_placement_state(conversation_id)

    if not state or not state.get("remaining"):
        return None

    # Sort by order
    sorted_params = sorted(
        state["remaining"].items(),
        key=lambda x: x[1].get("order", 999)
    )

    if sorted_params:
        return sorted_params[0]

    return None


async def clear_placement_state(conversation_id: str) -> None:
    """Clear placement state after completion or error.

    Args:
        conversation_id: Ticket code
    """
    if settings.redis_enabled:
        key = f"{PREFIX_PLACEMENT}{conversation_id}"
        await RedisIntegration.delete(key)
    else:
        if conversation_id in _placement_cache:
            del _placement_cache[conversation_id]

    logger.info(f"Cleared placement state for {conversation_id}")


async def cleanup_expired() -> int:
    """Clean up expired cache entries (only for in-memory mode).

    Redis handles TTL automatically, so this is a no-op for Redis mode.

    Returns:
        Number of entries cleaned up
    """
    if settings.redis_enabled:
        # Redis handles TTL automatically
        return 0

    now = datetime.utcnow()
    expired = []

    for conv_id, state in _placement_cache.items():
        created_at = state.get("created_at")
        if created_at and now - created_at > timedelta(seconds=CACHE_TTL_SECONDS):
            expired.append(conv_id)

    for conv_id in expired:
        del _placement_cache[conv_id]

    if expired:
        logger.info(f"Cleaned up {len(expired)} expired placement cache entries")

    return len(expired)


# ============================================================
# PENDING DEPLOYMENT MESSAGES
# ============================================================

async def store_pending_deployment_message(
    conversation_id: str,
    channel_id: str,
    message_ts: str
) -> None:
    """Store the message_ts of a deployment button message.

    When user sends a new message, we can use this to invalidate the old buttons.

    Args:
        conversation_id: Ticket code
        channel_id: Slack channel ID
        message_ts: Message timestamp of the button message
    """
    data = {
        "channel_id": channel_id,
        "message_ts": message_ts,
        "created_at": datetime.utcnow().isoformat()
    }

    if settings.redis_enabled:
        key = f"{PREFIX_DEPLOYMENT_MSG}{conversation_id}"
        await RedisIntegration.set_json(key, data, ttl=DEPLOYMENT_MSG_TTL)
    else:
        _pending_deployment_messages[conversation_id] = data

    logger.info(f"Stored pending deployment message for {conversation_id}: {message_ts}")


async def get_pending_deployment_message(conversation_id: str) -> Optional[Dict[str, str]]:
    """Get the pending deployment message info.

    Args:
        conversation_id: Ticket code

    Returns:
        Dict with channel_id and message_ts, or None if not found
    """
    if settings.redis_enabled:
        key = f"{PREFIX_DEPLOYMENT_MSG}{conversation_id}"
        return await RedisIntegration.get_json(key)
    else:
        return _pending_deployment_messages.get(conversation_id)


async def clear_pending_deployment_message(conversation_id: str) -> None:
    """Clear the pending deployment message after PR created or cancelled.

    Args:
        conversation_id: Ticket code
    """
    if settings.redis_enabled:
        key = f"{PREFIX_DEPLOYMENT_MSG}{conversation_id}"
        await RedisIntegration.delete(key)
    else:
        if conversation_id in _pending_deployment_messages:
            del _pending_deployment_messages[conversation_id]

    logger.info(f"Cleared pending deployment message for {conversation_id}")


# ============================================================
# PENDING PLACEMENT MESSAGES
# ============================================================

async def store_pending_placement_message(
    conversation_id: str,
    channel_id: str,
    message_ts: str
) -> None:
    """Store the message_ts of a placement parameter button message.

    When user sends a new message during placement collection, we can use this
    to invalidate the old buttons.

    Args:
        conversation_id: Ticket code
        channel_id: Slack channel ID
        message_ts: Message timestamp of the button message
    """
    data = {
        "channel_id": channel_id,
        "message_ts": message_ts,
        "created_at": datetime.utcnow().isoformat()
    }

    if settings.redis_enabled:
        key = f"{PREFIX_PLACEMENT_MSG}{conversation_id}"
        await RedisIntegration.set_json(key, data, ttl=PLACEMENT_MSG_TTL)
    else:
        _pending_placement_messages[conversation_id] = data

    logger.info(f"Stored pending placement message for {conversation_id}: {message_ts}")


async def get_pending_placement_message(conversation_id: str) -> Optional[Dict[str, str]]:
    """Get the pending placement parameter message info.

    Args:
        conversation_id: Ticket code

    Returns:
        Dict with channel_id and message_ts, or None if not found
    """
    if settings.redis_enabled:
        key = f"{PREFIX_PLACEMENT_MSG}{conversation_id}"
        return await RedisIntegration.get_json(key)
    else:
        return _pending_placement_messages.get(conversation_id)


async def clear_pending_placement_message(conversation_id: str) -> None:
    """Clear the pending placement message after selection or invalidation.

    Args:
        conversation_id: Ticket code
    """
    if settings.redis_enabled:
        key = f"{PREFIX_PLACEMENT_MSG}{conversation_id}"
        await RedisIntegration.delete(key)
    else:
        if conversation_id in _pending_placement_messages:
            del _pending_placement_messages[conversation_id]

    logger.info(f"Cleared pending placement message for {conversation_id}")


# ============================================================
# PENDING REMINDER MESSAGES
# ============================================================

async def store_pending_reminder_message(
    conversation_id: str,
    channel_id: str,
    message_ts: str
) -> None:
    """Store the message_ts of a reminder message.

    When user makes a selection, we can use this to delete the reminder.

    Args:
        conversation_id: Ticket code
        channel_id: Slack channel ID
        message_ts: Message timestamp of the reminder message
    """
    data = {
        "channel_id": channel_id,
        "message_ts": message_ts
    }

    if settings.redis_enabled:
        key = f"{PREFIX_REMINDER_MSG}{conversation_id}"
        await RedisIntegration.set_json(key, data, ttl=REMINDER_MSG_TTL)
    else:
        _pending_reminder_messages[conversation_id] = data

    logger.info(f"Stored pending reminder message for {conversation_id}: {message_ts}")


async def get_pending_reminder_message(conversation_id: str) -> Optional[Dict[str, str]]:
    """Get the pending reminder message info.

    Args:
        conversation_id: Ticket code

    Returns:
        Dict with channel_id and message_ts, or None if not found
    """
    if settings.redis_enabled:
        key = f"{PREFIX_REMINDER_MSG}{conversation_id}"
        return await RedisIntegration.get_json(key)
    else:
        return _pending_reminder_messages.get(conversation_id)


async def clear_pending_reminder_message(conversation_id: str) -> None:
    """Clear the pending reminder message after deletion.

    Args:
        conversation_id: Ticket code
    """
    if settings.redis_enabled:
        key = f"{PREFIX_REMINDER_MSG}{conversation_id}"
        await RedisIntegration.delete(key)
    else:
        if conversation_id in _pending_reminder_messages:
            del _pending_reminder_messages[conversation_id]

    logger.info(f"Cleared pending reminder message for {conversation_id}")


# ============================================================
# PENDING WAIT MESSAGES
# ============================================================

async def store_pending_wait_message(
    conversation_id: str,
    channel_id: str,
    message_ts: str
) -> None:
    """Store the message_ts of a 'please wait' message.

    When the lock is released, we can use this to delete the wait message.

    Args:
        conversation_id: Ticket code
        channel_id: Slack channel ID
        message_ts: Message timestamp of the wait message
    """
    data = {
        "channel_id": channel_id,
        "message_ts": message_ts
    }

    if settings.redis_enabled:
        key = f"{PREFIX_WAIT_MSG}{conversation_id}"
        await RedisIntegration.set_json(key, data, ttl=WAIT_MSG_TTL)
    else:
        _pending_wait_messages[conversation_id] = data

    logger.info(f"Stored pending wait message for {conversation_id}: {message_ts}")


async def get_pending_wait_message(conversation_id: str) -> Optional[Dict[str, str]]:
    """Get the pending wait message info.

    Args:
        conversation_id: Ticket code

    Returns:
        Dict with channel_id and message_ts, or None if not found
    """
    if settings.redis_enabled:
        key = f"{PREFIX_WAIT_MSG}{conversation_id}"
        return await RedisIntegration.get_json(key)
    else:
        return _pending_wait_messages.get(conversation_id)


async def clear_pending_wait_message(conversation_id: str) -> None:
    """Clear the pending wait message after deletion.

    Args:
        conversation_id: Ticket code
    """
    if settings.redis_enabled:
        key = f"{PREFIX_WAIT_MSG}{conversation_id}"
        await RedisIntegration.delete(key)
    else:
        if conversation_id in _pending_wait_messages:
            del _pending_wait_messages[conversation_id]

    logger.info(f"Cleared pending wait message for {conversation_id}")


# ============================================================
# CONVERSATION LOCKS
# ============================================================

async def acquire_conversation_lock(conversation_id: str, operation: str = "processing") -> bool:
    """Attempt to acquire a lock for a conversation.

    Used to prevent concurrent processing of messages in the same conversation.
    Only one operation (LLM call, placement selection, etc.) can run at a time.

    Args:
        conversation_id: Ticket code
        operation: Description of the operation (for logging/display)

    Returns:
        True if lock acquired, False if conversation is already locked
    """
    if settings.redis_enabled:
        key = f"{PREFIX_LOCK}{conversation_id}"
        # Use Redis distributed lock with SET NX EX
        success = await RedisIntegration.acquire_lock(key, ttl=LOCK_TTL)

        if success:
            logger.info(f"Acquired lock for {conversation_id}: {operation}")
        else:
            logger.info(f"Conversation {conversation_id} is already locked")
        return success
    else:
        # In-memory fallback
        if conversation_id in _conversation_locks:
            logger.info(f"Conversation {conversation_id} is locked for: {_conversation_locks[conversation_id]['operation']}")
            return False

        _conversation_locks[conversation_id] = {
            "operation": operation,
            "locked_at": datetime.utcnow()
        }
        logger.info(f"Acquired lock for {conversation_id}: {operation}")
        return True


async def release_conversation_lock(conversation_id: str) -> None:
    """Release the lock for a conversation.

    Args:
        conversation_id: Ticket code
    """
    if settings.redis_enabled:
        key = f"{PREFIX_LOCK}{conversation_id}"
        await RedisIntegration.delete(key)
    else:
        if conversation_id in _conversation_locks:
            del _conversation_locks[conversation_id]

    logger.info(f"Released lock for {conversation_id}")


async def is_conversation_locked(conversation_id: str) -> bool:
    """Check if a conversation is currently locked.

    Args:
        conversation_id: Ticket code

    Returns:
        True if locked, False otherwise
    """
    if settings.redis_enabled:
        key = f"{PREFIX_LOCK}{conversation_id}"
        return await RedisIntegration.exists(key)
    else:
        return conversation_id in _conversation_locks


async def get_conversation_lock_info(conversation_id: str) -> Optional[Dict[str, Any]]:
    """Get information about a conversation lock.

    Args:
        conversation_id: Ticket code

    Returns:
        Lock info dict or None if not locked
    """
    if settings.redis_enabled:
        key = f"{PREFIX_LOCK}{conversation_id}"
        # For Redis mode, we just store "locked" value, not detailed info
        # Return a dict if locked, None if not
        if await RedisIntegration.exists(key):
            return {"operation": "processing", "locked_at": datetime.utcnow().isoformat()}
        return None
    else:
        return _conversation_locks.get(conversation_id)


# ============================================================
# PREVIEW STATE
# ============================================================

async def store_preview_state(
    conversation_id: str,
    queue_item_id: int,
    queue_code: str,
    infrastructure_code: Optional[str]
) -> None:
    """Store preview state for reuse by Create PR.

    After preview generates infrastructure and queue records, store them
    so Create PR can reuse instead of creating duplicates.

    Args:
        conversation_id: Ticket code
        queue_item_id: ID of the queue item created during preview
        queue_code: Code of the queue item
        infrastructure_code: Code of infrastructure_mst record (None for Kong routes)
    """
    data = {
        "queue_item_id": queue_item_id,
        "queue_code": queue_code,
        "infrastructure_code": infrastructure_code,
        "previewed": True,
        "created_at": datetime.utcnow().isoformat()
    }

    if settings.redis_enabled:
        key = f"{PREFIX_PREVIEW}{conversation_id}"
        await RedisIntegration.set_json(key, data, ttl=PREVIEW_STATE_TTL)
    else:
        _preview_states[conversation_id] = data

    logger.info(f"Stored preview state for {conversation_id}: queue_item_id={queue_item_id}, queue_code={queue_code}")


async def get_preview_state(conversation_id: str) -> Optional[Dict[str, Any]]:
    """Get preview state if exists.

    Args:
        conversation_id: Ticket code

    Returns:
        Dict with queue_item_id, infrastructure_code, previewed flag, or None
    """
    if settings.redis_enabled:
        key = f"{PREFIX_PREVIEW}{conversation_id}"
        return await RedisIntegration.get_json(key)
    else:
        return _preview_states.get(conversation_id)


async def clear_preview_state(conversation_id: str) -> None:
    """Clear preview state after PR created or cancelled.

    Args:
        conversation_id: Ticket code
    """
    if settings.redis_enabled:
        key = f"{PREFIX_PREVIEW}{conversation_id}"
        await RedisIntegration.delete(key)
    else:
        if conversation_id in _preview_states:
            del _preview_states[conversation_id]

    logger.info(f"Cleared preview state for {conversation_id}")
