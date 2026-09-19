"""Authentication context for the DevLift MCP server.

With OAuth 2.1, FastMCP handles token validation internally via
RequireAuthMiddleware + BearerAuthBackend. This module provides the
bridge from the MCP SDK's validated AccessToken to the AuthContext
that the dispatcher expects.

Flow:
    1. Claude Code sends Bearer token on every request
    2. FastMCP's middleware calls load_access_token() -> validates
    3. MCP SDK stores the AccessToken in its contextvar
    4. Tool call reaches the dispatcher
    5. Dispatcher calls get_auth_context() (this module)
    6. We read the MCP SDK's contextvar -> look up user info from Redis
    7. Return AuthContext with user_code, tenant_code, user_email
"""

import logging
from dataclasses import dataclass
from typing import Optional

from mcp.server.auth.middleware.auth_context import (
    get_access_token as mcp_get_access_token,
)

from app.mcp_servers.devlift_mcp.oauth_provider import get_user_from_access_token

logger = logging.getLogger(__name__)


# ============================================================
# Auth context (same shape as before — dispatcher unchanged)
# ============================================================


@dataclass
class AuthContext:
    """Resolved identity for a single MCP request."""

    user_code: str
    tenant_code: str
    user_email: str
    clerk_user_id: str


async def get_auth_context() -> Optional[AuthContext]:
    """Return the auth context for the current request, or None if absent.

    Reads the MCP SDK's contextvar (set by FastMCP's auth middleware),
    then looks up the user identity from Redis by token hash.

    All callers are async, so this is safe to await.
    """
    access_token = mcp_get_access_token()
    if access_token is None:
        return None

    user_info = await get_user_from_access_token(access_token.token)
    if not user_info:
        logger.warning("mcp_oauth: valid token but no user info in Redis")
        return None

    return AuthContext(
        user_code=user_info["user_code"],
        tenant_code=user_info["tenant_code"],
        user_email=user_info["user_email"],
        clerk_user_id="",  # Not available via OAuth tokens
    )


async def require_auth_context() -> AuthContext:
    """Return the auth context, raising RuntimeError if not authenticated.

    Use this from the dispatcher when you've already established the request is
    authenticated (i.e. you reached the dispatcher via an MCP tool call).
    """
    ctx = await get_auth_context()
    if ctx is None:
        raise RuntimeError(
            "AuthContext not set — the MCP OAuth middleware did not run "
            "before this dispatcher call, or the token has no user info in Redis."
        )
    return ctx
