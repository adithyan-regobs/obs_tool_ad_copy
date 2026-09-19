"""Hardcoded geo-location resolution — DEPRECATED.

The chatbot is now the source of truth for geo dropdown options, and the
new `provision_and_trigger_from_ticket_handler` reads `geo_loc_mst_code`
directly from the chatbot's `placement_parameters` (no label-to-code
resolution needed).

The functions below are retained because dispatcher.py and the legacy
list_supported_resources still import them; they are reachable only via
the deprecated provision_resource_handler / describe_resource_impl code
paths, which are no longer registered as MCP tools.
"""

_ASPORA_GROUP = {"aspora", "vance"}

_ASPORA_STAGE = [
    {"label": "mumbai", "value": "region-aspora-mumbai"},
]
_ASPORA_PROD = [
    {"label": "mumbai", "value": "region-aspora-mumbai"},
    {"label": "london", "value": "region-aspora-london"},
]


def get_environment_options(tenant_code: str) -> list[str]:
    """Return the valid placement environment labels for this tenant.

    aspora/vance → Stage + Prod. Every other tenant → Stage only.
    """
    if tenant_code in _ASPORA_GROUP:
        return ["Stage", "Prod"]
    return ["Stage"]


def is_environment_allowed(tenant_code: str, environment_label: str) -> bool:
    """True if the environment label is valid for this tenant."""
    return environment_label in get_environment_options(tenant_code)


def get_geo_options(tenant_code: str, environment: str | None = None) -> list[dict]:
    """Return available geo options for (tenant_code, environment) as
    [{label, value}] dropdown entries.

    environment is the EnvironmentEnum value (e.g. "stage", "prod"). When
    None, returns the tenant's full superset — used by describe_resource
    where the environment isn't picked yet.
    """
    if tenant_code in _ASPORA_GROUP:
        if environment == "stage":
            return list(_ASPORA_STAGE)
        if environment == "prod":
            return list(_ASPORA_PROD)
        return list(_ASPORA_PROD)

    return [{"label": "us", "value": f"region-{tenant_code}-us"}]


def resolve_geo_code(
    tenant_code: str,
    environment: str,
    geo_label: str,
) -> tuple[str | None, list[str]]:
    """Resolve a user-provided geo label to a geo_loc_code for the given
    tenant + environment. Case-insensitive label match.

    Returns (geo_loc_code, available_labels). On a miss, geo_loc_code is
    None and available_labels lists the valid labels for this env so the
    caller can build a helpful error.
    """
    options = get_geo_options(tenant_code, environment)
    labels = [o["label"] for o in options]
    target = (geo_label or "").strip().lower()
    for o in options:
        if o["label"].lower() == target:
            return o["value"], labels
    return None, labels
