"""
Domain Provisioning Service

Handles subdomain provisioning for new organizations by adding
custom domains to the Vercel project. DNS is handled by a wildcard
CNAME record in Cloudflare (*.devlift.ai → cname.vercel-dns.com).
"""
import logging
from typing import Any, Dict

from app.core.config import settings
from app.integrations.vercel_integration import VercelIntegration

logger = logging.getLogger(__name__)


class DomainProvisioningService:
    """Provisions subdomain routing (Vercel domain) for new organizations."""

    async def provision_subdomain(self, subdomain: str) -> Dict[str, Any]:
        """
        Add {subdomain}.{base_domain} to the Vercel project.

        DNS is handled by the wildcard CNAME in Cloudflare (one-time setup).
        This method is idempotent — calling it twice for the same subdomain is safe.

        Args:
            subdomain: The tenant subdomain (e.g., "acme")

        Returns:
            Dict with provisioning result:
              - success: bool
              - domain: str (e.g., "acme.devlift.ai")
              - skipped: bool (if Vercel token not configured)
              - already_existed: bool (if domain was already on the project)
              - verified: bool (Vercel domain verification status)
              - error: str | None
        """
        domain = f"{subdomain}.{settings.base_domain}"

        # Skip if Vercel is not configured (e.g., local dev)
        if not settings.vercel_api_token or not settings.vercel_project_id:
            logger.warning(
                f"[DOMAIN] Skipping domain provisioning for '{domain}' — "
                "VERCEL_API_TOKEN or VERCEL_PROJECT_ID not configured"
            )
            return {
                "success": True,
                "domain": domain,
                "skipped": True,
                "already_existed": False,
                "verified": False,
                "error": None,
            }

        try:
            logger.info(f"[DOMAIN] Provisioning domain: {domain}")
            result = await VercelIntegration.add_domain(domain)

            return {
                "success": True,
                "domain": domain,
                "skipped": False,
                "already_existed": result.get("already_existed", False),
                "verified": result.get("verified", False),
                "error": None,
            }

        except Exception as e:
            logger.error(f"[DOMAIN] Failed to provision domain '{domain}': {e}")
            return {
                "success": False,
                "domain": domain,
                "skipped": False,
                "already_existed": False,
                "verified": False,
                "error": str(e),
            }

    async def rollback_subdomain(self, subdomain: str) -> None:
        """
        Best-effort removal of domain from Vercel (for rollback scenarios).

        Args:
            subdomain: The tenant subdomain (e.g., "acme")
        """
        domain = f"{subdomain}.{settings.base_domain}"
        try:
            await VercelIntegration.remove_domain(domain)
            logger.info(f"[DOMAIN] Rollback successful: removed '{domain}'")
        except Exception as e:
            logger.warning(f"[DOMAIN] Rollback failed for '{domain}': {e}")
