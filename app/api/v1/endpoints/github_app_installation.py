"""
GitHub App Installation Management Endpoints

Provides endpoints for:
- Checking installation status for a tenant
- Getting the install URL to redirect tenants
- Linking an installation to a tenant (frontend callback)
"""

import logging
from typing import Tuple

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.github_app_installation_service import GitHubAppInstallationService

router = APIRouter()
logger = logging.getLogger(__name__)


class LinkInstallationRequest(BaseModel):
    installation_id: str


@router.get("/installations", summary="Get GitHub App installations for tenant")
async def get_installations(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all active GitHub App installations for the current tenant.

    Returns list of installations with github_org and status.
    """
    user, tenant = user_and_tenant
    service = GitHubAppInstallationService(db)
    installations = await service.get_installations_for_tenant(tenant.code)
    return {"installations": installations, "total_count": len(installations)}


@router.get("/install-url", summary="Get GitHub App install URL")
async def get_install_url(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Get the URL to redirect the tenant to install the GitHub App.

    The URL includes a state parameter with the tenant_code so the
    frontend callback can link the installation back to the tenant.
    """
    user, tenant = user_and_tenant
    service = GitHubAppInstallationService(db)
    return {"install_url": service.get_install_url(tenant.code)}


@router.post("/link-installation", summary="Link GitHub App installation to tenant")
async def link_installation(
    request: LinkInstallationRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Link a GitHub App installation to the current tenant.

    Called by the frontend after the GitHub App install callback redirects
    back with the installation_id and state (tenant_code).
    """
    user, tenant = user_and_tenant
    service = GitHubAppInstallationService(db)
    result = await service.link_installation_to_tenant(
        installation_id=request.installation_id,
        tenant_code=tenant.code,
    )

    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"Installation {request.installation_id} not found. "
                   f"The GitHub App may not have been installed yet."
        )

    return {"status": "success", **result}
