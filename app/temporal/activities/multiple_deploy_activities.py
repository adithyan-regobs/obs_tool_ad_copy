"""
Activities for the /multiple-deploy flow (DeploymentOrchestratorWorkflow).

  prepare_multiple_deploy   — resolve queue items + project_dirs, create
                              pipeline_mst / pipeline_run_track rows, and
                              pre-generate the infra child workflow id.
  deploy_variables_activity — thin adapter around VariableService.deploy_variables:
                              own DB session, user/tenant loaded from codes,
                              milestone callback writes pipeline_run_track stages
                              and service_config.deployment_status. Values are
                              fetched from the staged bucket INSIDE the activity
                              and never appear in Temporal payloads or results.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from temporalio import activity

logger = logging.getLogger(__name__)


# ── Stage / status writers (plain functions, own short-lived sessions) ───────
# Called from inside deploy_variables_activity's progress callback — activities
# cannot invoke other activities, so these write directly through the repos
# (same append/update semantics as update_pipeline_run_track_stage).

async def _write_run_track_stage(
    vendor_deployment_id: str,
    stage_name: str,
    stage_status: str,
    started_at: str,
    error_message: Optional[str] = None,
) -> None:
    from app.db.session import AsyncSessionLocal
    from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
    from app.core.enum import PipelineRunStatusEnum
    import app.db.models  # noqa: F401

    async with AsyncSessionLocal() as db:
        repo = PipelineRunTrackRepository(db)
        rows = await repo.get_by_vendor_deployment_id(vendor_deployment_id)
        if not rows:
            logger.warning(
                "run-track stage '%s': no rows for vendor_deployment_id=%s",
                stage_name, vendor_deployment_id,
            )
            return

        for row in rows:
            stages = list(row.build_stages or [])
            if stage_status == "running":
                stages.append({"name": stage_name, "status": "running", "started_at": started_at})
            else:
                # Terminal update: call time is the stage's end time.
                last_idx = None
                for i in range(len(stages) - 1, -1, -1):
                    if stages[i].get("name") == stage_name:
                        last_idx = i
                        break
                if last_idx is not None:
                    stages[last_idx]["status"] = stage_status
                    stages[last_idx]["ended_at"] = started_at
                    if error_message:
                        stages[last_idx]["error"] = error_message
                else:
                    # Single-shot marker (no running phase): started_at == ended_at.
                    entry = {
                        "name": stage_name, "status": stage_status,
                        "started_at": started_at, "ended_at": started_at,
                    }
                    if error_message:
                        entry["error"] = error_message
                    stages.append(entry)

            final_status = PipelineRunStatusEnum.FAILED if stage_status == "failed" else None
            await repo.update(code=row.code, build_stages=stages, status=final_status)


async def _write_service_deployment_status(
    service_config_code: str,
    status,  # ResourceDeploymentStatusEnum
    error_message: Optional[str] = None,
) -> None:
    from app.db.session import AsyncSessionLocal
    from app.repository.service_config_repository import ServiceConfigRepository
    import app.db.models  # noqa: F401

    async with AsyncSessionLocal() as db:
        repo = ServiceConfigRepository(db)
        await repo.bulk_update_deployment_status([service_config_code], status, error_message)
        await db.commit()


async def _fail_inflight_variable_stage(track_id: str, sc_code: str, error: str) -> None:
    """Transport-failure bookkeeping for the variable deploy: no HTTP response
    means the secret service could not tell us which phase was in flight — but
    it HAS been writing its stage rows to the shared DB all along. Read them
    back and fail whichever variable stage is currently 'running'; default to
    the first stage when nothing started. (If the server actually finished
    after our timeout, the Temporal retry hits no_staged_variables and reports
    success, superseding this pessimistic write.)"""
    from datetime import datetime, timezone
    from app.db.session import AsyncSessionLocal
    from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
    from app.core.enum import ResourceDeploymentStatusEnum
    import app.db.models  # noqa: F401

    failed_stage = "secrets: save"
    failed_status = ResourceDeploymentStatusEnum.SECRETS_DEPLOYMENT_FAILED
    try:
        async with AsyncSessionLocal() as db:
            rows = await PipelineRunTrackRepository(db).get_by_vendor_deployment_id(track_id)
        for row in rows:
            for st in row.build_stages or []:
                if st.get("name") == "configs: save" and st.get("status") == "running":
                    failed_stage = "configs: save"
                    failed_status = ResourceDeploymentStatusEnum.CONFIG_DEPLOYMENT_FAILED
    except Exception as exc:  # read-back is best-effort; the default stands
        logger.warning("could not read run-track for %s: %s", track_id, exc)

    await _write_service_deployment_status(sc_code, failed_status, error[:900])
    await _write_run_track_stage(
        track_id, failed_stage, "failed",
        datetime.now(timezone.utc).isoformat(), error[:500],
    )


# ── Activity: prepare_multiple_deploy ────────────────────────────────────────

@activity.defn
async def prepare_multiple_deploy(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resolve everything the orchestrator needs before locking.

    params:
      tenant_code, user_code, orchestrator_id,
      variables: {transaction_code, table_name, environment} | None,
      infra:     {environment, item_ids} | None,
      service_config_code: str | None  # explicit anchor for the batch's ONE
                                        # run-track row when no service queue
                                        # item is present (e.g. kong-only, or
                                        # kong + s3 with no service/variables
                                        # deploy). Ignored when a service item
                                        # IS present — that item is always the
                                        # anchor then.

    Returns (JSON-safe, no values):
      {
        "project_dirs":   [..],          # lock keys — union of infra + variable dirs
        "queue_ids":      [..],          # SERVICE queue item ids ([] when no service item)
        "infra_child_id": str | None,    # pre-generated service DeploymentWorkflow id
        "track_id":       str,           # vendor_deployment_id shared by the WHOLE batch
        "extra_groups":   [..],          # [{project_dir, queue_ids, child_id}, ...] —
                                          # non-service items (kong today), grouped by
                                          # terragrunt dir, one DeploymentWorkflow child each
        "has_variables":  bool,
        "variable_params": {..} | None,  # codes-only input for VariableDeployWorkflow
        "error":          str | None,    # set → orchestrator fails fast, no lock taken
      }
    """
    from sqlalchemy import select, and_
    from app.db.session import AsyncSessionLocal
    from app.db.models.transaction_queue_model import TransactionQueueStatusEnum
    from app.db.models.pipeline_vendor_mst_model import PipelineVendorMstModel
    from app.db.models.pipeline_mst_model import PipelineMstModel
    from app.repository.transaction_queue_repository import TransactionQueueRepository
    from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
    from app.repository.service_config_repository import ServiceConfigRepository
    from app.handlers.file_location_handler import FileLocationHandler
    from app.schemas.pr_workflow_context import PRWorkflowContext
    from app.core.enum import (
        EnvironmentEnum,
        PipelineAgentEnum,
        PipelineRunStatusEnum,
        WorkflowSourceTableEnum,
    )
    from app.utils.pipeline_helpers import generate_run_code
    import app.db.models  # noqa: F401

    tenant_code = params["tenant_code"]
    user_code = params["user_code"]
    orchestrator_id = params["orchestrator_id"]
    variables_part = params.get("variables")
    infra_part = params.get("infra")
    gateway_part = params.get("gateway")
    explicit_service_config_code = params.get("service_config_code")

    project_dirs: List[str] = []
    queue_ids: List[int] = []
    items: list = []
    item_group_keys: Dict[int, str] = {}  # queue item id → its terragrunt dir (grouping key)
    item_envs: set = set()  # environments seen across infra items (prod detection)
    workflow_context = PRWorkflowContext()

    async with AsyncSessionLocal() as db:
        queue_repo = TransactionQueueRepository(db)

        # ── Gateway part: resolve the service's ONE approved gateway row here,
        # server-side, so any can_deploy user ships the author's routes (parity
        # with variables/settings). The caller sends the service code, not the
        # queue-id — which only the author's client had. Merge the resolved id
        # into infra.item_ids so it flows through the same project-dir + routing
        # + STARTING_DEPLOYMENT processing as any other queue item.
        if gateway_part:
            gw_row = await queue_repo.find_gateway_request_for_deploy(
                gateway_part["transaction_code"], tenant_code
            )
            if gw_row is not None:
                base_ids = list(infra_part.get("item_ids") or []) if infra_part else []
                if gw_row.id not in base_ids:
                    base_ids.append(gw_row.id)
                infra_part = {**(infra_part or {}), "item_ids": base_ids}
            elif not infra_part and not variables_part:
                # Gateway was the only thing to deploy and there is no approved
                # gateway row — nothing to ship, say so rather than deploy empty.
                return {"error": "No approved gateway change to deploy for this service"}

        # ── Infra part: resolve queue items + derive their project_dirs ──────
        if infra_part:
            item_ids = infra_part.get("item_ids")
            if item_ids:
                # Environment/geo/application scoping is inherent to the items
                # themselves (a queue row points at one env-specific source row),
                # so no environment filter is needed — and TransactionQueueModel
                # has no environment column to filter on anyway.
                for item_id in item_ids:
                    item = await queue_repo.get_by_id(item_id)
                    if item and item.status == TransactionQueueStatusEnum.APPROVED:
                        items.append(item)
            else:
                items = await queue_repo.get_pending_items_for_user(user_code, tenant_code)
            if not items:
                return {"error": "No pending items to deploy for the infra part"}

            queue_ids = [item.id for item in items]

            # NOTE: create_secrets / create_ssm are NOT coerced here.
            #
            # This used to flip those flags ON in config_snapshot when the
            # service had matching variables, and persist it. That edit broke
            # the approval seal: approved_snapshot_hash fingerprints the
            # snapshot at approval and validate_deployable_queue_items
            # re-verifies it on every deploy. The running deploy never noticed
            # (nothing re-checks once Temporal has the batch), but a FAILED
            # deploy returns the row to APPROVED with the original seal, and the
            # retry was refused as "changed after it was approved" — with a
            # false `seal-broken` event written to the row's history.
            #
            # The flags are now resolved at render time in the EKS terragrunt
            # component, which is their only consumer, leaving the approved
            # content immutable. See aspora_eks_terragrunt_script_gen_component.

            async def _apply_gateway_routing(queue_item, queue_dict: dict) -> None:
                # Mirrors ScriptPRWorkflowService._apply_gateway_routing
                # (script_pr_workflow_service.py:1272). A KONG_ROUTE gateway
                # row deliberately carries no product_name on its snapshot —
                # it is resolved LIVE from the service's application so a
                # renamed application can't leave a saved row pointing at a
                # stale folder. Without this, FileLocationHandler.locate()
                # raises "product_name is required" for every kong item here,
                # and the except-fallback below gives each item its OWN unique
                # item-{id} group key — splitting what should be ONE shared
                # gateway PR into one PR per route.
                #
                # resolve_routing accepts EITHER gateway shape (route-group
                # code or service_configs code). Deliberately: this function
                # is a copy, and shape knowledge kept here is knowledge the
                # two copies can drift on.
                from app.db.models.transaction_queue_model import is_gateway_row

                if not is_gateway_row(queue_item):
                    return
                if not queue_item.transaction_code:
                    return
                from app.repository.kong_route_groups_repository import KongRouteGroupsRepository
                routing = await KongRouteGroupsRepository(db).resolve_routing(
                    queue_item.transaction_code
                )
                if not routing:
                    return
                queue_dict["environment"] = routing["environment"] or ""
                queue_dict["region"] = routing["region"] or ""
                queue_dict["geo_loc_mst_code"] = routing["region"] or ""
                queue_dict["product_name"] = routing["product_name"] or ""
                queue_dict["service_name"] = routing["service_name"] or ""
                queue_dict["service_mst_code"] = routing["service_mst_code"] or ""

            for queue_item in items:
                environment_val = None
                geo_loc_mst_code = None
                infra_vendor_accounts_mst_code = None
                if getattr(queue_item, "source_entity", None):
                    src = queue_item.source_entity
                    if hasattr(src, "environments_enum"):
                        environment_val = (
                            src.environments_enum.value
                            if hasattr(src.environments_enum, "value")
                            else str(src.environments_enum)
                        )
                    geo_loc_mst_code = getattr(src, "geo_loc_mst_code", None)
                    infra_vendor_accounts_mst_code = getattr(
                        src, "infra_vendor_accounts_mst_code", None
                    )

                # Same fallback chain as deploy_temporal: source_entity is
                # only attached on some query paths — prod routing must not
                # silently miss (source row → snapshot → source-table query).
                env_for_detection = (
                    (environment_val or "").lower()
                    or await queue_repo.resolve_item_environment(queue_item)
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
                    await _apply_gateway_routing(queue_item, queue_dict)
                    file_resp = await FileLocationHandler.locate(
                        tenant_code, queue_dict, workflow_context
                    )
                    _item_dir = None
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
                            if _item_dir is None:
                                _item_dir = project_dir
                    item_group_keys[queue_item.id] = _item_dir or f"item-{queue_item.id}"
                except Exception as e:
                    logger.warning(
                        "prepare_multiple_deploy: FileLocationHandler failed for item %s: %s "
                        "— falling back to item-id lock key", queue_item.id, e,
                    )
                    project_dirs.append(f"item-{queue_item.id}")
                    item_group_keys[queue_item.id] = f"item-{queue_item.id}"

            if not project_dirs:
                project_dirs = [f"item-{item_id}" for item_id in queue_ids]

        # ── Variable part: derive the service's dir so the SAME key set
        # serializes variable and infra deploys of this service ───────────────
        variable_params: Optional[Dict[str, Any]] = None
        if variables_part:
            sc_code = variables_part["transaction_code"]
            sc_repo = ServiceConfigRepository(db)
            sc_row = await sc_repo.get_by_code_and_tenant(sc_code, tenant_code)
            if sc_row is None:
                return {"error": f"service_config '{sc_code}' not found for tenant '{tenant_code}'"}

            # ServiceConfigModel's column is `environment` (an EnvironmentEnum).
            sc_env_attr = sc_row.environment
            sc_env = sc_env_attr.value if hasattr(sc_env_attr, "value") else str(sc_env_attr or "")

            # Variables have NO queue item (values live in the draft bucket; the
            # transaction queue is for IaC changes only) — build the locator's
            # path fields directly from the DB, via the same joins a real
            # snapshot's enrichment would use: product/env/region/service/
            # service_type drive the terragrunt path (= the coordinator lock
            # key); case_ref 'manage_variable' routes the locator's EKS/ECS
            # service branch without ever generating IaC.
            from app.repository.services_mst_repository import ServicesMstRepository
            from app.repository.applications_mst_repository import ApplicationsMstRepository

            var_snapshot: Dict[str, Any] = {
                "environment": sc_env,
                "geo_loc_mst_code": sc_row.geo_loc_mst_code or "",
                "infrastructuretype_ref_code": sc_row.infrastructuretype_ref_code or "",
                "infrastructure_mst_code": sc_row.infrastructure_mst_code or "",
                "services_mst_code": sc_row.services_mst_code or "",
            }
            if sc_row.services_mst_code:
                svc = await ServicesMstRepository(db).get_by_code(sc_row.services_mst_code)
                if svc:
                    var_snapshot["service_name"] = svc.name
                    svc_type = getattr(svc, "service_type", None)
                    if svc_type is not None:
                        var_snapshot["service_type"] = (
                            svc_type.value if hasattr(svc_type, "value") else str(svc_type)
                        )
                    if getattr(svc, "applications_mst_code", None):
                        app_row = await ApplicationsMstRepository(db).get_by_code(
                            svc.applications_mst_code
                        )
                        if app_row:
                            # The locator hard-requires product_name.
                            var_snapshot["product_name"] = app_row.name

            var_queue_dict = {
                "id": 0,
                "code": f"vars-{sc_code}",
                "user_code": user_code,
                "transaction_code": sc_code,
                "case_ref_code": "manage_variable",
                "table_name": "service_config",
                "config_snapshot": var_snapshot,
                "tenant_code": tenant_code,
                "status": "approved",
                "environment": sc_env,
                "geo_loc_mst_code": sc_row.geo_loc_mst_code or "",
                "infra_vendor_accounts_mst_code": "",
            }
            try:
                # skip_commit=True: this locate() is ONLY for path derivation —
                # without it the locator's _get_or_create_feature_branch would
                # create real GitHub feature branches as a side effect, junk for
                # a variables deploy that never touches IaC. (The infra items'
                # locate above keeps the default, matching deploy_temporal.)
                file_resp = await FileLocationHandler.locate(
                    tenant_code, var_queue_dict, PRWorkflowContext(skip_commit=True)
                )
                located = False
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
                        located = True
                if not located:
                    raise ValueError("no terragrunt.hcl located for service")
            except Exception as e:
                # Fallback key still serializes variable deploys of this service
                # against each other, but NOT against infra deploys (their dir
                # derivation succeeded from real queue snapshots). Logged loudly.
                fallback = f"service-{sc_code}"
                logger.warning(
                    "prepare_multiple_deploy: variable dir derivation failed for %s (%s) "
                    "— using fallback lock key '%s'", sc_code, e, fallback,
                )
                if fallback not in project_dirs:
                    project_dirs.append(fallback)

            variable_params = {
                "transaction_code": sc_code,
                "table_name": variables_part.get("table_name") or "service_config",
                "environment": variables_part.get("environment"),
                "user_code": user_code,
                "tenant_code": tenant_code,
                # track_id filled in below once known
            }

        if not project_dirs:
            return {"error": "Nothing to deploy — both variable and infra parts are empty"}

        # ── Segregate items — every phase is optional (present → deploy) ─────
        # Service config items keep today's flow exactly: they are the phase-1
        # DeploymentWorkflow child (deployed FIRST — Terraform owns the SM/SSM
        # containers the variables stage fills right after). Every OTHER item
        # (kong routes today, s3/sqs/more later) is grouped by its terragrunt
        # dir; each group becomes its own DeploymentWorkflow child run AFTER
        # variables. One dir per child keeps the one-Atlantis-project-per-PR
        # invariant. No service items → phase 1 is simply skipped, same as the
        # existing variables handling.
        _svc_tables = {
            WorkflowSourceTableEnum.SERVICE_CONFIG,
            WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
        }

        def _is_service_item(it) -> bool:
            # add_route items can live on the service_config table too — they
            # are kong routes, not service deployments (same distinction as
            # transaction_queue_service's display logic).
            return it.table_name in _svc_tables and (it.case_ref_code or "") != "add_route"

        service_items = [i for i in items if _is_service_item(i)]
        other_items = [i for i in items if not _is_service_item(i)]

        other_groups: Dict[str, List[Any]] = {}  # terragrunt dir → [queue items]
        for queue_item in other_items:
            key = item_group_keys.get(queue_item.id) or f"item-{queue_item.id}"
            other_groups.setdefault(key, []).append(queue_item)

        # ── Pre-generate the children's Temporal workflow ids ────────────────
        # infra_child_id / extra child_ids are the ids the DeploymentWorkflow
        # children will RUN under (execute_child_workflow id=...). They are NOT
        # run-track keys — the whole batch's single run-track row is tagged
        # with the ORCHESTRATOR's workflow id (track_id below), and every
        # child (service infra, variables, extras) is handed track_id to
        # write its stages onto that one shared timeline.
        service_queue_ids = [i.id for i in service_items]
        infra_child_id = (
            f"deploy-{tenant_code}-{uuid.uuid4().hex[:12]}" if service_queue_ids else None
        )
        track_id = orchestrator_id

        def _pr_group_label(grp: List[Any]) -> str:
            # Tags every PR the group's child creates (deploy_result.prs[].group)
            # so a shared run-track row can tell resources apart. Gateway rows
            # are the only non-service type routed here today — matched by
            # case_ref_code since they now sit on SERVICE_CONFIG (pre-flip rows
            # still say KONG_ROUTE); any future type falls back to its table
            # name so nothing needs updating here.
            if (grp[0].case_ref_code or "") == "add_route":
                return "kong"
            _tbl = grp[0].table_name
            _tbl_val = _tbl.value if hasattr(_tbl, "value") else str(_tbl)
            return {"KONG_ROUTE": "kong"}.get(_tbl_val, _tbl_val.lower())

        extra_groups = [
            {
                "project_dir": key,
                "queue_ids": [i.id for i in grp],
                "child_id": f"deploy-{tenant_code}-{uuid.uuid4().hex[:12]}",
                "pr_group": _pr_group_label(grp),
            }
            for key, grp in other_groups.items()
        ]
        # ── Production promotion mode ────────────────────────────────────────
        # Engages ONLY when the batch has infra items in a prod environment.
        # A prod variables-only batch has no infra items → item_envs stays
        # empty → today's path, byte-identical (variables never touch git).
        deploy_mode = None
        repo_full_name = None
        source_branch = None
        target_branch = None
        service_dirs: List[str] = []
        park_release_dirs: List[str] = []
        if items and any("prod" in e for e in item_envs if e):
            from app.core.config import settings as _settings
            from app.utils.tenant_config import get_tenant_config
            from app.temporal.workflows.production_deployment_workflow import promotion_lock_key

            tenant_cfg = await get_tenant_config(tenant_code, db)
            repo_full_name = tenant_cfg.github_infra_repository
            if not repo_full_name:
                return {"error": f"No infra repository configured for tenant '{tenant_code}'"}
            deploy_mode = "production"
            source_branch = _settings.temporal_prod_source_branch
            target_branch = _settings.temporal_prod_target_branch
            # The service child's own dirs (kept held on park).
            service_dirs = sorted({
                item_group_keys.get(i.id) or f"item-{i.id}" for i in service_items
            })
            # Shared (kong/gateway) dirs — released on park along with the key.
            shared = sorted({g["project_dir"] for g in extra_groups})
            promo_key = promotion_lock_key(repo_full_name)
            # The key rides the orchestrator's normal all-or-nothing acquire:
            # one prod batch at a time per repo, held for the whole batch.
            if promo_key not in project_dirs:
                project_dirs.append(promo_key)
            park_release_dirs = [promo_key] + shared
            logger.info(
                "prepare_multiple_deploy: PRODUCTION mode — repo=%s %s->%s "
                "service_dirs=%s park_release=%s",
                repo_full_name, source_branch, target_branch,
                service_dirs, park_release_dirs,
            )

        if variable_params is not None:
            variable_params["track_id"] = track_id

        # ── Close the settings-diff refresh window (same as deploy_temporal) ─
        for item in items:
            await queue_repo.update_status(
                item.id, TransactionQueueStatusEnum.STARTING_DEPLOYMENT.value
            )

        # ── Pipeline vendor / pipeline_mst / run_track rows ──────────────────
        vendor_code = f"pv-temporal-{tenant_code}"
        vendor_result = await db.execute(
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
            db.add(vendor)
            await db.flush()

        run_track_repo = PipelineRunTrackRepository(db)

        async def _ensure_pipeline_and_track(
            transaction_code: str, table_name, display: str, queue_codes: List[str]
        ) -> None:
            pipeline_result = await db.execute(
                select(PipelineMstModel).where(
                    and_(
                        PipelineMstModel.transaction_code == transaction_code,
                        PipelineMstModel.table_name == table_name,
                        PipelineMstModel.tenant_code == tenant_code,
                        PipelineMstModel.pipeline_vendor_mst_code == vendor_code,
                    )
                )
            )
            pipeline = pipeline_result.scalar_one_or_none()
            if not pipeline:
                pipeline = PipelineMstModel(
                    code=f"pipeline-temporal-{transaction_code[:10]}-{uuid.uuid4().hex[:8]}",
                    name=f"Temporal: {transaction_code} ({display})",
                    pipeline_vendor_mst_code=vendor_code,
                    transaction_code=transaction_code,
                    table_name=table_name,
                    tenant_code=tenant_code,
                    repo_url="temporal://",
                    repo_branch="main",
                )
                db.add(pipeline)
                await db.flush()
            await run_track_repo.create(
                pipeline_mst_code=pipeline.code,
                code=generate_run_code(pipeline.code),
                status=PipelineRunStatusEnum.RUNNING,
                transaction_queue_code=queue_codes,
                vendor_deployment_id=track_id,
            )

        # ── ONE pipeline_mst + ONE run-track row for the WHOLE multi-deploy ──
        # Multi-deploy is always in the context of one particular service —
        # anchored under that service_config's OWN pipeline (transaction_code
        # = service_config.code, table = SERVICE_CONFIG). Every child (service
        # infra, variables, extra groups / kong) writes its stages onto THIS
        # one row via the shared track_id (vendor_deployment_id). No separate
        # pipeline_mst/run_track is created for kong or any other extra-group
        # item. Anchor precedence:
        #   1) the service item in the batch, when present.
        #   2) variables.transaction_code — a real variable deploy IS running
        #      against that service.
        #   3) explicit service_config_code — caller states the owning
        #      service directly (kong-only, no variables staged).
        _anchor_sc_code = (
            service_items[0].transaction_code if service_items
            else variable_params["transaction_code"] if variable_params is not None
            else explicit_service_config_code
        )
        if _anchor_sc_code:
            await _ensure_pipeline_and_track(
                _anchor_sc_code,
                WorkflowSourceTableEnum.SERVICE_CONFIG,
                "multi-deploy",
                [item.code for item in items],  # ALL items — service + every extra group
            )

        await db.commit()

    logger.info(
        "prepare_multiple_deploy: tenant=%s dirs=%s service_queues=%s extra_groups=%s "
        "infra_child=%s track=%s vars=%s",
        tenant_code, project_dirs, service_queue_ids,
        [(g["project_dir"], g["queue_ids"]) for g in extra_groups],
        infra_child_id, track_id, bool(variable_params),
    )
    return {
        "project_dirs": project_dirs,
        "queue_ids": service_queue_ids,
        "infra_child_id": infra_child_id,
        "track_id": track_id,
        "extra_groups": extra_groups,
        "has_variables": variable_params is not None,
        "variable_params": variable_params,
        # Production promotion mode (None/absent on stage batches).
        "deploy_mode": deploy_mode,
        "repo_full_name": repo_full_name,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "service_dirs": service_dirs,
        "park_release_dirs": park_release_dirs,
        "error": None,
    }


# ── Activity: deploy_variables_activity ─────────────────────────────────────

# Milestone → (run-track stage writes, deployment_status enum value).
# Stage names follow the '{artifact}: {action}' convention shared with the
# infra stages. audit_trail_saved is internal detail — no stage for it.
_MILESTONE_PLAN = {
    "starting_secrets_deployment": [("secrets: save", "running")],
    "secrets_deployed":            [("secrets: save", "success")],
    "starting_config_deployment":  [("configs: save", "running")],
    "configs_deployed":            [("configs: save", "success")],
}


@activity.defn
async def deploy_variables_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deploy the caller's staged variables for one service — by calling
    devlift-secret-config-manager's internal variable-deploy endpoint.

    Why HTTP instead of calling VariableService here: that service is the
    auditable home of ALL secret handling (staged-file reads, KMS, Secrets
    Manager / SSM writes). Listing and save already execute there; this moves
    the deploy execution too, so obs_tool's worker never touches secret values.
    The endpoint writes the same run-track stages and deployment statuses to
    the shared DB (it receives track_id), so the UI behaves identically.

    Authorization: the acting user's can_deploy was enforced by the FGA card on
    /deployments/multiple-deploy before this workflow started. The HTTP call
    authenticates as obs_tool via the shared X-Internal-Key; the user travels
    as data (the deploy needs their email for the staged-file path, and the
    audit trail keeps naming the real deployer).

    params (codes only — NEVER values):
      transaction_code, table_name, environment, user_code, tenant_code, track_id

    Returns (values-free): {"all_success": bool, "results": [...], "error": str|None}

    Retry rule: a 404 {"code": "no_staged_variables"} on attempt 1 is a real
    user error ("run save first"). On attempt > 1 it means the PREVIOUS attempt
    already consumed the staged file — a completed deploy whose response was
    lost — so it is treated as success instead of failing a finished deploy.
    """
    import httpx
    from app.core.config import settings

    sc_code = params["transaction_code"]
    track_id = params["track_id"]
    attempt = activity.info().attempt

    if not settings.secret_service_internal_key:
        raise ValueError("SECRET_SERVICE_INTERNAL_KEY is not configured — cannot call the secret service")

    # SECRET_SERVICE_URL must include the service's path prefix, e.g.
    #   https://be.prod.aspora.devlift.ai/secret-config-manager
    # The ALB is shared: the bare origin hits obs_tool via the "/*" catch-all,
    # so the prefix is the only thing that selects this service's target group.
    # Only the route is appended here — putting the prefix in both places is
    # what produced /secret-config-manager/secrets-manager/api/v1/... and a 404.
    url = settings.secret_service_url.rstrip("/") + "/api/v1/internal/variable-deploy"
    payload = {
        "resource_code": sc_code,
        "table_name": params.get("table_name"),
        "environment": params.get("environment"),
        "user_code": params["user_code"],
        "tenant_code": params["tenant_code"],
        "track_id": track_id,
    }

    try:
        # Timeout sits just under the activity's 15-min start_to_close so a
        # hung call surfaces as THIS activity failing, with Temporal retrying.
        async with httpx.AsyncClient(timeout=840.0) as client:
            resp = await client.post(
                url, json=payload,
                headers={"X-Internal-Key": settings.secret_service_internal_key},
            )
    except httpx.HTTPError as exc:
        # Transport-level failure: no response, so the service could not write
        # its own failure rows. Read the shared run-track back to fail whichever
        # variable stage was actually in flight (configs vs secrets).
        activity.logger.error("variable-deploy call failed for %s: %s", sc_code, exc)
        await _fail_inflight_variable_stage(track_id, sc_code, str(exc))
        raise

    if resp.status_code == 200:
        return resp.json()

    detail = None
    try:
        detail = resp.json().get("detail")
    except Exception:
        pass

    if resp.status_code == 404 and isinstance(detail, dict) and detail.get("code") == "no_staged_variables":
        if attempt > 1:
            activity.logger.info(
                "variable-deploy retry %d for %s found no staged file — previous "
                "attempt completed; treating as success", attempt, sc_code,
            )
            return {"all_success": True, "results": [], "error": None}
        raise ValueError(detail.get("message") or "No staged variables found — run save first")

    # Non-200 WITH a response: the service already wrote its failure
    # bookkeeping before answering — don't double-write, just fail the activity.
    message = detail if isinstance(detail, str) else (detail or {}).get("message") if isinstance(detail, dict) else None
    activity.logger.error(
        "variable-deploy returned %s for %s: %s", resp.status_code, sc_code, message or resp.text[:300]
    )
    raise ValueError(message or f"variable-deploy failed with HTTP {resp.status_code}")
