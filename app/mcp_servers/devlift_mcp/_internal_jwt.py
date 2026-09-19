"""HS256 internal JWT minted by the MCP server for chatbot calls.

The chatbot forwards this token verbatim on its outgoing requests back into
obs_tool's API endpoints (e.g. validate-duplicate-bucket, dropdown sources).

Token claims:
    user_code, tenant_code, user_email  — identity copied from AuthContext
    type: "mcp_internal"                 — marker so the receiving validator
                                            can distinguish from Clerk RS256
    iat, exp                              — short-lived (default 15 min)

NOTE: For receivers (obs_tool API endpoints) to actually accept this token,
their JWT validator must be extended with an HS256 path that recognizes
type="mcp_internal" and resolves the user from the claims. That work lives
on the API side, not here.
"""

import logging
import time

import jwt

from app.core.config import settings
from app.mcp_servers.devlift_mcp.auth import AuthContext

logger = logging.getLogger(__name__)


def mint_internal_jwt(auth_ctx: AuthContext) -> str:
    """Sign a short-lived HS256 JWT carrying the caller's identity."""
    secret = settings.mcp_internal_jwt_secret
    if not secret:
        logger.warning("mcp_internal_jwt: MCP_INTERNAL_JWT_SECRET not set")
        return ""

    now = int(time.time())
    payload = {
        "type": "mcp_internal",
        "user_code": auth_ctx.user_code,
        "tenant_code": auth_ctx.tenant_code,
        "user_email": auth_ctx.user_email,
        "iat": now,
        "exp": now + settings.mcp_internal_jwt_ttl,
        "iss": "devlift-mcp",
    }
    return jwt.encode(payload, secret, algorithm="HS256")
