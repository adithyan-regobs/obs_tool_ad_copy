"""
Vercel API Integration — Domain Management

API Reference: https://vercel.com/docs/rest-api/projects/add-a-domain-to-a-project

Endpoints used:
  - POST   /v10/projects/{idOrName}/domains         — Add domain to project
  - DELETE /v9/projects/{idOrName}/domains/{domain}  — Remove domain from project
"""
import httpx
from typing import Dict, Any
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
VERCEL_API_BASE = "https://api.vercel.com"


class VercelIntegration:
    """Integration class for Vercel REST API — domain management."""

    @staticmethod
    def _headers() -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.vercel_api_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _team_params() -> Dict[str, str]:
        """Query params for team-scoped requests."""
        if settings.vercel_team_id:
            return {"teamId": settings.vercel_team_id}
        return {}

    @staticmethod
    async def add_domain(domain: str) -> Dict[str, Any]:
        """
        Add a custom domain to the Vercel project.

        API: POST /v10/projects/{idOrName}/domains
        Body: {"name": "acme.devlift.ai"}

        Status codes:
          - 200: Domain added successfully. Response includes `verified` field.
          - 400: Domain already exists on this project, or invalid domain.
          - 409: Domain is assigned to another Vercel project.

        Args:
            domain: Full domain name (e.g., "acme.devlift.ai")

        Returns:
            Dict with Vercel API response including `name`, `verified`, etc.
        """
        url = f"{VERCEL_API_BASE}/v10/projects/{settings.vercel_project_id}/domains"

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(
                url,
                headers=VercelIntegration._headers(),
                params=VercelIntegration._team_params(),
                json={"name": domain},
            )

            # Domain already exists on THIS project — treat as success (idempotent)
            if response.status_code == 400:
                body = response.json()
                error_code = body.get("error", {}).get("code", "")
                if error_code == "domain_already_in_use" or "already" in str(body).lower():
                    logger.info(f"[VERCEL] Domain '{domain}' already exists on project (idempotent)")
                    return {"name": domain, "already_existed": True, "verified": True}
                # Otherwise it's a real 400 error
                response.raise_for_status()

            response.raise_for_status()
            result = response.json()
            logger.info(
                f"[VERCEL] Domain '{domain}' added successfully "
                f"(verified={result.get('verified', 'unknown')})"
            )
            return result

    @staticmethod
    async def remove_domain(domain: str) -> Dict[str, Any]:
        """
        Remove a custom domain from the Vercel project (for rollback).

        API: DELETE /v9/projects/{idOrName}/domains/{domain}

        Args:
            domain: Full domain name (e.g., "acme.devlift.ai")

        Returns:
            Dict with Vercel API response
        """
        url = (
            f"{VERCEL_API_BASE}/v9/projects/{settings.vercel_project_id}"
            f"/domains/{domain}"
        )

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.delete(
                url,
                headers=VercelIntegration._headers(),
                params=VercelIntegration._team_params(),
            )
            response.raise_for_status()
            logger.info(f"[VERCEL] Domain '{domain}' removed successfully")
            return response.json()
