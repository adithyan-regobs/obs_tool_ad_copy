"""
GitHub App Installation Master model.

Stores per-tenant GitHub App installations. When a tenant installs the
GitHub App on their org, GitHub sends a webhook with the installation_id.
This table links that installation to the tenant so we can generate
scoped tokens for each org.
"""

from sqlalchemy import Column, String, UniqueConstraint
from app.db.models.base_model import BaseModel


class GitHubAppInstallationMstModel(BaseModel):
    """
    Tracks GitHub App installations across tenants.

    One tenant can have multiple installations (one per GitHub org).
    Each installation grants access to repos the tenant selected during install.
    """

    __tablename__ = "github_app_installation_mst"

    # Internal tenant identifier (matches TenantsMstModel.code)
    # Set to "pending" until the frontend callback links it
    tenant_code = Column(String(100), nullable=False, index=True)

    # GitHub org/account where the app was installed
    github_org = Column(String(255), nullable=False, index=True)

    # GitHub's installation ID (used to generate scoped tokens)
    installation_id = Column(String(50), nullable=False)

    __table_args__ = (
        UniqueConstraint("github_org", name="uq_github_app_installation_mst_org"),
    )

    def __repr__(self):
        return (
            f"<GitHubAppInstallationMst(id={self.id}, code='{self.code}', "
            f"tenant_code='{self.tenant_code}', github_org='{self.github_org}', "
            f"installation_id='{self.installation_id}')>"
        )
