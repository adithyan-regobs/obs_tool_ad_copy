"""
GitHub App Installation Service

Handles GitHub App webhook events and installation management:
- Processing installation/uninstallation webhooks from GitHub
- Linking installations to tenants
- Querying installation status for tenants
"""

import hmac
import hashlib
import logging
import httpx
import jwt
import time
from typing import Dict, Any, Optional, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repository.github_app_installation_mst_repository import GitHubAppInstallationMstRepository

logger = logging.getLogger(__name__)


class GitHubAppInstallationService:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = GitHubAppInstallationMstRepository(db)

    @staticmethod
    def verify_webhook_signature(payload_body: bytes, signature_header: str) -> bool:
        """Verify the GitHub webhook signature using HMAC-SHA256."""
        if not settings.github_app_webhook_secret:
            logger.warning("GITHUB_APP_WEBHOOK_SECRET not configured, skipping signature verification")
            return True

        if not signature_header:
            return False

        expected = hmac.new(
            settings.github_app_webhook_secret.encode("utf-8"),
            payload_body,
            hashlib.sha256,
        ).hexdigest()

        received = signature_header.removeprefix("sha256=")
        return hmac.compare_digest(expected, received)

    async def handle_webhook(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Route a GitHub webhook event to the correct handler."""
        action = payload.get("action", "")

        if event == "installation":
            if action == "created":
                return await self._handle_installation_created(payload)
            elif action == "deleted":
                return await self._handle_installation_deleted(payload)

        return {"status": "ignored", "event": event, "action": action}

    async def _handle_installation_created(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle GitHub App installation created event."""
        installation = payload.get("installation", {})
        account = installation.get("account", {})

        installation_id = str(installation.get("id", ""))
        github_org = account.get("login", "")

        if not installation_id or not github_org:
            logger.error(f"Missing installation_id or github_org in webhook payload")
            return {"status": "error", "error": "Missing required fields in payload"}

        record = await self.repo.upsert_installation(
            github_org=github_org,
            installation_id=installation_id,
            tenant_code="pending",
        )
        await self.db.commit()

        logger.info(f"GitHub App installed on org '{github_org}' (installation_id={installation_id})")

        return {
            "status": "success",
            "action": "installation_created",
            "github_org": github_org,
            "installation_id": installation_id,
            "code": record.code,
        }

    async def _handle_installation_deleted(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle GitHub App uninstalled event."""
        installation = payload.get("installation", {})
        installation_id = str(installation.get("id", ""))

        if not installation_id:
            return {"status": "error", "error": "Missing installation_id in payload"}

        record = await self.repo.deactivate_installation(installation_id)
        await self.db.commit()

        if record:
            logger.info(f"GitHub App uninstalled from org '{record.github_org}'")
            return {"status": "success", "action": "installation_deleted", "github_org": record.github_org}

        logger.warning(f"Uninstall webhook for unknown installation_id={installation_id}")
        return {"status": "ignored", "action": "installation_deleted", "message": "Installation not found"}

    async def link_installation_to_tenant(
        self,
        installation_id: str,
        tenant_code: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Link a GitHub App installation to a tenant (callback-first flow).

        This does NOT depend on the webhook arriving first.
        1. Try to find existing record (webhook may have created it).
        2. If not found, fetch the org name from GitHub API and create the record.
        3. Set the tenant_code on the record.
        """
        # Try existing record first (webhook may have beaten us)
        record = await self.repo.link_tenant(installation_id, tenant_code)
        if record:
            await self.db.commit()
            logger.info(f"Linked existing installation {installation_id} to tenant '{tenant_code}'")
            return self._record_to_dict(record)

        # No record yet — fetch org info from GitHub API and create it
        github_org = await self._fetch_installation_org(installation_id)
        if not github_org:
            logger.error(f"Could not resolve org for installation_id={installation_id}")
            return None

        record = await self.repo.upsert_installation(
            github_org=github_org,
            installation_id=installation_id,
            tenant_code=tenant_code,
        )
        await self.db.commit()

        logger.info(
            f"Created and linked installation {installation_id} "
            f"(org={github_org}) to tenant '{tenant_code}'"
        )
        return self._record_to_dict(record)

    @staticmethod
    def _record_to_dict(record) -> Dict[str, Any]:
        return {
            "code": record.code,
            "github_org": record.github_org,
            "installation_id": record.installation_id,
            "tenant_code": record.tenant_code,
        }

    @staticmethod
    async def _fetch_installation_org(installation_id: str) -> Optional[str]:
        """Fetch the GitHub org/account login for an installation via the GitHub API."""
        if not settings.github_app_id or not settings.github_app_private_key:
            logger.error("GitHub App credentials not configured")
            return None

        try:
            now = int(time.time())
            payload = {
                "iat": now - 60,
                "exp": now + (10 * 60),
                "iss": settings.github_app_id,
            }
            app_jwt = jwt.encode(payload, settings.github_app_private_key, algorithm="RS256")

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"https://api.github.com/app/installations/{installation_id}",
                    headers={
                        "Authorization": f"Bearer {app_jwt}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("account", {}).get("login")
        except Exception as e:
            logger.error(f"Failed to fetch installation {installation_id} from GitHub: {e}")
            return None

    async def get_installations_for_tenant(self, tenant_code: str) -> List[Dict[str, Any]]:
        """Get all active installations for a tenant."""
        installations = await self.repo.get_by_tenant_code(tenant_code)
        return [
            {
                "code": inst.code,
                "github_org": inst.github_org,
                "installation_id": inst.installation_id,
                "is_active": inst.is_active,
            }
            for inst in installations
        ]

    def get_install_url(self, tenant_code: str) -> str:
        """Generate the GitHub App install URL with state param for tenant linking."""
        return f"https://github.com/apps/{settings.github_app_slug}/installations/new?state={tenant_code}"
