"""
ArgoCD Status Service

Serves live Application sync/health for canvas services, read through from
ArgoCD with a short Redis cache.

Nothing is persisted. The status changes every few seconds and ArgoCD is the
source of truth, so a stored copy would be stale before anyone read it — and
the canvas polls every 5s, which would mean constant writes for no gain.

Call shape per environment, not per service: one list call returns every
Application in the project, so 110 services on a canvas cost one request.

Failure behaviour is deliberately quiet, but never silent: every service gets
an entry, and `argocd_state` says which of the four cases it is. The UI needs
that distinction — "never deployed" and "ArgoCD is down" both leave the status
blank, and telling a user their service failed a status check when it simply
does not exist yet is worse than saying nothing.
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.argocd_integration import (
    DEFAULT_APP_NAMESPACE,
    ArgoCDError,
    ArgoCDIntegration,
)
from app.integrations.redis_integration import RedisIntegration
from app.mcp_servers.devlift_mcp.service_payloads import eks_namespace_for
from app.utils.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)

# Fresh window. The canvas polls every 5s, so at most one ArgoCD call per
# environment per 15s no matter how many users or services are on screen.
FRESH_TTL = 15

# Last-known-good window. Keeps badges on screen through an ArgoCD restart
# instead of blanking them, and carries an `as_of` so the UI can grey them.
STALE_TTL = 600

# Held by whichever request refreshes an expired key; the rest serve stale
# rather than stampeding ArgoCD.
LOCK_TTL = 10

# After a failed call, stop calling for this long and serve stale. Without it
# a hung ArgoCD would make every canvas poll wait on the timeout.
COOLDOWN_TTL = 60

# Why a service has no live status. `ok` carries real values; the rest carry
# nulls and exist so the UI can explain itself.
STATE_OK = "ok"
STATE_NOT_FOUND = "not_found"
STATE_UNAVAILABLE = "unavailable"
STATE_NOT_CONFIGURED = "not_configured"


def _blank(state: str) -> dict:
    return {
        "argocd_sync_status": None,
        "argocd_health_status": None,
        "argocd_url": None,
        "argocd_as_of": None,
        "argocd_state": state,
    }


def _fresh_key(tenant_code: str, environment: str) -> str:
    return f"argocd:status:{tenant_code}:{environment}"


def _last_key(tenant_code: str, environment: str) -> str:
    return f"argocd:status:{tenant_code}:{environment}:last"


def _lock_key(tenant_code: str, environment: str) -> str:
    return f"argocd:status:{tenant_code}:{environment}:lock"


def _cooldown_key(tenant_code: str, environment: str) -> str:
    return f"argocd:status:{tenant_code}:{environment}:cooldown"


async def _application_map(
    tenant_code: str,
    environment: str,
    client: ArgoCDIntegration,
    project: str,
) -> dict:
    """Cached {app_name: {sync, health}} plus the time it was fetched."""
    fresh = await RedisIntegration.get_json(_fresh_key(tenant_code, environment))
    if fresh:
        return fresh

    last = await RedisIntegration.get_json(_last_key(tenant_code, environment))

    if await RedisIntegration.exists(_cooldown_key(tenant_code, environment)):
        return last or {}

    lock = _lock_key(tenant_code, environment)
    if not await RedisIntegration.acquire_lock(lock, ttl=LOCK_TTL):
        return last or {}

    try:
        apps = await client.list_application_status(project)
    except ArgoCDError as e:
        logger.warning("argocd status: %s/%s refresh failed: %s", tenant_code, environment, e)
        await RedisIntegration.set(_cooldown_key(tenant_code, environment), "1", ttl=COOLDOWN_TTL)
        return last or {}
    finally:
        await RedisIntegration.release_lock(lock)

    payload = {"apps": apps, "as_of": datetime.now(timezone.utc).isoformat()}
    await RedisIntegration.set_json(_fresh_key(tenant_code, environment), payload, ttl=FRESH_TTL)
    await RedisIntegration.set_json(_last_key(tenant_code, environment), payload, ttl=STALE_TTL)
    return payload


async def status_for_services(
    tenant_code: str,
    services: Sequence[Tuple[str, str, str]],
    db: AsyncSession,
) -> Dict[str, dict]:
    """ArgoCD state for each service, keyed by service_config code.

    `services` carries (service_config_code, service_name, environment).
    Every service gets an entry; `argocd_state` says whether the values in it
    are real.
    """
    if not services:
        return {}

    by_environment: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for code, service_name, environment in services:
        if code and service_name and environment:
            by_environment[environment].append((code, service_name))

    try:
        tenant_config = await get_tenant_config(tenant_code, db)
    except Exception as e:
        logger.warning("argocd status: tenant config unavailable for %s: %s", tenant_code, e)
        return {
            code: _blank(STATE_NOT_CONFIGURED)
            for items in by_environment.values()
            for code, _ in items
        }

    results: Dict[str, dict] = {}

    for environment, items in by_environment.items():
        settings = tenant_config.argocd_for(environment)
        host = (settings.get("host") or "").strip()
        token = os.getenv((settings.get("token_env") or "").strip(), "")
        if not host or not token:
            results.update({code: _blank(STATE_NOT_CONFIGURED) for code, _ in items})
            continue

        namespace = settings.get("app_namespace") or DEFAULT_APP_NAMESPACE
        client = ArgoCDIntegration(host=host, token=token)
        payload = await _application_map(
            tenant_code, environment, client, settings.get("project") or ""
        )
        apps = payload.get("apps") or {}
        if not apps:
            results.update({code: _blank(STATE_UNAVAILABLE) for code, _ in items})
            continue

        for code, service_name in items:
            app_name = eks_namespace_for(service_name)
            state = apps.get(app_name)
            if not state:
                results[code] = _blank(STATE_NOT_FOUND)
                continue
            results[code] = {
                "argocd_sync_status": state.get("sync"),
                "argocd_health_status": state.get("health"),
                "argocd_url": client.application_url(app_name, namespace),
                "argocd_as_of": payload.get("as_of"),
                "argocd_state": STATE_OK,
            }

    return results
