"""Backfill the per-tenant default IAM role for existing tenants.

For each tenant with ``config.github.infra_repository`` set:

1. Push ``environment/<tenant>/<index>/aws/<region>/default-role/terragrunt.hcl``
   to their infra repo (if missing) — a fresh template with empty
   ``custom_policy_json``. Terraform will create the role on next apply.

2. Write ``default_role_arn`` into ``infra_vendor_accounts_mst.auth_config`` for
   the tenant's AWS + devlift_k8s rows (so manifest generation picks it up).

Idempotent. Skips tenants missing an infra repo. Skips the GitHub push when the
file already exists.

Usage:
  python scripts/backfill_tenant_default_role.py --dry-run   # show plan, no writes
  python scripts/backfill_tenant_default_role.py             # execute
  python scripts/backfill_tenant_default_role.py --tenant aslam   # single tenant
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Iterable, List

# Make the backend package importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.enum import InfraVendorEnum
from app.db.session import AsyncSessionLocal
from app.db.models.infra_vendor_accounts_mst_model import InfraVendorAccountsMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.integrations.github_integration import GitHubIntegration
from app.plugin.default.default_aws_role_gen_component import (
    _render_template_for_tenant,
)
from app.services.org_infrastructure_service import compute_default_role_name
from app.utils.github_app_token import get_token


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_tenant_default_role")


def _role_arn(subdomain: str) -> str:
    name = compute_default_role_name(subdomain)
    return f"arn:aws:iam::{settings.onboarding_default_account_id}:role/{name}"


def _role_file_path(subdomain: str) -> str:
    return (
        f"environment/{subdomain}/{settings.onboarding_default_index}"
        f"/aws/{settings.onboarding_default_region}"
        f"/application/default/terragrunt.hcl"
    )


async def _ensure_github_file(
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    file_path: str,
    content: str,
    dry_run: bool,
) -> str:
    """Push the file if it's missing. Returns a status word."""
    base_url = settings.github_base_url.rstrip("/")
    existing = await GitHubIntegration.get_file_content(
        token=token, base_url=base_url, owner=owner, repo=repo,
        file_path=file_path, branch=branch,
    )
    if existing and existing.get("exists"):
        return "already_exists"

    if dry_run:
        return "would_create"

    await GitHubIntegration.update_or_create_file(
        token=token, base_url=base_url, owner=owner, repo=repo, branch=branch,
        file_path=file_path, content=content,
        message="iam: add per-tenant default IAM role terragrunt",
    )
    return "created"


async def _update_auth_config(
    session,
    *,
    tenant_code: str,
    default_role_arn: str,
    dry_run: bool,
) -> List[str]:
    """Set ``auth_config.default_role_arn`` on every vendor row for this tenant.

    Returns list of ``{code}:{status}`` strings.
    """
    q = await session.execute(
        select(InfraVendorAccountsMstModel).where(
            InfraVendorAccountsMstModel.tenants_mst_code == tenant_code,
            InfraVendorAccountsMstModel.is_deleted == False,
        )
    )
    rows = q.scalars().all()
    results: List[str] = []
    for row in rows:
        auth = dict(row.auth_config or {})
        if auth.get("default_role_arn") == default_role_arn:
            results.append(f"{row.code}:unchanged")
            continue
        if dry_run:
            results.append(f"{row.code}:would_update")
            continue
        auth["default_role_arn"] = default_role_arn
        row.auth_config = auth
        flag_modified(row, "auth_config")
        results.append(f"{row.code}:updated")
    return results


async def _run(
    tenant_filter: Iterable[str] | None,
    dry_run: bool,
    skip_github: bool,
) -> None:
    async with AsyncSessionLocal() as session:
        q = await session.execute(
            select(TenantsMstModel).where(TenantsMstModel.is_deleted == False)
        )
        tenants = q.scalars().all()

        token = None if skip_github else await get_token(
            settings.github_app_platform_installation_id
        )

        processed = 0
        for t in tenants:
            if tenant_filter and t.code not in tenant_filter:
                continue

            config = t.config or {}
            gh = config.get("github") or {}
            infra_repo = gh.get("infra_repository")
            branch = gh.get("infra_branch")

            if not infra_repo or "/" not in infra_repo or not branch:
                logger.info("SKIP %s — no infra_repository/infra_branch", t.code)
                continue

            owner, repo = infra_repo.split("/", 1)
            logger.info("TENANT %s → %s/%s@%s", t.code, owner, repo, branch)

            if not skip_github:
                file_path = _role_file_path(t.code)
                content = await _render_template_for_tenant(t.code)
                try:
                    gh_status = await _ensure_github_file(
                        token=token, owner=owner, repo=repo, branch=branch,
                        file_path=file_path, content=content, dry_run=dry_run,
                    )
                    logger.info("  github: %s (path=%s)", gh_status, file_path)
                except Exception as exc:
                    logger.warning("  github: ERROR %s", exc)
            else:
                logger.info("  github: skipped (--skip-github)")

            default_arn = _role_arn(t.code)
            auth_status = await _update_auth_config(
                session, tenant_code=t.code,
                default_role_arn=default_arn, dry_run=dry_run,
            )
            logger.info("  auth_config (%s): %s", default_arn, ", ".join(auth_status) or "no rows")

            processed += 1

        if not dry_run:
            await session.commit()
        logger.info("Processed %d tenants (dry_run=%s skip_github=%s)", processed, dry_run, skip_github)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Plan only; no DB or GitHub writes.")
    p.add_argument(
        "--tenant", action="append", default=None,
        help="Process only this tenant code (repeatable).",
    )
    p.add_argument(
        "--skip-github", action="store_true",
        help="Only update auth_config in DB; don't push the terragrunt file.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(_run(
        tenant_filter=args.tenant, dry_run=args.dry_run,
        skip_github=args.skip_github,
    ))
