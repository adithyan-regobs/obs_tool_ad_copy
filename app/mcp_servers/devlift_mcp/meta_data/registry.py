"""Tenant-aware resource-metadata registry for the DevLift MCP server.

Each tenant ships its own `meta_data/<tenant>.py` module exporting:
  - RESOURCE_METADATA   (dict of resource_type -> metadata entry)

Whatever is present in the tenant's `RESOURCE_METADATA` is considered enabled
for that tenant. The infra vs service distinction comes from each entry's
`transaction_table` field:
  - "infrastructure_mst" → provision_resource / trigger_resource_deployment
  - "service_config"     → provision_service / trigger_service_deployment

Lookup order (mirrors ScriptGenHandler.get_script_generator):
  1. Tenant-specific metadata file
  2. `default.py` fallback

Placement options (environments) are shared across tenants for v1.
"""

from types import ModuleType
from typing import Optional

from app.mcp_servers.devlift_mcp.meta_data import aspora, default, vance


# ============================================================
# Shared placement options (non-tenant-specific for v1)
# ============================================================

PLACEMENT_ENVIRONMENTS = ["Stage", "Prod"]

ENVIRONMENT_TO_ENUM_VALUE = {
    "Stage": "stage",
    "Prod": "prod",
}


# ============================================================
# Tenant dispatch
# ============================================================

_TENANT_META: dict[str, ModuleType] = {
    "aspora": aspora,
    "vance": vance,
    "default": default,
}


def _meta_for(tenant_code: str) -> ModuleType:
    """Return the tenant's metadata module, falling back to `default`."""
    return _TENANT_META.get(tenant_code, default)


def is_paas_tenant(tenant_code: str) -> bool:
    """Return True if the tenant uses the PaaS deploy flow (Jenkins + ALB +
    polling). False for enterprise tenants whose deployments are managed via
    a GitOps PR to their own repo.

    Falls back to `True` if the tenant's metadata module doesn't set IS_PAAS.
    """
    return bool(getattr(_meta_for(tenant_code), "IS_PAAS", True))


# ============================================================
# Metadata access helpers (tenant-aware)
# ============================================================

_INFRA_TABLE = "infrastructure_mst"
_SERVICE_TABLE = "service_config"


def get_enabled_metadata(tenant_code: str) -> dict:
    """Return the enabled infrastructure_mst resources for the tenant."""
    meta = _meta_for(tenant_code)
    return {
        k: v
        for k, v in meta.RESOURCE_METADATA.items()
        if v.get("transaction_table") == _INFRA_TABLE
    }


def get_enabled_service_metadata(tenant_code: str) -> dict:
    """Return the enabled service_config resources for the tenant."""
    meta = _meta_for(tenant_code)
    return {
        k: v
        for k, v in meta.RESOURCE_METADATA.items()
        if v.get("transaction_table") == _SERVICE_TABLE
    }


def get_resource_metadata(tenant_code: str, resource_type: str) -> Optional[dict]:
    """Return the infra metadata for a resource type if present for the tenant."""
    meta = _meta_for(tenant_code)
    entry = meta.RESOURCE_METADATA.get(resource_type)
    if not entry or entry.get("transaction_table") != _INFRA_TABLE:
        return None
    return entry


def get_service_metadata(tenant_code: str, resource_type: str) -> Optional[dict]:
    """Return the service metadata for a resource type if present for the tenant."""
    meta = _meta_for(tenant_code)
    entry = meta.RESOURCE_METADATA.get(resource_type)
    if not entry or entry.get("transaction_table") != _SERVICE_TABLE:
        return None
    return entry


def find_metadata_by_case_ref_code(tenant_code: str, case_ref_code: str) -> Optional[dict]:
    """Find a metadata entry (resource OR service) by its case_ref_code."""
    meta = _meta_for(tenant_code)
    for entry in meta.RESOURCE_METADATA.values():
        if entry.get("case_ref_code") == case_ref_code:
            return entry
    return None


def translate_to_canonical(tenant_code: str, resource_type: str, attrs: dict) -> dict:
    """DEPRECATED — chatbot now returns canonical attribute_parameters directly.

    Translation lived here when the LLM submitted LLM-friendly field names
    (e.g. `bucket_name`) and the metadata mapped each to its canonical key
    (e.g. `identifier`). Now chat-bot-POC's `result_template.attribute_parameters`
    already carries canonical keys, so this helper is unreachable from the
    new flow. Retained as a safe no-op for any legacy code path that still
    imports it — returns {} when RESOURCE_METADATA is empty.
    """
    entry = get_resource_metadata(tenant_code, resource_type) or get_service_metadata(
        tenant_code, resource_type
    )
    if not entry:
        return {}
    name_map = {f["name"]: f["canonical_name"] for f in entry["fields"]}
    return {
        name_map[k]: v
        for k, v in attrs.items()
        if k in name_map
    }
