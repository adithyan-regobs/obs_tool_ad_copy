"""Deep links into the DevLift dashboard.

One place for the URL shapes the frontend restores from its query string, so
Slack notifications, the MCP server and anything else that hands a user a
link agree on them. The canvas reads `?resource=<service_configs.code>` (its
nodes' resourceDbCode is that same code), selects the node and opens the
panel; `env`, `app` and `ws` pin the selection the restore waits for, and
`tab` picks the panel tab (`variables`, `settings`, ...).

The first path segment is the tenant's SUBDOMAIN, not its code: the frontend
middleware rewrites `<subdomain>.devlift.ai/x` to `/<subdomain>/x`, so the
path-based form has to carry the same value. They differ in practice — the
tenant coded `aspora` is served at `vance` — and a link built from the code
lands on a tenant that does not exist. `tenant_url_slug` resolves it; it is
read fresh every time because a subdomain can be changed at any time.

Returns None when FRONTEND_BASE_URL is not configured: callers then simply
omit the link rather than sending a broken one.
"""

import logging
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy import select

from app.core.config import settings
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def tenant_url_slug(tenant_code: str) -> str:
    """The tenant's dashboard path segment: its subdomain, or the code when no
    subdomain is set. Falls back to the code on any lookup failure so a link is
    still produced."""
    if not tenant_code:
        return ""
    try:
        async with AsyncSessionLocal() as db:
            subdomain = (
                await db.execute(
                    select(TenantsMstModel.subdomain).where(
                        TenantsMstModel.code == tenant_code,
                        TenantsMstModel.is_deleted.isnot(True),
                    )
                )
            ).scalar_one_or_none()
    except Exception:
        logger.exception("frontend_links: subdomain lookup failed for tenant %s", tenant_code)
        return tenant_code
    return (subdomain or "").strip() or tenant_code


def service_panel_link(
    *,
    tenant_slug: str,
    resource_code: str,
    environment: str = "",
    app_code: str = "",
    workspace_code: str = "",
    tab: str = "",
) -> Optional[str]:
    base = (settings.frontend_base_url or "").rstrip("/")
    if not base or not tenant_slug or not resource_code:
        return None
    params = {"resource": resource_code}
    if environment:
        params["env"] = environment
    if app_code:
        params["app"] = app_code
    if workspace_code:
        params["ws"] = workspace_code
    if tab:
        params["tab"] = tab
    return f"{base}/{tenant_slug}/dashboard/projects?{urlencode(params)}"


def variables_tab_link(
    *,
    tenant_slug: str,
    resource_code: str,
    environment: str = "",
    app_code: str = "",
    workspace_code: str = "",
) -> Optional[str]:
    """The service panel opened on the Variables tab, where variable and
    secret values are entered in the browser and saved straight to the
    secret service."""
    return service_panel_link(
        tenant_slug=tenant_slug,
        resource_code=resource_code,
        environment=environment,
        app_code=app_code,
        workspace_code=workspace_code,
        tab="variables",
    )


def approvals_link(*, tenant_slug: str, resource_code: str = "") -> Optional[str]:
    """The approvals view; `approval=<service_configs.code>` opens that
    service's drawer so the reader lands on the change, not just the list."""
    base = (settings.frontend_base_url or "").rstrip("/")
    if not base or not tenant_slug:
        return None
    params = {"view": "approvals"}
    if resource_code:
        params["approval"] = resource_code
    return f"{base}/{tenant_slug}/dashboard/projects?{urlencode(params)}"
