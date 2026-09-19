"""
Tenant Configuration Helper

Reads per-tenant config from the JSONB `config` column on tenants_mst.
GitHub keys have no fallback — tenant must have config set. The Clerk
organization ID falls back to a legacy mapping, see
resolve_clerk_organization_id.

Config structure:
{
    "github": {
        "infra_repository": "org/repo",
        "infra_branch": "stage"
    },
    "clerk": {
        "organization_id": "org_xxx"
    },
    "argocd": {
        "<env>": {
            "host": "...-application-argocd.internal.genorim.xyz",
            "project": "argocd-project-core-stage-applications-service",
            "app_namespace": "argocd-system",
            "token_env": "ARGOCD_TOKEN_ASPORA_STAGE"
        }
    }
}

Usage:
    from app.utils.tenant_config import TenantConfig, get_tenant_config

    # From tenant model directly
    cfg = TenantConfig(tenant)

    # From tenant_code + db session
    cfg = await get_tenant_config(tenant_code, db)

    repo = cfg.github_infra_repository
    branch = cfg.github_infra_branch
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.tenants_mst_model import TenantsMstModel

logger = logging.getLogger(__name__)


async def get_tenant_config(tenant_code: str, db: AsyncSession) -> "TenantConfig":
    """Resolve TenantConfig from tenant_code via DB lookup."""
    stmt = select(TenantsMstModel).where(
        TenantsMstModel.code == tenant_code,
        TenantsMstModel.is_deleted == False,
    )
    result = await db.execute(stmt)
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise ValueError(f"Tenant not found: {tenant_code}")
    return TenantConfig(tenant)


class TenantConfig:
    """Read tenant-specific config from JSONB column."""

    def __init__(self, tenant: TenantsMstModel):
        self._config = tenant.config or {}
        self._github = self._config.get("github", {})
        self._clerk = self._config.get("clerk", {})
        self._argocd = self._config.get("argocd", {})

    # -- Clerk --

    @property
    def clerk_organization_id(self) -> str:
        """Clerk Organization ID for this tenant, scoped to the Clerk instance
        this deployment is configured against."""
        return self._clerk.get("organization_id", "")

    # -- GitHub --

    @property
    def github_infra_repository(self) -> str:
        """GitHub repo for infrastructure (e.g. 'org/repo')."""
        return self._github.get("infra_repository", "")

    @property
    def github_infra_branch(self) -> str:
        """Branch name for infrastructure repo."""
        return self._github.get("infra_branch", "stage")

    @property
    def github_infra_owner(self) -> str:
        """Extract owner from infra_repository (format: owner/repo)."""
        repo = self.github_infra_repository
        if repo and "/" in repo:
            return repo.split("/")[0]
        return ""

    @property
    def github_infra_repo_name(self) -> str:
        """Extract repo name from infra_repository (format: owner/repo)."""
        repo = self.github_infra_repository
        if repo and "/" in repo:
            return repo.split("/")[1]
        return ""

    # -- ArgoCD --

    def argocd_for(self, environment) -> dict:
        """ArgoCD connection settings for one environment.

        Empty dict when the tenant has no ArgoCD configured for that
        environment — callers treat that as "feature off", not an error.
        """
        env = environment.value if hasattr(environment, "value") else str(environment or "")
        settings = self._argocd.get(env.strip().lower())
        return settings if isinstance(settings, dict) else {}


# Clerk Organization IDs that predate the config column. Only valid against the
# Clerk instance these were created in — a deployment pointed at a different
# instance MUST have config["clerk"]["organization_id"] set instead.
# Remove once every tenant row carries it.
_LEGACY_CLERK_ORG_IDS = {
    "aspora": "org_35jlG9vsM0Fvpfrn7h5ywD3zM99",
    "vance": "org_35mxz5hMPsvIT264syGlAR7HeCV",
    "vance-hackathon": "org_3DdaUbcbggsllY9I7qpnkwwT3z4",
}
_LEGACY_CLERK_ORG_ID_DEFAULT = "org_35jQHDmrOweepfNU9VgTxuFMjRc"  # regobs


def resolve_clerk_organization_id(tenant: TenantsMstModel) -> str:
    """Clerk Organization ID for a tenant, preferring the config column."""
    organization_id = TenantConfig(tenant).clerk_organization_id
    if organization_id:
        return organization_id

    organization_id = _LEGACY_CLERK_ORG_IDS.get(tenant.code, _LEGACY_CLERK_ORG_ID_DEFAULT)
    logger.warning(
        f"[TENANT CONFIG] Tenant '{tenant.code}' has no config['clerk']['organization_id']; "
        f"falling back to legacy mapping {organization_id}. This is wrong if this "
        f"deployment points at a different Clerk instance."
    )
    return organization_id


def set_clerk_organization_id(
    tenant: TenantsMstModel, organization_id: str, db: AsyncSession
) -> None:
    """Persist the Clerk Organization ID onto the tenant's config column."""
    config = dict(tenant.config or {})
    clerk = dict(config.get("clerk", {}))
    clerk["organization_id"] = organization_id
    config["clerk"] = clerk
    # Reassign the whole dict — SQLAlchemy will not detect in-place JSONB mutation.
    tenant.config = config
    db.add(tenant)
