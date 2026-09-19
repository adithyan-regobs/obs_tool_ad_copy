"""
Jenkins Build Webhook Endpoint

Receives per-stage and final build notifications from Jenkins.
- Stage webhooks: update queue items to stage status, append stage data to run_track.build_stages
- Final webhook (SUCCESS/FAILURE): mark DEPLOYED/FAILED, store ALB URL

Authenticated via shared secret in X-Webhook-Secret header.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.dependencies import get_db
from app.core.config import settings
from app.core.enum import (
    DeploymentStatusEnum,
    PipelineRunStatusEnum,
    ResourceStatusEnum,
    WorkflowSourceTableEnum,
)
from app.db.models.pipeline_mst_model import PipelineMstModel
from app.db.models.pipeline_run_track_model import PipelineRunTrackModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.transaction_queue_model import TransactionQueueModel, TransactionQueueStatusEnum
from app.integrations.cloudflare_integration import CloudflareIntegration
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
from app.repository.transaction_queue_repository import TransactionQueueRepository
from app.repository.service_config_repository import ServiceConfigRepository

router = APIRouter()
logger = logging.getLogger(__name__)


class JenkinsBuildPayload(BaseModel):
    """Payload sent by Jenkins at each stage and on build completion."""
    job_name: str = Field(..., description="Jenkins job name")
    build_number: int = Field(..., description="Jenkins build number")
    status: str = Field(..., description="Build result: SUCCESS, FAILURE, ABORTED, or IN_PROGRESS for stage webhooks")
    build_url: str = Field(default="", description="Full Jenkins build URL")
    commit_sha: str = Field(default="", description="Git commit SHA that triggered the build")
    alb_url: str = Field(default="", description="ALB URL from kubectl get ingress")
    # Stage webhook fields
    stage: Optional[str] = Field(default=None, description="Current stage: initialisation, checkout, building, deploying, verification")
    stage_duration_secs: Optional[int] = Field(default=None, description="Duration of the completed stage in seconds")
    # Pod failure diagnosis (populated on deployment failure)
    error_message: str = Field(default="", description="Pod failure diagnosis: OOMKilled, CrashLoopBackOff, etc.")
    # ObsTool run tracking code for exact pipeline_run_track matching
    run_code: Optional[str] = Field(default=None, description="ObsTool pipeline_run_track code passed via OBS_TOOL_RUN_CODE build param")
    # Service type for determining which stages are applicable
    service_type: Optional[str] = Field(default=None, description="REGULAR | MODEL_SERVING | BOOTSTRAP | INFRA_APPLY")
    # Infra-apply final-payload fields (only populated for service_type=INFRA_APPLY)
    resource_arn: str = Field(default="", description="Created resource ARN captured from terragrunt outputs")
    resource_outputs: str = Field(default="", description="Raw JSON of terragrunt outputs (for locator merge)")
    failure_kind: Optional[str] = Field(default=None, description="PERMISSIONS_MISSING vs FAILURE — disambiguates failure cause for infra-apply runs")


_JENKINS_STATUS_MAP = {
    "SUCCESS": PipelineRunStatusEnum.COMPLETED,
    "FAILURE": PipelineRunStatusEnum.FAILED,
    "ABORTED": PipelineRunStatusEnum.CANCELLED,
    "UNSTABLE": PipelineRunStatusEnum.FAILED,
}

# Map stage names to transaction queue status values
_STAGE_TO_QUEUE_STATUS = {
    "initialisation": TransactionQueueStatusEnum.PROVISIONING,
    "checkout": TransactionQueueStatusEnum.CHECKOUT,
    "building": TransactionQueueStatusEnum.BUILDING,
    "deploying": TransactionQueueStatusEnum.DEPLOYING,
    "verification": TransactionQueueStatusEnum.VERIFICATION,
}

# Map webhook stage keys to user-facing display names
_STAGE_DISPLAY_NAMES: dict[str, str] = {
    "initialisation": "Initialisation",
    "checkout": "Checkout",
    "building": "Build",
    "deploying": "Deploy",
    "verification": "Verify",
}

# Sequential stage order — each new stage auto-completes the previous one
_STAGE_ORDER = ["initialisation", "checkout", "building", "deploying", "verification"]


def _build_infra_variables(
    infra_type: str,
    locator: dict,
) -> list:
    """Compute the ``[(key, value), ...]`` pairs to upsert into variable_mst.

    Mirrors what the post-action components save at PR-creation time:
    S3 → S3_BUCKET_NAME, SQS → SQS_QUEUE_URL, DynamoDB → DYNAMODB_TABLE_NAME,
    Redis → ELASTICACHE_CLUSTER_NAME/PORT/USER_ID/HOST,
    plus AWS_REGION for all. The webhook re-upserts on apply-success so any
    TF-output divergence from the predicted naming lands in variable_mst.
    """
    region = settings.onboarding_default_region
    account_id = settings.onboarding_default_account_id
    pairs: list = []

    # Lowercase resource names — see InfraApplyOrchestratorService._resource_arn
    # for the rationale. The factory preserves user-typed case in
    # locator.{queue|bucket|table}_name but Terraform/AWS actually create the
    # resource with a lowercased name, and the role-policy renderer also
    # lowercases. Stored env-var values must match AWS reality so apps can
    # actually find their resource.
    if infra_type == "s3_infrastructuretype_ref":
        bucket = (locator.get("bucket_name") or "").lower() or None
        if bucket:
            pairs.append(("S3_BUCKET_NAME", bucket))

    elif infra_type == "sqs_infrastructuretype_ref":
        queue = (locator.get("queue_name") or "").lower() or None
        if queue:
            pairs.append(("SQS_QUEUE_URL", f"https://sqs.{region}.amazonaws.com/{account_id}/{queue}"))

    elif infra_type == "dynamodb_infrastructuretype_ref":
        table = (locator.get("table_name") or "").lower() or None
        if table:
            pairs.append(("DYNAMODB_TABLE_NAME", table))

    elif infra_type == "elasticache_redis_infrastructuretype_ref":
        cluster_name = (locator.get("redis_cluster_name") or "").lower() or None
        if cluster_name:
            pairs.append(("ELASTICACHE_CLUSTER_NAME", cluster_name))
            pairs.append(("ELASTICACHE_PORT", locator.get("port") or "6379"))
            pairs.append(("ELASTICACHE_USER_ID", locator.get("user_id") or ""))
            # ELASTICACHE_HOST is only known after terragrunt apply — TF outputs
            # are merged into locator by _apply_infra_run_to_db before this runs.
            host = locator.get("replication_group_primary_endpoint_address") or ""
            if host:
                pairs.append(("ELASTICACHE_HOST", host))

    elif infra_type in (
        "aurora_postgres_infrastructuretype_ref",
        "aurora_mysql_infrastructuretype_ref",
    ):
        # Phase 1 vars (DB_PORT, DB_NAME, DB_USERNAME, AWS_REGION) are saved by
        # DefaultAuroraPostActionComponent at PR-creation time.
        # Phase 2: DB_HOST and DB_READER_HOST are only known after terragrunt apply —
        # TF outputs are merged into locator by _apply_infra_run_to_db before this runs.
        host = locator.get("cluster_endpoint") or ""
        reader_host = locator.get("cluster_reader_endpoint") or ""
        if host:
            pairs.append(("DB_HOST", host))
        if reader_host:
            pairs.append(("DB_READER_HOST", reader_host))

    if pairs:
        pairs.append(("AWS_REGION", region))
    return pairs


# Map infra-apply stages to deployment status enum values.
# Stage names ("cloning_repo", "applying_infra", etc.) and per-stage timestamps
# live in pipeline_run_track.build_stages JSONB; the enum captures coarse
# infrastructure_mst.infra_status only.
_INFRA_STAGE_TO_DEPLOYMENT_STATUS = {
    "cloning_repo":           DeploymentStatusEnum.TERRAFORM_APPLYING,
    "terragrunt_init":        DeploymentStatusEnum.TERRAFORM_APPLYING,
    "applying_infra":         DeploymentStatusEnum.TERRAFORM_APPLYING,
    "applying_iam":           DeploymentStatusEnum.TERRAFORM_APPLYING,
    "capturing_outputs":      DeploymentStatusEnum.TERRAFORM_APPLIED,
    "verifying_permissions":  DeploymentStatusEnum.VERIFYING_PERMISSIONS,
}


# Map Jenkins stage names → UI-ready ResourceStatusEnum for the resource's status column.
# Covers both service-deploy stages and infra-apply stages.
_STAGE_TO_RESOURCE_STATUS: dict[str, ResourceStatusEnum] = {
    # Service deploy stages
    "initialisation":       ResourceStatusEnum.PROVISIONING,
    "checkout":             ResourceStatusEnum.PROVISIONING,
    "building":             ResourceStatusEnum.BUILDING,
    "deploying":            ResourceStatusEnum.DEPLOYING,
    "verification":         ResourceStatusEnum.VERIFYING,
    # Infra-apply stages
    "cloning_repo":         ResourceStatusEnum.PROVISIONING,
    "terragrunt_init":      ResourceStatusEnum.PROVISIONING,
    "applying_infra":       ResourceStatusEnum.DEPLOYING,
    "applying_iam":         ResourceStatusEnum.DEPLOYING,
    "capturing_outputs":    ResourceStatusEnum.DEPLOYING,
    "verifying_permissions": ResourceStatusEnum.VERIFYING,
}

_FINAL_TO_RESOURCE_STATUS: dict[str, ResourceStatusEnum] = {
    "SUCCESS":  ResourceStatusEnum.ONLINE,
    "FAILURE":  ResourceStatusEnum.FAILED,
    "ABORTED":  ResourceStatusEnum.FAILED,
    "UNSTABLE": ResourceStatusEnum.FAILED,
}


async def _update_resource_status(
    db: AsyncSession,
    queue_codes: list,
    new_status: ResourceStatusEnum,
) -> None:
    """Update the UI-ready ``status`` column on the source resource(s).

    Resolves queue codes → (table_name, transaction_code) → bulk-update
    on the appropriate resource table (service_configs / infrastructure_mst).
    """
    if not queue_codes:
        return

    stmt = (
        select(
            TransactionQueueModel.transaction_code,
            TransactionQueueModel.table_name,
        )
        .where(TransactionQueueModel.code.in_(queue_codes))
    )
    rows = (await db.execute(stmt)).all()

    svc_codes = [
        r.transaction_code for r in rows
        if r.transaction_code
        and getattr(r.table_name, "value", r.table_name) == "SERVICE_CONFIG"
    ]
    infra_codes = [
        r.transaction_code for r in rows
        if r.transaction_code
        and getattr(r.table_name, "value", r.table_name) == "INFRASTRUCTURE"
    ]

    if svc_codes:
        svc_repo = ServiceConfigRepository(db)
        await svc_repo.bulk_update_status(svc_codes, new_status)

    if infra_codes:
        infra_repo = InfrastructureMstRepository(db)
        await infra_repo.bulk_update_status(infra_codes, new_status)


async def _apply_aurora_iam_policy(db: AsyncSession, infra_code: str) -> None:
    """After Aurora Terraform apply, set rds-db:connect IAM policy on the tenant
    default role using the now-known DbClusterResourceId from TF outputs.

    Idempotent — apply_policy_direct is a no-op if ARN already present.
    """
    try:
        infra_repo = InfrastructureMstRepository(db)
        infra = await infra_repo.get_by_code(infra_code)
        if not infra:
            return

        infra_type = getattr(infra, "infrastructuretype_ref_code", "") or ""
        if infra_type not in (
            "aurora_postgres_infrastructuretype_ref",
            "aurora_mysql_infrastructuretype_ref",
        ):
            return

        locator = infra.locator or {}
        db_cluster_resource_id = locator.get("db_cluster_resource_id") or ""
        if not db_cluster_resource_id:
            logger.warning(
                "[AURORA_IAM] db_cluster_resource_id not in locator for %s — skipping IAM policy",
                infra_code,
            )
            return

        tenant_code = infra.tenants_mst_code or ""
        env_value = getattr(infra, "environments_enum", None)
        environment = env_value.value if hasattr(env_value, "value") else (env_value or "")

        region = settings.onboarding_default_region
        account_id = settings.onboarding_default_account_id
        resource_arn = (
            f"arn:aws:rds-db:{region}:{account_id}"
            f":dbuser:{db_cluster_resource_id}/iam_user"
        )

        from app.plugin.default.default_aws_role_gen_component import apply_policy_direct
        result = await apply_policy_direct(
            tenant_code=tenant_code,
            environment=environment,
            kind_name="rds-aurora-connect",
            resource_arn=resource_arn,
            operation="add",
            db=db,
        )
        logger.info(
            "[AURORA_IAM] IAM policy update for %s: changed=%s commit=%s",
            infra_code, result.get("changed"), result.get("commit_sha"),
        )
    except Exception as exc:
        logger.error(
            "[AURORA_IAM] Failed to set IAM policy for Aurora infra %s: %s",
            infra_code, exc, exc_info=True,
        )


async def _create_aurora_db_objects(db: AsyncSession, infra_code: str) -> None:
    """Create db_object_mst entries (database / schema / table) for an Aurora cluster.

    Idempotent — DbObjectService.create_database skips if entries already exist.
    """
    try:
        from app.services.db_object_service import DbObjectService
        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository

        infra_repo = InfrastructureMstRepository(db)
        infra = await infra_repo.get_by_code(infra_code)
        if not infra:
            return

        infra_type = getattr(infra, "infrastructuretype_ref_code", "") or ""
        if infra_type not in (
            "aurora_postgres_infrastructuretype_ref",
            "aurora_mysql_infrastructuretype_ref",
        ):
            return

        locator = infra.locator or {}
        database_name = locator.get("database_name") or "app_db"
        tenant_code = infra.tenants_mst_code or ""
        env_value = getattr(infra, "environments_enum", None)
        environment = env_value.value if hasattr(env_value, "value") else (env_value or "")

        svc = DbObjectService(db)
        await svc.create_database(
            infrastructure_mst_code=infra_code,
            database_name=database_name,
            tenant_code=tenant_code,
            environment=environment,
        )
        logger.info(
            "[AURORA_DB_OBJECTS] Created db_object_mst entries for %s (db=%s)",
            infra_code, database_name,
        )
    except Exception as exc:
        logger.error(
            "[AURORA_DB_OBJECTS] Failed to create db objects for %s: %s",
            infra_code, exc, exc_info=True,
        )


async def _create_aurora_db_permission(db: AsyncSession, infra_code: str) -> None:
    """Create a server-level admin db_permission_mst entry for iam_user on an Aurora cluster.

    Mirrors what _record_postgres_server_defaults does for K8s PostgreSQL.
    Idempotent — skips if entry already exists.
    """
    try:
        from app.repository.db_permission_mst_repository import DbPermissionMstRepository
        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
        import uuid as _uuid

        infra_repo = InfrastructureMstRepository(db)
        infra = await infra_repo.get_by_code(infra_code)
        if not infra:
            return

        infra_type = getattr(infra, "infrastructuretype_ref_code", "") or ""
        if infra_type not in (
            "aurora_postgres_infrastructuretype_ref",
            "aurora_mysql_infrastructuretype_ref",
        ):
            return

        tenant_code = infra.tenants_mst_code or ""
        env_value = getattr(infra, "environments_enum", None)
        environment = env_value.value if hasattr(env_value, "value") else (env_value or "")

        db_perm_repo = DbPermissionMstRepository(db)
        existing = await db_perm_repo.get_by_object_and_user(
            db_object_mst_id=None,
            username="iam_user",
            infrastructure_mst_code=infra_code,
        )
        if not existing:
            await db_perm_repo.create(
                code=f"dbperm-{_uuid.uuid4().hex[:12]}",
                name="iam_user",
                infrastructure_mst_code=infra_code,
                db_object_mst_id=None,
                username="iam_user",
                permissions="admin",
                tenant_code=tenant_code,
                environment=environment,
            )
            logger.info(
                "[AURORA_DB_PERM] Created admin db_permission_mst for iam_user on %s",
                infra_code,
            )
    except Exception as exc:
        logger.error(
            "[AURORA_DB_PERM] Failed to create db permission for %s: %s",
            infra_code, exc, exc_info=True,
        )


async def _apply_infra_run_to_db(
    db: AsyncSession,
    payload: "JenkinsBuildPayload",
    queue_codes: list,
) -> None:
    """Persist infra-apply webhook results onto infrastructure_mst rows.

    Stage events update infra_status; the final SUCCESS event additionally
    writes the captured ARN and merges terragrunt outputs into the locator.
    Failure events mark the row FAILED with the error_message attached.
    """
    if not queue_codes:
        return

    queue_stmt = (
        select(TransactionQueueModel)
        .where(TransactionQueueModel.code.in_(queue_codes))
    )
    queue_rows = (await db.execute(queue_stmt)).scalars().all()
    infra_codes = [
        q.transaction_code for q in queue_rows
        if q.transaction_code and getattr(q.table_name, "value", q.table_name) == "INFRASTRUCTURE"
    ]
    if not infra_codes:
        return

    infra_repo = InfrastructureMstRepository(db)
    is_stage = payload.stage is not None
    is_success = (not is_stage) and payload.status.upper() == "SUCCESS"

    new_status: Optional[DeploymentStatusEnum]
    if is_stage:
        new_status = _INFRA_STAGE_TO_DEPLOYMENT_STATUS.get(payload.stage)
    elif is_success:
        new_status = DeploymentStatusEnum.ACTIVE
    elif (payload.failure_kind or "").upper() == "PERMISSIONS_MISSING":
        new_status = DeploymentStatusEnum.PERMISSIONS_MISSING
    else:
        new_status = DeploymentStatusEnum.FAILED

    parsed_outputs: dict = {}
    if is_success and payload.resource_outputs:
        try:
            parsed_outputs = json.loads(payload.resource_outputs) or {}
        except json.JSONDecodeError:
            logger.warning("Could not parse resource_outputs JSON for run %s", payload.run_code)

    for infra_code in infra_codes:
        infra_row = await infra_repo.get_by_code(infra_code)
        if not infra_row:
            continue

        if new_status is not None:
            await infra_repo.update_infra_status(
                infrastructure_id=infra_row.id,
                status=new_status,
            )

        if is_success and payload.resource_arn:
            await infra_repo.update_resource_identifier(
                infrastructure_id=infra_row.id,
                resource_identifier=payload.resource_arn,
            )

        if is_success and parsed_outputs:
            for key, val in parsed_outputs.items():
                # terragrunt output -json wraps each value as {"value": ..., "type": ...}
                value = val.get("value") if isinstance(val, dict) else val
                if isinstance(value, str):
                    await infra_repo.update_locator_field(infra_row.code, key, value)

        # Re-fetch after locator merge so variable upserts see TF outputs too.
        if is_success:
            refreshed = await infra_repo.get_by_code(infra_row.code)
            await _upsert_infra_variables(db, refreshed or infra_row)

        # Aurora Phase 2: set rds-db:connect IAM policy with the specific
        # DbClusterResourceId now that it's known from TF outputs.
        if is_success:
            await _apply_aurora_iam_policy(db, infra_row.code)
            await _create_aurora_db_objects(db, infra_row.code)
            await _create_aurora_db_permission(db, infra_row.code)

        if (not is_stage) and payload.status.upper() != "SUCCESS" and payload.error_message:
            # Stash the failure reason on locator so the UI can surface it.
            # PERMISSIONS_MISSING vs generic FAILURE is in failure_kind.
            failure_kind = payload.failure_kind or "FAILURE"
            await infra_repo.update_locator_field(
                infra_row.code, "last_apply_error",
                f"[{failure_kind}] {payload.error_message[:1000]}",
            )


async def _upsert_infra_variables(db: AsyncSession, infra) -> None:
    """Persist the resource's ARN/URL/name into variable_mst on apply success
    so service deployments can reference them as env vars (S3_BUCKET_ARN,
    SQS_QUEUE_URL, etc.). Idempotent — re-runs are no-ops when values match.

    Mirrors the upsert pattern used by ``DefaultS3PostActionComponent`` /
    ``DefaultSqsPostActionComponent`` / ``DefaultDynamoDbPostActionComponent``
    (which run at PR-creation time on predicted values); this duplicates the
    write at apply success so any TF-output divergence from the predicted
    naming lands in variable_mst too.
    """
    infra_type = getattr(infra, "infrastructuretype_ref_code", None)
    if not infra_type:
        return

    locator = infra.locator or {}
    tenant_code = infra.tenants_mst_code
    application_code = getattr(infra, "applications_mst_code", None)

    # Resolve environment from the infra row. The actual column is
    # ``environments_enum`` (plural) on InfrastructureMstModel. NEVER fall back
    # to ``settings.onboarding_default_env`` — that's "trial", which is not a
    # valid EnvironmentEnum value (dev/stage/qa/prod) and the upsert would
    # then poison the whole session with an enum-cast error.
    env_value = getattr(infra, "environments_enum", None)
    environment = env_value.value if hasattr(env_value, "value") else env_value
    if not environment:
        logger.warning(
            "[INFRA_APPLY] Skipping variable upsert for infra %s — no environments_enum",
            infra.code,
        )
        return

    pairs = _build_infra_variables(infra_type, locator)
    if not pairs:
        return

    # Upserts go via devlift-secret-config-manager's internal API — obs_tool no
    # longer writes variable_mst directly (variable-mst-isolation-spec). One
    # call per pair keeps the old SAVEPOINT-per-pair isolation (one failure
    # can't block the rest), and the surrounding transaction (which also
    # carries the infra_status = ACTIVE write) is no longer at risk at all.
    from app.integrations.secret_config_client import SecretConfigClient
    client = SecretConfigClient()

    for key, value in pairs:
        if not value or not isinstance(value, str):
            continue
        try:
            results = await client.upsert_variables(
                table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
                transaction_code=infra.code,
                environment=environment,
                tenant_code=tenant_code,
                items=[{
                    "key": key,
                    "value": value,
                    "variable_type": "VARIABLE",
                    "secret_provider": "local",
                    "description": f"{key} for {infra_type} {infra.code} (apply-success)",
                    "metadata_json": {"application_code": application_code},
                }],
            )
            if results and results[0]["status"] == "unchanged":
                continue
            logger.info(
                "[INFRA_APPLY] Upserted variable %s=%s for infra=%s env=%s",
                key, value, infra.code, environment,
            )
        except Exception as exc:
            logger.error(
                "[INFRA_APPLY] Failed to upsert variable %s for infra %s: %s",
                key, infra.code, exc, exc_info=True,
            )


@router.post("/jenkins-webhook", summary="Jenkins Build Webhook")
async def jenkins_build_webhook(
    payload: JenkinsBuildPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Webhook endpoint for Jenkins build notifications.

    Called by Jenkins at each stage start and on build completion.
    - Stage webhook (stage != None, status == IN_PROGRESS): updates queue items to stage status
    - Final webhook (stage == None, status == SUCCESS/FAILURE): marks DEPLOYED/FAILED
    """
    webhook_secret = request.headers.get("X-Webhook-Secret", "")
    if not webhook_secret or webhook_secret != settings.pipeline_webhook_secret:
        logger.warning("Jenkins webhook: invalid or missing secret for job %s", payload.job_name)
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    is_stage_webhook = payload.stage is not None
    is_final = not is_stage_webhook and payload.status.upper() in _JENKINS_STATUS_MAP

    logger.info(
        "Jenkins webhook: job=%s build=#%d status=%s stage=%s",
        payload.job_name, payload.build_number, payload.status,
        payload.stage or "final",
    )

    # Find the pipeline_mst by jenkins_job_name in deployment_config
    stmt = select(PipelineMstModel).where(
        and_(
            PipelineMstModel.is_deleted == False,
            PipelineMstModel.deployment_config["jenkins_job_name"].astext == payload.job_name,
        )
    )
    result = await db.execute(stmt)
    pipeline = result.scalar_one_or_none()

    if not pipeline:
        logger.warning("Jenkins webhook: no pipeline_mst found for job %s", payload.job_name)
        return {"status": "ignored", "reason": "no matching pipeline"}

    # Find run_track using a 3-tier strategy:
    # 1. Exact match by run_code (most reliable — passed as Jenkins build param)
    # 2. Exact match by pipeline_mst_code + build_number
    # 3. Fallback: latest PENDING/RUNNING run (backwards compat)
    run_track_repo = PipelineRunTrackRepository(db)
    run_track = None

    # Tier 1: Match by run_code (OBS_TOOL_RUN_CODE from Jenkins build param)
    if payload.run_code:
        run_code_stmt = (
            select(PipelineRunTrackModel)
            .where(PipelineRunTrackModel.code == payload.run_code)
        )
        run_code_result = await db.execute(run_code_stmt)
        run_track = run_code_result.scalar_one_or_none()
        if run_track:
            if not run_track.build_number:
                run_track.build_number = payload.build_number
                await db.flush()
            logger.info(
                "Matched run_track %s by run_code, build #%d",
                run_track.code, payload.build_number,
            )

    # Tier 2: Match by pipeline + build_number
    if not run_track:
        run_track = await run_track_repo.get_by_pipeline_and_build_number(
            pipeline.code, payload.build_number
        )

    # Tier 3: Fallback to latest PENDING/RUNNING run
    if not run_track:
        fallback_stmt = (
            select(PipelineRunTrackModel)
            .where(
                and_(
                    PipelineRunTrackModel.pipeline_mst_code == pipeline.code,
                    PipelineRunTrackModel.status.in_([
                        PipelineRunStatusEnum.PENDING.value,
                        PipelineRunStatusEnum.RUNNING.value,
                    ]),
                )
            )
            .order_by(PipelineRunTrackModel.created_at.desc())
            .limit(1)
        )
        fallback_result = await db.execute(fallback_stmt)
        run_track = fallback_result.scalar_one_or_none()
        if run_track:
            run_track.build_number = payload.build_number
            await db.flush()
            logger.info(
                "Linked build_number #%d to run_track %s (fallback)",
                payload.build_number, run_track.code,
            )

    if not run_track:
        logger.info(
            "Jenkins webhook: no run_track for pipeline %s build #%d, skipping",
            pipeline.code, payload.build_number,
        )
        await db.commit()
        return {"status": "ignored", "reason": "no matching run_track"}

    # Get queue_codes from run_track (not from pipeline_mst.deployment_config)
    queue_codes = run_track.transaction_queue_code or []
    # Fallback to pipeline_mst for backward compatibility
    if not queue_codes:
        deployment_config = pipeline.deployment_config or {}
        queue_codes = deployment_config.get("queue_codes", [])

    logger.info(
        "Jenkins webhook: queue_codes=%s (source=%s) for run_track %s",
        queue_codes,
        "run_track" if run_track.transaction_queue_code else "pipeline_mst",
        run_track.code,
    )

    queue_repo = TransactionQueueRepository(db)
    queue_update_count = 0

    if is_stage_webhook:
        # --- Stage webhook: update queue items to stage status, append to build_stages ---
        run_track.status = PipelineRunStatusEnum.RUNNING.value
        run_track.log_url = payload.build_url or run_track.log_url
        if payload.commit_sha:
            run_track.commit_sha = payload.commit_sha

        # Append stage to build_stages JSONB
        display_name = _STAGE_DISPLAY_NAMES.get(payload.stage, payload.stage)
        current_stages = list(run_track.build_stages or [])

        # Prevent duplicate stage entries
        if any(s.get("name") == display_name for s in current_stages):
            logger.info("Stage %s already recorded, skipping duplicate", payload.stage)
        else:
            # Auto-complete previous running stages (all stages are sequential)
            for prev in current_stages:
                if prev.get("status") == "running":
                    prev["status"] = "success"

            stage_entry = {
                "name": display_name,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            current_stages.append(stage_entry)

        run_track.build_stages = current_stages
        flag_modified(run_track, "build_stages")
        await db.flush()

        # Update queue items to stage status
        queue_status = _STAGE_TO_QUEUE_STATUS.get(payload.stage)
        if queue_codes and queue_status:
            try:
                queue_update_count = await queue_repo.bulk_update_status_by_codes(
                    queue_codes, queue_status
                )
                logger.info(
                    "Stage %s: updated %d queue items for pipeline %s",
                    payload.stage, queue_update_count, pipeline.code,
                )
            except Exception as exc:
                logger.error(
                    "Failed to update queue items for stage %s: %s",
                    payload.stage, exc, exc_info=True,
                )

        # Update the UI-ready status column on the source resource
        resource_status = _STAGE_TO_RESOURCE_STATUS.get(payload.stage)
        if queue_codes and resource_status:
            try:
                await _update_resource_status(db, queue_codes, resource_status)
            except Exception as exc:
                logger.error(
                    "Resource status update failed for stage %s: %s",
                    payload.stage, exc, exc_info=True,
                )

        # Stream infra-apply stage status into infrastructure_mst.infra_status
        # so the UI can show TERRAFORM_APPLYING / VERIFYING_PERMISSIONS in real
        # time, not just the terminal result.
        if payload.service_type == "INFRA_APPLY":
            try:
                await _apply_infra_run_to_db(
                    db=db,
                    payload=payload,
                    queue_codes=queue_codes,
                )
            except Exception as exc:
                logger.error(
                    "INFRA_APPLY stage update failed for stage %s: %s",
                    payload.stage, exc, exc_info=True,
                )

    elif is_final:
        # --- Final webhook: SUCCESS/FAILURE/ABORTED ---
        new_status = _JENKINS_STATUS_MAP.get(
            payload.status.upper(), PipelineRunStatusEnum.FAILED
        )
        run_track.status = new_status.value
        run_track.log_url = payload.build_url or run_track.log_url
        if payload.commit_sha:
            run_track.commit_sha = payload.commit_sha

        # Finalize ALL running stages
        current_stages = list(run_track.build_stages or [])
        final_stage_status = "success" if payload.status.upper() == "SUCCESS" else "failed"
        for stage in current_stages:
            if stage.get("status") == "running":
                stage["status"] = final_stage_status

        # Add remaining stages as "skipped" on failure so the UI shows the full pipeline
        # Model serving only has: initialisation, deploying, verification (no checkout/building)
        if payload.status.upper() != "SUCCESS":
            if payload.service_type == "MODEL_SERVING":
                _APPLICABLE_STAGE_KEYS = ["initialisation", "deploying", "verification"]
            else:
                _APPLICABLE_STAGE_KEYS = ["initialisation", "checkout", "building", "deploying", "verification"]
            completed_names = {s.get("name") for s in current_stages}
            for key in _APPLICABLE_STAGE_KEYS:
                display = _STAGE_DISPLAY_NAMES.get(key, key)
                if display not in completed_names:
                    current_stages.append({"name": display, "status": "skipped"})

        run_track.build_stages = current_stages
        flag_modified(run_track, "build_stages")

        # Store pod failure diagnosis if present
        if payload.error_message:
            run_track.error_message = payload.error_message

        # Store deploy result on pipeline_run_track.
        # MODEL_SERVING: Jenkins sends alb_url="" (URL is wildcard CNAME, not from ingress).
        # Use the pre-calculated URL already stored in service_config.config["alb_url"].
        deploy_alb_url = payload.alb_url
        if payload.service_type == "MODEL_SERVING" and not deploy_alb_url:
            try:
                pre_calc_stmt = (
                    select(ServiceConfigModel.config)
                    .join(TransactionQueueModel, TransactionQueueModel.transaction_code == ServiceConfigModel.code)
                    .where(TransactionQueueModel.code.in_(queue_codes))
                    .limit(1)
                )
                pre_calc_result = await db.execute(pre_calc_stmt)
                pre_calc_row = pre_calc_result.one_or_none()
                if pre_calc_row and isinstance(pre_calc_row[0], dict):
                    deploy_alb_url = pre_calc_row[0].get("alb_url") or deploy_alb_url
            except Exception as exc:
                logger.warning("Could not resolve pre-calculated MODEL_SERVING URL: %s", exc)

        run_track.deploy_result = {
            "alb_url": deploy_alb_url,
            "build_url": payload.build_url,
            "build_number": payload.build_number,
            "build_result": payload.status,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error_message": payload.error_message or None,
            "failure_kind": payload.failure_kind or None,
        }
        flag_modified(run_track, "deploy_result")
        await db.flush()

        # Update queue items to DEPLOYED/FAILED
        if queue_codes:
            queue_status = (
                TransactionQueueStatusEnum.DEPLOYED
                if payload.status.upper() == "SUCCESS"
                else TransactionQueueStatusEnum.FAILED
            )

            try:
                queue_update_count = await queue_repo.bulk_update_status_by_codes(
                    queue_codes, queue_status
                )
                logger.info(
                    "Final: updated %d queue items → %s for pipeline %s",
                    queue_update_count, queue_status.value, pipeline.code,
                )
            except Exception as exc:
                logger.error(
                    "Failed to update queue items on final webhook: %s",
                    exc, exc_info=True,
                )

        # Update the UI-ready status column on the source resource
        resource_status = _FINAL_TO_RESOURCE_STATUS.get(payload.status.upper())
        if queue_codes and resource_status:
            try:
                await _update_resource_status(db, queue_codes, resource_status)
            except Exception as exc:
                logger.error(
                    "Resource status update failed on final webhook: %s",
                    exc, exc_info=True,
                )

        # Create Cloudflare CNAME and resolve friendly URL.
        # MODEL_SERVING uses a wildcard CNAME (*.models.devlift.ai → Kourier LB)
        # set once at cluster level — no per-deploy Cloudflare API call needed.
        # The URL is pre-calculated and stored before Jenkins starts.
        friendly_url = payload.alb_url
        is_model_serving = payload.service_type == "MODEL_SERVING"
        is_bootstrap = payload.service_type == "BOOTSTRAP"

        # BOOTSTRAP pipeline success: persist shared ALB hostname in infra record.
        # No per-tenant Cloudflare DNS needed — service CNAMEs are created per-deploy
        # using the existing *.apps.devlift.ai wildcard cert.
        if is_bootstrap and payload.status.upper() == "SUCCESS" and payload.alb_url:
            try:
                # Job name pattern: {subdomain}-eks-bootstrap-{env}-pipeline
                # e.g. "aslam-eks-bootstrap-stage-pipeline"
                job_parts = payload.job_name.split("-eks-bootstrap-")
                if len(job_parts) == 2:
                    subdomain = job_parts[0]
                    infra_repo = InfrastructureMstRepository(db)
                    await infra_repo.update_locator_field_by_tenant(
                        tenant_code=subdomain,
                        key="shared_alb_hostname",
                        value=payload.alb_url,
                    )
                    logger.info(
                        "Persisted shared_alb_hostname=%s for tenant %s", payload.alb_url, subdomain
                    )
                else:
                    logger.warning(
                        "Cannot extract subdomain from bootstrap job name: %s", payload.job_name
                    )
            except Exception as exc:
                logger.error("Bootstrap post-success handler failed: %s", exc, exc_info=True)

        if is_bootstrap:
            await db.commit()
            return {
                "status": "ok",
                "pipeline_code": pipeline.code,
                "run_track_code": run_track.code,
                "stage": payload.stage,
                "build_number": payload.build_number,
                "queue_items_updated": 0,
            }

        if payload.service_type == "INFRA_APPLY":
            await _apply_infra_run_to_db(
                db=db,
                payload=payload,
                queue_codes=queue_codes,
            )
            await db.commit()
            return {
                "status": "ok",
                "pipeline_code": pipeline.code,
                "run_track_code": run_track.code,
                "stage": payload.stage,
                "build_number": payload.build_number,
                "queue_items_updated": queue_update_count,
            }

        if payload.alb_url and payload.status.upper() == "SUCCESS" and settings.cloudflare_api_token and not is_model_serving:
            try:
                # Resolve tenant subdomain: pipeline → service_config → service → tenant
                tenant_stmt = (
                    select(TenantsMstModel.subdomain, ServicesMstModel.name)
                    .select_from(ServiceConfigModel)
                    .join(ServicesMstModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
                    .join(TenantsMstModel, TenantsMstModel.code == ServicesMstModel.tenants_mst_code)
                    .where(ServiceConfigModel.code == pipeline.transaction_code)
                )
                tenant_result = await db.execute(tenant_stmt)
                tenant_row = tenant_result.one_or_none()

                if tenant_row and tenant_row.subdomain:
                    service_name = tenant_row.name.lower().replace(" ", "-")
                    # Extract env from job name: {tenant}-{service}-{env}-pipeline
                    # env is always the segment immediately before "-pipeline"
                    job_segs = payload.job_name.split("-")
                    deploy_env = job_segs[-2] if len(job_segs) >= 2 and job_segs[-1] == "pipeline" else "stage"
                    friendly_url = await CloudflareIntegration.create_or_update_cname(
                        subdomain=tenant_row.subdomain,
                        service_name=service_name,
                        alb_hostname=payload.alb_url,
                        env=deploy_env,
                    )
                    # Update deploy_result with the friendly URL
                    run_track.deploy_result["alb_url"] = friendly_url
                    flag_modified(run_track, "deploy_result")
                    await db.flush()
                    logger.info("Created Cloudflare CNAME → %s", friendly_url)
                else:
                    logger.warning("No tenant subdomain found for pipeline %s, skipping CNAME", pipeline.code)
            except Exception as exc:
                logger.error("Failed to create Cloudflare CNAME: %s", exc, exc_info=True)
                # Fall back to raw ALB URL — don't block the deploy

        # Store ALB URL on service_configs for persistence across page refreshes.
        # MODEL_SERVING URL is pre-calculated before Jenkins — skip here.
        if payload.alb_url and payload.status.upper() == "SUCCESS" and not is_model_serving:
            try:
                svc_config_stmt = (
                    select(TransactionQueueModel.transaction_code)
                    .where(TransactionQueueModel.code.in_(queue_codes))
                    .distinct()
                )
                svc_result = await db.execute(svc_config_stmt)
                svc_config_codes = [r[0] for r in svc_result.all() if r[0]]
                if svc_config_codes:
                    svc_config_repo = ServiceConfigRepository(db)
                    updated = await svc_config_repo.update_alb_url_by_codes(svc_config_codes, friendly_url)
                    logger.info("Updated ALB URL on %d service_configs: %s", updated, friendly_url)
            except Exception as exc:
                logger.error("Failed to update ALB URL on service_configs: %s", exc, exc_info=True)

    await db.commit()

    return {
        "status": "ok",
        "pipeline_code": pipeline.code,
        "run_track_code": run_track.code,
        "stage": payload.stage,
        "build_number": payload.build_number,
        "queue_items_updated": queue_update_count,
    }
