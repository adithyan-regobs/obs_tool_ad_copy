"""list_supported_resources tool — proxy to chatbot's /services endpoint.

The chatbot is the source of truth for the form catalog. We pass the
authenticated user's tenant_code so the chatbot returns the tenant-specific
form list (default vs vance vs aspora). Read-only.
"""

import logging

from app.mcp_servers.devlift_mcp.auth import get_auth_context
from app.mcp_servers.devlift_mcp.chatbot_client import get_services

logger = logging.getLogger(__name__)


async def list_supported_resources_impl() -> dict:
    auth_ctx = await get_auth_context()
    if auth_ctx is None:
        return {
            "status": "error",
            "message": (
                "Authentication required. Run the 'authenticate' tool first "
                "to log in via your browser."
            ),
        }

    try:
        services = await get_services(tenant_code=auth_ctx.tenant_code)
    except Exception as e:
        logger.exception("list_supported_resources: chatbot /services call failed")
        return {
            "status": "error",
            "message": f"Failed to load resource catalog from chatbot: {e}",
        }

    # Chatbot's /services returns a list of {form_id, title, ...}. Pass it
    # through with a thin wrapper so the LLM knows which forms are available.
    return {
        "resources": [
            {
                "form_id": s.get("form_id"),
                "label": s.get("title") or s.get("form_id"),
                "description": s.get("description") or "",
            }
            for s in services
            if s.get("form_id")
        ],
    }
