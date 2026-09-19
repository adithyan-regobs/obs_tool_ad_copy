"""MCP OAuth 2.1 authorization completion endpoint.

Called by the frontend consent page after the user clicks "Authorize".
The frontend sends the auth_req_id (from the URL) along with its Clerk JWT.
We validate the JWT, generate an authorization code, and return the redirect
URL that sends the user back to Claude Code with the code.
"""

from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.dependencies import get_current_user_and_tenant
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.mcp_servers.devlift_mcp.oauth_provider import (
    consume_auth_request_and_create_code,
    get_client_name,
    load_auth_request,
)

router = APIRouter(prefix="/auth/mcp", tags=["MCP Authentication"])


# ============================================================
# Request / Response schemas
# ============================================================


class McpAuthCompleteRequest(BaseModel):
    auth_req_id: str


class McpAuthCompleteResponse(BaseModel):
    redirect_url: str


class McpAuthCheckResponse(BaseModel):
    valid: bool
    client_id: str | None = None
    # Human-readable name the client registered with (e.g. "devlift-cli"), so the
    # consent page can name the actual app being authorized instead of a hardcoded
    # string. None if the client didn't provide a name.
    client_name: str | None = None


# ============================================================
# Endpoints
# ============================================================


@router.get(
    "/check",
    response_model=McpAuthCheckResponse,
    summary="Check if an auth request is still valid (frontend polls this)",
)
async def check_auth_request(auth_req_id: str) -> McpAuthCheckResponse:
    """Check if the auth_req_id exists and hasn't expired.

    The frontend can call this without authentication to verify the
    auth_req_id in the URL is valid before showing the consent screen.
    """
    auth_req = await load_auth_request(auth_req_id)
    if not auth_req:
        return McpAuthCheckResponse(valid=False)
    client_id = auth_req.get("client_id")
    client_name = await get_client_name(client_id)
    return McpAuthCheckResponse(
        valid=True,
        client_id=client_id,
        client_name=client_name,
    )


@router.post(
    "/complete",
    response_model=McpAuthCompleteResponse,
    summary="Complete MCP authorization (called by frontend after user consent)",
)
async def complete_mcp_authorization(
    request: McpAuthCompleteRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(
        get_current_user_and_tenant
    ),
) -> McpAuthCompleteResponse:
    """Complete the MCP OAuth authorization flow.

    The frontend calls this after the user clicks "Authorize" on the consent
    page. The Clerk JWT in the Authorization header is validated by the
    existing get_current_user_and_tenant dependency — same as every other
    protected endpoint in the API.

    Steps:
        1. Validate Clerk JWT → get user + tenant (handled by dependency)
        2. Load the stored OAuth params from Redis by auth_req_id
        3. Generate an authorization code with user identity embedded
        4. Delete the auth request (single-use)
        5. Return the redirect URL → frontend does window.location.href
    """
    user, tenant = user_and_tenant

    result = await consume_auth_request_and_create_code(
        auth_req_id=request.auth_req_id,
        user_code=user.code,
        tenant_code=tenant.code,
        user_email=user.email_id,
    )

    if result is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Authorization request not found or expired. "
                "Please restart the authentication flow in Claude Code."
            ),
        )

    return McpAuthCompleteResponse(redirect_url=result["redirect_url"])
