"""obs_tool glue: map DB rows onto FGA object ids and create-permissions.

Kept separate from the verbatim PoC kit (fga.py / security.py / ...) — this is
the only module that knows how obs_tool's reference codes translate into FGA
object ids.

NOTE ON VOCABULARY: the compound-id builders below still speak the older
aspora.fga chain (geo_loc, infra_mst, can_create_s3/sqs/dynamo). The PUBLISHED
model — rbac-model.fga, mirrored in app/core/model/ — has none of those types;
it checks at `service` only. So ids built here for infra objects name a type
the store does not define, and every check against one errors and fails closed,
which is the intended state until infra_mst enters the model (see the route
note in rbac-model.fga). The `service` builders are the live path and are
unaffected. Do not "fix" a denial here by loosening the check; the model has to
gain the type first.

The compound-id builders here are the single source of truth for the two
per-path levels — the backfill, route glue, and tests must all build ids
through them so they can never drift apart.
"""

# infrastructuretype_ref_code fragment -> create permission on geo_loc.
# Access checks are type-agnostic (infra_mst); CREATION is the per-type control.
CREATE_PERMISSIONS = {"s3": "can_create_s3", "sqs": "can_create_sqs",
                      "dynamo": "can_create_dynamo"}


def create_permission(ref_code: str | None) -> str | None:
    """Create-permission name for an infrastructure type, or None when the
    type has no lane in the model (db, redis, kong, ...). Callers must decide
    what None means — there is deliberately no generic fallback."""
    low = (ref_code or "").lower()
    for key, perm in CREATE_PERMISSIONS.items():
        if key in low:
            return perm
    return None


def infra_object(code: str) -> str:
    """FGA object id for an infrastructure_mst row — type-agnostic."""
    return f"infra_mst:{code}"


def config_object(code: str) -> str:
    """FGA object id for a service_configs row.

    The FGA type is `service`, not `service_config`: the model names the node
    after the thing being authorized, and a service_configs row IS the service
    in one environment/region.
    """
    return f"service:{code}"


# ── default nodes for rows missing an org level ──────────────────────────────
def default_product(tenant_code: str) -> str:
    """product node id for rows with no applications_mst_code."""
    return f"{tenant_code}--product"


def default_rg(product_code: str) -> str:
    """resource_group node id for rows with no resource_group_mst_code."""
    return f"{product_code}--rg"


# ── the two per-path levels (compound ids) ───────────────────────────────────
def env_node(rg_code: str, env: str) -> str:
    """environment node id: one env per resource group, no table needed.
    e.g. resourcegrp1--stage"""
    return f"{rg_code}--{env}"


def geo_node(rg_code: str, env: str, geo_code: str) -> str:
    """geo_loc node id: the geo inside one rg+env path.
    e.g. resourcegrp1--stage--region-aspora-mumbai"""
    return f"{rg_code}--{env}--{geo_code}"
