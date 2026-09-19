"""
Cloudflare API Integration — DNS Record Management

Creates/updates CNAME records for deployed services so they get friendly URLs:
  {service}-{tenant}-{env}.apps.devlift.ai → ALB hostname

API Reference: https://developers.cloudflare.com/api/operations/dns-records-for-a-zone-list-dns-records
"""
import httpx
from typing import Dict, Any
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
CF_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareIntegration:
    """Integration class for Cloudflare DNS API — CNAME management."""

    @staticmethod
    def _headers() -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.cloudflare_api_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _zone_url() -> str:
        return f"{CF_API_BASE}/zones/{settings.cloudflare_zone_id}/dns_records"

    @staticmethod
    async def create_or_update_cname(
        subdomain: str,
        service_name: str,
        alb_hostname: str,
        env: str = "stage",
    ) -> str:
        """
        Create or update a per-service CNAME record:
          {service_name}-{subdomain}-{env}.apps.{base_domain} → alb_hostname

        Args:
            subdomain:    Tenant subdomain (e.g., "aslam")
            service_name: Service name (e.g., "my-api"), already lowercased
            alb_hostname: Raw ALB DNS hostname (e.g., "k8s-xxx.elb.amazonaws.com")
            env:          Deployment environment (e.g., "stage", "prod")

        Returns:
            Friendly URL: https://{service_name}-{subdomain}-{env}.apps.devlift.ai
        """
        record_name = f"{service_name}-{subdomain}-{env}.apps.{settings.base_domain}"
        # Strip protocol from ALB hostname if present
        target = alb_hostname.replace("https://", "").replace("http://", "").rstrip("/")

        headers = CloudflareIntegration._headers()
        base_url = CloudflareIntegration._zone_url()

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            # Check if record already exists — use name= filter and verify exact match
            list_resp = await client.get(
                base_url,
                headers=headers,
                params={"type": "CNAME", "name": record_name},
            )
            list_resp.raise_for_status()
            all_results = list_resp.json().get("result", [])
            # Filter to exact name match (Cloudflare name filter can be substring-based)
            existing = [r for r in all_results if r.get("name") == record_name]
            logger.info(
                "[CLOUDFLARE] Lookup for %s: %d total results, %d exact matches",
                record_name, len(all_results), len(existing),
            )

            if existing:
                record_id = existing[0]["id"]
                patch_resp = await client.patch(
                    f"{base_url}/{record_id}",
                    headers=headers,
                    json={
                        "type": "CNAME",
                        "name": record_name,
                        "content": target,
                        "ttl": 1,
                        "comment": f"[ObsTool] Auto-managed CNAME for service '{service_name}' deployed via DevLift",
                    },
                )
                patch_resp.raise_for_status()
                logger.info("[CLOUDFLARE] Updated CNAME %s → %s (record_id=%s)", record_name, target, record_id)
            else:
                create_resp = await client.post(
                    base_url,
                    headers=headers,
                    json={
                        "type": "CNAME",
                        "name": record_name,
                        "content": target,
                        "proxied": False,
                        "ttl": 1,
                        "comment": f"[ObsTool] Auto-managed CNAME for service '{service_name}' deployed via DevLift",
                    },
                )
                create_resp.raise_for_status()
                logger.info("[CLOUDFLARE] Created CNAME %s → %s", record_name, target)

        friendly_url = f"https://{record_name}"
        return friendly_url

    @staticmethod
    async def delete_cname(
        subdomain: str,
        service_name: str,
        env: str = "stage",
    ) -> bool:
        """
        Delete a CNAME record (for cleanup/rollback).

        Returns:
            True if deleted, False if record didn't exist.
        """
        record_name = f"{service_name}-{subdomain}-{env}.apps.{settings.base_domain}"
        headers = CloudflareIntegration._headers()
        base_url = CloudflareIntegration._zone_url()

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            list_resp = await client.get(
                base_url,
                headers=headers,
                params={"type": "CNAME", "name": record_name},
            )
            list_resp.raise_for_status()
            all_results = list_resp.json().get("result", [])
            existing = [r for r in all_results if r.get("name") == record_name]

            if not existing:
                logger.info("[CLOUDFLARE] CNAME %s not found, nothing to delete", record_name)
                return False

            record_id = existing[0]["id"]
            del_resp = await client.delete(
                f"{base_url}/{record_id}",
                headers=headers,
            )
            del_resp.raise_for_status()
            logger.info("[CLOUDFLARE] Deleted CNAME %s", record_name)
            return True
