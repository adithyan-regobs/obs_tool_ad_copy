"""
GitHub App Token Resolution Helpers

Resolves GitHub App installation tokens for any org/tenant
by looking up the installation_id from the database.
Replaces all PAT usage across the codebase.
"""

import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.integrations.github_auth import GitHubAppAuth
from app.repository.github_app_installation_mst_repository import GitHubAppInstallationMstRepository

logger = logging.getLogger(__name__)

# Module-level auth instance (reuses JWT + token cache across calls)
_github_app_auth = GitHubAppAuth(
    app_id=settings.github_app_id,
    private_key=settings.github_app_private_key,
    base_url=settings.github_base_url,
)

# Approval app auth instance — separate GitHub App with PR review write permission
_github_approval_app_auth = GitHubAppAuth(
    app_id=settings.github_approval_app_id,
    private_key=settings.github_approval_app_private_key,
    base_url=settings.github_base_url,
)


async def get_token(installation_id: str) -> str:
    """Get token for a known installation ID."""
    return await _github_app_auth.get_installation_token(installation_id)


async def get_token_for_org(github_org: str, db: AsyncSession) -> str:
    """Resolve installation_id from DB by org name, then get token."""
    repo = GitHubAppInstallationMstRepository(db)
    installation = await repo.get_by(
        github_org=github_org, is_active=True, is_deleted=False
    )
    if not installation:
        raise ValueError(
            f"No GitHub App installation found for org '{github_org}'. "
            f"Install the app at: https://github.com/apps/{settings.github_app_slug}/installations/new"
        )
    return await _github_app_auth.get_installation_token(installation.installation_id)


async def get_approval_token() -> str:
    """Get token for the approval app using the installation ID from env.

    Temporary: installation_id is hardcoded per-env. Move to DB per-tenant later.
    """
    from app.core.config import settings as _s
    if not _s.github_approval_app_installation_id:
        raise ValueError(
            "GITHUB_APPROVAL_APP_INSTALLATION_ID is not configured. "
            "Set it in env or disable approval via GITHUB_APPROVAL_ENABLED=false."
        )
    return await _github_approval_app_auth.get_installation_token(
        _s.github_approval_app_installation_id
    )


async def get_token_for_tenant(tenant_code: str, db: AsyncSession) -> str:
    """Get token for a tenant's first active installation.

    For tenants with multiple orgs, use get_token_for_org instead.
    """
    repo = GitHubAppInstallationMstRepository(db)
    installations = await repo.get_by_tenant_code(tenant_code)
    if not installations:
        raise ValueError(
            f"No GitHub App installation found for tenant '{tenant_code}'. "
            f"Install the app at: https://github.com/apps/{settings.github_app_slug}/installations/new"
        )
    return await _github_app_auth.get_installation_token(installations[0].installation_id)
