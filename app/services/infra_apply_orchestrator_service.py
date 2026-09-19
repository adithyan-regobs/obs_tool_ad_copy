"""Infra-apply orchestrator.

Fires per-resource Jenkins builds (one per infra row) plus one IAM build
right after the tenant's infra repo PR is auto-merged. Each pipeline runs
``terragrunt apply`` against a single resource directory; IAM gets its own
pipeline that applies the shared ``application/default`` role policy. All
builds run in parallel — there is no fence and no ordering between them.

Stage signalling and run-track persistence go through the existing
``/jenkins-webhook`` endpoint.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enum import EnvironmentEnum, InfraVendorEnum
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.domain.policies.kinds import get_kind_for_resource
from app.repository.infra_vendor_accounts_mst_repository import (
    InfraVendorAccountsMstRepository,
)
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.services.jenkins_provisioning_service import JenkinsProvisioningService

logger = logging.getLogger(__name__)


_SUPPORTED_INFRA_TYPES = {
    "s3_infrastructuretype_ref": "s3",
    "sqs_infrastructuretype_ref": "sqs",
    "dynamodb_infrastructuretype_ref": "dynamodb",
    "elasticache_redis_infrastructuretype_ref": "redis",
    "aurora_postgres_infrastructuretype_ref": "aurora",
    "aurora_mysql_infrastructuretype_ref": "aurora",
}


def _sanitize_identifier(identifier: str) -> str:
    """Mirror DefaultFileLocator._sanitize_identifier — lowercase + dash-only."""
    safe = re.sub(r"[^a-z0-9-]", "-", (identifier or "").lower()).strip("-")
    return safe or "default"


def _resource_dir(resource_type: str, tenant: str, identifier_sanitized: str) -> str:
    """Path inside the tenant infra repo for a given resource."""
    base = f"environment/{tenant}/{settings.onboarding_default_index}/aws/{settings.onboarding_default_region}"
    if resource_type == "s3":
        return f"{base}/buckets/{identifier_sanitized}"
    if resource_type == "sqs":
        return f"{base}/queues/{identifier_sanitized}"
    if resource_type == "dynamodb":
        return f"{base}/dynamo/{identifier_sanitized}"
    if resource_type == "redis":
        return f"{base}/redis/{identifier_sanitized}"
    if resource_type == "aurora":
        return f"{base}/rds/{identifier_sanitized}"
    raise ValueError(f"Unsupported resource type: {resource_type}")


def _iam_dir(tenant: str) -> str:
    """Path inside the tenant infra repo for the shared default-role policy."""
    return (
        f"environment/{tenant}/{settings.onboarding_default_index}"
        f"/aws/{settings.onboarding_default_region}"
        f"/application/default"
    )


def _expected_resource_arns(resource_type: str, primary_arn: str, locator: dict | None = None) -> str:
    """Build the comma-separated ARN list for IAM simulate-principal-policy.

    Must match what the role-policy renderer writes:
    - S3: bucket ARN + object ARN (bucket/*)
    - Redis: replication group ARN + user ARN
    """
    if not primary_arn:
        return ""
    if resource_type == "s3":
        return f"{primary_arn},{primary_arn}/*"
    if resource_type == "redis" and locator:
        user_id = locator.get("user_id")
        if user_id:
            region = settings.onboarding_default_region
            account_id = settings.onboarding_default_account_id
            user_arn = f"arn:aws:elasticache:{region}:{account_id}:user:{user_id.lower()}"
            return f"{primary_arn},{user_arn}"
    return primary_arn


def _resolve_environment_enum(value: str) -> Optional[EnvironmentEnum]:
    if not value:
        return None
    try:
        return EnvironmentEnum(value)
    except ValueError:
        return None


class InfraApplyOrchestratorService:
    """Fires per-resource + IAM Jenkins builds after the infra PR is merged."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.jenkins_svc = JenkinsProvisioningService(db)
        self.infra_repo = InfrastructureMstRepository(db)
        self.vendor_repo = InfraVendorAccountsMstRepository(db)

    async def trigger_for_merged_infra_pr(
        self,
        tenant_code: str,
        infra_repo: str,
        infra_branch: str,
        queue_items: Iterable[TransactionQueueModel],
    ) -> List[Dict[str, Any]]:
        """Fire infra-apply builds for every supported resource in this batch
        plus one IAM build per (tenant, environment).

        Returns a list of build trigger results (one per fired build).
        Failures triggering an individual build are logged and skipped — they
        don't abort the whole orchestration.
        """
        eligible = self._select_eligible_items(queue_items)
        if not eligible:
            logger.info(
                "No infra-apply eligible queue items for tenant %s in this batch",
                tenant_code,
            )
            return []

        # Group by environment so each (tenant, env) gets exactly one IAM build
        by_env: Dict[str, List[TransactionQueueModel]] = {}
        for item in eligible:
            env = self._extract_environment(item)
            if not env:
                logger.warning(
                    "Skipping queue item %s — could not determine environment", item.code,
                )
                continue
            by_env.setdefault(env, []).append(item)

        # Trigger sequentially. The orchestrator + jenkins service share a single
        # AsyncSession (one asyncpg connection); fanning out via asyncio.gather
        # raises ``InterfaceError: cannot perform operation: another operation in
        # progress`` because all branches contend for the same connection (vendor
        # lookup, GitHub token issuance, run_track create).
        #
        # Real parallelism still happens — Jenkins runs the submitted builds in
        # parallel on its side. Submitting them serially adds only a few hundred
        # ms total.
        results: List[Dict[str, Any]] = []
        for environment, items in by_env.items():
            try:
                ensured = await self.jenkins_svc.ensure_infra_apply_pipeline(
                    tenant_code, environment,
                )
            except Exception:
                logger.exception(
                    "ensure_infra_apply_pipeline failed for tenant=%s env=%s — "
                    "skipping fan-out for this env",
                    tenant_code, environment,
                )
                continue
            if not ensured.get("job_name") or not ensured.get("pipeline_code"):
                logger.error(
                    "ensure_infra_apply_pipeline returned no job_name for tenant=%s "
                    "env=%s: %s — skipping fan-out for this env",
                    tenant_code, environment, ensured,
                )
                continue

            for item in items:
                try:
                    r = await self._trigger_resource_build(
                        tenant_code=tenant_code,
                        environment=environment,
                        infra_repo=infra_repo,
                        infra_branch=infra_branch,
                        queue_item=item,
                        ensured=ensured,
                    )
                    if r is not None:
                        results.append(r)
                except Exception as exc:
                    logger.error(
                        "Infra-apply resource build failed for queue %s: %s",
                        item.code, exc, exc_info=True,
                    )
                    results.append({"status": "error", "error": str(exc), "queue_code": item.code})

            try:
                r = await self._trigger_iam_build(
                    tenant_code=tenant_code,
                    environment=environment,
                    infra_repo=infra_repo,
                    infra_branch=infra_branch,
                    ensured=ensured,
                )
                if r is not None:
                    results.append(r)
            except Exception as exc:
                logger.error(
                    "Infra-apply IAM build failed for tenant=%s env=%s: %s",
                    tenant_code, environment, exc, exc_info=True,
                )
                results.append({"status": "error", "error": str(exc), "kind": "iam"})

        return results

    @staticmethod
    def _select_eligible_items(
        queue_items: Iterable[TransactionQueueModel],
    ) -> List[TransactionQueueModel]:
        out = []
        for item in queue_items:
            table_name = getattr(item.table_name, "value", item.table_name)
            if table_name != "INFRASTRUCTURE":
                continue
            # skip delete operations — no terragrunt apply needed, only IAM revoke
            case_ref = getattr(item, "case_ref_code", None) or ""
            if case_ref.startswith("delete_"):
                continue
            snap = item.config_snapshot or {}
            infra_type_ref = snap.get("infrastructuretype_ref_code")
            if infra_type_ref in _SUPPORTED_INFRA_TYPES:
                out.append(item)
        return out

    @staticmethod
    def _extract_environment(item: TransactionQueueModel) -> str:
        snap = item.config_snapshot or {}
        env = snap.get("environment")
        if isinstance(env, dict):
            env = env.get("value")
        return env or ""

    async def _trigger_resource_build(
        self,
        tenant_code: str,
        environment: str,
        infra_repo: str,
        infra_branch: str,
        queue_item: TransactionQueueModel,
        ensured: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        snap = queue_item.config_snapshot or {}
        infra_type_ref = snap.get("infrastructuretype_ref_code")
        resource_type = _SUPPORTED_INFRA_TYPES[infra_type_ref]

        infra_code = queue_item.transaction_code
        infra_row = await self.infra_repo.get_by_code(infra_code) if infra_code else None
        if not infra_row:
            logger.warning(
                "infrastructure_mst row %s not found for queue %s — skipping",
                infra_code, queue_item.code,
            )
            return None

        identifier = (
            (infra_row.locator or {}).get("identifier")
            or (infra_row.locator or {}).get("db_server_name")
            or snap.get("identifier")
            or snap.get("db_server_name")
            or ""
        )
        terragrunt_path = _resource_dir(
            resource_type, tenant_code, _sanitize_identifier(identifier),
        )

        primary_arn = self._resource_arn(infra_row, resource_type)
        role_arn = await self._tenant_role_arn(tenant_code, environment)
        kind = get_kind_for_resource(infra_type_ref)
        expected_actions = ",".join(kind.actions) if kind else ""
        expected_resource_arns = _expected_resource_arns(resource_type, primary_arn, infra_row.locator)

        try:
            result = await self.jenkins_svc.trigger_infra_apply_build(
                tenant_code=tenant_code,
                environment=environment,
                resource_type=resource_type,
                resource_code=infra_row.code,
                terragrunt_path=terragrunt_path,
                infra_repo=infra_repo,
                infra_branch=infra_branch,
                role_arn=role_arn,
                expected_actions=expected_actions,
                expected_resource_arns=expected_resource_arns,
                queue_codes=[queue_item.code],
                ensured=ensured,
            )
            # Echo queue context so callers can correlate this build's
            # run_code back to the originating queue item — required by the
            # MCP polling path (Redis draft → pipeline_run_track_code).
            if isinstance(result, dict):
                return {
                    **result,
                    "queue_id": queue_item.id,
                    "queue_code": queue_item.code,
                    "kind": "infra_apply",
                }
            return result
        except Exception:
            logger.exception(
                "Failed to trigger infra-apply build for %s (%s)",
                infra_row.code, resource_type,
            )
            raise

    async def _trigger_iam_build(
        self,
        tenant_code: str,
        environment: str,
        infra_repo: str,
        infra_branch: str,
        ensured: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            return await self.jenkins_svc.trigger_infra_apply_build(
                tenant_code=tenant_code,
                environment=environment,
                resource_type="iam",
                resource_code=f"IAM_{tenant_code}_{environment}",
                terragrunt_path=_iam_dir(tenant_code),
                infra_repo=infra_repo,
                infra_branch=infra_branch,
                queue_codes=[],
                ensured=ensured,
            )
        except Exception:
            logger.exception(
                "Failed to trigger IAM apply build for tenant=%s env=%s",
                tenant_code, environment,
            )
            raise

    @staticmethod
    def _resource_arn(infra_row, resource_type: str) -> str:
        """Build the ARN of the actual deployed resource.

        The factory-stored ``locator.{bucket,queue,table}_arn`` are NOT trustworthy:
        they are computed from ``locator.region`` / ``locator.accountId`` which
        come straight off the create-form snapshot (often a placeholder
        account/region). The role policy renderer uses
        ``settings.onboarding_default_region`` + ``settings.onboarding_default_account_id``
        instead, and the real resource lands in those — so reconstruct from
        the same source of truth here.
        """
        if getattr(infra_row, "resource_identifier", None):
            return infra_row.resource_identifier  # backfilled from terragrunt output

        loc = infra_row.locator or {}
        region = settings.onboarding_default_region
        account_id = settings.onboarding_default_account_id

        # Lowercase the name segment to match what default_aws_role_gen_component
        # writes into the role policy (which itself matches what Terraform
        # actually creates in AWS — see e.g. SQS apply log producing
        # "kenzmo-virginia-01-kenzmoq" even when the user typed "kenzmoQ").
        # Without this, simulate-principal-policy compares "kenzmo-...-kenzmoQ"
        # against the role's "kenzmo-...-kenzmoq" and returns implicitDeny.
        if resource_type == "s3":
            bucket = loc.get("bucket_name")
            return f"arn:aws:s3:::{bucket.lower()}" if bucket else ""
        if resource_type == "sqs":
            queue = loc.get("queue_name")
            return f"arn:aws:sqs:{region}:{account_id}:{queue.lower()}" if queue else ""
        if resource_type == "dynamodb":
            table = loc.get("table_name")
            return f"arn:aws:dynamodb:{region}:{account_id}:table/{table.lower()}" if table else ""
        if resource_type == "redis":
            cluster = loc.get("redis_cluster_name")
            return f"arn:aws:elasticache:{region}:{account_id}:replicationgroup:{cluster.lower()}" if cluster else ""
        if resource_type == "aurora":
            # Aurora ARN uses the cluster identifier (predictable from naming convention).
            # The rds-db:connect IAM ARN is set via Phase 2 webhook using db_cluster_resource_id.
            db_server_name = loc.get("db_server_name", "")
            return f"arn:aws:rds:{region}:{account_id}:cluster:{db_server_name.lower()}" if db_server_name else ""
        return ""

    async def _tenant_role_arn(self, tenant_code: str, environment: str) -> str:
        env_enum = _resolve_environment_enum(environment)
        if not env_enum:
            return ""
        vendor = await self.vendor_repo.get_by_tenant_and_vendor(
            tenant_code=tenant_code,
            infra_vendor_enum=InfraVendorEnum.aws,
            environments_enum=env_enum,
        )
        if not vendor:
            return ""
        auth = vendor.auth_config or {}
        return auth.get("default_role_arn") or ""
