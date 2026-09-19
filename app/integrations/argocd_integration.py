"""
ArgoCD Integration Client

Reads Application sync/health state from a tenant's ArgoCD API server.

Read-only by design: this class exposes GET calls only, so a bug here can
never sync, roll back or delete an Application. The bot token may carry
wider rights than we use — the restriction lives here, not in the token.

Connection settings come from tenants_mst.config["argocd"][<env>]:
    {
        "host": "vance-core-stage-mumbai-01-application-argocd.internal...",
        "project": "argocd-project-core-stage-applications-service",
        "app_namespace": "argocd-system",
        "token_env": "ARGOCD_TOKEN_ASPORA_STAGE"
    }

The token itself is never stored in the DB — only the name of the env var
holding it.
"""

import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 3.0
DEFAULT_APP_NAMESPACE = "argocd-system"
LIST_ENDPOINT = "/api/v1/applications"

# Server-side projection. The unfiltered list response is ~1.8 MB for 110
# applications; restricted to these three leaves it is ~9 KB.
# `items.status.health` is the whole object on purpose: ArgoCD's projection
# drops the value when asked for `items.status.health.status`, and its own web
# UI requests the object for the same reason. Sync does project one level
# deeper, so that one stays narrow.
LIST_FIELDS = ",".join(
    [
        "items.metadata.name",
        "items.status.sync.status",
        "items.status.health",
    ]
)


class ArgoCDError(Exception):
    """Raised when an ArgoCD API call fails."""


def _why(error: Exception) -> str:
    """A description that survives httpx's empty-message exceptions.

    str(ConnectTimeout()) is "", so a plain f"{e}" produced log lines that
    ended at the colon and said nothing about whether we were blocked, timing
    out or being refused.
    """
    parts = [type(error).__name__]
    detail = str(error).strip()
    if detail:
        parts.append(detail)
    response = getattr(error, "response", None)
    if response is not None:
        parts.append(f"HTTP {response.status_code}: {response.text[:200]}")
    return " — ".join(parts)


class ArgoCDIntegration:
    """Async read-only client for the ArgoCD API server."""

    def __init__(self, host: str, token: str, timeout: float = DEFAULT_TIMEOUT):
        host = (host or "").strip()
        if not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        self._base_url = host.rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    async def list_application_status(self, project: str = "") -> Dict[str, Dict[str, str]]:
        """Sync + health for every Application, keyed by Application name.

        One call covers the whole project — fetching a single application
        costs the same round trip, so callers cache the full map.
        """
        params = {"fields": LIST_FIELDS}
        if project:
            params["projects"] = project

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}{LIST_ENDPOINT}",
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ArgoCDError(f"ArgoCD list failed for {self._base_url}: {_why(e)}") from e

        statuses: Dict[str, Dict[str, str]] = {}
        for item in payload.get("items") or []:
            name = ((item.get("metadata") or {}).get("name") or "").strip()
            if not name:
                continue
            status = item.get("status") or {}
            statuses[name] = {
                "sync": (status.get("sync") or {}).get("status") or None,
                "health": (status.get("health") or {}).get("status") or None,
            }
        return statuses

    async def get_application(
        self, app_name: str, app_namespace: str = DEFAULT_APP_NAMESPACE
    ) -> Dict[str, Any]:
        """One Application, with the timestamps and running images.

        Deliberately unprojected. The list projection this module uses for the
        canvas drops anything deeper than `status.health`, and the fields that
        say WHEN Argo last acted sit deeper than that. One application is small
        enough to take whole.

        Returns {} when no Application by that name exists.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}{LIST_ENDPOINT}/{app_name}",
                    params={"appNamespace": app_namespace},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            # 403 as well as 404: ArgoCD hides an application the caller may
            # not see behind "permission denied", so for a read-only token the
            # two are indistinguishable and both mean "no such app to report".
            if response.status_code in (403, 404):
                if response.status_code == 403:
                    logger.info("ArgoCD returned 403 for %r — treating as absent", app_name)
                return {}
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ArgoCDError(f"ArgoCD get failed for {app_name!r}: {_why(e)}") from e

        status = payload.get("status") or {}
        operation_state = status.get("operationState") or {}
        return {
            "sync": (status.get("sync") or {}).get("status") or None,
            "health": (status.get("health") or {}).get("status") or None,
            "health_changed_at": (status.get("health") or {}).get("lastTransitionTime") or None,
            "sync_finished_at": operation_state.get("finishedAt") or None,
            "reconciled_at": status.get("reconciledAt") or None,
            "images": list((status.get("summary") or {}).get("images") or []),
        }

    def application_url(self, app_name: str, app_namespace: str = DEFAULT_APP_NAMESPACE) -> str:
        """Deep link that opens this Application's page in the ArgoCD UI."""
        namespace = (app_namespace or DEFAULT_APP_NAMESPACE).strip()
        return f"{self._base_url}/applications/{namespace}/{app_name}"
