"""MCP OAuth 2.1 provider backed entirely by Redis.

Implements the MCP SDK's OAuthAuthorizationServerProvider protocol so FastMCP
auto-creates all OAuth endpoints (/.well-known, /authorize, /token, /register,
/revoke). Claude Code handles the client-side OAuth flow — browser open, token
storage, auto-refresh.

Redis key layout (all under mcp:oauth: prefix):
    client:{client_id}          — registered OAuth clients
    auth_req:{auth_req_id}      — temporary auth params during consent flow
    code:{sha256(code)}         — authorization codes (single-use)
    access:{sha256(token)}      — access tokens
    refresh:{sha256(token)}     — refresh tokens

See MCP_OAUTH_AUTH_TODO.md for the full design.
"""

import hashlib
import logging
import secrets
import time
from typing import Optional

from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.core.config import settings
from app.integrations.redis_integration import RedisIntegration

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

PREFIX = "mcp:oauth:"

# These TTLs come from config but we read them at call time so tests
# can override settings. The constants here are just the Redis key prefixes.
_KEY_CLIENT = f"{PREFIX}client:"
_KEY_AUTH_REQ = f"{PREFIX}auth_req:"
_KEY_CODE = f"{PREFIX}code:"
_KEY_ACCESS = f"{PREFIX}access:"
_KEY_REFRESH = f"{PREFIX}refresh:"


def _hash(value: str) -> str:
    """SHA-256 hash of a token/code string — used as the Redis lookup key."""
    return hashlib.sha256(value.encode()).hexdigest()


# ============================================================
# Provider
# ============================================================


class DevLiftOAuthProvider:
    """MCP OAuth 2.1 authorization server provider backed by Redis.

    Every method maps to one step in the OAuth 2.1 flow:
        register_client / get_client   — Dynamic Client Registration (RFC 7591)
        authorize                      — Authorization request → redirect to frontend
        load_authorization_code        — Load stored auth code for exchange
        exchange_authorization_code    — Auth code → access + refresh tokens
        load_access_token              — Validate Bearer token on every MCP request
        load_refresh_token             — Load stored refresh token
        exchange_refresh_token         — Refresh token → new token pair (rotation)
        revoke_token                   — Delete token from Redis
    """

    # ── Client Registration ──────────────────────────

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        data = await RedisIntegration.get_json(f"{_KEY_CLIENT}{client_id}")
        if not data:
            return None
        return OAuthClientInformationFull(**data)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Serialize with Pydantic, converting AnyUrl fields to strings
        data = client_info.model_dump(mode="json")
        await RedisIntegration.set_json(
            f"{_KEY_CLIENT}{client_info.client_id}",
            data,
            # No TTL — clients persist until manually revoked
        )
        logger.info("mcp_oauth: registered client %s", client_info.client_id)

    # ── Authorization ────────────────────────────────

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Store OAuth params in Redis and redirect to the frontend consent page.

        The frontend shows the Clerk login (if needed) + consent screen.
        After the user clicks "Authorize", the frontend calls
        POST /api/v1/auth/mcp/complete which generates the auth code
        and redirects back to Claude Code.
        """
        auth_req_id = secrets.token_urlsafe(32)
        await RedisIntegration.set_json(
            f"{_KEY_AUTH_REQ}{auth_req_id}",
            {
                "client_id": client.client_id,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "state": params.state,
                "scopes": params.scopes or [],
                "resource": params.resource,
            },
            ttl=settings.mcp_oauth_auth_code_ttl,
        )
        frontend_url = settings.mcp_oauth_frontend_authorize_url
        logger.info(
            "mcp_oauth: authorize redirect for client=%s auth_req=%s",
            client.client_id,
            auth_req_id,
        )
        return f"{frontend_url}?auth_req_id={auth_req_id}"

    # ── Authorization Code ───────────────────────────

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> Optional[AuthorizationCode]:
        key = f"{_KEY_CODE}{_hash(authorization_code)}"
        data = await RedisIntegration.get_json(key)
        if not data:
            return None
        if data.get("client_id") != client.client_id:
            logger.warning(
                "mcp_oauth: auth code client mismatch: expected=%s got=%s",
                client.client_id,
                data.get("client_id"),
            )
            return None
        return AuthorizationCode(
            code=data["code"],
            scopes=data.get("scopes", []),
            expires_at=data["expires_at"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data.get(
                "redirect_uri_provided_explicitly", True
            ),
            resource=data.get("resource"),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Load full data (includes user identity)
        code_key = f"{_KEY_CODE}{_hash(authorization_code.code)}"
        code_data = await RedisIntegration.get_json(code_key)

        # Delete auth code — single-use
        await RedisIntegration.delete(code_key)

        if not code_data:
            raise ValueError("Authorization code not found or already used")

        # Generate token pair
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        now = int(time.time())
        access_ttl = settings.mcp_oauth_access_token_ttl
        refresh_ttl = settings.mcp_oauth_refresh_token_ttl

        user_info = {
            "user_code": code_data["user_code"],
            "tenant_code": code_data["tenant_code"],
            "user_email": code_data["user_email"],
        }

        # Store access token
        await RedisIntegration.set_json(
            f"{_KEY_ACCESS}{_hash(access_token)}",
            {
                "token": access_token,
                "client_id": client.client_id,
                **user_info,
                "scopes": authorization_code.scopes,
                "expires_at": now + access_ttl,
                "resource": authorization_code.resource,
            },
            ttl=access_ttl,
        )

        # Store refresh token
        await RedisIntegration.set_json(
            f"{_KEY_REFRESH}{_hash(refresh_token)}",
            {
                "token": refresh_token,
                "client_id": client.client_id,
                **user_info,
                "scopes": authorization_code.scopes,
                "expires_at": now + refresh_ttl,
            },
            ttl=refresh_ttl,
        )

        logger.info(
            "mcp_oauth: issued tokens for user=%s client=%s",
            code_data.get("user_code"),
            client.client_id,
        )

        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=access_ttl,
            token_type="Bearer",
        )

    # ── Access Token ─────────────────────────────────

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        data = await RedisIntegration.get_json(f"{_KEY_ACCESS}{_hash(token)}")
        if not data:
            return None
        return AccessToken(
            token=data["token"],
            client_id=data["client_id"],
            scopes=data.get("scopes", []),
            expires_at=data.get("expires_at"),
            resource=data.get("resource"),
        )

    # ── Refresh Token ────────────────────────────────

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        token: str,
    ) -> Optional[RefreshToken]:
        data = await RedisIntegration.get_json(f"{_KEY_REFRESH}{_hash(token)}")
        if not data:
            return None
        if data.get("client_id") != client.client_id:
            return None
        return RefreshToken(
            token=data["token"],
            client_id=data["client_id"],
            scopes=data.get("scopes", []),
            expires_at=data.get("expires_at"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Load user info from old refresh token
        old_key = f"{_KEY_REFRESH}{_hash(refresh_token.token)}"
        old_data = await RedisIntegration.get_json(old_key)

        # Revoke old refresh token (rotation)
        await RedisIntegration.delete(old_key)

        if not old_data:
            raise ValueError("Refresh token not found or already revoked")

        # Generate new pair
        new_access = secrets.token_urlsafe(48)
        new_refresh = secrets.token_urlsafe(48)
        now = int(time.time())
        access_ttl = settings.mcp_oauth_access_token_ttl
        refresh_ttl = settings.mcp_oauth_refresh_token_ttl
        effective_scopes = scopes if scopes else refresh_token.scopes

        user_info = {
            "user_code": old_data["user_code"],
            "tenant_code": old_data["tenant_code"],
            "user_email": old_data["user_email"],
        }

        await RedisIntegration.set_json(
            f"{_KEY_ACCESS}{_hash(new_access)}",
            {
                "token": new_access,
                "client_id": client.client_id,
                **user_info,
                "scopes": effective_scopes,
                "expires_at": now + access_ttl,
            },
            ttl=access_ttl,
        )

        await RedisIntegration.set_json(
            f"{_KEY_REFRESH}{_hash(new_refresh)}",
            {
                "token": new_refresh,
                "client_id": client.client_id,
                **user_info,
                "scopes": effective_scopes,
                "expires_at": now + refresh_ttl,
            },
            ttl=refresh_ttl,
        )

        logger.info(
            "mcp_oauth: refreshed tokens for user=%s client=%s",
            old_data.get("user_code"),
            client.client_id,
        )

        return OAuthToken(
            access_token=new_access,
            refresh_token=new_refresh,
            expires_in=access_ttl,
            token_type="Bearer",
        )

    # ── Revocation ───────────────────────────────────

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        if isinstance(token, AccessToken):
            await RedisIntegration.delete(f"{_KEY_ACCESS}{_hash(token.token)}")
        elif isinstance(token, RefreshToken):
            await RedisIntegration.delete(f"{_KEY_REFRESH}{_hash(token.token)}")
        logger.info("mcp_oauth: revoked token type=%s", type(token).__name__)


# ============================================================
# Auth-request helpers (used by the /api/v1/auth/mcp/complete endpoint)
# ============================================================


async def load_auth_request(auth_req_id: str) -> Optional[dict]:
    """Load and return the stored OAuth params for a pending authorization."""
    return await RedisIntegration.get_json(f"{_KEY_AUTH_REQ}{auth_req_id}")


async def get_client_name(client_id: str) -> Optional[str]:
    """Return the human-readable client_name a client registered with (RFC 7591),
    so the consent UI can name the actual app being authorized (e.g. "devlift-cli")
    instead of hardcoding one. Returns None if the client or name is unknown.
    """
    if not client_id:
        return None
    data = await RedisIntegration.get_json(f"{_KEY_CLIENT}{client_id}")
    if not data:
        return None
    return data.get("client_name")


async def consume_auth_request_and_create_code(
    auth_req_id: str,
    user_code: str,
    tenant_code: str,
    user_email: str,
) -> Optional[dict]:
    """Complete the authorization: consume the auth request, create an auth code.

    Returns {"code": ..., "redirect_url": ...} on success, None if the auth
    request doesn't exist or was already consumed.
    """
    key = f"{_KEY_AUTH_REQ}{auth_req_id}"
    auth_req = await RedisIntegration.get_json(key)
    if not auth_req:
        return None

    # Delete auth request (single-use)
    await RedisIntegration.delete(key)

    # Generate authorization code
    code = secrets.token_urlsafe(32)
    now = int(time.time())
    code_ttl = settings.mcp_oauth_auth_code_ttl

    # Store auth code in Redis with user identity
    await RedisIntegration.set_json(
        f"{_KEY_CODE}{_hash(code)}",
        {
            "code": code,
            "client_id": auth_req["client_id"],
            "user_code": user_code,
            "tenant_code": tenant_code,
            "user_email": user_email,
            "code_challenge": auth_req["code_challenge"],
            "redirect_uri": auth_req["redirect_uri"],
            "redirect_uri_provided_explicitly": auth_req.get(
                "redirect_uri_provided_explicitly", True
            ),
            "scopes": auth_req.get("scopes", []),
            "resource": auth_req.get("resource"),
            "expires_at": now + code_ttl,
        },
        ttl=code_ttl,
    )

    # Build redirect URL back to Claude Code
    redirect_uri = auth_req["redirect_uri"]
    state = auth_req.get("state", "")
    sep = "&" if "?" in redirect_uri else "?"
    redirect_url = f"{redirect_uri}{sep}code={code}"
    if state:
        redirect_url += f"&state={state}"

    logger.info(
        "mcp_oauth: created auth code for user=%s auth_req=%s",
        user_code,
        auth_req_id,
    )

    return {"code": code, "redirect_url": redirect_url}


async def get_user_from_access_token(token_str: str) -> Optional[dict]:
    """Look up the user identity from a validated access token.

    Returns {"user_code", "tenant_code", "user_email"} or None.
    Used by the auth context bridge in auth.py.
    """
    data = await RedisIntegration.get_json(f"{_KEY_ACCESS}{_hash(token_str)}")
    if not data:
        return None
    return {
        "user_code": data["user_code"],
        "tenant_code": data["tenant_code"],
        "user_email": data["user_email"],
    }
