"""
HTTP client for devlift-secret-config-manager's /internal/variables surface.

Why: variable_mst is sensitive — obs_tool must not read/write the table
directly (docs/variable-mst-isolation-spec.md). Every former
VariableMstRepository call site goes through this client instead.

URL + auth follow the existing deploy_variables_activity pattern exactly:
SECRET_SERVICE_URL already includes the service's mount prefix (behind the
shared ALB the prefix is what selects the service — see config.py), so only
the route is appended here; auth is the shared X-Internal-Key
(SECRET_SERVICE_INTERNAL_KEY), fail-closed when unconfigured.

Enum arguments accept either the enum member or its .value string.
"""

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def _v(value) -> Optional[str]:
    """Enum member → its value; strings pass through."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else value


class SecretConfigClient:

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        if not settings.secret_service_internal_key:
            raise ValueError(
                "SECRET_SERVICE_INTERNAL_KEY is not configured — cannot call the secret service"
            )
        return {"X-Internal-Key": settings.secret_service_internal_key}

    def _url(self, route: str) -> str:
        return settings.secret_service_url.rstrip("/") + "/api/v1/internal" + route

    async def _get(self, route: str, params: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self._url(route), params=params, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def _post(self, route: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self._url(route), json=payload, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    # ── reads ─────────────────────────────────────────────────────────────

    async def get_variables(
        self,
        *,
        table_name,
        transaction_code: str,
        environment,
        variable_type=None,
    ) -> List[Dict[str, Any]]:
        """All live rows for one (owner, environment). SECRET rows carry key +
        ARN but never a value; VARIABLE rows arrive with LOCAL refs resolved."""
        params = {
            "table_name": _v(table_name),
            "transaction_code": transaction_code,
            "environment": _v(environment),
        }
        if variable_type is not None:
            params["variable_type"] = _v(variable_type)
        data = await self._get("/variables", params)
        return data["items"]

    async def get_variables_summary(
        self, *, table_name, transaction_code: str,
    ) -> Dict[str, bool]:
        """{"has_secrets": bool, "has_variables": bool} across all envs."""
        return await self._get("/variables/summary", {
            "table_name": _v(table_name),
            "transaction_code": transaction_code,
        })

    # ── writes ────────────────────────────────────────────────────────────

    async def upsert_variables(
        self,
        *,
        table_name,
        transaction_code: str,
        environment,
        tenant_code: str,
        items: List[Dict[str, Any]],
        match_environment: bool = True,
    ) -> List[Dict[str, Any]]:
        """Upsert infra-owned variables. Each item: {key, value} plus optional
        variable_type, secret_provider, description, scope_type, data_type,
        metadata_json, variable_cloud_identifier, skip_if_unchanged.
        Returns [{key, status: created|updated|unchanged, id}]."""
        data = await self._post("/variables/upsert", {
            "table_name": _v(table_name),
            "transaction_code": transaction_code,
            "environment": _v(environment),
            "tenant_code": tenant_code,
            "match_environment": match_environment,
            "items": items,
        })
        return data["items"]

    async def cascade_delete_variables(
        self, *, table_name, transaction_code: str, environment,
    ) -> Dict[str, Any]:
        """Soft-delete an owner's variables plus everything referencing them.
        Returns {"deleted": int, "ids": [...]}."""
        return await self._post("/variables/cascade-delete", {
            "table_name": _v(table_name),
            "transaction_code": transaction_code,
            "environment": _v(environment),
        })

    async def sync_env_refs(
        self,
        *,
        source_table_name,
        source_transaction_code: str,
        environment,
        tenant_code: str,
        application_code: str,
        service_transaction_code: str,
        resource_name: str,
        source_label: str,
        env_mapping: Dict[str, str],
    ) -> Dict[str, Any]:
        """MCP env-sync: scm resolves the source's values from its AWS secret
        and refs them into the service — obs_tool never reads the secret.
        Returns {"status": "ok"|"error", "message"?, "synced": [...]}."""
        return await self._post("/variables/sync-env-refs", {
            "source_table_name": _v(source_table_name),
            "source_transaction_code": source_transaction_code,
            "environment": _v(environment),
            "tenant_code": tenant_code,
            "application_code": application_code,
            "service_transaction_code": service_transaction_code,
            "resource_name": resource_name,
            "source_label": source_label,
            "env_mapping": env_mapping,
        })

    async def record_resource_secret(
        self,
        *,
        table_name,
        transaction_code: str,
        environment,
        tenant_code: str,
        application_code: Optional[str],
        resource_name: str,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """k8s-postgres flow: write items into the resource's consolidated AWS
        secret (fail-soft server-side), then upsert rows matched by (owner,
        key). Each item: {key, value, variable_type, description}.
        Returns {"secret_arn": ..., "secret_path": ..., "ids": [...]}."""
        return await self._post("/variables/record-resource-secret", {
            "table_name": _v(table_name),
            "transaction_code": transaction_code,
            "environment": _v(environment),
            "tenant_code": tenant_code,
            "application_code": application_code,
            "resource_name": resource_name,
            "items": items,
        })

    async def map_service_to_resource_group(
        self, *, resource_group_code: str, service_config_code: str, tenant_code: str,
        admin_service_code: Optional[str] = None, admin_user_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Parent a service config under a resource group in OpenFGA so it
        inherits the group's roles/permissions, and optionally give the creator
        admin on it through a per-service user group.

        `admin_service_code` / `admin_user_code` are sent together or not at
        all: a group with no member grants nothing, and a member with no group
        has nowhere to go. When both are present scm derives the per-service
        admin group id from the service, adds the user to it, and grants that
        group admin on this config. The id is built THERE, not here — its shape
        is an OpenFGA concern and every tuple write already lives in scm.

        Returns {service_config_code, requested_resource_group_code,
        mapped_resource_group_code, previous_resource_group_code,
        outcome: mapped|already_mapped, admin_group, admin_group_granted,
        admin_member_added, admin_grant_skipped}. admin_grant_skipped=True
        means scm found the creator already holds admin on the config through
        the resource group and deliberately wrote no per-service group — not a
        failure."""
        # tenant_code and admin_service_code ride along because this is called
        # BEFORE the config row's transaction commits — scm cannot reach the
        # service from a service_configs row it cannot see yet. The service row
        # itself IS committed (created in an earlier request), so scm resolves
        # the product and the group name from it.
        payload: Dict[str, Any] = {
            "resource_group_code": resource_group_code,
            "service_config_code": service_config_code,
            "tenant_code": tenant_code,
        }
        if admin_service_code and admin_user_code:
            payload["admin_service_code"] = admin_service_code
            payload["admin_user_code"] = admin_user_code
        return await self._post("/authz/map-service-resource-group", payload)

    async def discard_variables_draft(
        self, *, transaction_code: str, user_code: str, tenant_code: str,
    ) -> Dict[str, Any]:
        """Approval discard, variables half: drop the AUTHOR's staged file, the
        never-deployed rows it created, and the variables queue row. The
        secret service owns all three; obs_tool only asks. Idempotent — no
        file and no row is a success.
        Returns {file_deleted, keys_dropped, orphans_purged, queue_row_retired}."""
        return await self._post("/variables/discard-draft", {
            "transaction_code": transaction_code,
            "user_code": user_code,
            "tenant_code": tenant_code,
        })

    async def sync_default_role(
        self, *, tenant_code: str, table_name, transaction_code: str, environment,
    ) -> None:
        """PR-workflow step 1.5: heal the tenant default-role secrets policy."""
        await self._post("/sync-default-role", {
            "tenant_code": tenant_code,
            "table_name": _v(table_name),
            "transaction_code": transaction_code,
            "environment": _v(environment),
        })
