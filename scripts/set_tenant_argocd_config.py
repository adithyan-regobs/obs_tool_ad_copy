"""Write a tenant's ArgoCD connection settings into tenants_mst.config.

The token itself never goes in the database — only the name of the env var
holding it, so rotating the token is an env change and nothing else.

Usage:
  python scripts/set_tenant_argocd_config.py --tenant aspora --env stage \
      --host vance-core-stage-mumbai-01-application-argocd.internal.genorim.xyz \
      --project argocd-project-core-stage-applications-service \
      --token-env ARGOCD_TOKEN_ASPORA_STAGE

  python scripts/set_tenant_argocd_config.py --tenant aspora --env stage --show
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import AsyncSessionLocal
from app.db.models.tenants_mst_model import TenantsMstModel

DEFAULT_APP_NAMESPACE = "argocd-system"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--env", required=True, choices=["dev", "stage", "qa", "prod"])
    parser.add_argument("--host")
    parser.add_argument("--project")
    parser.add_argument("--token-env")
    parser.add_argument("--app-namespace", default=DEFAULT_APP_NAMESPACE)
    parser.add_argument("--show", action="store_true", help="print current config and exit")
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        tenant = (
            await db.execute(
                select(TenantsMstModel).where(
                    TenantsMstModel.code == args.tenant,
                    TenantsMstModel.is_deleted == False,
                )
            )
        ).scalar_one_or_none()

        if not tenant:
            print(f"tenant not found: {args.tenant}")
            return 1

        config = dict(tenant.config or {})
        argocd = dict(config.get("argocd") or {})

        if args.show:
            print(json.dumps(argocd, indent=2))
            return 0

        missing = [f for f in ("host", "project", "token_env") if not getattr(args, f)]
        if missing:
            print("missing required options: " + ", ".join("--" + m.replace("_", "-") for m in missing))
            return 1

        argocd[args.env] = {
            "host": args.host,
            "project": args.project,
            "app_namespace": args.app_namespace,
            "token_env": args.token_env,
        }
        config["argocd"] = argocd
        tenant.config = config
        flag_modified(tenant, "config")
        db.add(tenant)
        await db.commit()

        print(f"{args.tenant}/{args.env} ->")
        print(json.dumps(argocd[args.env], indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
