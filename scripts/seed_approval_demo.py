"""Seed one reviewable change request, so the approval flow can be exercised.

Nothing creates `submit` rows yet (draft -> submit lands in a later pass) and
nobody holds `approver`, so every inbox is empty by construction. This makes
both exist:

  1. an `approver` tuple in OpenFGA, on the RESOURCE GROUP that owns the chosen
     service — granted at the group so it cascades to every service under it,
     which is the whole point of the model
  2. a transaction_queue row in `submit`, carrying a full config_snapshot and a
     from/to `changes` diff for the UI to render

Everything it writes is tagged DEMO and removable with --cleanup.

    python scripts/seed_approval_demo.py --list
    python scripts/seed_approval_demo.py --user <user_mst.code>
    python scripts/seed_approval_demo.py --user <user_mst.code> --cleanup
"""
import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.session import AsyncSessionLocal  # noqa: E402

DEMO_PREFIX = "queue-demo-"


async def list_candidates() -> None:
    """Users and services worth seeding against."""
    async with AsyncSessionLocal() as s:
        print("\n--- users (pick one for --user) ---")
        rows = await s.execute(text("""
            SELECT code, name, tenants_mst_code, is_org_owner
            FROM user_mst
            WHERE is_deleted IS NOT TRUE
            ORDER BY is_org_owner DESC NULLS LAST, name
            LIMIT 8"""))
        for code, name, tenant, owner in rows:
            print(f"  {code}  {name}{' (org owner)' if owner else ''}")
            print(f"      tenant={tenant}")

        print("\n--- service configs that have a resource group ---")
        rows = await s.execute(text("""
            SELECT sc.code, sm.name, sc.environment, sm.resource_group_mst_code
            FROM service_configs sc
            JOIN services_mst sm ON sm.code = sc.services_mst_code
            WHERE sc.is_deleted IS NOT TRUE
              AND sm.resource_group_mst_code IS NOT NULL
            LIMIT 5"""))
        for code, name, env, rg in rows:
            print(f"  {code}")
            print(f"      service={name} env={env} rg={rg}")


async def pick_targets(user_code: str, count: int = 1) -> list[dict]:
    """Service configs in the user's tenant that have a resource group.

    The group matters: `approver` is granted there, not on the service, so a
    service whose group is NULL could never be approved by anyone.

    Distinct services, so the demo shows several rows rather than one service
    with several requests stacked on it.
    """
    async with AsyncSessionLocal() as s:
        user = (await s.execute(text("""
            SELECT code, tenants_mst_code FROM user_mst
            WHERE code = :u AND is_deleted IS NOT TRUE"""), {"u": user_code})).first()
        if not user:
            sys.exit(f"no such user: {user_code}")

        rows = (await s.execute(text("""
            SELECT DISTINCT ON (sc.services_mst_code)
                   sc.code AS cfg, sc.config, sc.environment, sc.geo_loc_mst_code,
                   sc.services_mst_code, sc.infrastructuretype_ref_code,
                   sc.infrastructure_mst_code, sm.name AS service_name,
                   sm.resource_group_mst_code AS rg, sm.applications_mst_code AS app
            FROM service_configs sc
            JOIN services_mst sm ON sm.code = sc.services_mst_code
            WHERE sc.tenant_mst_code = :t
              AND sc.is_deleted IS NOT TRUE
              -- The backfill skips configs whose SERVICE is deleted, so such a
              -- config has no object in OpenFGA at all: can_approve is false
              -- for everyone and the request would silently never appear.
              -- Same filter here, or the seed picks something invisible.
              AND sm.is_deleted IS NOT TRUE
              AND sm.resource_group_mst_code IS NOT NULL
            ORDER BY sc.services_mst_code, sc.code
            LIMIT :n"""), {"t": user[1], "n": count})).mappings().all()
        if not rows:
            sys.exit(f"no service config with a resource group in tenant {user[1]}")

        # Colleagues to attribute the requests to. The approver reviewing their
        # OWN request is the uninteresting case — and with
        # allow_self_approval=false it would be refused outright — so requests
        # come from other people in the tenant, rotated across the set.
        others = [r[0] for r in (await s.execute(text("""
            SELECT code FROM user_mst
            WHERE tenants_mst_code = :t AND is_deleted IS NOT TRUE
              AND code <> :u
            ORDER BY name"""), {"t": user[1], "u": user_code}))]
        if not others:
            others = [user[0]]  # a lone user in the tenant reviews their own

        return [
            {
                "user_code": user[0],                       # the approver
                "requested_by": others[i % len(others)],    # a colleague
                "tenant_code": user[1],
                **dict(r),
            }
            for i, r in enumerate(rows)
        ]


async def seed(user_code: str, count: int = 1) -> None:
    targets = await pick_targets(user_code, count)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"\nuser    {targets[0]['user_code']}")
    print(f"tenant  {targets[0]['tenant_code']}")

    # ── 1. approver on every group involved, granted at the GROUP so it
    #      cascades down to the services under it ───────────────────────────
    from app.core.authz import fga

    groups = sorted({t["rg"] for t in targets})
    async with fga.lifespan_client():
        await fga.write_tuples(
            writes=[(f"user:{user_code}", "approver", f"resource_group:{g}")
                    for g in groups],
            idempotent=True,
        )
    print(f"\nFGA   approver on {len(groups)} resource group(s):")
    for g in groups:
        print(f"        resource_group:{g}")
    print()

    for t in targets:
        await _seed_one(t, now)

    print("\ntry:")
    print("  GET  /api/v1/approvals/services")
    print("  GET  /api/v1/approvals?status=submit")


async def _seed_one(t: dict, now: str) -> None:
    """One change request against one service config.

    config_snapshot is the FULL config — a partial one would fail at deploy.
    `changes` is only the from/to the reviewer reads.
    """
    current = dict(t["config"] or {})
    old_cpu = str(current.get("cpu", "1000m"))
    new_cpu = "4000m" if old_cpu != "4000m" else "2000m"
    proposed = {**current, "cpu": new_cpu}

    snapshot = {
        **proposed,
        "code": t["cfg"],
        "services_mst_code": t["services_mst_code"],
        "service_name": t["service_name"],
        "geo_loc_mst_code": t["geo_loc_mst_code"],
        "environment": t["environment"],
        "infrastructuretype_ref_code": t["infrastructuretype_ref_code"],
        "infrastructure_mst_code": t["infrastructure_mst_code"],
        "applications_mst_code": t["app"],
    }
    queue_code = f"{DEMO_PREFIX}{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            INSERT INTO transaction_queue
                (code, user_code, tenant_code, transaction_code, table_name,
                 config_snapshot, changes, display_name, status, history,
                 created_at, updated_at, status_last_updated_at,
                 is_deleted, is_active)
            VALUES
                (:code, :user_code, :tenant_code, :txn, 'SERVICE_CONFIG',
                 CAST(:snapshot AS jsonb), CAST(:changes AS jsonb), :display,
                 'submit', CAST(:history AS jsonb),
                 now(), now(), now(), false, true)"""),
            {
                "code": queue_code,
                "user_code": t["requested_by"],
                "tenant_code": t["tenant_code"],
                "txn": t["cfg"],
                "snapshot": json.dumps(snapshot),
                "changes": json.dumps({"cpu": {"from": old_cpu, "to": new_cpu}}),
                "display": f"DEMO: {t['service_name']} {t['environment']} cpu",
                "history": json.dumps([
                    {"at": now, "by": t["requested_by"], "event": "created"},
                    {"at": now, "by": t["requested_by"], "event": "submitted"},
                ]),
            })
        await s.commit()

    print(f"DB    {queue_code}  {t['service_name']} ({t['environment']})  "
          f"cpu {old_cpu} -> {new_cpu}  by {t['requested_by'][:8]}")


async def cleanup(user_code: str) -> None:
    """Remove everything this script created."""
    async with AsyncSessionLocal() as s:
        res = await s.execute(text("""
            DELETE FROM transaction_queue
            WHERE code LIKE :p"""), {"p": f"{DEMO_PREFIX}%"})
        await s.commit()
        print(f"removed {res.rowcount} demo queue row(s)")

    targets = await pick_targets(user_code, 25)
    from app.core.authz import fga

    groups = sorted({t["rg"] for t in targets})
    async with fga.lifespan_client():
        await fga.write_tuples(
            deletes=[(f"user:{user_code}", "approver", f"resource_group:{g}")
                     for g in groups],
            idempotent=True,
        )
    print(f"removed approver tuple(s) on {len(groups)} resource group(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show users and services")
    ap.add_argument("--user", help="user_mst.code to make an approver")
    ap.add_argument("--count", type=int, default=1,
                    help="how many services to seed a request against")
    ap.add_argument("--cleanup", action="store_true", help="undo what this created")
    args = ap.parse_args()

    if args.list:
        asyncio.run(list_candidates())
    elif args.user and args.cleanup:
        asyncio.run(cleanup(args.user))
    elif args.user:
        asyncio.run(seed(args.user, args.count))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
