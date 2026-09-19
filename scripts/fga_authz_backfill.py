"""Backfill / reconcile OpenFGA tuples from obs_tool's DB — model v4
(the simplified sequential chain).

  tenant -> workspace -> product -> resource_group -> environment -> geo_loc
         -> infra_mst -> service_config

Every edge comes from a DB column on the row it describes — nothing inferred:

  workspace       workspace_mst.tenants_mst_code
  product         applications_mst (workspace_code; tenant default when NULL)
  resource_group  resource_group_mst.applications_mst_code
                  (rows without one use the default node {product}--rg)
  environment     {rg}--{env}; an infra row's env comes from its vendor
                  account row (accounts are env-specific: aws_vance_prod)
  geo_loc         {rg}--{env}--{geo_loc_mst_code}
  infra_mst       every infrastructure_mst row, whatever its type
  service_config  SIBLING of infra_mst under the same geo (the cluster it
                  deploys onto is a data attribute, not an authz parent);
                  configs without a geo are SKIPPED and logged

Org owners: an admin tuple on every resource group in their tenant. Roles start
at resource_group by design — tenant / workspace / product are structure only
and have no admin relation, so granting higher up is rejected by the model.

Idempotent — safe to re-run any time; doubles as reconciliation. Column
selects on purpose: some rows carry enum values the ORM models don't know,
which break entity loading.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.db.models.workspace_mst_model import WorkspaceMstModel  # noqa: E402
from app.db.models.applications_mst_model import ApplicationsMstModel  # noqa: E402
from app.db.models.resource_group_mst_model import ResourceGroupMstModel  # noqa: E402
from app.db.models.infra_vendor_accounts_mst_model import InfraVendorAccountsMstModel  # noqa: E402
from app.db.models.infrastructure_mst_model import InfrastructureMstModel  # noqa: E402
from app.db.models.service_config_model import ServiceConfigModel  # noqa: E402
from app.db.models.services_mst_model import ServicesMstModel  # noqa: E402
from app.db.models.user_mst_model import UserMstModel  # noqa: E402
from app.core.authz.objects import (  # noqa: E402
    default_product,
    default_rg,
    env_node,
    geo_node,
)


def collect() -> list[tuple[str, str, str]]:
    tuples: set[tuple[str, str, str]] = set()
    with SessionLocal() as db:
        workspaces = db.execute(select(
            WorkspaceMstModel.code, WorkspaceMstModel.tenants_mst_code,
        ).where(WorkspaceMstModel.is_deleted.is_(False))).all()
        apps = db.execute(select(
            ApplicationsMstModel.code, ApplicationsMstModel.workspace_code,
            ApplicationsMstModel.tenants_mst_code,
        ).where(ApplicationsMstModel.is_deleted.is_(False))).all()
        rgs = db.execute(select(
            ResourceGroupMstModel.code, ResourceGroupMstModel.applications_mst_code,
            ResourceGroupMstModel.tenants_mst_code,
        ).where(ResourceGroupMstModel.is_deleted.is_(False))).all()
        accounts = db.execute(select(
            InfraVendorAccountsMstModel.code,
            InfraVendorAccountsMstModel.environments_enum,
        )).all()
        owners = db.execute(select(
            UserMstModel.code, UserMstModel.tenants_mst_code,
        ).where(UserMstModel.is_org_owner.is_(True),
                UserMstModel.is_deleted.is_(False))).all()
        infra = db.execute(select(
            InfrastructureMstModel.code,
            InfrastructureMstModel.tenants_mst_code,
            InfrastructureMstModel.applications_mst_code,
            InfrastructureMstModel.resource_group_mst_code,
            InfrastructureMstModel.geo_loc_mst_code,
            InfrastructureMstModel.infra_vendor_accounts_mst_code,
        ).where(InfrastructureMstModel.is_deleted.is_(False))).all()
        # A service config knows where it lives without asking its cluster:
        # tenant, environment and geo are its own columns, and the application
        # and resource group come from its services_mst row.
        configs = db.execute(select(
            ServiceConfigModel.code,
            ServiceConfigModel.tenant_mst_code,
            ServiceConfigModel.services_mst_code,
            ServiceConfigModel.environment,
            ServiceConfigModel.geo_loc_mst_code,
        ).where(ServiceConfigModel.is_deleted.is_(False))).all()
        services = db.execute(select(
            ServicesMstModel.code,
            ServicesMstModel.applications_mst_code,
            ServicesMstModel.resource_group_mst_code,
        ).where(ServicesMstModel.is_deleted.is_(False))).all()

    ws_of_tenant = {w.tenants_mst_code: w.code for w in workspaces}
    svc_rows = {s.code: s for s in services}
    app_rows = {a.code: a for a in apps}
    acct_env = {a.code: a.environments_enum.value for a in accounts}

    # ── org structure: every workspace / product / resource_group row ───────
    for w in workspaces:
        tuples.add((f"tenant:{w.tenants_mst_code}", "parent", f"workspace:{w.code}"))

    def product_chain(app_code: str | None, tenant: str) -> str:
        """product node for a row; materializes its workspace link. Returns
        the product code used in ids further down the chain."""
        a = app_rows.get(app_code) if app_code else None
        if a is not None:
            ws = a.workspace_code or ws_of_tenant.get(a.tenants_mst_code)
            prod = a.code
        else:
            ws = ws_of_tenant.get(tenant)
            prod = default_product(tenant)
        if ws is None:
            ws = f"{tenant}--ws"
            tuples.add((f"tenant:{tenant}", "parent", f"workspace:{ws}"))
        tuples.add((f"workspace:{ws}", "parent", f"product:{prod}"))
        return prod

    # Which tenant each resource group belongs to. Org owner grants are emitted
    # at the END, once the default groups created below are known too.
    rg_tenant: dict[str, str] = {}

    rg_rows = {}
    for rg in rgs:
        rg_rows[rg.code] = rg
        rg_tenant[rg.code] = rg.tenants_mst_code
        prod = product_chain(rg.applications_mst_code, rg.tenants_mst_code)
        tuples.add((f"product:{prod}", "parent", f"resource_group:{rg.code}"))

    def path_to_geo(tenant: str, app_code: str | None, rg_code: str | None,
                    env: str, geo: str) -> str:
        """Materialize product -> rg -> env -> geo for one row's path;
        returns the geo node's full FGA id."""
        prod = product_chain(app_code, tenant)
        if rg_code and rg_code in rg_rows:
            rg = rg_code
        else:
            rg = default_rg(prod)
            rg_tenant.setdefault(rg, tenant)
            tuples.add((f"product:{prod}", "parent", f"resource_group:{rg}"))
        e = env_node(rg, env)
        g = geo_node(rg, env, geo)
        tuples.add((f"resource_group:{rg}", "parent", f"environment:{e}"))
        tuples.add((f"environment:{e}", "parent", f"geo_loc:{g}"))
        return f"geo_loc:{g}"

    # ── infra rows: the leaf of the geo path ────────────────────────────────
    infra_rows: dict[str, tuple] = {}
    for row in infra:
        infra_rows[row.code] = row
        if not row.geo_loc_mst_code:
            print(f"  skip {row.code}: no geo_loc_mst_code")
            continue
        env = acct_env.get(row.infra_vendor_accounts_mst_code, "unknown")
        gnode = path_to_geo(row.tenants_mst_code, row.applications_mst_code,
                            row.resource_group_mst_code, env, row.geo_loc_mst_code)
        tuples.add((gnode, "parent", f"infra_mst:{row.code}"))

    # ── service configs: sibling of infra under the same geo path ──────────
    for cfg in configs:
        svc = svc_rows.get(cfg.services_mst_code)
        if svc is None:
            print(f"  skip config {cfg.code}: service "
                  f"{cfg.services_mst_code!r} not found")
            continue
        if not cfg.geo_loc_mst_code:
            print(f"  skip config {cfg.code}: no geo_loc_mst_code")
            continue
        # Placed from the SERVICE's own columns, not from the cluster it runs
        # on. infrastructure_mst.resource_group_mst_code is NULL on every row,
        # so deriving the path from the cluster sent every service_config under
        # an invented `{product}--rg` node that exists nowhere in the database —
        # and a grant on the real group could then never reach it.
        env = cfg.environment.value if hasattr(cfg.environment, "value") else cfg.environment
        gnode = path_to_geo(cfg.tenant_mst_code, svc.applications_mst_code,
                            svc.resource_group_mst_code, env, cfg.geo_loc_mst_code)
        # FGA type is `service` (the model names the node after the thing
        # being authorized); the DB table remains service_configs.
        tuples.add((gnode, "parent", f"service:{cfg.code}"))

    # ── org owners: admin on every resource group in their tenant ───────────
    # Granted HERE, not on the tenant: roles start at resource_group by design.
    # tenant / workspace / product are structure only and carry no admin
    # relation, so a tuple there is rejected — and would not cascade down even
    # if it were accepted, because resource_group.admin has no `from parent`.
    #
    # One tuple per (owner, resource group). Emitted last so the default groups
    # created by path_to_geo are included. Re-running picks up new groups.
    for u in owners:
        for rg_code, tenant in rg_tenant.items():
            if tenant == u.tenants_mst_code:
                tuples.add((f"user:{u.code}", "admin", f"resource_group:{rg_code}"))

    return sorted(tuples)


async def write(tuples: list[tuple[str, str, str]]) -> None:
    from app.core.authz import fga
    from app.core.authz.config import settings
    print(f"\nwriting {len(tuples)} tuples to {settings.fga_api_url} "
          f"store {settings.fga_store_id}")
    async with fga.lifespan_client():
        for t in tuples:
            await fga.write_tuples(writes=[t], idempotent=True)
    print("done")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print tuples, write nothing")
    args = ap.parse_args()
    tuples = collect()
    if args.dry_run:
        for u, r, o in tuples:
            print(f"{u}  {r}  {o}")
        print(f"\n{len(tuples)} tuples (dry run — nothing written)")
        return
    asyncio.run(write(tuples))


if __name__ == "__main__":
    main()
