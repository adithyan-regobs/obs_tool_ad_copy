"""Thin HTTP client for the chat-bot-POC backend.

The chatbot is the source of truth for resource metadata, field validation,
and conversational data collection. The MCP server proxies user messages to
chatbot's `/chat` endpoint and reads the resolved result from `/sessions/select`
when it's time to provision.
"""

import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.integrations.redis_integration import RedisIntegration

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(90.0, connect=5.0)

# Cache the resolved chatbot result (attribute_parameters + placement_parameters)
# per ticket_code so trigger_resource_deployment can look it up without a fresh
# chat round-trip. Lifetime is bounded — long enough for the LLM to swing from
# "isReady=true" to actually triggering, short enough not to hold stale data.
_CHATBOT_RESULT_PREFIX = "mcp:chatbot_result:"
_CHATBOT_RESULT_TTL_SECONDS = 6 * 3600  # 6 hours

# The placement an edit session opened on. Without it the save handler cannot
# tell an edit from a fresh create: it resolves the target row from the
# ANSWERS, so a session opened on stage that answers "prod" simply resolves
# somewhere else — creating a prod configuration, or writing the draft onto an
# existing one. Both report success; neither is the edit the user asked for.
_EDIT_ORIGIN_PREFIX = "mcp:edit_origin:"

# The language template applied to a create session, kept so it is offered
# once and so the closing summary can say which values came from it.
_TEMPLATE_APPLIED_PREFIX = "mcp:template_applied:"


def _result_key(user_code: str, ticket_code: str) -> str:
    return f"{_CHATBOT_RESULT_PREFIX}{user_code}:{ticket_code}"


def _edit_origin_key(user_code: str, ticket_code: str) -> str:
    return f"{_EDIT_ORIGIN_PREFIX}{user_code}:{ticket_code}"


async def cache_chatbot_result(user_code: str, ticket_code: str, payload: dict) -> bool:
    """Persist the resolved chatbot result for later retrieval by trigger."""
    return await RedisIntegration.set_json(
        _result_key(user_code, ticket_code),
        payload,
        ttl=_CHATBOT_RESULT_TTL_SECONDS,
    )


async def get_cached_chatbot_result(user_code: str, ticket_code: str) -> Optional[dict]:
    """Retrieve the cached chatbot result. Returns None if missing or expired."""
    blob = await RedisIntegration.get_json(_result_key(user_code, ticket_code))
    if isinstance(blob, dict):
        return blob
    return None


async def cache_edit_origin(user_code: str, ticket_code: str, origin: dict) -> bool:
    """Record the configuration an edit session was opened on."""
    return await RedisIntegration.set_json(
        _edit_origin_key(user_code, ticket_code),
        origin,
        ttl=_CHATBOT_RESULT_TTL_SECONDS,
    )


async def get_edit_origin(user_code: str, ticket_code: str) -> Optional[dict]:
    """The placement an edit session started from; None for a fresh create."""
    blob = await RedisIntegration.get_json(_edit_origin_key(user_code, ticket_code))
    if isinstance(blob, dict):
        return blob
    return None


def _template_key(user_code: str, ticket_code: str) -> str:
    return f"{_TEMPLATE_APPLIED_PREFIX}{user_code}:{ticket_code}"


async def cache_template_applied(user_code: str, ticket_code: str, applied: dict) -> bool:
    """Record the language template poured into this session.

    Two jobs: it stops the template being offered twice on the same ticket,
    and it is what the closing summary diffs against — "23 from the Go
    template, you changed port" needs to know what the template said.
    """
    return await RedisIntegration.set_json(
        _template_key(user_code, ticket_code), applied, ttl=_CHATBOT_RESULT_TTL_SECONDS
    )


async def mark_session_prefilled(user_code: str, ticket_code: str, source: str) -> bool:
    """Claim the template slot for a session that opens already filled in.

    The language template is for CREATING a service. An edit or a clone opens
    with every field answered — from the live service, or from the one being
    copied — so the language is known on turn one and the offer would fire at
    once, proposing to replace a running service's configuration with
    defaults. Writing the record here means the offer sees the ticket as
    already served. `answers` stays empty, so the closing summary reports no
    template either: none was used.
    """
    return await cache_template_applied(
        user_code, ticket_code,
        {"language": None, "answers": {}, "applied": False, "source": source},
    )


async def get_template_applied(user_code: str, ticket_code: str) -> Optional[dict]:
    """{language, answers} for a session the template was poured into, else None."""
    blob = await RedisIntegration.get_json(_template_key(user_code, ticket_code))
    if isinstance(blob, dict):
        return blob
    return None


def _base_url() -> str:
    return settings.new_chat_backend_url.rstrip("/")


async def post_chat(
    *,
    message: str,
    ticket_code: str,
    tenant_code: str,
    user_mst_code: str,
    jwt_token: str,
    answers: Optional[dict] = None,
    skip: Optional[list] = None,
) -> dict:
    """Send a user message to chatbot's /chat endpoint and return its response.

    `answers` ({field_id: value}) and `skip` ([field_id]) carry a whole
    dialog's worth of answers deterministically (the chatbot fills them
    without its extraction LLM); `message` is then only the transcript line.
    """
    url = f"{_base_url()}/chat"
    payload = {
        "message": message,
        "ticket_code": ticket_code,
        "tenant_code": tenant_code,
        "user_mst_code": user_mst_code,
        "jwt_token": jwt_token,
    }
    if answers:
        payload["answers"] = answers
    if skip:
        payload["skip"] = list(skip)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def post_select_session(
    *,
    ticket_code: str,
    tenant_code: str,
    user_mst_code: str,
) -> Optional[dict]:
    """Fetch the chatbot's stored session by ticket_code."""
    url = f"{_base_url()}/sessions/select"
    payload = {
        "ticket_code": ticket_code,
        "tenant_code": tenant_code,
        "user_mst_code": user_mst_code,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def post_select_form(
    *,
    form_id: str,
    ticket_code: str,
    tenant_code: str,
    user_mst_code: str,
    jwt_token: str,
    prefill: Optional[dict] = None,
    ask_optional: bool = False,
) -> dict:
    """Start a session on a specific form via chatbot's /select-service.

    With `prefill` (edit mode) the session is seeded with the service's
    current values and the chatbot asks nothing up front; the user's next
    `chat` messages say what changes.
    """
    url = f"{_base_url()}/select-service"
    payload = {
        "form_id": form_id,
        "ticket_code": ticket_code,
        "tenant_code": tenant_code,
        "user_mst_code": user_mst_code,
        "jwt_token": jwt_token,
    }
    if prefill:
        payload["prefill"] = prefill
    if ask_optional:
        # Prefill otherwise closes out every unfilled optional field; the kong
        # form prefills only the placement and must still ask for the card.
        payload["ask_optional"] = True
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def get_services(*, tenant_code: str) -> list[dict]:
    """List the forms/services chatbot exposes for this tenant."""
    url = f"{_base_url()}/services"
    payload = {"tenant_code": tenant_code}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
