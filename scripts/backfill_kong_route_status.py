"""
One-off backfill: mark a service's already-deployed Kong routes as ACTIVE.

Use when routes were merged/applied before the deploy pipeline learned to update
kong_route_configs.creation_status (so they're stuck at INITIATED / "Not deployed").

Usage:
    ./venv/bin/python scripts/backfill_kong_route_status.py --list
    ./venv/bin/python scripts/backfill_kong_route_status.py --service SVC_CODE --env stage [--geo GEO] [--apply]

Without --apply it's a dry run (prints what would change).
"""
import argparse
import asyncio

from sqlalchemy import select, func

from app.db.session import AsyncSessionLocal
from app.db.models.kong_route_config_model import KongRouteConfigModel as M
from app.core.enum import DeploymentStatusEnum
from app.repository.kong_route_configs_repository import KongRouteConfigsRepository


async def list_stuck() -> None:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(
                M.services_mst_code, M.environments_enum, M.geo_loc_mst_code,
                M.creation_status, func.count().label("n"),
            )
            .where(M.is_deleted == False)  # noqa: E712
            .group_by(M.services_mst_code, M.environments_enum, M.geo_loc_mst_code, M.creation_status)
            .order_by(M.services_mst_code)
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            print("No active Kong routes found.")
            return
        print(f"{'service':<24} {'env':<8} {'geo':<10} {'status':<20} count")
        print("-" * 78)
        for r in rows:
            env = getattr(r.environments_enum, "value", r.environments_enum)
            cs = getattr(r.creation_status, "value", r.creation_status)
            print(f"{r.services_mst_code:<24} {str(env):<8} {str(r.geo_loc_mst_code or ''):<10} {str(cs):<20} {r.n}")


async def backfill(service: str, env: str, geo: str | None, apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        repo = KongRouteConfigsRepository(db)
        if not apply:
            # Dry run: count what matches.
            filters = [M.services_mst_code == service, M.is_deleted == False]  # noqa: E712
            if env:
                filters.append(M.environments_enum == env)
            if geo:
                filters.append(M.geo_loc_mst_code == geo)
            n = (await db.execute(select(func.count()).select_from(M).where(*filters))).scalar_one()
            print(f"[dry-run] would set {n} route(s) for service={service} env={env} geo={geo or '*'} -> ACTIVE")
            print("Re-run with --apply to write.")
            return
        updated = await repo.bulk_update_creation_status(
            service_code=service,
            creation_status=DeploymentStatusEnum.ACTIVE,
            environment=env or None,
            geo_loc_mst_code=geo or None,
        )
        await db.commit()
        print(f"Updated {updated} route(s) for service={service} env={env} geo={geo or '*'} -> ACTIVE ✓")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List active routes grouped by service/env/status")
    ap.add_argument("--service", help="services_mst_code to backfill")
    ap.add_argument("--env", default="", help="environment (e.g. stage, prod)")
    ap.add_argument("--geo", default="", help="geo_loc_mst_code (optional)")
    ap.add_argument("--apply", action="store_true", help="Actually write (otherwise dry run)")
    args = ap.parse_args()

    if args.list:
        asyncio.run(list_stuck())
        return
    if not args.service:
        ap.error("--service is required (or use --list)")
    asyncio.run(backfill(args.service, args.env, args.geo or None, args.apply))


if __name__ == "__main__":
    main()
