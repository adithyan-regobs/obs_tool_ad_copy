"""
CLI context — who am I, and where is everything.

    GET /api/v1/cli/context

One authenticated call the devlift CLI makes after login (and for `whoami`):
the caller's identity and tenant, the secret service's base URL (the CLI posts
secret VALUES straight there, never through obs_tool or the chatbot), the
frontend URL for deep links, and feature flags so the CLI only offers what the
server currently supports. Only obs_tool can resolve the CLI's opaque token,
which is why this lives here rather than being baked into the binary.
"""

from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, Request
from pydantic import BaseModel, Field

from app.api.dependencies import get_current_user_and_tenant
from app.core.authz.security import AuthenticationOnly, SecureRouter, authenticated_user, mark_checked
from app.core.config import settings
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel

router = SecureRouter()

# Flipped per slice as the CLI flows land. The CLI gates its commands and its
# welcome line on these rather than on its own version.
FEATURES: Dict[str, bool] = {
    "assistant": True,      # "edit demo", "can I edit it", "status of demo" (EKS services)
    "approvals": True,      # REST approval verbs the CLI may call directly
    "edit_service": False,  # Configuration edits from the terminal (slice 1)
}


class CliContextResponse(BaseModel):
    user_code: str
    email: Optional[str] = None
    tenant_code: str
    tenant_name: Optional[str] = None
    secrets_api_base_url: str = Field(description="Base for /secrets|/configs/{sc}/save")
    frontend_base_url: Optional[str] = None
    features: Dict[str, bool]


@router.get(
    "/context",
    response_model=CliContextResponse,
    summary="Identity, service URLs and feature flags for the devlift CLI",
    access=AuthenticationOnly(reason="returns only the caller's own identity and public URLs"),
)
async def cli_context(
    request: Request,
    user: str = Depends(authenticated_user),
    user_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
) -> CliContextResponse:
    db_user, tenant = user_tenant
    mark_checked(request)
    return CliContextResponse(
        user_code=db_user.code,
        email=db_user.email_id,
        tenant_code=tenant.code,
        tenant_name=getattr(tenant, "name", None),
        secrets_api_base_url=settings.secret_service_url.rstrip("/") + "/api/v1",
        frontend_base_url=settings.frontend_base_url or None,
        features=dict(FEATURES),
    )
