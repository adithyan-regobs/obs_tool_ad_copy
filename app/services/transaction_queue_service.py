"""
GitOps Queue Service

Business logic for the deploy queue feature.
Handles adding items to queue, deploying all items as a single PR,
and refreshing stale PRs.
"""

import logging
import re
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func

from app.core.config import settings
from app.repository.transaction_queue_repository import TransactionQueueRepository
from app.repository.service_config_repository import ServiceConfigRepository
from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.db.models.transaction_queue_model import TransactionQueueModel, TransactionQueueStatusEnum
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.integrations.github_integration import GitHubIntegration
from app.services.terragrunt_sync_service import TerragruntSyncService
from app.core.enum import PRStatusEnum, WorkflowSourceTableEnum, EnvironmentEnum, DeploymentStatusEnum
from app.schemas.transaction_queue_schemas import (
    TransactionQueueItemResponse,
    TransactionQueueListResponse,
    TransactionQueueDeployResponse,
    DeployItemResult,
    TransactionQueuePreviewResponse,
    TransactionQueuePRStatusResponse,
    TransactionQueuePRRefreshResponse,
)
from app.domain.factories.infrastructure_mst_factory import (
    make_infrastructure_mst_s3,
    make_infrastructure_mst_sqs,
    make_infrastructure_mst_dynamodb,
)
from app.utils.github_sync_helpers import should_skip_commit
from app.services.dockerfile_sync_service import DockerfileSyncService
from app.utils.tenant_config import get_tenant_config
from app.utils.pr_body_helpers import (
    generate_pr_title,
    generate_pr_body,
    get_region_display,
    compute_atlantis_project_name
)

logger = logging.getLogger(__name__)


# ── the deploy guard ────────────────────────────────────────────────────────
# One validation, shared by the three ways a queue item can ship:
#
#     POST /transaction-queue/create-pr   (and its by-config / by-infra cards)
#     POST /transaction-queue/deploy
#     POST /deployments/multiple-deploy
#
# Each of those used to answer "is this deployable?" its own way — create-pr by
# FILTERING to APPROVED rows, deploy_temporal by collecting `skipped_items`,
# deploy_all by an `if`. None of them refused; they quietly shipped less than
# was asked for, and none looked at the approval seal at all, so a change that
# was approved and then edited straight against the database deployed anyway.
#
# The OpenFGA can_deploy card on each route still decides WHO may deploy. This
# decides WHETHER THESE ROWS may be deployed at all.


# Kinds that are born APPROVED and sealed at creation. The deploy seal check
# (validate_deployable_queue_items) requires approved_snapshot_hash on EVERY
# row, but only approval_service.approve() writes it — and these S3/SQS/
# DynamoDB rows never pass through approve(): no approval is wanted for them.
# So they are sealed with the snapshot they are born with (and re-sealed on
# the pre-deploy re-save the infra flow always does), which keeps the
# no-exemption check honest for them too: an edit made straight against the
# database is still caught. Every other kind keeps approval-gate sealing.
SEAL_AT_BIRTH_CASE_REFS = {
    "create_bucket", "delete_bucket",             # s3
    "create_queue", "delete_queue",               # sqs
    "table_management", "delete_dynamodb_table",  # dynamodb
}


def _deploy_label(row: TransactionQueueModel) -> str:
    """What to call a row in a refusal.

    `display_name`, not anything dug out of config_snapshot: the queue is
    polymorphic and the snapshot names its subject differently in every shape —
    service_name for a service, identifier for an infra_mst row, a route for a
    gateway one. _generate_display_name already resolved that at creation, per
    kind, so this reads the one field that is meaningful for all of them.
    """
    return f"{row.code} ({row.display_name})" if row.display_name else row.code


async def validate_deployable_queue_items(
    db: AsyncSession,
    queue_ids: List[int],
    tenant_code: str,
    authorized_transaction_code: Optional[str] = None,
    lock: bool = True,
) -> List[TransactionQueueModel]:
    """Every row named by `queue_ids`, verified and locked, or an HTTPException.

    Checks, in the order a caller would want to hear about them:

      1. something was actually asked for
      2. every id resolves to a live row IN THIS TENANT
      3. every row belongs to the target the caller was authorized against
      4. every row is APPROVED
      5. every row still matches the seal taken when it was approved

    `authorized_transaction_code` closes a real hole rather than repeating the
    route's card. The card checks can_deploy against ONE object — the path
    parameter on /by-config/{code} and /by-infra/{code}, the body field on
    /multiple-deploy — while the ids travel separately. Authorized against a
    target you may deploy, the ids could name one you may not. So the rows are
    checked against the object that was actually authorized.

    Named for the COLUMN it is compared against, not for one kind of target.
    transaction_queue is polymorphic: transaction_code holds a service_configs
    code for a service or gateway row and an infra_mst code for an
    infrastructure one, so a name like service_config_code would be wrong on
    half the rows this guard sees. Callers with no such object (the legacy
    /create-pr and /deploy routes) pass None and skip only this step.

    Rows are locked FOR UPDATE by default. Without it the window between "these
    are approved" and "these are shipping" is one an approver can revoke into,
    and the deploy would carry a decision that no longer exists.
    """
    # Imported here, not at module scope: approval_service reaches back into
    # this module (see its save_draft), so a top-level import would make a
    # cycle. The seal and the audit-event shape come from the service that
    # WRITES them — a second copy of either is free to drift from the one that
    # sealed the row.
    from app.services.approval_service import _event, seal

    ids = list(dict.fromkeys(queue_ids or []))
    if not ids:
        raise HTTPException(400, "no queue items were given to deploy")

    stmt = select(TransactionQueueModel).where(
        TransactionQueueModel.id.in_(ids),
        TransactionQueueModel.tenant_code == tenant_code,
        # isnot(True), not == False: rows written outside the ORM hold NULL,
        # and `== False` would drop them — here that would report "not found"
        # for a row that is perfectly live.
        TransactionQueueModel.is_deleted.isnot(True),
    )
    if lock:
        # Ordered by id so two concurrent deploys take the rows in the same
        # order rather than deadlocking on each other.
        stmt = stmt.order_by(TransactionQueueModel.id).with_for_update()

    rows = list((await db.execute(stmt)).scalars().all())
    found: Dict[int, TransactionQueueModel] = {r.id: r for r in rows}

    # 2. Missing ids are NAMED. The old behaviour dropped them silently and
    # failed later with "No queue items found matching the criteria", the same
    # sentence whether one id was wrong or all of them were.
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            404,
            "no queue item "
            + ", ".join(str(i) for i in missing)
            + " in this tenant — it may have been deployed, discarded, or never existed",
        )

    ordered = [found[i] for i in ids]

    # 3. The rows must belong to the target the card authorized.
    if authorized_transaction_code:
        foreign = [
            r for r in ordered if r.transaction_code != authorized_transaction_code
        ]
        if foreign:
            # 403, not 404: the caller proved they may deploy SOMETHING, just
            # not this. Saying so is not a disclosure — they named the id.
            raise HTTPException(
                403,
                f"{_deploy_label(foreign[0])} belongs to "
                f"{foreign[0].transaction_code}, not to "
                f"{authorized_transaction_code} — you were not authorized to "
                f"deploy it",
            )

    # 4. Approved, every one of them.
    unapproved = [
        r for r in ordered if r.status != TransactionQueueStatusEnum.APPROVED
    ]
    if unapproved:
        row = unapproved[0]
        state = getattr(row.status, "value", str(row.status))
        raise HTTPException(
            409,
            f"{_deploy_label(row)} is {state} — only an approved change can be "
            f"deployed. Submit it for review, or wait for a decision.",
        )

    # 5. The seal: is this still the content that was approved?
    #
    # Required on EVERY row, with no exemption. An approved row without a hash
    # is not a trusted row that happens to lack one — it is a row nothing can
    # vouch for, so it fails exactly like a mismatch does.
    for row in ordered:
        if row.approved_snapshot_hash and row.approved_snapshot_hash == seal(
            row.config_snapshot
        ):
            continue
        # Logged on the row, not just raised: an edit made straight against the
        # database is exactly what this catches, and the attempt to ship it
        # belongs in the record. Same event name the approvals gate wrote.
        row.history = (row.history or []) + [_event("system", "seal-broken")]
        db.add(row)
        await db.commit()
        logger.warning(
            "deploy refused: seal %s on queue=%s target=%s",
            "missing" if not row.approved_snapshot_hash else "mismatched",
            row.code,
            row.transaction_code,
        )
        raise HTTPException(
            409,
            f"{_deploy_label(row)} changed after it was approved, so it will "
            f"not be deployed. Send it back through review.",
        )

    return ordered


class TransactionQueueService:
    """
    Service for Transaction Queue operations.

    Handles:
    - Adding items to queue with config snapshots
    - Deploying all pending items as a single PR
    - Previewing HCL from snapshots
    - Refreshing stale PRs
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.queue_repo = TransactionQueueRepository(db)
        self.service_config_repo = ServiceConfigRepository(db)
        self.workflow_repo = GitopsWorkflowDetailRepository(db)
        self.infra_repo = InfrastructureMstRepository(db)
        self.app_repo = ApplicationsMstRepository(db)
        self.terragrunt_service = TerragruntSyncService()
        self.dockerfile_sync_service = DockerfileSyncService()

    async def _get_github_token(self, owner: str) -> str:
        """Get GitHub App installation token for the given org."""
        from app.utils.github_app_token import get_token_for_org
        return await get_token_for_org(owner, self.db)

    def _generate_display_name(
        self,
        config_snapshot: Dict,
        table_name: WorkflowSourceTableEnum,
        case_ref_code: Optional[str] = None
    ) -> str:
        """
        Generate human-readable display name for queue items.

        Format (non-Kong): "{case_ref_code}: {identifier} {geo_loc_mst_code} {environment}"
        Format (Kong): "add route:{method} {route}"

        Example:
        - "create bucket:new-bucket mumbai prod"
        - "add route:GET ~/api/v1/users$"
        """
        print("!!!"*10)
        print(config_snapshot)
        print(table_name)
        print(case_ref_code)
        print("!!!"*10)
        # Kong "add route" items: keyed on case_ref_code so the label stays correct
        # whether the queue item is recorded under KONG_ROUTE or (host-based) SERVICE_CONFIG.
        if table_name == WorkflowSourceTableEnum.KONG_ROUTE or case_ref_code == "add_route":
            method = (
                config_snapshot.get('method') or
                config_snapshot.get('http_method') or
                'unknown-method'
            )
            route = config_snapshot.get('route') or config_snapshot.get('route_path') or 'unknown-route'
            route = route.replace("~", "").replace("$", "")
            display_name = f"add route : {method} {route}"
            if len(display_name) > 255:
                # Truncate route field to fit within limit, keeping prefix/method intact
                prefix = f"add route : {method} "
                max_route_length = 255 - len(prefix)
                if max_route_length > 10:
                    route = route[:max_route_length - 3] + "..."
                    display_name = f"{prefix}{route}"
                else:
                    display_name = display_name[:252] + "..."
            return display_name

        case_ref = case_ref_code or config_snapshot.get('case_ref_code') or 'unknown'
        case_ref_label = case_ref.replace("_", " ")
        geo_loc_code = config_snapshot.get('geo_loc_mst_code', 'unknown-region')
        if case_ref == "database_creation":
            database_name = (
                config_snapshot.get('database_name') or
                config_snapshot.get('db_name') or
                config_snapshot.get('name') or
                'unnamed'
            )
            display_name = f"{case_ref_label} : {database_name} {geo_loc_code}"
            if len(display_name) > 255:
                prefix = f"{case_ref_label} : "
                suffix = f" {geo_loc_code}"
                max_name_length = 255 - (len(prefix) + len(suffix))
                if max_name_length > 10:
                    database_name = database_name[:max_name_length - 3] + "..."
                    display_name = f"{prefix}{database_name}{suffix}"
                else:
                    display_name = display_name[:252] + "..."
            return display_name
        if case_ref == "user_management":
            db_user_name = (
                config_snapshot.get('db_user_name') or
                config_snapshot.get('user_name') or
                config_snapshot.get('name') or
                'unnamed'
            )
            display_name = f"{case_ref_label} : {db_user_name} {geo_loc_code}"
            if len(display_name) > 255:
                prefix = f"{case_ref_label} : "
                suffix = f" {geo_loc_code}"
                max_name_length = 255 - (len(prefix) + len(suffix))
                if max_name_length > 10:
                    db_user_name = db_user_name[:max_name_length - 3] + "..."
                    display_name = f"{prefix}{db_user_name}{suffix}"
                else:
                    display_name = display_name[:252] + "..."
            return display_name

        # Extract identifier (varies by type)
        identifier = (
            config_snapshot.get('identifier') or
            config_snapshot.get('service_name') or
            config_snapshot.get('name') or
            'unnamed'
        )

        # Generate display name and truncate to database column limit (255 chars)
        display_name = f"{case_ref_label}:{identifier} {geo_loc_code}"
        print(display_name)
        if len(display_name) > 255:
            print("inside>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
            # Truncate identifier field to fit within limit, keeping other fields intact
            prefix = f"{case_ref_label}:"
            suffix = f" {geo_loc_code}"
            max_identifier_length = 255 - (len(prefix) + len(suffix))
            if max_identifier_length > 10:  # Ensure we have reasonable space for identifier
                identifier = identifier[:max_identifier_length - 3] + "..."
                display_name = f"{prefix} {identifier} {suffix}"
            else:
                # If still too long, just truncate the whole thing
                display_name = display_name[:252] + "..."
        print(display_name)
        return display_name

    async def _settle_snapshot_routing(
        self,
        nested_config: Dict[str, Any],
        service_config,
        tenant_code: str,
    ) -> Dict[str, Any]:
        """fill_routing_defaults for a queued settings proposal, against the
        live row it will be applied to."""
        from app.repository.language_ref_repository import LanguageRefRepository
        from app.repository.services_mst_repository import ServicesMstRepository
        from app.utils.service_routing import fill_routing_defaults

        service = await ServicesMstRepository(self.db).get_by_code(service_config.services_mst_code)
        language_name = nested_config.get("language_name")
        language_ref_code = nested_config.get("language_ref_code") or service_config.language_ref_code
        if not language_name and language_ref_code:
            language_ref = await LanguageRefRepository(self.db).get_by_code(language_ref_code)
            language_name = language_ref.name if language_ref else None
        stored = service_config.config if isinstance(service_config.config, dict) else {}
        deployed = False
        if not (stored.get("service_path") and stored.get("health")):
            deployed = bool(await self.queue_repo.get_latest_deployed_by_transaction_code(
                service_config.code, tenant_code, settings_rows_only=True
            ))
        return fill_routing_defaults(
            nested_config,
            service_name=service.name if service else None,
            language_name=language_name,
            infrastructuretype_ref_code=service_config.infrastructuretype_ref_code,
            service_type=service.service_type if service else None,
            alb_selection=nested_config.get("alb_selection") or service_config.alb_selection,
            stored=stored,
            deployed=deployed,
        )

    async def _enrich_config_snapshot(
        self,
        config_snapshot: Dict,
        table_name: WorkflowSourceTableEnum
    ) -> Dict:
        """
        Enrich config_snapshot with resolved values like product_name.

        Frontend sends raw data (UUIDs, codes), backend resolves to human-readable names.
        """
        enriched = {**config_snapshot}

        # ── one shape for every reader ──────────────────────────────────────
        # The approvals form sends the settings NESTED under `config`, while the
        # deploy pipeline reads them FLAT off the snapshot root — repository,
        # branches, selected_branches, language_name, language_ref_code. Against
        # a nested snapshot every one of those lookups returns None, so the
        # generator built its terragrunt from missing values, produced output
        # identical to what was already committed, and the deploy died with
        # "No pull request was created - no diff detected". The change looked
        # deployed-but-unchanged, with nothing naming the real cause.
        #
        # Lifted here, at the boundary that creates the divergence, rather than
        # by teaching each reader both shapes: one place to be right, and the
        # readers that already handle nesting (compute_changes, the settings
        # saver) keep working because `config` is left in place.
        #
        # setdefault, never overwrite: the root carries resolved metadata that
        # the nested copy may hold a staler version of - language_ref_code
        # appears in both - and the root is the authority for those.
        nested_config = enriched.get("config")
        if isinstance(nested_config, dict):
            for key, value in nested_config.items():
                enriched.setdefault(key, value)

        # Resolve product name from application code
        application_code = config_snapshot.get('applications_mst_code') or config_snapshot.get('product')
        existing_product_name = enriched.get('product_name')

        # Check if product_name is missing or invalid (None, "None", "null", empty string)
        is_invalid_product_name = not existing_product_name or existing_product_name in ("None", "null", "")

        logger.info(f"[DB_USER_MGMT_DEBUG] Enrichment - existing_product_name='{existing_product_name}', applications_mst_code='{application_code}', is_invalid={is_invalid_product_name}")

        if is_invalid_product_name:
            # Need to resolve from applications_mst_code
            if not application_code or application_code in ("None", "null", ""):
                raise ValueError(
                    f"product_name is required but missing, and applications_mst_code is also missing. "
                    f"Cannot resolve product name. config_snapshot keys: {list(config_snapshot.keys())}"
                )

            app_record = await self.app_repo.get_by_code(application_code)
            if app_record:
                enriched['product_name'] = app_record.name
                logger.info(f"Resolved application code {application_code} to name: {app_record.name}")
            else:
                raise ValueError(
                    f"Could not resolve applications_mst_code='{application_code}' to product name. "
                    f"Application record not found in database."
                )

        # For infrastructure, derive vendor account code if missing
        if table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
            if not enriched.get('infra_vendor_accounts_mst_code'):
                vendor = 'aws'  # Default to AWS
                tenant_code = enriched.get('tenant_code', 'unknown')
                environment = enriched.get('environment', 'dev')
                env_for_vendor_account = "staging" if environment == "stage" else environment
                enriched['infra_vendor_accounts_mst_code'] = f"{vendor}_{tenant_code}_{env_for_vendor_account}".lower()

        return enriched

    # Flags that create a container Terraform owns, paired with the evidence
    # that the container is in use and the words for refusing.
    _CONTAINER_FLAGS = (
        ("create_secrets", "has_secrets", "Secrets Manager", "secret"),
        ("create_ssm", "has_variables", "SSM Parameters", "config variable"),
    )

    async def _refuse_disabling_a_non_empty_container(
        self,
        *,
        transaction_code: str,
        config_snapshot: Dict[str, Any],
    ) -> None:
        """409 if the save turns off a container that still holds values.

        Reads the flags from either snapshot shape — new saves nest the form
        payload under `config`, older ones are flat — because both reach this
        method and a missed nested flag would let the bad save straight through.
        """
        nested = config_snapshot.get("config")
        cfg = nested if isinstance(nested, dict) else config_snapshot

        # Only ask the variables service when something is actually switched
        # off. The common save leaves both on and costs no extra call.
        wanted_off = [f for f in self._CONTAINER_FLAGS if cfg.get(f[0], True) is False]
        if not wanted_off:
            return

        from app.integrations.secret_config_client import SecretConfigClient

        summary = await SecretConfigClient().get_variables_summary(
            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
            transaction_code=transaction_code,
        )

        for flag, evidence, label, noun in wanted_off:
            if not summary.get(evidence):
                continue  # nothing in the container — switching it off is fine
            logger.info(
                "settings save refused: %s turned off on %s but the service has %ss",
                flag, transaction_code, noun,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{label} can't be turned off while this service still has "
                    f"{noun}s. Delete them first, then turn it off."
                ),
            )

    async def add_item_to_queue(
        self,
        user_code: str,
        tenant_code: str,
        transaction_code: str,
        table_name: WorkflowSourceTableEnum,
        config_snapshot: Dict[str, Any],
        case_ref_code: Optional[str] = None,
        ticket_code: Optional[str] = None,
        queue_code: Optional[str] = None,
        validate_container_flags: bool = True,
    ) -> TransactionQueueModel:
        """
        Add any item type to the transaction queue.

        Unified method that accepts polymorphic payload from frontend.

        Args:
            user_code: User code from JWT
            tenant_code: Tenant code from JWT
            transaction_code: Source entity code (service_config.code, infrastructure_mst.code, etc.)
            table_name: Source table enum
            config_snapshot: Configuration parameters from frontend
            case_ref_code: Optional case reference
            ticket_code: Optional ticket code reference
            queue_code: Optional queue code - if provided, updates existing item; otherwise creates new

        Returns:
            Created/updated queue item
        """
        # A container flag cannot be switched off while the container has
        # contents. create_secrets / create_ssm say "should Terraform create the
        # Secrets Manager secret / SSM parameters", so turning one off on a
        # service that HAS secrets or parameters asks for something that cannot
        # be built: the variables stage right after the deploy would have
        # nowhere to write.
        #
        # This used to be resolved by silently overriding the user at deploy
        # time. That override wrote the corrected flag back onto the approved
        # snapshot, which is sealed by approved_snapshot_hash — so a deploy that
        # later failed could not be retried (the seal no longer matched) and the
        # row's history gained a `seal-broken` event blaming the user for an
        # edit the system had made. Refusing the save instead means the person
        # is told at the moment they make the choice, and the approved content
        # is never something nobody actually agreed to.
        #
        # Placed here rather than on a route: all three paths that stage a
        # settings change (the Settings tab Save, clone-settings, and the
        # auto-approved infra save) funnel through this method.
        if (
            validate_container_flags
            and table_name == WorkflowSourceTableEnum.SERVICE_CONFIG
            and case_ref_code == "update_service"
        ):
            await self._refuse_disabling_a_non_empty_container(
                transaction_code=transaction_code,
                config_snapshot=config_snapshot,
            )

        # Kong rows are keyed on the kong HOST service's config when one can be
        # resolved — SERVICE_CONFIG + add_route, the anchoring Ajmal's
        # KongGatewayTab commit established — so deployment status tracks on
        # that service. The KRC row stays as route inventory only. A caller
        # that already sent SERVICE_CONFIG (new UI, Slack/chat) passes through.
        # When NO host config resolves, the row keeps the caller's legacy
        # keying instead of being refused: pre-v2 kong services (host in a
        # different application, name without "kong", or never onboarded with
        # a service config for that env/region) were saving routes this way
        # for years, and the 409 that briefly stood here broke their add-route
        # with no code change on their side.
        if (
            case_ref_code == "add_route"
            and table_name != WorkflowSourceTableEnum.SERVICE_CONFIG
        ):
            from app.services.slack.shared.infrastructure_request_builder import (
                resolve_kong_host_config_code,
            )
            host_code = await resolve_kong_host_config_code(
                self.db, tenant_code, config_snapshot
            )
            if host_code:
                logger.info(
                    "add_item_to_queue: anchored kong add_route on host %s/SERVICE_CONFIG "
                    "(caller sent %s/%s)",
                    host_code, transaction_code, getattr(table_name, "value", table_name),
                )
                transaction_code = host_code
                table_name = WorkflowSourceTableEnum.SERVICE_CONFIG
            else:
                logger.info(
                    "add_item_to_queue: no kong host config resolved for add_route — "
                    "keeping legacy keying %s/%s",
                    transaction_code, getattr(table_name, "value", table_name),
                )

        # Enrich config_snapshot with resolved values
        enriched_snapshot = await self._enrich_config_snapshot(config_snapshot, table_name)

        # For service configs, enforce server-side snapshot enrichment
        if table_name == WorkflowSourceTableEnum.SERVICE_CONFIG and transaction_code:
            service_config = await self.service_config_repo.get_by_code_and_tenant(
                code=transaction_code,
                tenant_code=tenant_code
            )
            if service_config:
                enriched_snapshot["id"] = service_config.id
                enriched_snapshot["code"] = service_config.code
                enriched_snapshot["services_mst_code"] = service_config.services_mst_code
                # language_ref_code is a column, not config JSONB — enrich it so the
                # settings-diff has a deployed baseline for language/version.
                # Only when the column HAS a value: it is NULL for services
                # created before the column was written, and overwriting the
                # form's real value ("PYTHON_3_10") with that NULL handed the
                # Dockerfile generator a snapshot whose root said no language
                # at all — while the correct value sat one level down.
                if service_config.language_ref_code:
                    enriched_snapshot["language_ref_code"] = service_config.language_ref_code
                # Merge server-side enriched fields from config JSONB
                # (efs_path, model_revision, download_job_name are set at
                # create/update time by ServiceConfigService but the frontend
                # doesn't send them — they only exist in the DB)
                if service_config.config and isinstance(service_config.config, dict):
                    for key in ("efs_path", "model_revision", "download_job_name"):
                        db_val = service_config.config.get(key)
                        if db_val and not enriched_snapshot.get(key):
                            enriched_snapshot[key] = db_val
                # Settle the ingress/probe paths on a settings proposal the same
                # way a Save does, so the render and the settings saver see the
                # value the row holds (or the default for a new service).
                nested_config = enriched_snapshot.get("config")
                if case_ref_code == "update_service" and isinstance(nested_config, dict):
                    settled = await self._settle_snapshot_routing(
                        nested_config, service_config, tenant_code
                    )
                    enriched_snapshot["config"] = settled
                    # The flat copy was lifted before the settle — keep it in step.
                    for key in ("service_path", "health", "alb_url", "resolved_service_path"):
                        if key in settled:
                            enriched_snapshot[key] = settled[key]
                        else:
                            enriched_snapshot.pop(key, None)
                logger.debug(
                    "Enriched service config snapshot with id/code for %s",
                    transaction_code
                )
            else:
                logger.warning(
                    "Service config not found for snapshot enrichment: %s (tenant=%s)",
                    transaction_code,
                    tenant_code
                )

        # Add tenant_code to snapshot if not present
        if 'tenant_code' not in enriched_snapshot:
            enriched_snapshot['tenant_code'] = tenant_code
            print(enriched_snapshot)
            print("##"*90)
            print(queue_code)
            print("##"*90)
        # Generate display name
        display_name = self._generate_display_name(
            enriched_snapshot,
            table_name,
            case_ref_code=case_ref_code
        )
        print("final name", display_name)
        # If queue_code provided, check if we can update existing item
        if queue_code:
            existing_item = await self.queue_repo.get_by(code=queue_code)
            # A soft-deleted row must never be reused: get_by does not filter
            # is_deleted, and a discarded draft keeps status=DRAFT — so a save
            # carrying the dead row's code would "succeed" by updating a row
            # every read hides. Fall through and create a fresh draft instead.
            if existing_item is not None and existing_item.is_deleted:
                logger.info(
                    "Queue item %s is discarded — creating a new draft instead",
                    queue_code,
                )
                existing_item = None
            if existing_item and existing_item.user_code == user_code:
                # Only allow updates if status is DRAFT or APPROVED
                if existing_item.status in [TransactionQueueStatusEnum.DRAFT, TransactionQueueStatusEnum.APPROVED]:
                    # Update existing item
                    existing_item.config_snapshot = enriched_snapshot
                    existing_item.display_name = display_name
                    existing_item.case_ref_code = case_ref_code
                    existing_item.ticket_code = ticket_code
                    existing_item.transaction_code = transaction_code
                    existing_item.table_name = table_name
                    # The infra flow re-saves the row right before create-pr,
                    # which rewrites the snapshot — re-seal it or the deploy
                    # seal check refuses the row as tampered. Only for the
                    # born-approved S3/SQS/DynamoDB kinds; everything else is
                    # sealed by approve() alone.
                    if (
                        existing_item.status == TransactionQueueStatusEnum.APPROVED
                        and case_ref_code in SEAL_AT_BIRTH_CASE_REFS
                    ):
                        from app.services.approval_service import seal
                        existing_item.approved_snapshot_hash = seal(enriched_snapshot)
                    existing_item.updated_at = func.now()
                    self.db.add(existing_item)
                    await self.db.commit()
                    await self.db.refresh(existing_item)
                    logger.info(f"Updated queue item {existing_item.code} for transaction {transaction_code}")
                    return existing_item
                else:
                    # Status is not DRAFT/APPROVED - cannot update, will create new item instead
                    logger.info(f"Queue item {queue_code} has status {existing_item.status}, creating new item instead")
            else:
                logger.warning(f"Queue item {queue_code} not found or not owned by user {user_code}")

        # A service-config settings edit has ONE pending item — reuse the
        # existing draft instead of stacking rows. Covers queue_code-less
        # callers (clone-settings, or a stale ref) that would otherwise create
        # duplicates and keep the settings diff showing after a deploy. Scoped
        # to SERVICE_CONFIG update_service so Kong add_route / infra are untouched.
        if (
            not queue_code
            and table_name == WorkflowSourceTableEnum.SERVICE_CONFIG
            and case_ref_code == "update_service"
        ):
            existing_settings_item = await self.queue_repo.search_draft_by_transaction(
                transaction_code=transaction_code,
                table_name=table_name,
                tenant_code=tenant_code,
                user_code=user_code,
                case_ref_code="update_service",
            )
            if existing_settings_item:
                existing_settings_item.config_snapshot = enriched_snapshot
                existing_settings_item.display_name = display_name
                existing_settings_item.case_ref_code = case_ref_code
                existing_settings_item.ticket_code = ticket_code
                existing_settings_item.updated_at = func.now()
                self.db.add(existing_settings_item)
                await self.db.commit()
                await self.db.refresh(existing_settings_item)
                logger.info(
                    f"Reused settings queue item {existing_settings_item.code} for {transaction_code}"
                )
                return existing_settings_item

        # Create new queue item
        import uuid
        new_queue_code = f"queue-{uuid.uuid4().hex[:12]}"

        # Determine initial status based on table_name
        # SERVICE_CONFIG starts as DRAFT (requires approval), others start as APPROVED.
        # Exception: Kong "add route" items ride on SERVICE_CONFIG (the gateway service is
        # the deployable host) but must skip the approval gate so Auto Deploy works.
        initial_status = (
            TransactionQueueStatusEnum.DRAFT
            if table_name == WorkflowSourceTableEnum.SERVICE_CONFIG and case_ref_code != "add_route"
            else TransactionQueueStatusEnum.APPROVED
        )

        # DEBUG: Log product_name BEFORE inserting into transaction_queue
        logger.info(f"[DB_USER_MGMT_DEBUG] BEFORE DB INSERT - product_name='{enriched_snapshot.get('product_name')}', case_ref_code='{case_ref_code}', queue_code='{new_queue_code}'")

        # S3/SQS/DynamoDB rows are born APPROVED with no approval step, so
        # they are sealed here with the content they are born with — see
        # SEAL_AT_BIRTH_CASE_REFS for why. Everything else leaves the hash
        # NULL for approve() to write.
        if (
            initial_status == TransactionQueueStatusEnum.APPROVED
            and case_ref_code in SEAL_AT_BIRTH_CASE_REFS
        ):
            from app.services.approval_service import seal
            birth_seal = seal(enriched_snapshot)
        else:
            birth_seal = None

        queue_item = await self.queue_repo.create(
            code=new_queue_code,
            user_code=user_code,
            tenant_code=tenant_code,
            transaction_code=transaction_code,
            table_name=table_name,
            config_snapshot=enriched_snapshot,
            display_name=display_name,
            case_ref_code=case_ref_code,
            ticket_code=ticket_code,
            status=initial_status,
            approved_snapshot_hash=birth_seal,
        )

        # DEBUG: Log product_name AFTER inserting into transaction_queue
        logger.info(f"[DB_USER_MGMT_DEBUG] AFTER DB INSERT - product_name='{queue_item.config_snapshot.get('product_name') if queue_item.config_snapshot else 'N/A'}', queue_id={queue_item.id}, queue_code='{queue_item.code}'")

        logger.info(f"Added {table_name} item to queue: {transaction_code} by {user_code}")
        return queue_item

    async def add_to_queue(
        self,
        user_code: str,
        service_config_code: str,
        environment: str,
        tenant_code: str
    ) -> TransactionQueueModel:
        """
        Add a service config to the deploy queue.

        Creates a snapshot of the current config data at the time of adding.
        This ensures predictable deploys - what you queue is what gets deployed.

        Uses TerragruntSyncService for path computation to ensure consistency
        with the single "Save & Sync" flow.

        Args:
            user_code: User who is adding to queue
            service_config_code: Service config code
            environment: Environment (dev, staging, prod)
            tenant_code: Tenant code

        Returns:
            Created or updated queue item

        Raises:
            ValueError: If config not found
        """
        # Check for existing pending item
        existing = await self.queue_repo.check_duplicate(
            user_code, service_config_code, environment
        )

        # Get service config WITH relationships loaded - needed for correct path computation
        # This ensures we have access to service.name and service.application.name
        config = await self._get_service_config_with_relations(service_config_code)
        if not config:
            raise ValueError(f"Service config not found: {service_config_code}")

        # Create config snapshot - capture all relevant data including relationships
        config_snapshot = await self._create_config_snapshot_with_relations(config)

        # Compute file path and atlantis project name using TerragruntSyncService
        # This ensures we use the same logic as "Save & Sync" for correct paths
        hcl_file_path, atlantis_project_name = self._compute_paths_using_terragrunt_service(
            config, environment, tenant_code
        )

        # Determine infrastructure type from config
        infra_type = self._get_infra_type(config)

        # If duplicate exists, UPDATE it with fresh snapshot instead of creating new
        if existing:
            existing.config_snapshot = config_snapshot
            existing.hcl_file_path = hcl_file_path
            existing.atlantis_project_name = atlantis_project_name
            existing.infra_type = infra_type
            self.db.add(existing)
            await self.db.commit()
            await self.db.refresh(existing)
            logger.info(f"Updated in queue: {service_config_code} for {environment} by {user_code}")
            return existing

        # Generate unique queue code
        import uuid
        queue_code = f"queue-{uuid.uuid4().hex[:12]}"

        # Create NEW queue item
        queue_item = await self.queue_repo.create(
            code=queue_code,
            user_code=user_code,
            service_config_code=service_config_code,
            environment=environment,
            infra_type=infra_type,
            config_snapshot=config_snapshot,
            hcl_file_path=hcl_file_path,
            atlantis_project_name=atlantis_project_name,
            status=TransactionQueueStatusEnum.APPROVED,
            tenant_code=tenant_code
        )

        await self.db.commit()
        logger.info(f"Added to queue: {service_config_code} for {environment} by {user_code}")

        return queue_item

    async def add_infra_to_queue(
        self,
        user_code: str,
        infra_type: str,
        environment: str,
        config_data: Dict[str, Any],
        tenant_code: str
    ) -> TransactionQueueModel:
        """
        Add standalone infrastructure to the deploy queue.

        This method performs two operations:
        1. Save/update the infrastructure record in infrastructure_mst table
        2. Add the item to gitops_queue for batch deployment

        Used for infrastructure created via Infrastructure Studio that's
        not tied to an existing service config. These items go into the
        same queue as service configs and will be deployed together in
        a single PR when "Deploy All" is clicked.

        Args:
            user_code: User who is adding to queue
            infra_type: Infrastructure type (s3, sqs, dynamodb, ecs, etc.)
            environment: Environment (dev, staging, prod)
            config_data: Infrastructure configuration data
            tenant_code: Tenant code

        Returns:
            Created queue item (same table as service configs)

        Raises:
            ValueError: If invalid configuration
        """
        # Get resource name/identifier from config_data
        # 'identifier' is the actual resource name entered by user in infra studio
        service_name = (
            config_data.get('identifier') or
            config_data.get('name') or
            config_data.get('service_name')
        )
        if not service_name:
            import uuid
            service_name = f"infra-{infra_type}-{uuid.uuid4().hex[:8]}"

        # Convert environment string to enum
        env_enum = EnvironmentEnum(environment.lower())

        # Extract common fields from config_data
        region = config_data.get('geo_loc_mst_code') or config_data.get('region') or 'ap-south-1'
        application_code = config_data.get('applications_mst_code') or config_data.get('application_code') or config_data.get('product')
        geo_loc_mst_code = config_data.get('geo_loc_mst_code') or 'region-aspora-mumbai'

        # Derive infra_vendor_accounts_mst_code if not provided
        # Pattern: {vendor}_{tenant_code}_{environment} (e.g., aws_vance_dev)
        infra_vendor_accounts_mst_code = config_data.get('infra_vendor_accounts_mst_code') or config_data.get('vendor_account_code')
        if not infra_vendor_accounts_mst_code:
            vendor = 'aws'  # Default to AWS, could be derived from provider selection
            env_for_vendor_account = "staging" if environment == "stage" else environment
            infra_vendor_accounts_mst_code = f"{vendor}_{tenant_code}_{env_for_vendor_account}".lower()

        if not application_code:
            raise ValueError("applications_mst_code (or product) is required in config_data")

        # Look up application name from database (application_code is a UUID, we need the actual name)
        product_name = config_data.get('product_name') or config_data.get('product')
        if not product_name:
            # Look up the application to get its human-readable name
            app_record = await self.app_repo.get_by_code(application_code)
            if app_record:
                product_name = app_record.name
                logger.info(f"Resolved application code {application_code} to name: {product_name}")
            else:
                # Fallback to application_code if not found (shouldn't happen normally)
                product_name = application_code
                logger.warning(f"Application not found for code {application_code}, using code as name")

        # =========================================================================
        # STEP 1: Save to infrastructure_mst table (create or update)
        # =========================================================================
        infrastructure_record = await self._save_infrastructure_record(
            infra_type=infra_type,
            identifier=service_name,
            tenant_code=tenant_code,
            application_code=application_code,
            environment=env_enum,
            region=region,
            infra_vendor_accounts_mst_code=infra_vendor_accounts_mst_code,
            geo_loc_mst_code=geo_loc_mst_code,
            config_data=config_data,
            user_code=user_code
        )

        # =========================================================================
        # STEP 2: Add to gitops_queue
        # =========================================================================
        # Check for duplicate in queue (same name + environment + user)
        existing_queue_item = await self.queue_repo.check_duplicate_infra(
            user_code, service_name, environment
        )

        # Generate unique queue code
        import uuid
        queue_code = f"queue-{uuid.uuid4().hex[:12]}"

        # Create config snapshot with infrastructure_mst reference
        # Ensure resource-specific names are at root level for display naming
        config_snapshot = {
            **config_data,
            'service_name': service_name,
            'infra_type': infra_type,
            'environment': environment,
            'tenant_code': tenant_code,
            'created_via': 'infrastructure_studio',
            'infrastructure_mst_code': infrastructure_record.code,  # Link to infra record
            # Store the RESOLVED product name (human-readable, not UUID)
            'product_name': product_name,
        }

        # Compute file path and atlantis project name using config_snapshot which has product_name
        hcl_file_path = self._compute_infra_hcl_file_path(
            infra_type, service_name, environment, tenant_code, config_snapshot
        )
        atlantis_project_name = self._compute_infra_atlantis_project_name(
            infra_type, service_name, environment, tenant_code, config_snapshot
        )

        # If duplicate queue item exists, UPDATE it instead of creating new
        if existing_queue_item:
            existing_queue_item.config_snapshot = config_snapshot
            existing_queue_item.hcl_file_path = hcl_file_path
            existing_queue_item.atlantis_project_name = atlantis_project_name
            existing_queue_item.infra_type = infra_type
            self.db.add(existing_queue_item)
            await self.db.commit()
            await self.db.refresh(existing_queue_item)
            logger.info(f"Updated infra in queue: {service_name} ({infra_type}) for {environment} by {user_code}, infra_mst={infrastructure_record.code}")
            return existing_queue_item

        # Create NEW queue item in the SAME table as service configs
        queue_item = await self.queue_repo.create(
            code=queue_code,
            user_code=user_code,
            service_config_code=None,  # No service config for standalone infra
            environment=environment,
            infra_type=infra_type,
            config_snapshot=config_snapshot,
            hcl_file_path=hcl_file_path,
            atlantis_project_name=atlantis_project_name,
            status=TransactionQueueStatusEnum.APPROVED,
            tenant_code=tenant_code
        )

        await self.db.commit()
        logger.info(f"Added infra to queue: {service_name} ({infra_type}) for {environment} by {user_code}, infra_mst={infrastructure_record.code}")

        return queue_item

    async def _save_infrastructure_record(
        self,
        infra_type: str,
        identifier: str,
        tenant_code: str,
        application_code: str,
        environment: EnvironmentEnum,
        region: str,
        infra_vendor_accounts_mst_code: str,
        geo_loc_mst_code: str,
        config_data: Dict[str, Any],
        user_code: str
    ):
        """
        Save or update infrastructure record in infrastructure_mst table.

        Uses type-specific factories and repository methods.

        Returns:
            InfrastructureMstModel - the created or updated record
        """
        # Map infra_type to infrastructuretype_ref_code
        infra_type_map = {
            's3': 's3_infrastructuretype_ref',
            'sqs': 'sqs_infrastructuretype_ref',
            'dynamodb': 'dynamodb_infrastructuretype_ref',
            'ecs': 'ecs_infrastructuretype_ref',
            'rds': 'rds_infrastructuretype_ref',
            'elasticache': 'elasticache_infrastructuretype_ref',
        }
        infrastructuretype_ref_code = infra_type_map.get(infra_type.lower(), f'{infra_type}_infrastructuretype_ref')

        # Check if infrastructure already exists and update or create based on type
        if infra_type.lower() == 's3':
            existing = await self.infra_repo.check_s3_bucket_exists(
                bucket_identifier=identifier,
                tenant_code=tenant_code,
                environment=environment
            )
            if existing:
                # Update existing record with new config data
                await self.infra_repo.update(existing, {
                    'infra_status': DeploymentStatusEnum.INITIATED,
                    'infra_status_updated_by': user_code,
                    'infra_status_updated_at': datetime.utcnow(),
                })
                logger.info(f"Updated existing S3 infrastructure: {existing.code}")
                return existing

            # Create new S3 infrastructure record
            infra_data = make_infrastructure_mst_s3(
                identifier=identifier,
                tenant_code=tenant_code,
                application_code=application_code,
                environment=environment,
                region=region,
                infrastructuretype_ref_code=infrastructuretype_ref_code,
                infra_vendor_accounts_mst_code=infra_vendor_accounts_mst_code,
                geo_loc_mst_code=geo_loc_mst_code,
                infra_status=DeploymentStatusEnum.INITIATED,
                infra_status_updated_by=user_code,
            )
            new_infra = await self.infra_repo.create(**infra_data)
            logger.info(f"Created new S3 infrastructure: {new_infra.code}")
            return new_infra

        elif infra_type.lower() == 'sqs':
            existing = await self.infra_repo.check_sqs_queue_exists(
                queue_identifier=identifier,
                tenant_code=tenant_code,
                environment=environment
            )
            if existing:
                await self.infra_repo.update(existing, {
                    'infra_status': DeploymentStatusEnum.INITIATED,
                    'infra_status_updated_by': user_code,
                    'infra_status_updated_at': datetime.utcnow(),
                })
                logger.info(f"Updated existing SQS infrastructure: {existing.code}")
                return existing

            infra_data = make_infrastructure_mst_sqs(
                identifier=identifier,
                tenant_code=tenant_code,
                application_code=application_code,
                environment=environment,
                region=region,
                infrastructuretype_ref_code=infrastructuretype_ref_code,
                infra_vendor_accounts_mst_code=infra_vendor_accounts_mst_code,
                geo_loc_mst_code=geo_loc_mst_code,
                infra_status=DeploymentStatusEnum.INITIATED,
                infra_status_updated_by=user_code,
            )
            new_infra = await self.infra_repo.create(**infra_data)
            logger.info(f"Created new SQS infrastructure: {new_infra.code}")
            return new_infra

        elif infra_type.lower() == 'dynamodb':
            existing = await self.infra_repo.check_dynamodb_table_exists(
                table_identifier=identifier,
                tenant_code=tenant_code,
                environment=environment
            )
            if existing:
                await self.infra_repo.update(existing, {
                    'infra_status': DeploymentStatusEnum.INITIATED,
                    'infra_status_updated_by': user_code,
                    'infra_status_updated_at': datetime.utcnow(),
                })
                logger.info(f"Updated existing DynamoDB infrastructure: {existing.code}")
                return existing

            # DynamoDB requires partition key info
            partition_key = config_data.get('partition_key') or config_data.get('hash_key') or 'id'
            partition_key_type = config_data.get('partition_key_type') or config_data.get('hash_key_type') or 'S'

            infra_data = make_infrastructure_mst_dynamodb(
                identifier=identifier,
                partition_key=partition_key,
                partition_key_type=partition_key_type,
                tenant_code=tenant_code,
                application_code=application_code,
                environment=environment,
                region=region,
                infrastructuretype_ref_code=infrastructuretype_ref_code,
                infra_vendor_accounts_mst_code=infra_vendor_accounts_mst_code,
                geo_loc_mst_code=geo_loc_mst_code,
                infra_status=DeploymentStatusEnum.INITIATED,
                infra_status_updated_by=user_code,
            )
            new_infra = await self.infra_repo.create(**infra_data)
            logger.info(f"Created new DynamoDB infrastructure: {new_infra.code}")
            return new_infra

        else:
            # For other infrastructure types, create a generic record
            # This is a fallback - specific types should be added above
            from uuid import uuid4

            infra_code = f"INFRA_{infra_type.upper()}_{uuid4().hex[:8].upper()}"
            locator = {
                'name': identifier,
                'region': region,
                **{k: v for k, v in config_data.items() if k not in ['service_name', 'name']}
            }

            new_infra = await self.infra_repo.create(
                code=infra_code,
                name=f"{infra_type.upper()} - {identifier}",
                description=f"{infra_type} {identifier} in {region} for {tenant_code}/{application_code}",
                infrastructuretype_ref_code=infrastructuretype_ref_code,
                infra_vendor_accounts_mst_code=infra_vendor_accounts_mst_code,
                resource_group_mst_code=None,
                tenants_mst_code=tenant_code,
                applications_mst_code=application_code,
                environments_enum=environment,
                geo_loc_mst_code=geo_loc_mst_code,
                locator=locator,
                infra_status=DeploymentStatusEnum.INITIATED,
                infra_status_updated_by=user_code,
                infra_status_updated_at=datetime.utcnow(),
            )
            logger.info(f"Created new {infra_type} infrastructure: {new_infra.code}")
            return new_infra

    # Mapping from infra_type to folder name and suffix for standalone infrastructure
    # These match the folder structure used in Save & Sync (TerragruntSyncService)
    # Example: s3 -> folder: buckets, suffix: bucket
    INFRA_TYPE_CONFIG = {
        's3': {'folder': 'buckets', 'suffix': 'bucket'},
        'sqs': {'folder': 'queues', 'suffix': 'queue'},
        'dynamodb': {'folder': 'tables', 'suffix': 'table'},
        'gateway': {'folder': 'gateways', 'suffix': 'gateway'},  # Kong API Gateway
        # For ECS/ECS_EC2: uses 'services', 'services-dev', or 'ops-tools' via _get_services_folder_name
        # Those are handled separately in the ECS path, not through this config
    }

    def _compute_infra_hcl_file_path(
        self,
        infra_type: str,
        service_name: str,
        environment: str,
        tenant_code: str,
        config_data: Dict[str, Any]
    ) -> str:
        """
        Compute the HCL file path for standalone infrastructure.

        Uses same base path structure as services but with infra type folder.
        Path: environment/{product}-{env}-{version_index}/{region}/{folder}/{name}/terragrunt.hcl

        Folder mapping:
        - s3 -> buckets
        - sqs -> queues
        - dynamodb -> tables
        """
        # Get product name from config
        product_name = config_data.get('product_name') or config_data.get('product') or config_data.get('applications_mst_code') or ''

        # Get geo location and convert to AWS region
        geo_loc_code = config_data.get('geo_loc_mst_code') or config_data.get('region') or 'ap-south-1'
        region = TerragruntSyncService._get_aws_region_from_geo_loc(geo_loc_code)

        # Normalize names
        product_sanitized = self.terragrunt_service._sanitize_name(product_name)
        service_sanitized = self.terragrunt_service._sanitize_name(service_name)
        # For standalone infrastructure (S3, SQS, DynamoDB), use the sanitized name directly
        # WITHOUT the -service suffix that _normalize_service_for_path() adds
        # The -service suffix is only for ECS services, not standalone infrastructure
        name_for_path = service_sanitized
        env_sanitized = TerragruntSyncService._normalize_environment_for_path(environment, tenant_code)
        region_sanitized = TerragruntSyncService._normalize_region_for_path(region)
        version_index = settings.infra_version_index or "01"

        # Get folder name from config (s3 -> buckets, sqs -> queues, etc.)
        infra_config = self.INFRA_TYPE_CONFIG.get(infra_type.lower(), {})
        folder_name = infra_config.get('folder', infra_type.lower())

        # Path: environment/{product}-{env}-{version_index}/{region}/{folder}/{name}/terragrunt.hcl
        return f"environment/{product_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/{folder_name}/{name_for_path}/terragrunt.hcl"

    def _compute_infra_atlantis_project_name(
        self,
        infra_type: str,
        service_name: str,
        environment: str,
        tenant_code: str,
        config_data: Dict[str, Any]
    ) -> str:
        """
        Compute the Atlantis project name for standalone infrastructure.

        Format: {product}-{env}-{name}-{suffix}
        Example: core-stage-ajuz-buck-1-bucket

        Suffix mapping (from INFRA_TYPE_CONFIG):
        - s3 -> bucket
        - sqs -> queue
        - dynamodb -> table
        """
        # Get product name from config
        product_name = config_data.get('product_name') or config_data.get('product') or config_data.get('applications_mst_code') or ''

        # Normalize environment for display
        env_display = TerragruntSyncService._normalize_environment_for_display(
            environment, tenant_code
        )

        # Sanitize names
        product_sanitized = self.terragrunt_service._sanitize_name(product_name)
        service_sanitized = self.terragrunt_service._sanitize_name(service_name)

        # Get suffix from config (s3 -> bucket, sqs -> queue, etc.)
        infra_config = self.INFRA_TYPE_CONFIG.get(infra_type.lower(), {})
        suffix = infra_config.get('suffix', infra_type.lower())

        return f"{product_sanitized}-{env_display}-{service_sanitized}-{suffix}"

    def _add_atlantis_entry(
        self,
        atlantis_content: str,
        file_path: str,
        product_name: str,
        env: str,
        service_name: str,
        infra_type: str,
        tenant: str = "",
        geo_loc: str = ""
    ) -> str:
        """
        Add atlantis project entry with correct naming based on infra type.

        For ECS/ECS_EC2: uses -service suffix (via _add_atlantis_ecs_entry)
        For standalone infra: uses type-specific suffix (-bucket, -queue, -table, -gateway)

        Args:
            atlantis_content: Current atlantis.yaml content
            file_path: Full terragrunt file path
            product_name: Product name
            env: Environment
            service_name: Service/resource name
            infra_type: Infrastructure type (ecs, s3, sqs, dynamodb, gateway, etc.)
            tenant: Tenant code
            geo_loc: Geographic location code

        Returns:
            Modified atlantis.yaml content
        """
        # For ECS services, use the standard method which adds -service suffix
        if infra_type.lower() in ('ecs', 'ecs_ec2'):
            return self.terragrunt_service._add_atlantis_ecs_entry(
                atlantis_content=atlantis_content,
                file_path=file_path,
                product_name=product_name,
                env=env,
                service_name=service_name,
                tenant=tenant,
                geo_loc=geo_loc
            )

        # For standalone infrastructure, build entry with type-specific suffix
        dir_path = file_path.replace("/terragrunt.hcl", "")

        product_sanitized = self.terragrunt_service._sanitize_name(product_name)
        service_sanitized = self.terragrunt_service._sanitize_name(service_name)
        env_for_name = TerragruntSyncService._normalize_environment_for_display(env, tenant)
        branch_pattern = TerragruntSyncService._get_atlantis_branch_pattern(env, tenant)

        # Get suffix from config (s3 -> bucket, sqs -> queue, etc.)
        infra_config = self.INFRA_TYPE_CONFIG.get(infra_type.lower(), {})
        suffix = infra_config.get('suffix', infra_type.lower())

        # Build name: {product}-{env}-{name}-{suffix}
        # Example: core-stage-ajuz-buck-1-bucket
        if product_sanitized == "core" and env_for_name == "prod" and geo_loc:
            geo_loc_normalized = TerragruntSyncService._normalize_geo_loc_for_atlantis(geo_loc)
            name = f"{product_sanitized}-{env_for_name}-{geo_loc_normalized}-{service_sanitized}-{suffix}"
        else:
            name = f"{product_sanitized}-{env_for_name}-{service_sanitized}-{suffix}"

        # Check if this entry already exists (by name)
        if f"name: {name}" in atlantis_content:
            logger.info(f"Atlantis project entry already exists: {name}")
            return atlantis_content

        # Build the new project entry with exact formatting (2-space indents)
        new_entry = f"  - name: {name}\n"
        new_entry += f"    dir: {dir_path}\n"
        new_entry += f"    workflow: terragrunt\n"
        new_entry += f"    branch: /{branch_pattern}/\n"

        # Insert at the beginning of projects list
        if 'projects:\n\n' in atlantis_content:
            atlantis_content = atlantis_content.replace(
                'projects:\n\n',
                f'projects:\n\n{new_entry}',
                1
            )
            logger.info(f"Added atlantis project entry: {name}")
        elif 'projects:\n' in atlantis_content:
            atlantis_content = atlantis_content.replace(
                'projects:\n',
                f'projects:\n{new_entry}',
                1
            )
            logger.info(f"Added atlantis project entry: {name}")
        else:
            logger.warning("Could not find projects: section in atlantis.yaml")

        return atlantis_content

    async def _get_service_config_with_relations(self, service_config_code: str):
        """
        Get service config with relationships loaded for correct path computation.

        This ensures we have access to:
        - service.name (human-readable service name)
        - service.application.name (product name)
        - service.service_type (for folder determination)

        This is critical for correct file path generation - without relationships,
        the code would fall back to using UUID-like service_config_code.
        """
        from sqlalchemy.orm import selectinload
        from sqlalchemy import select
        from app.db.models.service_config_model import ServiceConfigModel
        from app.db.models.services_mst_model import ServicesMstModel

        stmt = (
            select(ServiceConfigModel)
            .where(ServiceConfigModel.code == service_config_code)
            .options(
                selectinload(ServiceConfigModel.language_ref),
                selectinload(ServiceConfigModel.service).selectinload(ServicesMstModel.application)
            )
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    def _compute_paths_using_terragrunt_service(
        self,
        config,
        environment: str,
        tenant_code: str
    ) -> Tuple[str, str]:
        """
        Compute HCL file path and atlantis project name using TerragruntSyncService.

        This ensures we use the EXACT same logic as "Save & Sync" for correct paths.
        The path comes from the service relationship data:
        - service_name from config.service.name (NOT config.code!)
        - product_name from config.service.application.name
        - service_type from config.service.service_type

        Returns:
            Tuple of (hcl_file_path, atlantis_project_name)
        """
        # Extract data from loaded relationships - MUST use service.name, not config.code
        service_name = ""
        product_name = ""
        service_type = None

        if hasattr(config, 'service') and config.service:
            # Get the human-readable service name (e.g., "my-service")
            service_name = config.service.name

            # Get service type for folder determination (API, BACKGROUND_SERVICE, OPS_TOOLS)
            if hasattr(config.service, 'service_type') and config.service.service_type:
                service_type = config.service.service_type.value if hasattr(config.service.service_type, 'value') else config.service.service_type

            # Get product name from application relationship
            if hasattr(config.service, 'application') and config.service.application:
                product_name = config.service.application.name
        else:
            logger.error(f"Service relationship not loaded for config {config.code} - paths will be incorrect")
            raise ValueError(f"Service relationship not loaded for config {config.code}. Cannot compute correct file path.")

        # Get geo location and convert to AWS region
        geo_loc_code = getattr(config, 'geo_loc_mst_code', 'ap-south-1')
        region = TerragruntSyncService._get_aws_region_from_geo_loc(geo_loc_code)
        version_index = settings.infra_version_index or "01"

        # Use TerragruntSyncService's path building - same logic as push_to_github
        hcl_file_path = self.terragrunt_service._build_github_file_path(
            product_name=product_name,
            service_name=service_name,
            environment=environment,
            region=region,
            tenant=tenant_code,
            version_index=version_index,
            service_type=service_type
        )

        # Build atlantis project name using same logic as TerragruntSyncService
        product_sanitized = self.terragrunt_service._sanitize_name(product_name)
        service_sanitized = self.terragrunt_service._sanitize_name(service_name)
        env_display = TerragruntSyncService._normalize_environment_for_display(environment, tenant_code)
        service_for_atlantis = TerragruntSyncService._get_service_name_for_env_files(service_sanitized)

        # Include geo_loc in atlantis name for core-prod (matches _add_atlantis_ecs_entry logic)
        if product_sanitized == "core" and env_display == "prod" and geo_loc_code:
            geo_loc_normalized = TerragruntSyncService._normalize_geo_loc_for_atlantis(geo_loc_code)
            atlantis_project_name = f"{product_sanitized}-{env_display}-{geo_loc_normalized}-{service_for_atlantis}"
        else:
            atlantis_project_name = f"{product_sanitized}-{env_display}-{service_for_atlantis}"

        logger.info(f"Computed paths for {service_name}: file={hcl_file_path}, atlantis={atlantis_project_name}")

        return hcl_file_path, atlantis_project_name

    def _create_config_snapshot(self, config) -> Dict[str, Any]:
        """
        Create a snapshot of the service config for the queue.

        This captures all fields needed for HCL generation.
        """
        # Convert model to dict, excluding internal SQLAlchemy attributes
        snapshot = {}
        for column in config.__table__.columns:
            value = getattr(config, column.name)
            # Handle datetime serialization
            if isinstance(value, datetime):
                value = value.isoformat()
            # Handle enum serialization
            elif hasattr(value, 'value'):
                value = value.value
            snapshot[column.name] = value

        return snapshot

    async def _create_config_snapshot_with_relations(self, config) -> Dict[str, Any]:
        """
        Create a snapshot including relationship data needed for HCL generation.

        Captures:
        - All ServiceConfigModel columns
        - language_name from language_ref relationship
        - service_type from service relationship
        - product_name from service.application relationship
        """
        from sqlalchemy.orm import selectinload
        from sqlalchemy import select
        from app.db.models.service_config_model import ServiceConfigModel
        from app.db.models.services_mst_model import ServicesMstModel

        # Re-query with relationships loaded
        stmt = (
            select(ServiceConfigModel)
            .where(ServiceConfigModel.code == config.code)
            .options(
                selectinload(ServiceConfigModel.language_ref),
                selectinload(ServiceConfigModel.service).selectinload(ServicesMstModel.application)
            )
        )
        result = await self.db.execute(stmt)
        config_with_rels = result.scalar_one_or_none()

        if not config_with_rels:
            # Fallback to basic snapshot
            return self._create_config_snapshot(config)

        # Create base snapshot from columns
        snapshot = self._create_config_snapshot(config_with_rels)

        # Add language_name from language_ref relationship
        if config_with_rels.language_ref:
            snapshot['language_name'] = config_with_rels.language_ref.name

        # Add service_type and product_name from service relationship
        if config_with_rels.service:
            service = config_with_rels.service
            if service.service_type:
                snapshot['service_type'] = service.service_type.value if hasattr(service.service_type, 'value') else service.service_type
            snapshot['service_name'] = service.name

            # Add product_name from application relationship
            if service.application:
                snapshot['product_name'] = service.application.name

        return snapshot

    def _get_infra_type(self, config) -> str:
        """Get infrastructure type from config.

        Uses infrastructuretype_ref_code column directly to avoid async lazy loading issues.
        """
        # Use the FK column directly instead of the relationship to avoid lazy loading
        infra_type_code = getattr(config, 'infrastructuretype_ref_code', None)
        if infra_type_code:
            # Extract the type from the code (e.g., "ecs_infrastructuretype_ref" -> "ecs")
            if '_infrastructuretype_ref' in infra_type_code:
                return infra_type_code.replace('_infrastructuretype_ref', '')
            return infra_type_code
        return "ecs"  # Default to ECS

    async def get_user_queue(
        self,
        user_code: str,
        tenant_code: Optional[str] = None
    ) -> TransactionQueueListResponse:
        """
        Get user's queue items with counts.

        Args:
            user_code: User code
            tenant_code: Optional tenant filter

        Returns:
            Queue list response with items and counts
        """
        items, pending_count, pr_raised_count = await self.queue_repo.get_user_queue_with_counts(
            user_code, tenant_code
        )

        return TransactionQueueListResponse(
            items=[
                self._item_to_response(
                    item,
                    # Group rows carry their own geo/env; route rows carry theirs.
                    # Only one join can match, so either side may be None.
                    kong_geo_loc_mst_code=kong_geo_loc_mst_code or kong_group_geo_loc_mst_code,
                    kong_environment=kong_environment or kong_group_environment,
                    service_mst_code=kong_service_mst_code,
                    infra_geo_loc_mst_code=infra_geo_loc_mst_code,
                    infra_environment=infra_environment,
                    pipeline_run_status=pipeline_run_status.value if pipeline_run_status else None,
                    pipeline_build_stages=pipeline_build_stages,
                    pipeline_run_started_at=pipeline_run_created_at.isoformat() if pipeline_run_created_at else None,
                    pipeline_deploy_result=pipeline_deploy_result,
                )
                for item, infra_geo_loc_mst_code, infra_environment, kong_geo_loc_mst_code, kong_environment, kong_group_geo_loc_mst_code, kong_group_environment, kong_service_mst_code, pipeline_run_status, pipeline_build_stages, pipeline_run_created_at, pipeline_deploy_result in items
            ],
            total=len(items),
            pending_count=pending_count,
            pr_raised_count=pr_raised_count
        )

    def _item_to_response(
        self,
        item: TransactionQueueModel,
        kong_geo_loc_mst_code: Optional[str] = None,
        kong_environment: Optional[EnvironmentEnum] = None,
        infra_geo_loc_mst_code: Optional[str] = None,
        infra_environment: Optional[EnvironmentEnum] = None,
        pipeline_run_status: Optional[str] = None,
        pipeline_build_stages: Optional[Any] = None,
        pipeline_run_started_at: Optional[str] = None,
        pipeline_deploy_result: Optional[Any] = None,
        service_mst_code: Optional[str] = None,
    ) -> TransactionQueueItemResponse:
        """Convert queue item model to response schema."""
        snapshot = item.config_snapshot or {}
        service_name = snapshot.get('service_name')
        display_name = item.display_name

        return TransactionQueueItemResponse(
            id=item.id,
            code=item.code,
            user_code=item.user_code,
            transaction_code=item.transaction_code,
            table_name=item.table_name,
            config_snapshot=item.config_snapshot,
            display_name=display_name,
            case_ref_code=item.case_ref_code,
            status=item.status,
            status_last_updated_at=item.status_last_updated_at,
            tenant_code=item.tenant_code,
            created_at=item.created_at,
            updated_at=item.updated_at,
            pipeline_run_status=pipeline_run_status,
            pipeline_build_stages=pipeline_build_stages,
            pipeline_run_started_at=pipeline_run_started_at,
            pipeline_deploy_result=pipeline_deploy_result,
            # Resolved server-side because transaction_code cannot say it: a KONG_ROUTE
            # item points at either a route or a route GROUP, and a KRG_ code means
            # nothing to a caller. Without this, anything filtering the queue by
            # service had to guess — and silently matched nothing for gateway items.
            service_mst_code=service_mst_code,
        )

    def _build_display_name(
        self,
        config_snapshot: Dict[str, Any],
        service_name: Optional[str],
        geo_loc_mst_code: Optional[str] = None,
        environment: Optional[EnvironmentEnum] = None
    ) -> str:
        """
        Build a formatted display name for the queue item.

        Format: "Type - Name - Region - Env"
        Example: "S3 - my-bucket - Mumbai - Dev"
        """
        # Format infra type
        infra_type_map = {
            's3': 'S3',
            'sqs': 'SQS',
            'dynamodb': 'DynamoDB',
            'ecs': 'ECS',
            'ecs_ec2': 'ECS',
            'rds': 'RDS',
            'elasticache': 'ElastiCache',
            'lambda': 'Lambda',
            'gateway': 'API Gateway',
        }
        # infra_type = infra_type_map.get(item.infra_type.lower(), item.infra_type.upper())

        # Get name from service_name or config_snapshot
        name = service_name
        if not name and config_snapshot:
            # Try various name fields from snapshot
            name = (
                config_snapshot.get('service_name') or
                config_snapshot.get('name') or
                config_snapshot.get('bucket_name') or
                config_snapshot.get('queue_name') or
                config_snapshot.get('table_name') or
                config_snapshot.get('identifier')
            )
        if not name:
            name = 'unnamed'

        # Extract region from atlantis_project_name or config_snapshot
        region = None
        region_patterns = {
            'mumbai': 'Mumbai',
            'london': 'London',
            'tokyo': 'Tokyo',
            'sydney': 'Sydney',
            'frankfurt': 'Frankfurt',
            'virginia': 'Virginia',
            'oregon': 'Oregon',
        }

        project_name_lower = (config_snapshot.get('atlantis_project_name') or '').lower()
        for pattern, display in region_patterns.items():
            if pattern in project_name_lower:
                region = display
                break

        # If region not found in project name, try config_snapshot
        if not region and (geo_loc_mst_code or config_snapshot):
            geo_loc = geo_loc_mst_code or config_snapshot.get('geo_loc_mst_code') or config_snapshot.get('region', '')
            for pattern, display in region_patterns.items():
                if pattern in geo_loc.lower():
                    region = display
                    break

        # Format environment
        env_map = {
            'dev': 'Dev',
            'staging': 'Staging',
            'prod': 'Prod',
            'production': 'Prod',
        }
        env_value = environment.value if hasattr(environment, "value") else environment
        env_value = (env_value or config_snapshot.get('environment') or '').lower()
        if not env_value:
            env = 'Unknown'
        else:
            env = env_map.get(env_value, env_value.capitalize())

        # Build display string
        parts = [ name]
        if region:
            parts.append(region)
        parts.append(env)

        return ' - '.join(parts)

    async def remove_from_queue(self, item_id: int, user_code: str) -> bool:
        """
        Remove an item from the queue (soft delete).

        Only approved items can be removed.

        Args:
            item_id: Queue item ID
            user_code: User requesting removal (for validation)

        Returns:
            True if removed, False if not found or not allowed
        """
        item = await self.queue_repo.get_by_id(item_id)
        if not item:
            return False

        if item.user_code != user_code:
            raise ValueError("Cannot remove another user's queue item")

        if item.status not in [TransactionQueueStatusEnum.DRAFT, TransactionQueueStatusEnum.APPROVED]:
            raise ValueError("Can only remove draft or approved items from queue")

        await self.queue_repo.soft_delete_item(item_id)
        await self.db.commit()

        logger.info(f"Removed from queue: item {item_id} by {user_code}")
        return True

    async def delete_queue_item(self, queue_code: str, user_code: str) -> Optional[int]:
        """
        Delete a queue item by queue code.

        Only items with status DRAFT or APPROVED can be deleted. After these statuses,
        entries become immutable and cannot be deleted.

        Args:
            queue_code: Queue item code (e.g., "queue-abc123def456")
            user_code: User requesting deletion (for validation)

        Returns:
            Deleted item ID if deleted, None if not foundgb

        Raises:
            ValueError: If item not owned by user or status is not DRAFT/APPROVED
        """
        # Get item by queue code
        item = await self.queue_repo.get_by(code=queue_code)
        if not item:
            return None
        if getattr(item, "is_deleted", False):
            return None

        # Verify ownership
        if item.user_code != user_code:
            raise ValueError("Cannot delete another user's queue item")

        # Only allow deletion if status is DRAFT or APPROVED
        if item.status not in [TransactionQueueStatusEnum.DRAFT, TransactionQueueStatusEnum.APPROVED]:
            raise ValueError(
                f"Cannot delete queue item with status '{item.status}'. "
                "Only items with status 'DRAFT' or 'APPROVED' can be deleted. "
                "After these statuses, entries become immutable."
            )

        # Soft delete the item
        await self.queue_repo.soft_delete_item(item.id)
        await self.db.commit()

        logger.info(f"Deleted queue item: {queue_code} (id={item.id}) by {user_code}")
        return item.id

    async def preview_item(self, item_id: int) -> TransactionQueuePreviewResponse:
        """
        Generate HCL preview from stored snapshot.

        Args:
            item_id: Queue item ID

        Returns:
            Preview response with generated HCL

        Raises:
            ValueError: If item not found
        """
        item = await self.queue_repo.get_by_id(item_id)
        if not item:
            raise ValueError(f"Queue item not found: {item_id}")

        # Generate HCL from snapshot (returns tuple, we only need hcl_content for preview)
        hcl_content, _field_mapping = await self._generate_hcl_from_snapshot(item)

        return TransactionQueuePreviewResponse(
            id=item.id,
            hcl_content=hcl_content,
            file_path=item.hcl_file_path,
            atlantis_project_name=item.atlantis_project_name,
            service_name=item.config_snapshot.get('service_name'),
            environment=item.environment,
            infra_type=item.infra_type
        )

    async def _generate_hcl_from_snapshot(
        self,
        item: TransactionQueueModel,
        existing_content: Optional[str] = None
    ) -> Tuple[str, Dict[str, str]]:
        """
        Generate HCL content from the stored config snapshot.

        For ECS/service configs: Uses TerragruntSyncService's _build_field_mapping()
        and _generate_hcl_content() to leverage the full HCL generation logic.

        For standalone infra (S3, SQS, DynamoDB): Uses template-based approach.

        Args:
            item: Queue item with config snapshot
            existing_content: Optional existing HCL content from GitHub (preserves manual edits)

        Returns:
            Tuple of (hcl_content, field_mapping) - field_mapping used for Dockerfile sync
        """
        snapshot = item.config_snapshot or {}
        infra_type = item.infra_type.lower()

        # ECS services use TerragruntSyncService for full HCL generation
        if infra_type in ('ecs', 'ecs_ec2'):
            return self._generate_ecs_hcl(item, snapshot, existing_content)

        # Standalone infrastructure uses template-based approach
        # Return empty field_mapping for non-ECS (no Dockerfile sync needed)
        return self._generate_infra_hcl(item, snapshot, infra_type), {}

    def _generate_ecs_hcl(
        self,
        item: TransactionQueueModel,
        snapshot: Dict[str, Any],
        existing_content: Optional[str] = None
    ) -> Tuple[str, Dict[str, str]]:
        """
        Generate ECS HCL using TerragruntSyncService's field mapping and HCL generation.

        This ensures the queue uses the same HCL generation as "Save & Sync".

        Args:
            item: Queue item with config snapshot
            snapshot: Config snapshot data
            existing_content: Optional existing HCL content from GitHub (preserves manual edits)

        Returns:
            Tuple of (hcl_content, field_mapping) - field_mapping used for Dockerfile sync
        """
        # Extract config data from snapshot
        # The snapshot contains all ServiceConfigModel columns serialized
        config = snapshot.get('config', {})
        sidecar_config = snapshot.get('sidecar_config', [])
        language_name = snapshot.get('language_name')
        service_type = snapshot.get('service_type', 'API')
        product_name = snapshot.get('product_name', '')
        service_name = snapshot.get('service_name', '')

        # Check if no-ALB configuration
        is_no_alb = self.terragrunt_service._is_no_alb(config)

        # Build field mapping using TerragruntSyncService
        field_mapping = self.terragrunt_service._build_field_mapping(
            config=config,
            sidecar_config=sidecar_config,
            language_name=language_name,
            is_no_alb=is_no_alb
        )

        # Get appropriate template based on service type and config
        template_file = self.terragrunt_service._get_template_file(
            config=config,
            environment=item.environment,
            tenant=item.tenant_code or '',
            product_name=product_name,
            service_type=service_type
        )

        # Generate HCL content using TerragruntSyncService
        # Pass existing_content to preserve manual edits (like Save & Sync does)
        hcl_content = self.terragrunt_service._generate_hcl_content(
            field_mapping=field_mapping,
            existing_content=existing_content,  # Preserve manual edits from GitHub
            template_file=template_file,
            environment=item.environment,
            service_name=service_name,
            tenant=item.tenant_code or '',
            product_name=product_name,
            service_type=service_type
        )

        return hcl_content, field_mapping

    def _should_modify_dockerfile_from_snapshot(
        self,
        snapshot: Dict[str, Any],
        field_mapping: Dict[str, str]
    ) -> bool:
        """
        Check if Dockerfile should be modified based on snapshot data.

        This is the snapshot-based equivalent of DockerfileSyncService.should_modify_dockerfile().
        Uses same eligibility logic but works with snapshot data instead of ServiceConfigModel.

        Returns True if:
        - Language is Java (from language_name in snapshot)
        - Repository is configured in snapshot config
        - Branches list is non-empty
        - AND any of:
          - Datadog sidecar is enabled/disabled
          - build_args are configured
          - xms or xmx memory settings are configured
        """
        from app.utils.dockerfile_transformer import is_java_language

        config = snapshot.get('config', {})
        language_name = snapshot.get('language_name')

        # Check if language is Java
        if not language_name or not is_java_language(language_name):
            return False

        # Check if config exists with repository and branches
        if not config:
            return False

        repository = config.get("repository")
        if not repository:
            return False

        branches = config.get("branches")
        if not branches or not isinstance(branches, list) or len(branches) == 0:
            return False

        # Check if there's a reason to modify the Dockerfile
        has_datadog = field_mapping.get("enable_datadog_sidecar") == "true"
        has_build_args = bool(config.get("build_args"))
        has_xms = bool(config.get("xms"))
        has_xmx = bool(config.get("xmx"))

        return has_datadog or has_build_args or has_xms or has_xmx

    async def _sync_dockerfile_from_snapshot(
        self,
        item: TransactionQueueModel,
        snapshot: Dict[str, Any],
        field_mapping: Dict[str, str],
        github_token: str,
        tenant_code: str
    ) -> Optional[Dict[str, Any]]:
        """
        Sync Dockerfile for Datadog configuration using snapshot data.

        This is the snapshot-based equivalent of the Dockerfile sync in Save & Sync.
        Non-blocking - logs errors but doesn't fail the deploy.

        Args:
            item: Queue item with config snapshot
            snapshot: Config snapshot data
            field_mapping: Field mapping for HCL (contains enable_datadog_sidecar)
            github_token: GitHub API token
            tenant_code: Tenant code

        Returns:
            Dockerfile sync result dict, or None if skipped/not applicable
        """
        from app.utils.dockerfile_transformer import (
            transform_dockerfile_for_datadog,
            transform_dockerfile_remove_datadog
        )
        from app.integrations.github_integration import GitHubIntegration
        from app.utils.language_helpers import sanitize_service_name_for_path

        config = snapshot.get('config', {})
        service_name = snapshot.get('service_name', '')

        # Get repository and branches
        repository = config.get("repository")
        branches = config.get("branches", [])

        if not repository or "/" not in repository or not branches:
            logger.debug(f"Dockerfile sync skipped for {service_name}: no valid repository/branches")
            return None

        owner, repo = repository.split("/", 1)
        environment = item.environment

        # Compute dockerfile_path
        dockerfile_path = config.get("dockerfile_path")
        if not dockerfile_path and config.get("generate_dockerfile", False):
            build_path = config.get("build_path")
            if build_path:
                sanitized_name = sanitize_service_name_for_path(service_name)
                dockerfile_path = f"docker/{sanitized_name}/Dockerfile"
        dockerfile_path = dockerfile_path or "Dockerfile"

        # Get Datadog settings
        enable_datadog = field_mapping.get("enable_datadog_sidecar") == "true"
        xms_mb = int(config.get("xms")) if config.get("xms") else None
        xmx_mb = int(config.get("xmx")) if config.get("xmx") else None
        build_args = config.get("build_args")

        # Get advanced options from sidecar_config in snapshot
        # Same logic as get_datadog_advanced_options() but works with snapshot data
        advanced_options = None
        sidecar_config = snapshot.get('sidecar_config', [])
        for sidecar in sidecar_config or []:
            if not isinstance(sidecar, dict):
                continue
            sidecar_name = sidecar.get("name", "").lower()
            sidecar_code = sidecar.get("sidecar_config_code", "").lower()
            is_datadog = "datadog" in sidecar_name or "datadog" in sidecar_code
            if is_datadog and sidecar.get("enabled"):
                advanced_options = sidecar.get("advanced_options")
                break

        # Sanitized values for branch naming
        service_sanitized = self.terragrunt_service._sanitize_name(service_name)
        env_normalized = self.terragrunt_service._normalize_environment_for_display(environment, tenant_code)
        geo_loc = snapshot.get('geo_loc_mst_code', '')
        geo_loc_sanitized = self.terragrunt_service._sanitize_name(geo_loc)

        logger.info(f"Dockerfile sync for {service_name}: {len(branches)} branches, datadog={enable_datadog}")

        branch_results = []
        success_count = 0
        error_count = 0

        for branch in branches:
            try:
                branch_sanitized = branch.replace("/", "-").replace("_", "-").lower()

                # Process branch using DockerfileSyncService's internal method
                # Note: We're using a simplified flow here since we don't have existing PR tracking
                result = self.dockerfile_sync_service._process_branch(
                    GitHubIntegration=GitHubIntegration,
                    github_token=github_token,
                    github_base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    branch=branch,
                    dockerfile_path=dockerfile_path,
                    service_name=service_name,
                    environment=environment,
                    service_sanitized=service_sanitized,
                    branch_sanitized=branch_sanitized,
                    env_normalized=env_normalized,
                    geo_loc_sanitized=geo_loc_sanitized,
                    enable_datadog=enable_datadog,
                    xms_mb=xms_mb,
                    xmx_mb=xmx_mb,
                    advanced_options=advanced_options,
                    build_args=build_args,
                    user_email=None,  # No user email in queue context
                    transform_add=transform_dockerfile_for_datadog,
                    transform_remove=transform_dockerfile_remove_datadog,
                    existing_pr_info=None,  # No existing PR tracking for queue
                    create_secondary_pr=False,
                    tenant_code=tenant_code
                )

                if result.get("status") == "success":
                    success_count += 1
                elif result.get("status") == "error":
                    error_count += 1

                branch_results.append(result)

            except Exception as e:
                logger.error(f"Dockerfile sync error for branch {branch}: {e}")
                error_count += 1
                branch_results.append({
                    "branch": branch,
                    "status": "error",
                    "error": str(e)
                })

        # Determine overall status
        from app.utils.dockerfile_helpers import determine_overall_status
        return determine_overall_status(branch_results, success_count, error_count, len(branches))

    def _generate_infra_hcl(
        self,
        item: TransactionQueueModel,
        snapshot: Dict[str, Any],
        infra_type: str
    ) -> str:
        """
        Generate HCL for standalone infrastructure (S3, SQS, DynamoDB).

        Uses template-based approach with value substitutions.
        """
        import os

        # Get environment for template selection (SQS uses different template for prod)
        environment = item.environment.lower() if item.environment else ''

        # Map infrastructure types to template paths
        # SQS uses different template for prod vs non-prod (same as Save & Sync)
        if infra_type == 'sqs':
            if environment == 'prod':
                template_rel_path = 'templates/terragrunt/sqs/terragrunt-prod.hcl'
            else:
                template_rel_path = 'templates/terragrunt/sqs/terragrunt.hcl'
        else:
            template_map = {
                's3': 'templates/terragrunt/s3/terragrunt.hcl',
                'dynamodb': 'templates/terragrunt/dynamoDB/terragrunt.hcl',
                'gateway': 'templates/terragrunt/gateway/terragrunt.hcl',
            }
            template_rel_path = template_map.get(infra_type)

        if template_rel_path:
            app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            template_path = os.path.join(app_root, template_rel_path)

            try:
                with open(template_path, 'r') as f:
                    template_content = f.read()

                # Use template content directly (same as Save & Sync)
                hcl = template_content

                # Apply snapshot-specific values (matching Save & Sync exactly)
                if infra_type == 's3':
                    # Replace versioning value using regex (same as terragrunt_mgmt_service)
                    versioning = snapshot.get('versioning', False)
                    # Handle string 'true'/'false' from frontend
                    if isinstance(versioning, str):
                        versioning = versioning.lower() == 'true'
                    hcl = re.sub(
                        r'versioning\s*=\s*(true|false)',
                        f'versioning            = {str(versioning).lower()}',
                        hcl
                    )

                    # Insert enable_s3_replication after versioning line if enabled
                    enable_s3_replication = snapshot.get('enable_s3_replication', False)
                    if isinstance(enable_s3_replication, str):
                        enable_s3_replication = enable_s3_replication.lower() == 'true'
                    if enable_s3_replication:
                        insert_pattern = r'(versioning\s*=\s*(?:true|false))'
                        hcl = re.sub(
                            insert_pattern,
                            rf'\1\n  enable_s3_replication = {str(enable_s3_replication).lower()}',
                            hcl
                        )

                    # Insert cross_account_account_id after enable_s3_replication if provided
                    cross_account_account_id = snapshot.get('cross_account_account_id', '')
                    if cross_account_account_id:
                        if enable_s3_replication:
                            insert_pattern = r'(enable_s3_replication\s*=\s*(?:true|false))'
                        else:
                            insert_pattern = r'(versioning\s*=\s*(?:true|false))'
                        hcl = re.sub(
                            insert_pattern,
                            rf'\1\n  cross_account_account_id = "{cross_account_account_id}"',
                            hcl
                        )

                elif infra_type == 'sqs':
                    # Handle string 'true'/'false' from frontend
                    fifo_queue = snapshot.get('fifo_queue', True)
                    if isinstance(fifo_queue, str):
                        fifo_queue = fifo_queue.lower() == 'true'
                    create_dlq = snapshot.get('create_dlq', True)
                    if isinstance(create_dlq, str):
                        create_dlq = create_dlq.lower() == 'true'

                    # Replace create_dlq and fifo_queue using regex (same as terragrunt_mgmt_service)
                    hcl = re.sub(
                        r'create_dlq\s*=\s*(true|false)',
                        f'create_dlq           = {str(create_dlq).lower()}',
                        hcl
                    )
                    hcl = re.sub(
                        r'fifo_queue\s*=\s*(true|false)',
                        f'fifo_queue           = {str(fifo_queue).lower()}',
                        hcl
                    )

                    # Add optional parameters only if provided (matching Save & Sync)
                    visibility_timeout_seconds = snapshot.get('visibility_timeout_seconds')
                    if visibility_timeout_seconds and str(visibility_timeout_seconds).strip():
                        insert_pattern = r'(fifo_queue\s*=\s*(?:true|false))'
                        hcl = re.sub(
                            insert_pattern,
                            rf'\1\n  visibility_timeout_seconds = {visibility_timeout_seconds}',
                            hcl
                        )

                    max_receive_count = snapshot.get('max_receive_count')
                    if max_receive_count and str(max_receive_count).strip():
                        if visibility_timeout_seconds and str(visibility_timeout_seconds).strip():
                            insert_pattern = r'(visibility_timeout_seconds\s*=\s*\d+)'
                        else:
                            insert_pattern = r'(fifo_queue\s*=\s*(?:true|false))'
                        hcl = re.sub(
                            insert_pattern,
                            rf'\1\n  max_receive_count         = {max_receive_count}',
                            hcl
                        )

                    message_retention_seconds = snapshot.get('message_retention_seconds')
                    if message_retention_seconds and str(message_retention_seconds).strip():
                        if max_receive_count and str(max_receive_count).strip():
                            insert_pattern = r'(max_receive_count\s*=\s*\d+)'
                        elif visibility_timeout_seconds and str(visibility_timeout_seconds).strip():
                            insert_pattern = r'(visibility_timeout_seconds\s*=\s*\d+)'
                        else:
                            insert_pattern = r'(fifo_queue\s*=\s*(?:true|false))'
                        hcl = re.sub(
                            insert_pattern,
                            rf'\1\n  message_retention_seconds = {message_retention_seconds}',
                            hcl
                        )

                    dlq_message_retention_seconds = snapshot.get('dlq_message_retention_seconds')
                    if dlq_message_retention_seconds and str(dlq_message_retention_seconds).strip():
                        if message_retention_seconds and str(message_retention_seconds).strip():
                            insert_pattern = r'(message_retention_seconds\s*=\s*\d+)'
                        elif max_receive_count and str(max_receive_count).strip():
                            insert_pattern = r'(max_receive_count\s*=\s*\d+)'
                        elif visibility_timeout_seconds and str(visibility_timeout_seconds).strip():
                            insert_pattern = r'(visibility_timeout_seconds\s*=\s*\d+)'
                        else:
                            insert_pattern = r'(fifo_queue\s*=\s*(?:true|false))'
                        hcl = re.sub(
                            insert_pattern,
                            rf'\1\n  dlq_message_retention_seconds = {dlq_message_retention_seconds}',
                            hcl
                        )

                    cross_account_ids = snapshot.get('cross_account_ids', [])
                    if cross_account_ids and len(cross_account_ids) > 0:
                        ids_formatted = ', '.join(f'"{aid}"' for aid in cross_account_ids)
                        if dlq_message_retention_seconds and str(dlq_message_retention_seconds).strip():
                            insert_pattern = r'(dlq_message_retention_seconds\s*=\s*\d+)'
                        elif message_retention_seconds and str(message_retention_seconds).strip():
                            insert_pattern = r'(message_retention_seconds\s*=\s*\d+)'
                        elif max_receive_count and str(max_receive_count).strip():
                            insert_pattern = r'(max_receive_count\s*=\s*\d+)'
                        elif visibility_timeout_seconds and str(visibility_timeout_seconds).strip():
                            insert_pattern = r'(visibility_timeout_seconds\s*=\s*\d+)'
                        else:
                            insert_pattern = r'(fifo_queue\s*=\s*(?:true|false))'
                        hcl = re.sub(
                            insert_pattern,
                            rf'\1\n  cross_account_ids            = [{ids_formatted}]',
                            hcl
                        )
                        # Add enable_cross_account_access = true when cross_account_ids is present
                        hcl = re.sub(
                            r'(cross_account_ids\s*=\s*\[[^\]]*\])',
                            r'\1\n  enable_cross_account_access = true',
                            hcl
                        )
                elif infra_type == 'dynamodb':
                    partition_key = snapshot.get('partition_key', 'event_name')
                    hcl = hcl.replace(
                        'partition_key         = "event_name"',
                        f'partition_key         = "{partition_key}"'
                    ).replace(
                        'name = "event_name"',
                        f'name = "{partition_key}"'
                    )

                return hcl

            except FileNotFoundError:
                logger.warning(f"Template not found: {template_path}, using fallback")

        # Fallback for unsupported types
        service_name = snapshot.get('service_name', 'unknown')
        return f'''# Generated from queue snapshot
# Service: {service_name}
# Environment: {item.environment}
# Infrastructure Type: {infra_type.upper()}
# Generated at: {datetime.utcnow().isoformat()}

terraform {{
  source = "../../../../../layers/{infra_type}"
}}

include "root" {{
  path = find_in_parent_folders()
}}

include "env" {{
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}}

inputs = {{
  organization = include.env.locals.organization
  env          = include.env.locals.env
  region       = include.env.locals.region
  index        = include.env.locals.index
  tags         = include.env.locals.tags
  identifier   = basename(get_terragrunt_dir())
}}
'''

    async def deploy_all(
        self,
        user_code: str,
        tenant_code: str,
        environment: Optional[str] = None,
        item_ids: Optional[List[int]] = None,
        user_email: Optional[str] = None
    ) -> TransactionQueueDeployResponse:
        """
        Deploy all pending queue items as a single PR.

        Creates atomic commit with all HCL files and atlantis.yaml update.

        Args:
            user_code: User deploying
            tenant_code: Tenant code
            environment: Optional environment filter
            item_ids: Optional specific item IDs to deploy
            user_email: User email for PR attribution

        Returns:
            Deploy response with PR info
        """
        # Get pending items
        if item_ids:
            items = []
            for item_id in item_ids:
                item = await self.queue_repo.get_by_id(item_id)
                if (
                    item
                    and item.tenant_code == tenant_code
                    and item.status == TransactionQueueStatusEnum.APPROVED
                ):
                    items.append(item)
        else:
            items = await self.queue_repo.get_pending_items_for_user(
                user_code, tenant_code, environment
            )

        if not items:
            return TransactionQueueDeployResponse(
                status="error",
                items_deployed=0,
                items_failed=0,
                details=[],
                error="No pending items to deploy"
            )

        logger.info(f"Deploying {len(items)} queue items for user {user_code}")

        try:
            # Resolve infra GitHub repo from tenant DB config (not env vars)
            tenant_cfg = await get_tenant_config(tenant_code, self.db)
            repo_owner = tenant_cfg.github_infra_owner
            repo_name = tenant_cfg.github_infra_repo_name
            base_branch = tenant_cfg.github_infra_branch
            if base_branch == "main":
                base_branch = "stage"
            if not repo_owner or not repo_name:
                raise ValueError(
                    f"Tenant '{tenant_code}' has no github infra_repository configured in tenants_mst.config"
                )

            # Get GitHub token once
            github_token = await self._get_github_token(repo_owner)

            # Fetch current atlantis.yaml from base branch ONCE
            # We'll incrementally update it using TerragruntSyncService's method
            atlantis_resp = await GitHubIntegration.get_file_content(
                token=github_token,
                base_url=settings.github_base_url,
                owner=repo_owner,
                repo=repo_name,
                file_path='atlantis.yaml',
                branch=base_branch
            )
            atlantis_content = atlantis_resp.get('content', '') if atlantis_resp and atlantis_resp.get('exists') else 'version: 3\nprojects:\n'

            # Generate files for all items
            files_to_commit = []
            item_results = []

            # Track env files to create (for ECS services only)
            env_files_to_create = []

            # Track Dockerfile sync tasks (for ECS services with Datadog changes)
            dockerfile_sync_tasks = []

            # Track skipped items (no changes)
            skipped_items = []

            for item in items:
                try:
                    snapshot = item.config_snapshot or {}
                    product_name = snapshot.get('product_name', '')
                    # Get resource name - 'identifier' is the actual resource name entered by user
                    # Fall back to other fields for backwards compatibility
                    service_name = (
                        snapshot.get('identifier') or   # Primary: actual resource name from infra studio
                        snapshot.get('name') or
                        snapshot.get('service_name') or
                        item.transaction_code or
                        ''
                    )
                    geo_loc = snapshot.get('geo_loc_mst_code', '')
                    service_type = snapshot.get('service_type', '')

                    # Check if product_name looks like a UUID (old bug) and resolve it
                    # A UUID pattern looks like: 178d48fc-8b2c-4e79-aca9-e18f089a05f9
                    uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
                    if uuid_pattern.match(product_name):
                        # product_name is a UUID, try to resolve from applications_mst_code in snapshot
                        app_code = snapshot.get('applications_mst_code') or snapshot.get('application_code') or product_name
                        app_record = await self.app_repo.get_by_code(app_code)
                        if app_record:
                            logger.info(f"Deploy: Resolved UUID {product_name} to application name: {app_record.name}")
                            product_name = app_record.name
                            # Update the snapshot so future operations use the correct name
                            snapshot['product_name'] = product_name
                            item.config_snapshot = snapshot

                    # RECOMPUTE file path from snapshot data (fixes incorrect paths from old queue items)
                    if item.infra_type.lower() in ('ecs', 'ecs_ec2'):
                        # ECS services: use TerragruntSyncService's path building
                        region = TerragruntSyncService._get_aws_region_from_geo_loc(geo_loc)
                        version_index = settings.infra_version_index or "01"
                        correct_file_path = self.terragrunt_service._build_github_file_path(
                            product_name=product_name,
                            service_name=service_name,
                            environment=item.environment,
                            region=region,
                            tenant=tenant_code,
                            version_index=version_index,
                            service_type=service_type
                        )
                    else:
                        # Standalone infra: recompute using infra path method
                        correct_file_path = self._compute_infra_hcl_file_path(
                            item.infra_type,
                            service_name,
                            item.environment,
                            tenant_code,
                            snapshot
                        )

                    # Log if path changed and update stored path
                    if correct_file_path != item.hcl_file_path:
                        logger.info(f"Deploy: Correcting file path for {service_name}: {item.hcl_file_path} -> {correct_file_path}")
                        item.hcl_file_path = correct_file_path

                    # Also recompute atlantis_project_name for standalone infra
                    if item.infra_type.lower() not in ('ecs', 'ecs_ec2'):
                        correct_atlantis_name = self._compute_infra_atlantis_project_name(
                            item.infra_type,
                            service_name,
                            item.environment,
                            tenant_code,
                            snapshot
                        )
                        if correct_atlantis_name != item.atlantis_project_name:
                            logger.info(f"Deploy: Correcting atlantis name for {service_name}: {item.atlantis_project_name} -> {correct_atlantis_name}")
                            item.atlantis_project_name = correct_atlantis_name

                    # Save corrected paths to database (item is already attached to session)
                    self.db.add(item)
                    await self.db.flush()

                    # STEP 1: Fetch existing HCL content from GitHub base branch
                    # This preserves manual edits (same as Save & Sync)
                    existing_content = None
                    try:
                        existing_file = await GitHubIntegration.get_file_content(
                            token=github_token,
                            base_url=settings.github_base_url,
                            owner=repo_owner,
                            repo=repo_name,
                            file_path=item.hcl_file_path,
                            branch=base_branch
                        )
                        if existing_file and existing_file.get('exists'):
                            existing_content = existing_file.get('content')
                            logger.info(f"Found existing HCL for {service_name} at {item.hcl_file_path}")
                    except Exception as fetch_err:
                        logger.warning(f"Could not fetch existing HCL for {service_name}: {fetch_err}")

                    # STEP 2: Generate HCL from snapshot with existing content as base
                    hcl_content, field_mapping = await self._generate_hcl_from_snapshot(item, existing_content)

                    # STEP 3: Check if content changed (skip if no changes)
                    # Same logic as Save & Sync's should_skip_commit
                    if existing_content and should_skip_commit(existing_content, hcl_content):
                        logger.info(f"No HCL changes for {service_name} - skipping")
                        skipped_items.append(item.id)

                        # Even if HCL hasn't changed, check if Dockerfile needs modification
                        # (e.g., advanced_options changed DD_TRACE_ENABLED but cpu/ram stayed same)
                        if item.infra_type.lower() in ('ecs', 'ecs_ec2') and field_mapping:
                            dockerfile_sync_tasks.append({
                                'item': item,
                                'snapshot': snapshot,
                                'field_mapping': field_mapping,
                                'service_name': service_name,
                                'product_name': product_name,
                                'geo_loc': geo_loc
                            })

                        item_results.append(DeployItemResult(
                            id=item.id,
                            service_config_code=item.transaction_code,
                            status="skipped",
                            file_path=item.hcl_file_path
                        ))
                        continue

                    # STEP 4: Add file to commit list
                    files_to_commit.append({
                        'path': item.hcl_file_path,
                        'content': hcl_content
                    })

                    # Add atlantis entry with correct naming based on infra type
                    # ECS uses -service suffix, standalone infra uses type-specific suffix
                    atlantis_content = self._add_atlantis_entry(
                        atlantis_content=atlantis_content,
                        file_path=item.hcl_file_path,
                        product_name=product_name,
                        env=item.environment,
                        service_name=service_name,
                        infra_type=item.infra_type,
                        tenant=tenant_code,
                        geo_loc=geo_loc
                    )

                    # For ECS services, collect env file info to create configs.json and secrets.json
                    # This matches what TerragruntSyncService.push_to_github does
                    if item.infra_type.lower() in ('ecs', 'ecs_ec2'):
                        region = TerragruntSyncService._get_aws_region_from_geo_loc(geo_loc)
                        env_files_to_create.append({
                            'product_name': product_name,
                            'service_name': service_name,
                            'environment': item.environment,
                            'region': region,
                            'tenant': tenant_code,
                            'service_type': service_type
                        })

                        # Track for Dockerfile sync
                        if field_mapping:
                            dockerfile_sync_tasks.append({
                                'item': item,
                                'snapshot': snapshot,
                                'field_mapping': field_mapping,
                                'service_name': service_name,
                                'product_name': product_name,
                                'geo_loc': geo_loc
                            })

                    item_results.append(DeployItemResult(
                        id=item.id,
                        service_config_code=item.transaction_code,
                        status="success",
                        file_path=item.hcl_file_path
                    ))

                except Exception as e:
                    logger.error(f"Failed to generate HCL for item {item.id}: {e}")
                    item_results.append(DeployItemResult(
                        id=item.id,
                        service_config_code=item.transaction_code,
                        status="error",
                        error=str(e),
                        file_path=item.hcl_file_path
                    ))

            # Check if all items were skipped (no changes) or failed
            if not files_to_commit:
                # If all items were skipped (no changes), return success with skipped status
                if skipped_items and len(skipped_items) == len(items):
                    logger.info(f"All {len(items)} items skipped - no changes detected")
                    return TransactionQueueDeployResponse(
                        status="no_changes",
                        items_deployed=0,
                        items_failed=0,
                        details=item_results,
                        error="No changes detected - all items already in sync"
                    )
                # Otherwise, return error (all items failed to generate)
                return TransactionQueueDeployResponse(
                    status="error",
                    items_deployed=0,
                    items_failed=len(items),
                    details=item_results,
                    error="No files generated successfully"
                )

            # Add updated atlantis.yaml to commit
            files_to_commit.append({
                'path': 'atlantis.yaml',
                'content': atlantis_content
            })

            # Create feature branch and commit
            timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
            feature_branch = f"deploy-queue/{tenant_code}/{timestamp}"

            # Create branch
            await GitHubIntegration.create_branch(
                token=github_token,
                base_url=settings.github_base_url,
                owner=repo_owner,
                repo=repo_name,
                branch_name=feature_branch,
                from_branch=base_branch
            )

            # Commit all files atomically
            commit_result = await GitHubIntegration.commit_multiple_files(
                token=github_token,
                base_url=settings.github_base_url,
                owner=repo_owner,
                repo=repo_name,
                branch=feature_branch,
                files=files_to_commit,
                message=f"Deploy queue: {len(files_to_commit)} files for {tenant_code}"
            )

            # Create env files (configs.json and secrets.json) for ECS services
            # This is a non-blocking operation - failures are logged but don't stop the PR
            # Uses TerragruntSyncService._create_env_files_if_not_exist() for consistency
            version_index = settings.infra_version_index or "01"
            for env_file_info in env_files_to_create:
                try:
                    env_result = await self.terragrunt_service._create_env_files_if_not_exist(
                        github_token=github_token,
                        github_base_url=settings.github_base_url,
                        owner=repo_owner,
                        repo=repo_name,
                        branch=feature_branch,
                        product_name=env_file_info['product_name'],
                        service_name=env_file_info['service_name'],
                        environment=env_file_info['environment'],
                        region=env_file_info['region'],
                        tenant=env_file_info['tenant'],
                        version_index=version_index,
                        service_type=env_file_info['service_type']
                    )
                    logger.info(f"Env files for {env_file_info['service_name']}: {env_result.get('message', 'done')}")
                except Exception as env_err:
                    # Non-blocking - log and continue
                    logger.warning(f"Failed to create env files for {env_file_info['service_name']}: {env_err}")

            # DOCKERFILE SYNC: Check if Dockerfile modifications are needed (like Save & Sync)
            # This is a non-blocking operation - failures are logged but don't stop the PR
            for dockerfile_task in dockerfile_sync_tasks:
                try:
                    task_item = dockerfile_task['item']
                    task_snapshot = dockerfile_task['snapshot']
                    task_field_mapping = dockerfile_task['field_mapping']
                    task_service_name = dockerfile_task['service_name']

                    # Check if Dockerfile modification is needed
                    should_modify = self._should_modify_dockerfile_from_snapshot(
                        task_snapshot, task_field_mapping
                    )

                    if should_modify:
                        logger.info(f"Dockerfile modification needed for {task_service_name}")
                        dockerfile_result = await self._sync_dockerfile_from_snapshot(
                            item=task_item,
                            snapshot=task_snapshot,
                            field_mapping=task_field_mapping,
                            github_token=github_token,
                            tenant_code=tenant_code
                        )
                        if dockerfile_result:
                            logger.info(f"Dockerfile sync for {task_service_name}: {dockerfile_result.get('status')}")
                    else:
                        logger.debug(f"Dockerfile modification not needed for {task_service_name}")

                except Exception as dockerfile_err:
                    # Non-blocking - log and continue
                    logger.warning(f"Dockerfile sync failed for {dockerfile_task.get('service_name', 'unknown')}: {dockerfile_err}")

            # Create PR with descriptive title like Save & Sync
            # Generate PR title using shared helper (deterministic — the guaranteed fallback)
            envs = set(item.environment.lower() for item in items if item.environment)
            infra_types = set(item.infra_type for item in items if item.infra_type)
            pr_title = generate_pr_title(len(items), envs, infra_types)
            pr_body = self._generate_pr_body(items, tenant_code, user_email or user_code)

            # AI-generated title/body from the ACTUAL (redacted) changes. Never blocks the PR.
            pr_title, pr_body = await self._maybe_ai_pr_content(
                github_token=github_token,
                base_url=settings.github_base_url,
                owner=repo_owner,
                repo=repo_name,
                base_branch=base_branch,
                feature_branch=feature_branch,
                items=items,
                envs=envs,
                tenant_code=tenant_code,
                fallback_files=files_to_commit,
                default_title=pr_title,
                default_body=pr_body,
            )

            pr_result = await GitHubIntegration.create_pull_request(
                token=github_token,
                base_url=settings.github_base_url,
                owner=repo_owner,
                repo=repo_name,
                head=feature_branch,
                base=base_branch,
                title=pr_title,
                body=pr_body
            )

            pr_number = pr_result.get('number')
            pr_url = pr_result.get('html_url')

            # Create gitops workflow record
            # Use first item's transaction_code and table_name for the workflow record
            first_item = items[0] if items else None
            workflow = await self._create_workflow_record(
                tenant_code=tenant_code,
                user_code=user_code,
                pr_number=pr_number,
                pr_url=pr_url,
                git_branch=feature_branch,
                commit_sha=commit_result.get('commit_sha'),
                git_repository=f"{repo_owner}/{repo_name}",
                transaction_code=first_item.transaction_code if first_item else None,
                table_name=first_item.table_name if first_item else None
            )

            # Update queue items with PR info
            # Include both successful and skipped items (skipped = no changes, but still part of the PR context)
            successful_ids = [r.id for r in item_results if r.status == "success"]
            skipped_ids_for_update = [r.id for r in item_results if r.status == "skipped"]

            # Update successful items as PR_RAISED
            if successful_ids:
                await self.queue_repo.bulk_update_status(
                    item_ids=successful_ids,
                    status=TransactionQueueStatusEnum.PR_RAISED,
                    pr_number=pr_number,
                    pr_url=pr_url,
                    git_branch=feature_branch,
                    commit_sha=commit_result.get('commit_sha'),
                    gitops_workflow_id=workflow.id if workflow else None
                )

            # Update skipped items as PR_RAISED as well (they're in sync with what the PR will produce)
            if skipped_ids_for_update:
                await self.queue_repo.bulk_update_status(
                    item_ids=skipped_ids_for_update,
                    status=TransactionQueueStatusEnum.PR_RAISED,
                    pr_number=pr_number,
                    pr_url=pr_url,
                    git_branch=feature_branch,
                    commit_sha=commit_result.get('commit_sha'),
                    gitops_workflow_id=workflow.id if workflow else None
                )

            await self.db.commit()

            items_deployed = len([r for r in item_results if r.status == "success"])
            items_skipped = len([r for r in item_results if r.status == "skipped"])
            items_failed = len([r for r in item_results if r.status == "error"])

            logger.info(f"Deploy complete: PR #{pr_number}, {items_deployed} deployed, {items_skipped} skipped (no changes), {items_failed} failed")

            return TransactionQueueDeployResponse(
                status="success" if items_failed == 0 else "partial",
                pr_number=pr_number,
                pr_url=pr_url,
                git_branch=feature_branch,
                commit_sha=commit_result.get('commit_sha'),
                items_deployed=items_deployed,
                items_failed=items_failed,
                details=item_results
            )

        except Exception as e:
            logger.error(f"Deploy failed: {e}")
            return TransactionQueueDeployResponse(
                status="error",
                items_deployed=0,
                items_failed=len(items),
                details=item_results if 'item_results' in locals() else [],
                error=str(e)
            )

    async def deploy_temporal(
        self,
        user_code: str,
        tenant_code: str,
        environment: Optional[str],
        item_ids: Optional[List[int]],
    ) -> "TransactionQueueDeployResponse":
        """
        Temporal deploy path: resolve queue items → derive project_dirs →
        start DeploymentWorkflow → create pipeline_mst + pipeline_run_track rows.
        """
        import uuid
        from sqlalchemy import select, and_
        from app.temporal.client import get_temporal_client
        from app.temporal.workflows.deployment_workflow import DeploymentWorkflow
        from app.db.models.transaction_queue_model import TransactionQueueStatusEnum
        from app.handlers.file_location_handler import FileLocationHandler
        from app.schemas.pr_workflow_context import PRWorkflowContext
        from app.db.models.pipeline_vendor_mst_model import PipelineVendorMstModel
        from app.db.models.pipeline_mst_model import PipelineMstModel
        from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
        from app.core.enum import PipelineAgentEnum, PipelineRunStatusEnum
        from app.utils.pipeline_helpers import generate_run_code

        # ── Resolve queue items ──────────────────────────────────────────────
        # Anything refused is recorded rather than dropped in silence — a mixed
        # batch keeps `items` non-empty, so the guard below cannot be relied on to
        # surface it. Gateway callers should come through
        # /kong-route-configs/gateway/ensure-deploy-items, which reconciles rows
        # against terragrunt first so they arrive APPROVED.
        skipped_items: list[dict] = []
        if item_ids:
            items = []
            for item_id in item_ids:
                item = await self.queue_repo.get_by_id(item_id)
                # Foreign-tenant rows answer not_found so ids can't be probed.
                if item is None or item.tenant_code != tenant_code:
                    skipped_items.append({"item_id": item_id, "reason": "not_found"})
                elif item.status != TransactionQueueStatusEnum.APPROVED:
                    skipped_items.append({
                        "item_id": item_id,
                        "reason": "not_approved",
                        "status": getattr(item.status, "value", str(item.status)),
                    })
                else:
                    items.append(item)
        else:
            items = await self.queue_repo.get_pending_items_for_user(
                user_code, tenant_code, environment
            )

        if skipped_items:
            logger.warning(
                "deploy_temporal: %d of %d requested item(s) skipped: %s",
                len(skipped_items), len(item_ids or []), skipped_items,
            )

        if not items:
            from fastapi import HTTPException
            from starlette import status as http_status
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No pending items to deploy"
                    + (f" — {len(skipped_items)} item(s) were not deployable: "
                       f"{[s.get('status') or s['reason'] for s in skipped_items]}"
                       if skipped_items else "")
                ),
            )

        queue_ids = [item.id for item in items]

        # ── Derive project_dirs for Atlantis lock keys ───────────────────────
        workflow_context = PRWorkflowContext()
        project_dirs: list[str] = []
        item_envs: set = set()  # environments seen — prod routes to the promotion flow

        for queue_item in items:
            environment_val = None
            geo_loc_mst_code = None
            infra_vendor_accounts_mst_code = None

            if hasattr(queue_item, "source_entity") and queue_item.source_entity:
                src = queue_item.source_entity
                if hasattr(src, "environments_enum"):
                    environment_val = (
                        src.environments_enum.value
                        if hasattr(src.environments_enum, "value")
                        else str(src.environments_enum)
                    )
                if hasattr(src, "geo_loc_mst_code"):
                    geo_loc_mst_code = src.geo_loc_mst_code
                if hasattr(src, "infra_vendor_accounts_mst_code"):
                    infra_vendor_accounts_mst_code = src.infra_vendor_accounts_mst_code

            # Routing must not miss: environment_val comes from source_entity,
            # which only get_pending_items_for_user attaches — the item_ids
            # path (get_by_id) has none. Resolve through the repo's fallback
            # chain (source row → snapshot → source-table query). Detection
            # only: queue_dict below keeps the original environment_val.
            env_for_detection = (
                (environment_val or "").lower()
                or await self.queue_repo.resolve_item_environment(queue_item)
                or ""
            )
            item_envs.add(env_for_detection)
            queue_dict = {
                "id": queue_item.id,
                "code": queue_item.code,
                "user_code": queue_item.user_code,
                "transaction_code": queue_item.transaction_code,
                "case_ref_code": queue_item.case_ref_code,
                "table_name": queue_item.table_name,
                "config_snapshot": queue_item.config_snapshot,
                "tenant_code": queue_item.tenant_code,
                "status": (
                    queue_item.status.value
                    if hasattr(queue_item.status, "value")
                    else queue_item.status
                ),
                "environment": environment_val or "",
                "geo_loc_mst_code": geo_loc_mst_code or "",
                "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code or "",
            }

            try:
                file_resp = await FileLocationHandler.locate(
                    tenant_code, queue_dict, workflow_context
                )
                for file_item in (file_resp.files or []):
                    hcl_path = None
                    if file_item.file_path and file_item.file_path.endswith("terragrunt.hcl"):
                        hcl_path = file_item.file_path
                    elif isinstance(file_item.config, dict):
                        hcl_path = file_item.config.get("hcl_file_path")
                    if hcl_path:
                        project_dir = hcl_path.replace("/terragrunt.hcl", "")
                        if project_dir not in project_dirs:
                            project_dirs.append(project_dir)
            except Exception as e:
                logger.warning(
                    f"FileLocationHandler failed for queue item {queue_item.id}: {e} — "
                    "falling back to item-id lock key"
                )
                project_dirs.append(f"item-{queue_item.id}")

        if not project_dirs:
            project_dirs = [f"item-{item_id}" for item_id in queue_ids]

        # ── Start Temporal workflow ──────────────────────────────────────────
        # Prod items route to the promotion flow (stage→main); everything
        # else keeps today's DeploymentWorkflow untouched.
        workflow_id = f"deploy-{tenant_code}-{uuid.uuid4().hex[:12]}"
        temporal_client = await get_temporal_client()
        if any("prod" in e for e in item_envs if e):
            # The workflow adds the promotion key to its own lock request —
            # standalone mode needs nothing extra here.
            from app.temporal.workflows.production_deployment_workflow import (
                ProductionDeploymentWorkflow,
            )
            from app.utils.tenant_config import get_tenant_config

            tenant_cfg = await get_tenant_config(tenant_code, self.db)
            repo_full_name = tenant_cfg.github_infra_repository
            if not repo_full_name:
                from fastapi import HTTPException
                from starlette import status as http_status
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail=f"No infra repository configured for tenant '{tenant_code}'",
                )
            await temporal_client.start_workflow(
                ProductionDeploymentWorkflow.run,
                args=[{
                    "tenant_code": tenant_code,
                    "user_code": user_code,
                    "queue_ids": queue_ids,
                    "project_dirs": project_dirs,
                    "shared_dirs": [],
                    "repo_full_name": repo_full_name,
                    "source_branch": settings.temporal_prod_source_branch,
                    "target_branch": settings.temporal_prod_target_branch,
                    # standalone: no parent, own run-track key, service group
                    "parent_holder_id": None,
                    "track_id": None,
                    "pr_group": "service",
                }],
                id=workflow_id,
                task_queue=settings.temporal_task_queue,
            )
            logger.info(
                "ProductionDeploymentWorkflow started: workflow_id=%s tenant=%s queues=%s",
                workflow_id, tenant_code, queue_ids,
            )
        else:
            await temporal_client.start_workflow(
                DeploymentWorkflow.run,
                args=[tenant_code, project_dirs, queue_ids, user_code],
                id=workflow_id,
                task_queue=settings.temporal_task_queue,
            )
            logger.info(
                "DeploymentWorkflow started: workflow_id=%s tenant=%s queues=%s",
                workflow_id, tenant_code, queue_ids,
            )

        # Close the settings-diff refresh window: the workflow runs async and
        # only flips status later, so mark items STARTING_DEPLOYMENT now (this
        # commits before the response returns). The diff gate matches only
        # DRAFT/APPROVED, so the diff stops showing on refresh. The workflow
        # re-sets this status and its script-PR step accepts it, so nothing in
        # the deploy flow breaks.
        for qid in queue_ids:
            await self.queue_repo.update_status(
                qid, TransactionQueueStatusEnum.STARTING_DEPLOYMENT.value
            )

        # ── Create pipeline_mst + pipeline_run_track per queue item ─────────
        vendor_code = f"pv-temporal-{tenant_code}"
        vendor_result = await self.db.execute(
            select(PipelineVendorMstModel).where(PipelineVendorMstModel.code == vendor_code)
        )
        vendor = vendor_result.scalar_one_or_none()
        if not vendor:
            vendor = PipelineVendorMstModel(
                code=vendor_code,
                name=f"Temporal - {tenant_code}",
                tenants_mst_code=tenant_code,
                environment=EnvironmentEnum.dev,
                pipeline_agent_enum=PipelineAgentEnum.temporal,
                auth_config={},
                runner_info_config={},
            )
            self.db.add(vendor)
            await self.db.flush()

        run_track_repo = PipelineRunTrackRepository(self.db)
        for item in items:
            transaction_code = item.transaction_code
            table_name_val = item.table_name.value if hasattr(item.table_name, "value") else item.table_name

            pipeline_result = await self.db.execute(
                select(PipelineMstModel).where(
                    and_(
                        PipelineMstModel.transaction_code == transaction_code,
                        PipelineMstModel.table_name == item.table_name,
                        PipelineMstModel.tenant_code == tenant_code,
                        PipelineMstModel.pipeline_vendor_mst_code == vendor_code,
                    )
                )
            )
            pipeline = pipeline_result.scalar_one_or_none()
            if not pipeline:
                pipeline_code = f"pipeline-temporal-{transaction_code[:10]}-{uuid.uuid4().hex[:8]}"
                pipeline = PipelineMstModel(
                    code=pipeline_code,
                    name=f"Temporal: {transaction_code} ({table_name_val})",
                    pipeline_vendor_mst_code=vendor_code,
                    transaction_code=transaction_code,
                    table_name=item.table_name,
                    tenant_code=tenant_code,
                    repo_url="temporal://",
                    repo_branch="main",
                )
                self.db.add(pipeline)
                await self.db.flush()

            run_code = generate_run_code(pipeline.code)
            await run_track_repo.create(
                pipeline_mst_code=pipeline.code,
                code=run_code,
                status=PipelineRunStatusEnum.RUNNING,
                transaction_queue_code=[item.code],
                vendor_deployment_id=workflow_id,
            )

        await self.db.commit()
        logger.info(
            "pipeline_run_track rows created: workflow_id=%s items=%d",
            workflow_id, len(items),
        )

        return TransactionQueueDeployResponse(
            status="queued",
            workflow_id=workflow_id,
            items_deployed=len(queue_ids),
            items_failed=0,
            details=[],
        )

    async def _create_workflow_record(
        self,
        tenant_code: str,
        user_code: str,
        pr_number: int,
        pr_url: str,
        git_branch: str,
        commit_sha: str,
        git_repository: str = "",
        transaction_code: Optional[str] = None,
        table_name: Optional[WorkflowSourceTableEnum] = None
    ) -> Optional[GitopsWorkflowDetailModel]:
        """Create a gitops workflow record for tracking."""
        try:
            import uuid
            workflow_code = f"queue-{uuid.uuid4().hex[:8]}"

            workflow = GitopsWorkflowDetailModel(
                code=workflow_code,
                name=f"Deploy Queue PR #{pr_number}",
                git_repository=git_repository,
                git_branch=git_branch,
                git_commit_sha=commit_sha,
                pr_number=pr_number,
                pr_url=pr_url,
                pr_status=PRStatusEnum.PR_OPEN,
                tenant_mst_code=tenant_code,
                user_mst_code=user_code,
                transaction_code=transaction_code,
                table_name=table_name or WorkflowSourceTableEnum.SERVICE_CONFIG
            )

            self.db.add(workflow)
            await self.db.flush()
            return workflow

        except Exception as e:
            logger.error(f"Failed to create workflow record: {e}")
            return None

    async def _maybe_ai_pr_content(
        self,
        *,
        github_token: str,
        base_url: str,
        owner: str,
        repo: str,
        base_branch: str,
        feature_branch: str,
        items: list,
        envs: set,
        tenant_code: str,
        fallback_files: list,
        default_title: str,
        default_body: str,
    ) -> tuple:
        """
        Return (title, body): AI-generated from the ACTUAL redacted diff when enabled,
        otherwise the deterministic defaults. Never raises — a failure, timeout, disabled
        flag, or empty result keeps the deterministic values so PR creation never blocks.
        Shared by the deploy and refresh/replacement PR paths.
        """
        if not settings.pr_ai_naming_enabled:
            return default_title, default_body
        try:
            from app.utils.pr_diff_helpers import collect_pr_changes
            from app.services.openai_service import OpenAIService
            changes = await collect_pr_changes(
                token=github_token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                base=base_branch,
                head=feature_branch,
                fallback_entries=fallback_files,
            )
            if changes:
                ai = await OpenAIService().generate_pr_content_from_changes(
                    changes=changes,
                    context={
                        "item_count": len(items),
                        "environments": ", ".join(sorted(envs)) if envs else "unknown",
                        "tenant": tenant_code,
                    },
                )
                title = ai.get("title") or default_title
                # Hybrid body: AI summary on top, then keep the deterministic details
                # (per-resource cards, Atlantis plan/apply commands, "Requested by").
                body = (ai["body"] + "\n\n---\n\n" + default_body) if ai.get("body") else default_body
                logger.info(f"AI-generated PR title: {title}")
                return title, body
        except Exception as ai_err:
            logger.warning(f"AI PR naming failed, using deterministic title/body: {ai_err}")
        return default_title, default_body

    def _generate_pr_body(
        self,
        items: List[TransactionQueueModel],
        tenant_code: str = "",
        user_email: str = ""
    ) -> str:
        """
        Generate PR description body using shared helper.
        """
        # Build items_data for the shared helper
        items_data = []
        environments = set()
        infra_types = set()

        for item in items:
            snapshot = item.config_snapshot or {}

            # Extract infra type
            infra_type = item.infra_type.upper()
            infra_types.add(infra_type)

            # Extract service/resource name
            service_name = (
                snapshot.get('identifier') or
                snapshot.get('name') or
                snapshot.get('service_name') or
                item.transaction_code or
                'unknown'
            )

            # Extract environment
            environment = item.environment or ''
            if environment:
                environments.add(environment.lower())

            # Extract region using shared helper
            geo_loc = snapshot.get('geo_loc_mst_code', '')
            region = get_region_display(geo_loc)

            # Compute atlantis name using shared helper
            product_name = snapshot.get('product_name', '')
            if item.infra_type.lower() in ('ecs', 'ecs_ec2'):
                atlantis_name = item.atlantis_project_name or ''
            else:
                atlantis_name = compute_atlantis_project_name(
                    infra_type=infra_type,
                    service_name=service_name,
                    environment=environment,
                    tenant_code=tenant_code,
                    product_name=product_name,
                    geo_loc=geo_loc
                )

            items_data.append({
                'infra_type': infra_type,
                'service_name': service_name,
                'environment': environment,
                'region': region,
                'atlantis_name': atlantis_name
            })

        # Note: transaction queue PRs don't have atlantis.yaml tracking
        # Pass None for atlantis_entries to not show atlantis commands
        return generate_pr_body(items_data, environments, infra_types, user_email, atlantis_entries=None)

    async def check_pr_status(self, pr_number: int) -> TransactionQueuePRStatusResponse:
        """
        Check if a PR is stale (behind base branch).

        Args:
            pr_number: PR number to check

        Returns:
            PR status response with staleness info
        """
        items = await self.queue_repo.get_items_for_pr(pr_number)
        if not items:
            raise ValueError(f"No queue items found for PR #{pr_number}")

        item = items[0]
        github_token = await self._get_github_token(settings.github_repo_owner)

        # Compare branches
        comparison = await GitHubIntegration.compare_branches(
            token=github_token,
            base_url=settings.github_base_url,
            owner=settings.github_repo_owner,
            repo=settings.github_terragrunt_repo,
            base=settings.github_base_branch,
            head=item.git_branch
        )

        return TransactionQueuePRStatusResponse(
            pr_number=pr_number,
            pr_url=item.pr_url or "",
            status=comparison.get('status', 'unknown'),
            is_stale=comparison.get('is_stale', False),
            behind_by=comparison.get('behind_by', 0),
            ahead_by=comparison.get('ahead_by', 0),
            base_branch=settings.github_base_branch,
            head_branch=item.git_branch or "",
            items_count=len(items)
        )

    async def refresh_pr(self, pr_number: int) -> TransactionQueuePRRefreshResponse:
        """
        Refresh a PR by regenerating from snapshots.

        For open PRs: Resets the feature branch to latest base and regenerates all files.
        For closed PRs: Recreates the branch if needed and creates a new PR.

        Args:
            pr_number: PR number to refresh

        Returns:
            Refresh response with new PR info if created
        """
        items = await self.queue_repo.get_items_for_pr(pr_number)
        if not items:
            raise ValueError(f"No queue items found for PR #{pr_number}")

        item = items[0]
        if not item.git_branch:
            raise ValueError("PR has no associated branch")

        github_token = await self._get_github_token(settings.github_repo_owner)
        tenant_code = item.tenant_code or ''
        new_pr_created = False
        new_pr_number = pr_number
        new_pr_url = item.pr_url

        try:
            # Check current PR status on GitHub
            pr_info = await GitHubIntegration.get_pull_request(
                token=github_token,
                base_url=settings.github_base_url,
                owner=settings.github_repo_owner,
                repo=settings.github_terragrunt_repo,
                pr_number=pr_number
            )

            pr_is_closed = pr_info and pr_info.get("state") == "closed"

            # Get latest base branch SHA
            base_sha = await GitHubIntegration.get_branch_sha(
                token=github_token,
                base_url=settings.github_base_url,
                owner=settings.github_repo_owner,
                repo=settings.github_terragrunt_repo,
                branch=settings.github_base_branch
            )

            if not base_sha:
                raise ValueError("Could not get base branch SHA")

            # Check if branch exists
            branch_exists = False
            try:
                existing_sha = await GitHubIntegration.get_branch_sha(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=settings.github_repo_owner,
                    repo=settings.github_terragrunt_repo,
                    branch=item.git_branch
                )
                branch_exists = existing_sha is not None
            except Exception:
                branch_exists = False

            if branch_exists:
                # Reset existing branch to base
                await GitHubIntegration.reset_branch_to_ref(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=settings.github_repo_owner,
                    repo=settings.github_terragrunt_repo,
                    branch=item.git_branch,
                    target_sha=base_sha,
                    force=True
                )
            else:
                # Create branch from base branch
                await GitHubIntegration.create_branch(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=settings.github_repo_owner,
                    repo=settings.github_terragrunt_repo,
                    branch_name=item.git_branch,
                    from_branch=settings.github_base_branch
                )

            # Fetch current atlantis.yaml from base branch
            atlantis_resp = await GitHubIntegration.get_file_content(
                token=github_token,
                base_url=settings.github_base_url,
                owner=settings.github_repo_owner,
                repo=settings.github_terragrunt_repo,
                file_path='atlantis.yaml',
                branch=settings.github_base_branch
            )
            atlantis_content = atlantis_resp.get('content', '') if atlantis_resp and atlantis_resp.get('exists') else 'version: 3\nprojects:\n'

            # Regenerate all files from snapshots
            files_to_commit = []
            env_files_to_create = []  # Track env files for ECS services

            for queue_item in items:
                # Extract snapshot data
                snapshot = queue_item.config_snapshot or {}
                product_name = snapshot.get('product_name', '')
                # Get resource name - 'identifier' is the actual resource name entered by user
                # Fall back to other fields for backwards compatibility
                service_name = (
                    snapshot.get('identifier') or   # Primary: actual resource name from infra studio
                    snapshot.get('name') or
                    snapshot.get('service_name') or
                    queue_item.transaction_code or
                    ''
                )
                geo_loc = snapshot.get('geo_loc_mst_code', '')
                tenant_code = queue_item.tenant_code or ''
                service_type = snapshot.get('service_type', '')

                # For infra items, check if product_name looks like a UUID (old bug) and resolve it
                # A UUID pattern looks like: 178d48fc-8b2c-4e79-aca9-e18f089a05f9
                uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
                if uuid_pattern.match(product_name):
                    # product_name is a UUID, try to resolve from applications_mst_code in snapshot
                    app_code = snapshot.get('applications_mst_code') or snapshot.get('application_code') or product_name
                    app_record = await self.app_repo.get_by_code(app_code)
                    if app_record:
                        logger.info(f"Refresh: Resolved UUID {product_name} to application name: {app_record.name}")
                        product_name = app_record.name
                        # Update the snapshot so future operations use the correct name
                        snapshot['product_name'] = product_name
                        queue_item.config_snapshot = snapshot

                # RECOMPUTE file path from snapshot data (fixes incorrect paths from old queue items)
                # This ensures refresh uses correct paths even if original add_to_queue had bugs
                if queue_item.infra_type.lower() in ('ecs', 'ecs_ec2'):
                    # ECS services: use TerragruntSyncService's path building
                    region = TerragruntSyncService._get_aws_region_from_geo_loc(geo_loc)
                    version_index = settings.infra_version_index or "01"
                    correct_file_path = self.terragrunt_service._build_github_file_path(
                        product_name=product_name,
                        service_name=service_name,
                        environment=queue_item.environment,
                        region=region,
                        tenant=tenant_code,
                        version_index=version_index,
                        service_type=service_type
                    )
                else:
                    # Standalone infra: recompute using infra path method
                    correct_file_path = self._compute_infra_hcl_file_path(
                        queue_item.infra_type,
                        service_name,
                        queue_item.environment,
                        tenant_code,
                        snapshot
                    )

                # Log if path changed (helps debug)
                if correct_file_path != queue_item.hcl_file_path:
                    logger.info(f"Refresh: Correcting file path for {service_name}: {queue_item.hcl_file_path} -> {correct_file_path}")

                # Also recompute atlantis_project_name for standalone infra
                if queue_item.infra_type.lower() not in ('ecs', 'ecs_ec2'):
                    correct_atlantis_name = self._compute_infra_atlantis_project_name(
                        queue_item.infra_type,
                        service_name,
                        queue_item.environment,
                        tenant_code,
                        snapshot
                    )
                    if correct_atlantis_name != queue_item.atlantis_project_name:
                        logger.info(f"Refresh: Correcting atlantis name for {service_name}: {queue_item.atlantis_project_name} -> {correct_atlantis_name}")
                        queue_item.atlantis_project_name = correct_atlantis_name

                # Update the queue item's stored path
                queue_item.hcl_file_path = correct_file_path

                # Save corrected paths to database
                self.db.add(queue_item)
                await self.db.flush()

                # _generate_hcl_from_snapshot returns tuple (hcl_content, field_mapping)
                hcl_content, _field_mapping = await self._generate_hcl_from_snapshot(queue_item)
                files_to_commit.append({
                    'path': correct_file_path,
                    'content': hcl_content
                })

                # Add atlantis entry with correct naming based on infra type
                atlantis_content = self._add_atlantis_entry(
                    atlantis_content=atlantis_content,
                    file_path=correct_file_path,
                    product_name=product_name,
                    env=queue_item.environment,
                    service_name=service_name,
                    infra_type=queue_item.infra_type,
                    tenant=tenant_code,
                    geo_loc=geo_loc
                )

                # Track ECS services for env file creation
                if queue_item.infra_type.lower() in ('ecs', 'ecs_ec2'):
                    region = TerragruntSyncService._get_aws_region_from_geo_loc(geo_loc)
                    env_files_to_create.append({
                        'product_name': product_name,
                        'service_name': service_name,
                        'environment': queue_item.environment,
                        'region': region,
                        'tenant': tenant_code,
                        'service_type': service_type
                    })

                # Update the queue item's stored path so future operations use the correct one
                queue_item.hcl_file_path = correct_file_path

            # Add updated atlantis.yaml to commit
            files_to_commit.append({
                'path': 'atlantis.yaml',
                'content': atlantis_content
            })

            # Commit refreshed files
            commit_result = await GitHubIntegration.commit_multiple_files(
                token=github_token,
                base_url=settings.github_base_url,
                owner=settings.github_repo_owner,
                repo=settings.github_terragrunt_repo,
                branch=item.git_branch,
                files=files_to_commit,
                message=f"Refresh PR #{pr_number}: regenerate from snapshots"
            )

            # Create env files (configs.json and secrets.json) for ECS services
            # Non-blocking - uses same logic as deploy_all
            version_index = settings.infra_version_index or "01"
            for env_file_info in env_files_to_create:
                try:
                    await self.terragrunt_service._create_env_files_if_not_exist(
                        github_token=github_token,
                        github_base_url=settings.github_base_url,
                        owner=settings.github_repo_owner,
                        repo=settings.github_terragrunt_repo,
                        branch=item.git_branch,
                        product_name=env_file_info['product_name'],
                        service_name=env_file_info['service_name'],
                        environment=env_file_info['environment'],
                        region=env_file_info['region'],
                        tenant=env_file_info['tenant'],
                        version_index=version_index,
                        service_type=env_file_info['service_type']
                    )
                except Exception as env_err:
                    logger.warning(f"Failed to create env files for {env_file_info['service_name']}: {env_err}")

            # Check if PR was auto-closed due to empty diff during branch reset
            # If so, reopen it now that we have content
            if not pr_is_closed:
                # Check current PR state after our commits
                pr_info_after = await GitHubIntegration.get_pull_request(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=settings.github_repo_owner,
                    repo=settings.github_terragrunt_repo,
                    pr_number=pr_number
                )
                if pr_info_after and pr_info_after.get("state") == "closed" and not pr_info_after.get("merged"):
                    # PR was auto-closed due to empty diff, reopen it
                    logger.info(f"PR #{pr_number} was auto-closed during refresh, reopening...")
                    try:
                        await GitHubIntegration.update_pull_request(
                            token=github_token,
                            base_url=settings.github_base_url,
                            owner=settings.github_repo_owner,
                            repo=settings.github_terragrunt_repo,
                            pr_number=pr_number,
                            state="open"
                        )
                        logger.info(f"Successfully reopened PR #{pr_number}")
                    except Exception as reopen_err:
                        logger.warning(f"Could not reopen PR #{pr_number}: {reopen_err}")
                        # Fall through to create new PR logic
                        pr_is_closed = True

            # If the original PR was closed (or couldn't be reopened), create a new PR
            if pr_is_closed:
                # Generate PR title using shared helper (with Regenerated prefix)
                envs = set(i.environment.lower() for i in items if i.environment)
                base_title = generate_pr_title(len(items), envs)
                pr_title = base_title.replace("[DevLift] Deploy", "[DevLift] Regenerated -")

                # Get user email from the loaded user relationship
                user_email = item.user.email_id if item.user else (item.user_code or '')
                pr_body = self._generate_pr_body(items, tenant_code, user_email)

                # AI-generated title/body from the ACTUAL redacted diff (never blocks).
                pr_title, pr_body = await self._maybe_ai_pr_content(
                    github_token=github_token,
                    base_url=settings.github_base_url,
                    owner=settings.github_repo_owner,
                    repo=settings.github_terragrunt_repo,
                    base_branch=settings.github_base_branch,
                    feature_branch=item.git_branch,
                    items=items,
                    envs=envs,
                    tenant_code=tenant_code,
                    fallback_files=files_to_commit,
                    default_title=pr_title,
                    default_body=pr_body,
                )
                pr_body += f"\n\n*Regenerated from closed PR #{pr_number}*"

                pr_result = await GitHubIntegration.create_pull_request(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=settings.github_repo_owner,
                    repo=settings.github_terragrunt_repo,
                    title=pr_title,
                    body=pr_body,
                    head=item.git_branch,
                    base=settings.github_base_branch
                )

                new_pr_number = pr_result.get('number', pr_number)
                new_pr_url = pr_result.get('html_url', item.pr_url)
                new_pr_created = True

                logger.info(f"Created new PR #{new_pr_number} to replace closed PR #{pr_number}")

            # Update all items with new PR info and status
            from app.repository.transaction_queue_repository import TransactionQueueRepository
            for queue_item in items:
                TransactionQueueRepository.append_status_event(
                    queue_item, TransactionQueueStatusEnum.PR_RAISED
                )
                queue_item.status = TransactionQueueStatusEnum.PR_RAISED
                queue_item.commit_sha = commit_result.get('commit_sha')
                if new_pr_created:
                    queue_item.pr_number = new_pr_number
                    queue_item.pr_url = new_pr_url

            await self.db.commit()

            if new_pr_created:
                logger.info(f"Created new PR #{new_pr_number} with {len(items)} items (replacing closed PR #{pr_number})")
            else:
                logger.info(f"Refreshed PR #{pr_number} with {len(items)} items")

            return TransactionQueuePRRefreshResponse(
                status="success",
                pr_number=new_pr_number,
                pr_url=new_pr_url,
                commit_sha=commit_result.get('commit_sha'),
                items_regenerated=len(items)
            )

        except Exception as e:
            logger.error(f"Failed to refresh PR #{pr_number}: {e}")
            return TransactionQueuePRRefreshResponse(
                status="error",
                pr_number=pr_number,
                items_regenerated=0,
                error=str(e)
            )

    async def update_pr_merged(self, pr_number: int) -> int:
        """
        Mark all items for a PR as merged.

        Called when PR is merged (webhook or manual).

        Args:
            pr_number: PR number

        Returns:
            Number of items updated
        """
        count = await self.queue_repo.update_pr_status(
            pr_number.PR_MERGED
        )
        await self.db.commit()
        logger.info(f"Marked {count} items as merged for PR #{pr_number}")
        return count

    async def update_pr_closed(self, pr_number: int) -> int:
        """
        Mark all items for a PR as closed (without merge).

        Args:
            pr_number: PR number

        Returns:
            Number of items updated
        """
        count = await self.queue_repo.update_pr_status(
            pr_number.PR_CLOSED
        )
        await self.db.commit()
        logger.info(f"Marked {count} items as closed for PR #{pr_number}")
        return count

    def _resolve_repo_owner(
        self,
        git_repository: Optional[str],
        pr_url: Optional[str]
    ) -> tuple[str, str]:
        """
        Resolve GitHub owner/repo from stored repository or PR URL.
        """
        if git_repository and "/" in git_repository:
            owner, repo = git_repository.split("/", 1)
            if owner and repo:
                return owner, repo

        if pr_url:
            try:
                from urllib.parse import urlparse

                path_parts = [p for p in urlparse(pr_url).path.split("/") if p]
                if len(path_parts) >= 2:
                    return path_parts[0], path_parts[1]
            except Exception:
                pass

        return settings.github_repo_owner, settings.github_terragrunt_repo

    async def sync_pr_status_from_github(
        self,
        pr_number: int,
        git_repository: Optional[str] = None,
        pr_url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Sync PR status from GitHub and update database if status changed.

        Fetches the actual PR status from GitHub and updates the database
        if the PR was merged or closed externally (e.g., via GitHub UI).

        Args:
            pr_number: PR number to check
            git_repository: Optional repo in "owner/repo" format for correct lookup
            pr_url: Optional PR URL used as fallback to resolve repo

        Returns:
            Dict with status info:
            - status: Current status (pr_raised, pr_merged, pr_closed)
            - is_stale: Whether PR is behind base branch
            - github_state: Raw GitHub state
        """
        items = await self.queue_repo.get_items_for_pr(pr_number)
        if not items:
            return None

        item = items[0]
        owner, repo = self._resolve_repo_owner(git_repository, pr_url)
        github_token = await self._get_github_token(owner)

        try:
            # Get actual PR status from GitHub
            pr_info = await GitHubIntegration.get_pull_request(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                pr_number=pr_number
            )

            if not pr_info:
                return None

            github_state = pr_info.get("state")  # "open" or "closed"
            is_merged = pr_info.get("merged", False)

            # Determine our status enum based on GitHub state
            current_db_status = item.status
            if github_state == "closed":
                new_status = (
                    TransactionQueueStatusEnum.PR_APPROVED
                    if is_merged
                    else TransactionQueueStatusEnum.PR_REJECTED
                )
            elif github_state == "open":
                new_status = TransactionQueueStatusEnum.PR_RAISED
            else:
                new_status = current_db_status

            # Update database if status changed
            if new_status != current_db_status:
                await self.queue_repo.update_pr_status(pr_number, new_status)
                await self.db.commit()
                logger.info(f"Synced PR #{pr_number} status: {current_db_status} -> {new_status}")

            # Check if PR is stale (behind base branch) - only for open PRs
            is_stale = False
            if github_state == "open" and item.git_branch:
                try:
                    comparison = await GitHubIntegration.compare_branches(
                        token=github_token,
                        base_url=settings.github_base_url,
                        owner=owner,
                        repo=repo,
                        base=settings.github_base_branch,
                        head=item.git_branch
                    )
                    is_stale = comparison.get("is_stale", False)
                except Exception:
                    pass  # Ignore staleness check failures

            data =  {
                "status": new_status.value if hasattr(new_status, 'value') else str(new_status),
                "is_stale": is_stale,
                "github_state": github_state,
                "is_merged": is_merged
            }
            print(data)
            return data

        except Exception as e:
            logger.warning(f"Failed to sync PR #{pr_number} status from GitHub: {e}")
            return None

    async def search_draft_queue(
        self,
        transaction_code: str,
        table_name: WorkflowSourceTableEnum,
        tenant_code: str,
        user_code: str,
        case_ref_code: Optional[str] = None,
        include_failed: bool = False,
    ) -> Optional[TransactionQueueModel]:
        """
        Search for the latest DRAFT/APPROVED queue item by transaction code and table name.

        If multiple entries exist, returns the most recently created one.
        If no entry exists, returns None.

        Args:
            transaction_code: Code of the source entity
            table_name: Source table enum (SERVICE_CONFIG, INFRASTRUCTURE, etc.)
            tenant_code: Tenant code for isolation
            user_code: User code
            case_ref_code: Optional case reference code to narrow the search
            include_failed: When True, also matches FAILED items so a failed
                            deploy can be found and retried (redeploy path only).

        Returns:
            Latest matching queue item if found, None otherwise
        """
        return await self.queue_repo.search_draft_by_transaction(
            transaction_code=transaction_code,
            table_name=table_name,
            tenant_code=tenant_code,
            user_code=user_code,
            case_ref_code=case_ref_code,
            include_failed=include_failed,
        )

    async def ensure_settings_deploy_item(
        self,
        transaction_code: str,
        tenant_code: str,
        user_code: str,
    ) -> Tuple[Optional[int], bool]:
        """Return (item_id, no_changes) for a settings deploy by `user_code`,
        ready to deploy the CURRENT live service_config.

        no_changes=True means the live config already equals the last deployed
        state (e.g. another user deployed the same change) — the caller should
        skip the deploy instead of starting a workflow that would fail with
        "no diff". item_id is None then. item_id is also None (no_changes=False)
        when the service_config no longer exists.

        The settings diff is a shared per-service fact (any user sees drift on
        the shared service_configs.config), but a deploy needs a queue item the
        deployer owns. Rather than reuse a possibly stale/thin snapshot on an
        existing item, we ALWAYS snapshot the current live config and let
        add_item_to_queue reuse the deployer's own DRAFT/APPROVED item (updating
        its snapshot to live) or create a fresh one — so the deployed snapshot,
        and therefore the next diff baseline, always equals the live config.
        A FAILED item is left as history; a fresh draft is created for the retry.

        Returns (item_id, no_changes).
        """
        config = await self.service_config_repo.get_by_code_and_tenant(
            transaction_code, tenant_code
        )
        if not config:
            return None, False

        # ── An approved, SEALED change is already the thing to deploy ────────
        # Everything below re-snapshots the live config onto the caller's own
        # row. That is right for the pre-approval flow this was written for,
        # and wrong the moment a row carries approved_snapshot_hash: the seal is
        # taken at approve() over the exact config_snapshot the reviewer said
        # yes to, and rewriting the snapshot is precisely what verify_seal
        # exists to catch.
        #
        # The deploy click itself was therefore breaking its own seal —
        # ensure_settings_deploy_item ran first, rewrote the snapshot, and the
        # deploy that called it was then refused with "changed after it was
        # approved". Every approved settings deploy failed this way.
        #
        # Not scoped to the caller: under the approval flow the deployable row
        # belongs to its AUTHOR, and the deployer is often someone else. Any
        # sealed approved row for this service IS the approved change, so it is
        # returned untouched rather than competing with a fresh one.
        sealed = (await self.db.execute(
            select(TransactionQueueModel)
            .where(
                TransactionQueueModel.transaction_code == transaction_code,
                TransactionQueueModel.tenant_code == tenant_code,
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG,
                TransactionQueueModel.case_ref_code == "update_service",
                TransactionQueueModel.status == TransactionQueueStatusEnum.APPROVED,
                TransactionQueueModel.approved_snapshot_hash.isnot(None),
                TransactionQueueModel.is_deleted.isnot(True),
            )
            .order_by(TransactionQueueModel.id.desc())
            .limit(1)
        )).scalars().first()
        if sealed is not None:
            logger.info(
                "ensure_settings_deploy_item: %s already has sealed approved item %s "
                "— returning it untouched (re-snapshotting would break the seal)",
                transaction_code, sealed.code,
            )
            return sealed.id, False

        # Nothing to deploy if the live config already matches the last deployed
        # state — skip so we don't start a workflow that fails with "no diff".
        from app.services.service_config_service import ServiceConfigService
        if not await ServiceConfigService(self.db).has_pending_settings_diff(transaction_code, tenant_code):
            return None, True

        from app.repository.services_mst_repository import ServicesMstRepository

        svc_row = await ServicesMstRepository(self.db).get_by_code(config.services_mst_code)
        env_val = config.environment.value if hasattr(config.environment, "value") else config.environment
        service_type_val = getattr(svc_row.service_type, "value", svc_row.service_type) if (svc_row and svc_row.service_type is not None) else None

        # Same flat snapshot shape the Settings-tab Save / clone produce, so the
        # HCL generator and file locator get the fields they expect.
        snapshot = {
            **(config.config or {}),
            "services_mst_code": config.services_mst_code,
            "service_name": svc_row.name if svc_row else None,
            "service_type": service_type_val,
            "geo_loc_mst_code": config.geo_loc_mst_code,
            "environment": env_val,
            "infrastructuretype_ref_code": config.infrastructuretype_ref_code,
            "infrastructure_mst_code": config.infrastructure_mst_code,
            "applications_mst_code": svc_row.applications_mst_code if svc_row else None,
        }
        item = await self.add_item_to_queue(
            user_code=user_code,
            tenant_code=tenant_code,
            transaction_code=transaction_code,
            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
            config_snapshot=snapshot,
            case_ref_code="update_service",
            # This snapshots the CURRENT live config to make it deployable — it
            # is not somebody choosing to turn a container off. A service saved
            # before that refusal existed can legitimately be sitting on
            # create_secrets=false with secrets present, and refusing here would
            # block the deploy of whatever else they just changed. The render
            # -time override still builds the container for those rows.
            validate_container_flags=False,
        )
        if item.status != TransactionQueueStatusEnum.APPROVED:
            await self.queue_repo.bulk_approve_by_ids(
                queue_ids=[item.id], user_code=user_code, tenant_code=tenant_code
            )
            await self.db.commit()
        return item.id, False

    async def search_by_ticket(
        self,
        ticket_code: str,
        tenant_code: str,
        user_code: str
    ) -> Optional[TransactionQueueModel]:
        """
        Search for the latest APPROVED queue item by ticket code.

        If multiple approved entries exist for the ticket, returns the most recently created one.
        If no approved entry exists, returns None.

        Args:
            ticket_code: Ticket code to search for
            tenant_code: Tenant code for isolation
            user_code: User code

        Returns:
            Latest approved queue item if found, None otherwise
        """
        return await self.queue_repo.search_by_ticket_code(
            ticket_code=ticket_code,
            tenant_code=tenant_code,
            user_code=user_code
        )

    async def bulk_approve(
        self,
        queue_ids: List[int],
        user_code: str,
        tenant_code: str
    ) -> int:
        """
        Bulk approve queue items by their IDs.

        Updates the status of multiple queue items to APPROVED.
        Only updates items that belong to the specified user and tenant.

        Args:
            queue_ids: List of queue IDs to approve
            user_code: User code for ownership validation
            tenant_code: Tenant code for isolation

        Returns:
            Number of items successfully updated
        """
        count = await self.queue_repo.bulk_approve_by_ids(
            queue_ids=queue_ids,
            user_code=user_code,
            tenant_code=tenant_code
        )
        await self.db.commit()
        logger.info(f"Bulk approved {count} queue items for user {user_code}")
        return count

    async def resolve_conflict(
        self,
        pr_number: int,
        git_repository: str,
        user_code: str,
        tenant_code: str
    ) -> Dict[str, Any]:
        """
        Resolve conflicts for an existing PR by rebasing and regenerating files.

        Data lookup layer:
        1. Query gitops_workflow_detail by pr_number + git_repository → get workflow record
        2. Validate: workflow exists
        3. Fetch queue codes via DB join chain
        4. Pass queue codes + workflow metadata to ScriptPRWorkflowService.conflict_resolve()

        Args:
            pr_number: GitHub PR number
            git_repository: GitHub repository (e.g., "owner/repo")
            user_code: User code
            tenant_code: Tenant code

        Returns:
            Dict with conflict resolve result from ScriptPRWorkflowService

        Raises:
            ValueError: If workflow not found or no queue items found
        """
        from app.services.script_pr_workflow_service import ScriptPRWorkflowService

        logger.info(
            "resolve_conflict start pr_number=%s git_repository=%s user=%s tenant=%s",
            pr_number, git_repository, user_code, tenant_code
        )

        # Step 1: Get workflow record
        workflow = await self.workflow_repo.get_latest_by_pr_and_repository(
            pr_number=pr_number,
            git_repository=git_repository
        )
        if not workflow:
            raise ValueError(
                f"No workflow found for PR #{pr_number} in {git_repository}"
            )

        feature_branch = workflow.git_branch
        logger.info(
            "resolve_conflict workflow found code=%s feature_branch=%s",
            workflow.code, feature_branch
        )

        # Step 2: Fetch queue codes via DB join chain
        queue_codes = await self.queue_repo.get_queue_codes_for_pr(
            pr_number=pr_number,
            git_repository=git_repository
        )
        if not queue_codes:
            raise ValueError(
                f"No queue items found for PR #{pr_number} in {git_repository}"
            )

        logger.info(
            "resolve_conflict found queue_codes count=%s codes=%s",
            len(queue_codes), queue_codes
        )

        # Step 3: Pass to ScriptPRWorkflowService.conflict_resolve()
        workflow_service = ScriptPRWorkflowService(self.db)
        result = await workflow_service.conflict_resolve(
            queue_codes=queue_codes,
            pr_number=pr_number,
            git_repository=git_repository,
            feature_branch=feature_branch,
            user_code=user_code,
            tenant_code=tenant_code
        )

        logger.info(
            "resolve_conflict done pr_number=%s git_repository=%s",
            pr_number, git_repository
        )

        return result
