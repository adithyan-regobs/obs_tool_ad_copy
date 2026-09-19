"""
Script PR Workflow Service

Orchestration layer for script preview and PR creation.
Coordinates FileLocationHandler and ScriptGenHandler.

Future extensions:
- Create actual PRs with generated scripts
- Update atlantis.yaml
- Handle multi-branch PRs
"""

import logging
import time
from collections import defaultdict
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.transaction_queue_model import TransactionQueueStatusEnum
from app.core.enum import WorkflowSourceTableEnum
from app.repository.transaction_queue_repository import TransactionQueueRepository
from app.repository.user_mst_repository import UserMstRepository
from app.handlers.file_location_handler import FileLocationHandler
from app.handlers.script_gen_handler import ScriptGenHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.handlers.resource_post_action_handler import ResourcePostActionHandler
from app.handlers.resource_pre_action_handler import ResourcePreActionHandler
from app.schemas.pr_workflow_context import PRWorkflowContext
from app.schemas.file_location_response_schema import FileLocationResponse
from app.core.config import settings
from app.utils.pr_body_helpers import (
    generate_pr_title,
    generate_pr_body,
    get_region_display,
    compute_atlantis_project_name,
    normalize_infra_type
)

logger = logging.getLogger(__name__)


class ScriptPRWorkflowService:
    """
    Service for orchestrating script preview and PR workflow.

    Workflow:
    1. Fetch queue item from gitops_queue table
    2. Call FileLocationHandler.locate_files(queue_item)
    3. For each file location:
       a. Call ScriptGenHandler.generate_script(queue_item)
       b. Get rendered script content (string)
    4. Return array of previews

    Future: Extend to create actual PRs with generated files.
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize service.

        Args:
            db: Database session
        """
        self.db = db
        self.queue_repo = TransactionQueueRepository(db)
        self.user_repo = UserMstRepository(db)
        self.logger = logging.getLogger(__name__)

    async def _fetch_batch_items(
        self,
        user_code: str,
        tenant_code: str,
        queue_ids: Optional[List[int]],
        all_pending_queues: bool,
    ) -> List[Any]:
        """The queue selection create() performs, run up-front.

        create() returns PRs, not queue items — so without this the wrapper has
        no way to know which resources are in the batch, which is what the
        run-track rows are grouped by. Deliberately the SAME query and statuses
        as create()'s Step 1, so the two can never disagree about the batch.
        create() reads them again; that second read is cheap beside the GitHub
        round-trips and keeps create()'s signature untouched.
        """
        if all_pending_queues:
            return await self.queue_repo.get_all_queues_for_user(
                user_code=user_code,
                tenant_code=tenant_code,
                status=TransactionQueueStatusEnum.APPROVED,
            )
        return await self.queue_repo.get_selected_queues(
            user_code=user_code,
            tenant_code=tenant_code,
            selected_queue_ids=queue_ids,
            statuses=[
                TransactionQueueStatusEnum.APPROVED,
                TransactionQueueStatusEnum.STARTING_DEPLOYMENT,
            ],
        )

    async def _ensure_pipeline_and_track(
        self,
        transaction_code: str,
        table_name: Any,
        group_items: List[Any],
        tenant_code: str,
    ) -> Optional[str]:
        """One pipeline_mst (reused) + one pipeline_run_track (always new) for
        one resource, returning the run's track id.

        Mirrors prepare_multiple_deploy._ensure_pipeline_and_track, with one
        difference: that one always anchors on the service and so hardcodes
        table_name=SERVICE_CONFIG. Here a batch can span unrelated resources
        (a bucket, a queue, a dynamo table), so table_name comes from the queue
        item — otherwise a bucket's pipeline would be labelled a service.

        Returns None if the rows cannot be written; the PR run then proceeds
        untracked rather than failing.
        """
        import uuid
        from sqlalchemy import select, and_
        from app.core.enum import (
            EnvironmentEnum,
            PipelineAgentEnum,
            PipelineRunStatusEnum,
        )
        from app.db.models.pipeline_vendor_mst_model import PipelineVendorMstModel
        from app.db.models.pipeline_mst_model import PipelineMstModel
        from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
        from app.utils.pipeline_helpers import generate_run_code

        try:
            # Same vendor row the Temporal path uses, so create-PR runs and
            # deploy runs sit under one vendor rather than splitting the history.
            vendor_code = f"pv-temporal-{tenant_code}"
            vendor = (
                await self.db.execute(
                    select(PipelineVendorMstModel).where(
                        PipelineVendorMstModel.code == vendor_code
                    )
                )
            ).scalar_one_or_none()
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

            pipeline = (
                await self.db.execute(
                    select(PipelineMstModel).where(
                        and_(
                            PipelineMstModel.transaction_code == transaction_code,
                            PipelineMstModel.table_name == table_name,
                            PipelineMstModel.tenant_code == tenant_code,
                            PipelineMstModel.pipeline_vendor_mst_code == vendor_code,
                        )
                    )
                )
            ).scalar_one_or_none()
            if not pipeline:
                pipeline = PipelineMstModel(
                    code=f"pipeline-temporal-{transaction_code[:10]}-{uuid.uuid4().hex[:8]}",
                    name=f"Temporal: {transaction_code} (create-pr)",
                    pipeline_vendor_mst_code=vendor_code,
                    transaction_code=transaction_code,
                    table_name=table_name,
                    tenant_code=tenant_code,
                    repo_url="temporal://",
                    repo_branch="main",
                )
                self.db.add(pipeline)
                await self.db.flush()

            # vendor_deployment_id is the run's key: Temporal puts its workflow
            # id here, create-PR has no workflow so it gets a generated one.
            track_id = f"create-pr-{uuid.uuid4().hex[:12]}"
            await PipelineRunTrackRepository(self.db).create(
                pipeline_mst_code=pipeline.code,
                code=generate_run_code(pipeline.code),
                status=PipelineRunStatusEnum.RUNNING,
                transaction_queue_code=[i.code for i in group_items],
                vendor_deployment_id=track_id,
            )
            await self.db.commit()
            return track_id
        except Exception as exc:  # noqa: BLE001 — best-effort bookkeeping
            self.logger.warning(
                "create-pr run-track for %s could not be opened: %s", transaction_code, exc
            )
            await self.db.rollback()
            return None

    async def _write_pr_stage(
        self,
        track_id: str,
        stage_name: str,
        stage_status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """Open or close one stage on this run's pipeline_run_track row.

        Same semantics as multiple_deploy_activities._write_run_track_stage, so
        the create-PR timeline renders exactly like a Temporal deploy's:
        'running' appends an entry, a terminal status closes the newest entry of
        that name (searching backwards, since a retry can repeat a name), and a
        terminal status with no open entry lands as a point-in-time marker.

        Never raises: bookkeeping must not fail a PR run that actually worked.

        Own short-lived session, NOT self.db: the failure path runs right
        after create() raised, which can leave self.db's transaction aborted
        (e.g. a failed INSERT) — every query on it then raises until rollback,
        and the request session's eventual rollback would erase the write
        anyway. A fresh session keeps the FAILED status durable, so the FE
        poller sees a terminal state instead of RUNNING forever.
        """
        from datetime import datetime, timezone
        from app.core.enum import PipelineRunStatusEnum
        from app.db.session import AsyncSessionLocal
        from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository

        now = datetime.now(timezone.utc).isoformat()
        try:
            async with AsyncSessionLocal() as db:
                repo = PipelineRunTrackRepository(db)
                rows = await repo.get_by_vendor_deployment_id(track_id)
                if not rows:
                    self.logger.warning(
                        "create-pr run-track stage '%s': no rows for track %s", stage_name, track_id
                    )
                    return
                for row in rows:
                    stages = list(row.build_stages or [])
                    if stage_status == "running":
                        stages.append({"name": stage_name, "status": "running", "started_at": now})
                    else:
                        idx = next(
                            (i for i in range(len(stages) - 1, -1, -1)
                             if stages[i].get("name") == stage_name),
                            None,
                        )
                        if idx is not None:
                            stages[idx]["status"] = stage_status
                            stages[idx]["ended_at"] = now
                            if error_message:
                                stages[idx]["error"] = error_message
                        else:
                            entry = {
                                "name": stage_name, "status": stage_status,
                                "started_at": now, "ended_at": now,
                            }
                            if error_message:
                                entry["error"] = error_message
                            stages.append(entry)
                    await repo.update(
                        code=row.code,
                        build_stages=stages,
                        status=PipelineRunStatusEnum.FAILED if stage_status == "failed" else None,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort bookkeeping
            self.logger.warning(
                "create-pr run-track stage '%s' could not be written: %s", stage_name, exc
            )

    async def _append_deploy_result_prs(self, track_id: str, prs: List[Dict]) -> None:
        """Add PR links to deploy_result.prs — {name, url, repo, number, group},
        the shape deployment_workflow persists so a run carries its PR links.

        Appends rather than replaces, matching
        deploy_activities.update_pipeline_run_track_deploy_result. Best-effort,
        for the same reason as _write_pr_stage.
        """
        from app.db.session import AsyncSessionLocal
        from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository

        if not prs:
            return
        try:
            async with AsyncSessionLocal() as db:
                repo = PipelineRunTrackRepository(db)
                for row in await repo.get_by_vendor_deployment_id(track_id):
                    merged = dict(row.deploy_result or {})
                    merged["prs"] = list(merged.get("prs") or []) + prs
                    await repo.update(code=row.code, deploy_result=merged)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "create-pr run-track %s: PR links could not be saved: %s", track_id, exc
            )

    async def create_with_run_track(
        self,
        user_code: str,
        tenant_code: str,
        queue_ids: Optional[List[int]] = None,
        all_pending_queues: bool = False,
    ) -> Dict:
        """create(), wrapped in a pipeline run-track timeline.

        Prod raises reviewed PRs instead of deploying, so the Deployments tab
        has nothing to show for it: the pipeline_run_track rows that make the
        timeline are only written by the Temporal path (prepare_multiple_deploy
        → _ensure_pipeline_and_track → _write_run_track_stage). This wrapper
        gives the create-PR flow the same timeline.

        Contract: behaviourally identical to create() for the caller — same
        arguments, same return value, same exceptions. Every run-track write is
        additive and best-effort, so bookkeeping can never fail a PR run that
        actually succeeded.
        """
        from collections import OrderedDict
        from app.core.enum import PipelineRunStatusEnum
        from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository

        # ── Group the batch by resource ──────────────────────────────────────
        # One run-track row per transaction_code, because a create-PR batch can
        # span unrelated resources. A kong route belongs to its service's row:
        # its queue item carries the service_config code as transaction_code, so
        # plain grouping puts them together with no special case.
        groups: "OrderedDict[str, List[Any]]" = OrderedDict()
        try:
            for item in await self._fetch_batch_items(
                user_code, tenant_code, queue_ids, all_pending_queues
            ):
                if item.transaction_code:
                    groups.setdefault(item.transaction_code, []).append(item)
        except Exception as exc:  # noqa: BLE001 — never block the PR run
            self.logger.warning("create-pr run-track: batch could not be read: %s", exc)

        tracks: Dict[str, str] = {}           # transaction_code → track_id
        codes_by_group: Dict[str, set] = {}   # transaction_code → its queue codes
        group_labels: Dict[str, str] = {}     # transaction_code → PR group label
        for txn_code, group_items in groups.items():
            # A scope-keyed gateway row's transaction_code IS a service_configs
            # code, so anchor its run on the SERVICE pipeline — the same anchor
            # prepare_multiple_deploy hardcodes. Left as KONG_ROUTE, the run
            # lands on a (SC_..., KONG_ROUTE) pipeline no screen ever queries:
            # the Deployments tab lists pipelines by (code, SERVICE_CONFIG).
            # Legacy KRC_/KRG_-keyed rows keep their own anchor as before.
            anchor_table = group_items[0].table_name
            if (
                anchor_table == WorkflowSourceTableEnum.KONG_ROUTE
                and not str(txn_code).startswith(("KRC_", "KRG_"))
            ):
                anchor_table = WorkflowSourceTableEnum.SERVICE_CONFIG
            track_id = await self._ensure_pipeline_and_track(
                txn_code, anchor_table, group_items, tenant_code
            )
            if track_id:
                tracks[txn_code] = track_id
                codes_by_group[txn_code] = {i.code for i in group_items}
                group_labels[txn_code] = self._pr_group_label(group_items[0])
                await self._write_pr_stage(track_id, "infra: create pr", "running")

        # ── The real work ────────────────────────────────────────────────────
        try:
            result = await self.create(
                user_code=user_code,
                tenant_code=tenant_code,
                queue_ids=queue_ids,
                all_pending_queues=all_pending_queues,
            )
        except Exception as exc:
            # create() may have died mid-INSERT, leaving self.db's transaction
            # aborted — reset it so nothing downstream trips on the poisoned
            # session (the stage writes below use their own sessions).
            try:
                await self.db.rollback()
            except Exception:  # noqa: BLE001
                pass
            for track_id in tracks.values():
                await self._write_pr_stage(track_id, "infra: create pr", "failed", str(exc))
            raise

        # ── The approved values become the live configuration ────────────────
        # AFTER create(), because a change that never shipped must not move the
        # live row — the raise above never reaches this.
        #
        # Dispatched per queue item on case_ref_code, the same way pre-actions
        # and post-actions are, so a new resource type is a component file plus
        # one map entry rather than a branch here. Items with nothing registered
        # are skipped silently.
        await self._save_settings_for_batch(tenant_code, groups)

        # ── The raised PR makes a gateway change real, so it gets recorded ───
        await self._record_gateway_routes_for_batch(groups)

        # ── Close each resource's timeline from the PRs it produced ──────────
        # gitops_responses is keyed by repo|||branch, which says nothing about
        # resources — a PR is matched to a group when they share a queue code.
        # One PR can match several groups (bucket + queue + dynamo committed to
        # one repo is one PR) and one group several PRs (service → terragrunt +
        # k8s manifest + workflow).
        prs_by_group: Dict[str, List[Dict]] = {t: [] for t in tracks}
        stages_by_group: Dict[str, List[str]] = {t: [] for t in tracks}

        for resp in (result.get("gitops_responses") or {}).values():
            if not isinstance(resp, dict):
                continue
            pr_info = resp.get("pr")
            if not isinstance(pr_info, dict):
                continue
            number = pr_info.get("number") or pr_info.get("pr_number")
            if not number:
                continue

            pr_type = resp.get("type", "infrastructure")
            # Same labels deployment_workflow uses, so both timelines read alike:
            # the terragrunt PR is 'infra', k8s-manifest PRs are 'k8s', and
            # service-repo (workflow YAML) PRs are 'service'.
            artifact = {"k8s_manifest": "k8s", "workflow": "service"}.get(pr_type, "infra")
            stage = (
                "infra: create pr" if artifact == "infra" else f"{artifact}: create PR"
            )
            repo_full = self._repo_full_name_from_pr(pr_info)
            entry = {
                "name": {"infra": "INFRA PR", "k8s": "K8S PR", "service": "SERVICE PR"}
                        .get(artifact, f"{artifact.upper()} PR"),
                "url": pr_info.get("url") or pr_info.get("html_url") or pr_info.get("pr_url")
                       or (f"https://github.com/{repo_full}/pull/{number}" if repo_full else None),
                "repo": repo_full,
                "number": number,
            }

            pr_codes = set(resp.get("queue_codes") or [])
            for txn_code, group_codes in codes_by_group.items():
                if pr_codes & group_codes:
                    stages_by_group[txn_code].append(stage)
                    # 'group' names the RESOURCE, so it is per-group, not per-PR:
                    # a service's infra + k8s + service PRs are all "service",
                    # and the SAME shared terragrunt PR is "bucket" on the
                    # bucket's row and "queue" on the queue's.
                    prs_by_group[txn_code].append(
                        {**entry, "group": group_labels[txn_code]}
                    )

        for txn_code, track_id in tracks.items():
            # 'infra: create pr' was opened as running, so writing it success
            # closes it. A resource whose terragrunt had no diff produced no PR
            # at all — close it success anyway: nothing to review is not a
            # failure. k8s/service stages only appear when their PR exists,
            # rather than showing a phantom failure for a PR never expected.
            for stage in dict.fromkeys(stages_by_group[txn_code]):
                if stage != "infra: create pr":
                    await self._write_pr_stage(track_id, stage, "success")
            await self._write_pr_stage(track_id, "infra: create pr", "success")
            await self._append_deploy_result_prs(track_id, prs_by_group[txn_code])
            try:
                repo = PipelineRunTrackRepository(self.db)
                for row in await repo.get_by_vendor_deployment_id(track_id):
                    if row.status != PipelineRunStatusEnum.FAILED:
                        await repo.update(code=row.code, status=PipelineRunStatusEnum.COMPLETED)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "create-pr run-track %s could not be completed: %s", track_id, exc
                )

        return result

    async def _record_gateway_routes_for_batch(self, groups) -> None:
        """Write this batch's gateway snapshots into kong_route_groups /
        kong_route_configs at PR_CREATED, once their pull requests exist.

        The prod counterpart of the deployment workflow's
        record_gateway_routes_pr_created step, and for the same reason: a raised
        PR is the outcome devlift produces on prod — merging and applying it is
        manual work someone does afterwards on their own schedule — so a write
        that waited for the merge would never happen. This path does not run the
        Temporal workflow at all, so without this nothing ever recorded a prod
        gateway change; the tables showed stage's routes and none of prod's.

        No environment filter, unlike the workflow's prod-only pass. That pass
        can afford to defer other environments because their PR is merged inside
        the same deploy and update_queue_status records them then. Nothing
        merges on this path, so deferring here means never — whatever went out
        through this entry point is recorded here regardless of environment.

        PR_CREATED, not ACTIVE: a PR that is closed instead of merged leaves
        these rows in place and no reader counts them as live. A later merge
        through the workflow promotes them, and the write is idempotent.

        Non-fatal and loud. By the time this runs the PR is raised and the change
        is out, so failing the call would not un-ship anything — it would turn a
        missing record into a failed create-PR run for every unrelated resource
        in the batch. Its own commit/rollback, so a failure cannot take the
        settings save above down with it.
        """
        from sqlalchemy import select

        from app.core.enum import DeploymentStatusEnum
        from app.db.models.transaction_queue_model import TransactionQueueModel
        # Reused rather than reimplemented: that helper is the only writer of
        # those tables, and a second copy here would be a second thing to keep in
        # step with the snapshot format. It is a plain async function, not the
        # activity around it — and its activity.logger degrades to a plain line
        # outside an activity context, so calling it from here is safe.
        from app.temporal.activities.deploy_activities import (
            _write_deployed_gateway_routes,
        )

        # Only the items that actually reached a PR. create() has already
        # flushed its queue-status decisions by now, so PR_RAISED on the row is
        # the record of which ones did — an item whose terragrunt had no diff
        # produced no PR and must not be recorded as shipped.
        from app.db.models.transaction_queue_model import is_gateway_row

        gateway_ids = [
            item.id
            for items in groups.values()
            for item in items
            if is_gateway_row(item)
        ]
        if not gateway_ids:
            return

        raised_ids = list((await self.db.execute(
            select(TransactionQueueModel.id).where(
                TransactionQueueModel.id.in_(gateway_ids),
                TransactionQueueModel.status == TransactionQueueStatusEnum.PR_RAISED,
                TransactionQueueModel.is_deleted.isnot(True),
            )
        )).scalars().all())
        if not raised_ids:
            self.logger.info(
                "gateway routes: none of %d gateway queue row(s) reached a PR — "
                "nothing recorded", len(gateway_ids),
            )
            return

        try:
            await _write_deployed_gateway_routes(
                self.db, raised_ids,
                creation_status=DeploymentStatusEnum.PR_CREATED,
            )
            await self.db.commit()
        except Exception as exc:  # noqa: BLE001 — never fail a raised PR
            await self.db.rollback()
            self.logger.error(
                "gateway route write FAILED for queue %s — the PR is raised and "
                "the change is out, but devlift has no record of these routes: %s",
                raised_ids, exc, exc_info=True,
            )

    async def _save_settings_for_batch(self, tenant_code: str, groups) -> int:
        """Write each shipped queue item's approved snapshot into its live row.

        Save parked the change in config_snapshot and wrote nothing live, so
        that row still describes what is really running — an honest baseline for
        the next diff. This is what closes the gap once the change is out;
        without it the settings screen would keep showing the old values however
        many times the change deployed.

        Committed here rather than left to the caller: the run-track writes that
        follow use their own sessions, so this session has no later commit to
        ride along with.

        Returns how many rows were actually written.
        """
        from app.handlers.resource_settings_saver_handler import (
            ResourceSettingsSaverHandler,
        )

        applied = 0
        for item in (i for group_items in groups.values() for i in group_items):
            queue_dict = {
                "id": item.id,
                "code": item.code,
                "tenant_code": item.tenant_code,
                "transaction_code": item.transaction_code,
                "case_ref_code": item.case_ref_code,
                "table_name": item.table_name,
                "config_snapshot": item.config_snapshot,
            }
            # The handler swallows and logs component failures: by now the PR is
            # raised and the change is out, so a stale live row is something to
            # chase, not a reason to fail a deploy that already happened.
            if await ResourceSettingsSaverHandler.run(
                tenant=tenant_code, queue_dict=queue_dict, db=self.db
            ):
                applied += 1

        if applied:
            try:
                await self.db.commit()
                self.logger.info(
                    "create-pr: saved %s approved snapshot(s) to their live rows",
                    applied,
                )
            except Exception as exc:  # noqa: BLE001
                await self.db.rollback()
                self.logger.error(
                    "create-pr: approved snapshots could not be committed — the "
                    "live records are now stale: %s", exc, exc_info=True,
                )
                return 0
        return applied

    #: case_ref_code → run-track PR group. The group names the RESOURCE a PR
    #: belongs to, not the artifact: a service's terragrunt, k8s-manifest and
    #: workflow PRs are all group "service" — 'name' (INFRA PR / K8S PR /
    #: SERVICE PR) is what tells those apart.
    #:
    #: Keyed on case_ref_code rather than table_name because table_name is too
    #: coarse here: a bucket, a queue and a dynamo table are all INFRASTRUCTURE,
    #: which is fine for Temporal (one resource per run) but not for create-PR,
    #: where one batch spans several. The verb position varies
    #: (create_bucket / database_creation / add_route), so stripping it is not
    #: reliable — hence an explicit table.
    _PR_GROUP_BY_CASE_REF = {
        "update_service": "service",
        "add_route": "kong",
        "create_bucket": "bucket",
        "create_queue": "queue",
        "database_creation": "database",
        "delete_dynamodb_table": "dynamo",
        "create_dynamodb_table": "dynamo",
    }

    @classmethod
    def _pr_group_label(cls, item) -> str:
        """Resource group for every PR raised for this queue item.

        Unknown case_ref_codes fall back to the rule multiple_deploy_activities
        ._pr_group_label uses, so a new resource type reads as its table name
        instead of crashing or writing something meaningless.
        """
        case_ref = (getattr(item, "case_ref_code", "") or "").strip()
        if case_ref in cls._PR_GROUP_BY_CASE_REF:
            return cls._PR_GROUP_BY_CASE_REF[case_ref]
        tbl = getattr(item, "table_name", None)
        tbl_val = tbl.value if hasattr(tbl, "value") else str(tbl or "")
        return {"KONG_ROUTE": "kong", "SERVICE_CONFIG": "service"}.get(
            tbl_val, tbl_val.lower() or "service"
        )

    @staticmethod
    def _repo_full_name_from_pr(pr_info: Dict) -> Optional[str]:
        """'owner/repo' from a PR url — the same parse deploy_activities does,
        since the PR payload carries the url but not the full repo name."""
        url = pr_info.get("url") or pr_info.get("html_url") or pr_info.get("pr_url")
        if not url:
            return None
        parts = str(url).rstrip("/").split("/")
        idx = next((i for i, p in enumerate(parts) if p == "pull"), -1)
        return f"{parts[idx - 2]}/{parts[idx - 1]}" if idx >= 2 else None

    async def create(
        self,
        user_code: str,
        tenant_code: str,
        queue_ids: Optional[List[int]] = None,
        all_pending_queues: bool = False
    ) -> Dict:
        """
        Generate script preview for multiple queue items.

        Uses PRWorkflowContext to aggregate file locations and scripts across all queues.
        Feature branches are automatically managed per (repo, base_branch) combination.

        Args:
            user_code: User code
            tenant_code: Tenant code
            queue_ids: Optional list of specific queue IDs to process
            all_pending_queues: If True, process all pending queues for the user

        Returns:
            Dictionary with structure:
            {
                'feature_branches': {'{repo}-{base_branch}': 'feature_branch_name', ...},
                'file_location_responses': {queue_id: response, ...},
                'script_gen_responses': {queue_id: response, ...}
            }

        Raises:
            ValueError: If neither queue_ids nor all_pending_queues is provided, or if queues not found
        """
        overall_start = time.monotonic()
        mode_label = "ALL_PENDING_QUEUES" if all_pending_queues else "SELECTED_QUEUES"
        queue_ids_label = queue_ids if queue_ids else "ALL_PENDING"
        self.logger.info(
            "PR workflow start user=%s tenant=%s mode=%s queue_ids=%s",
            user_code,
            tenant_code,
            mode_label,
            queue_ids_label
        )

        # Step 1: Validate input - must provide either queue_ids or all_pending_queues
        if not queue_ids and not all_pending_queues:
            self.logger.error("❌ Validation failed: Must provide either queue_ids or all_pending_queues")
            raise ValueError("Must provide either queue_ids or set all_pending_queues=True")

        # ========================================================================
        # STEP 1: FETCH QUEUE ITEMS
        # ========================================================================
        fetch_start = time.monotonic()

        # Step 2: Fetch queue items based on input
        if all_pending_queues:
            # Get all pending queues for the user
            # TODO: need to validate this status filteration logic
            queue_items = await self.queue_repo.get_all_queues_for_user(
                user_code=user_code,
                tenant_code=tenant_code,
                status=TransactionQueueStatusEnum.APPROVED
            )
        else:
            # Get specific queues by IDs.
            # Accept APPROVED and STARTING_DEPLOYMENT — the workflow sets
            # STARTING_DEPLOYMENT before calling this activity.
            queue_items = await self.queue_repo.get_selected_queues(
                user_code=user_code,
                tenant_code=tenant_code,
                selected_queue_ids=queue_ids,
                statuses=[
                    TransactionQueueStatusEnum.APPROVED,
                    TransactionQueueStatusEnum.STARTING_DEPLOYMENT,
                ]
            )

        if not queue_items:
            self.logger.error("❌ No queue items found matching the criteria")
            raise ValueError("No queue items found matching the criteria")

        fetch_ms = int((time.monotonic() - fetch_start) * 1000)
        self.logger.info(
            "PR workflow fetched queues count=%s ms=%s",
            len(queue_items),
            fetch_ms
        )

        # ========================================================================
        # STEP 1.5: SYNC DEFAULT-ROLE SECRET POLICY
        # ========================================================================
        # Heal drifted TenantDefaultSecretsRead for redeploys: re-grant the
        # tenant default-role access to every secret owned by the services in
        # this batch. Idempotent — no-op when the ARN is already present.
        # Runs inside devlift-secret-config-manager (variable-mst-isolation-spec):
        # its verbatim copy of sync_default_role_for_transaction reads the
        # SECRET ARNs and re-grants the default role, so the ARNs never
        # transit obs_tool.
        from app.integrations.secret_config_client import SecretConfigClient
        secret_config_client = SecretConfigClient()
        sync_targets = {
            WorkflowSourceTableEnum.SERVICE_CONFIG,
            WorkflowSourceTableEnum.INFRASTRUCTURE,
        }
        for item in queue_items:
            if item.table_name not in sync_targets or not item.transaction_code:
                continue
            source_entity = getattr(item, "source_entity", None)
            env_enum = getattr(source_entity, "environments_enum", None) if source_entity else None
            if env_enum is None:
                continue
            await secret_config_client.sync_default_role(
                tenant_code=tenant_code,
                table_name=item.table_name,
                transaction_code=item.transaction_code,
                environment=env_enum,
            )

        # ========================================================================
        # STEP 2: INITIALIZE WORKFLOW CONTEXT
        # ========================================================================
        init_start = time.monotonic()
        file_locator = FileLocationHandler()
        script_gen = ScriptGenHandler()
        workflow_context = PRWorkflowContext()  # Create context for aggregation
        init_ms = int((time.monotonic() - init_start) * 1000)
        self.logger.info("PR workflow init done ms=%s", init_ms)

        # ========================================================================
        # STEP 3: PROCESS QUEUE ITEMS AND GENERATE SCRIPTS
        # ========================================================================
        self.logger.info("PR workflow processing queue items count=%s", len(queue_items))

        for idx, queue_item in enumerate(queue_items, start=1):
            try:
                item_start = time.monotonic()

                # Extract placement parameters from source_entity (if available)
                environment = None
                geo_loc_mst_code = None
                infra_vendor_accounts_mst_code = None

                if hasattr(queue_item, 'source_entity') and queue_item.source_entity:
                    source_entity = queue_item.source_entity
                    # Extract placement params from joined table
                    if hasattr(source_entity, 'environments_enum'):
                        environment = source_entity.environments_enum.value if hasattr(source_entity.environments_enum, 'value') else str(source_entity.environments_enum)
                    if hasattr(source_entity, 'geo_loc_mst_code'):
                        geo_loc_mst_code = source_entity.geo_loc_mst_code
                    if hasattr(source_entity, 'infra_vendor_accounts_mst_code'):
                        infra_vendor_accounts_mst_code = source_entity.infra_vendor_accounts_mst_code

                # Convert queue_item to dict for handlers, including placement parameters
                queue_dict = {
                    'id': queue_item.id,
                    'code': queue_item.code,
                    'user_code': queue_item.user_code,
                    'transaction_code': queue_item.transaction_code,
                    'case_ref_code': queue_item.case_ref_code,
                    'table_name': queue_item.table_name,
                    'config_snapshot': queue_item.config_snapshot,
                    'tenant_code': queue_item.tenant_code,
                    'status': queue_item.status.value if hasattr(queue_item.status, 'value') else queue_item.status,
                    # Placement parameters from joined source table
                    'environment': environment or "",
                    'geo_loc_mst_code': geo_loc_mst_code or "",
                    'infra_vendor_accounts_mst_code': infra_vendor_accounts_mst_code or ""
                }
                await self._apply_gateway_routing(queue_item, queue_dict)

                # ────────────────────────────────────────────────────────────
                # PRE-ACTION (per-queue): flips the referenced resource's
                # status (INITIALISING / SOFT_DELETING per case_ref_code) so
                # the canvas sees the in-progress state before the heavy
                # pipeline work begins.
                # ────────────────────────────────────────────────────────────
                await ResourcePreActionHandler.run(
                    tenant=tenant_code,
                    queue_dict=queue_dict,
                    db=self.db,
                )

                # Enrich config_snapshot with infrastructure values (tenant-specific)
                # This is required for workflow generation to have ECR repository, ECS service name, etc.
                if queue_item.config_snapshot and queue_item.config_snapshot.get('infrastructure_mst_code'):
                    # Use tenant-specific enrichment logic
                    if queue_item.tenant_code in ['aspora', 'vance']:
                        from app.plugin.aspora.config_enrichment import enrich_config_with_infrastructure_values
                        await enrich_config_with_infrastructure_values(
                            db=self.db,
                            config_snapshot=queue_item.config_snapshot,
                            tenant_code=queue_item.tenant_code
                        )
                    # Other tenants: enrich repository/branches from service_config DB record
                    elif (
                        queue_item.config_snapshot.get('infrastructuretype_ref_code') == 'eks_infrastructuretype_ref'
                        and not queue_item.config_snapshot.get('repository')
                        and queue_item.transaction_code
                    ):
                        try:
                            from app.repository.service_config_repository import ServiceConfigRepository
                            svc_cfg_repo = ServiceConfigRepository(self.db)
                            svc_cfg = await svc_cfg_repo.get_by_code_and_tenant(
                                queue_item.transaction_code, queue_item.tenant_code
                            )
                            if svc_cfg and svc_cfg.config:
                                db_cfg = svc_cfg.config
                                if not queue_item.config_snapshot.get('repository'):
                                    queue_item.config_snapshot['repository'] = db_cfg.get('repository', '')
                                if not queue_item.config_snapshot.get('branches') and not queue_item.config_snapshot.get('selected_branches'):
                                    queue_item.config_snapshot['branches'] = (
                                        db_cfg.get('selected_branches') or db_cfg.get('branches', [])
                                    )
                        except Exception as _exc:
                            logger.warning("Default EKS enrichment failed: %s", _exc)

                # Store config_snapshot in workflow context for PR body generation
                if queue_item.id and queue_item.config_snapshot:
                    workflow_context.config_snapshots[queue_item.id] = queue_item.config_snapshot

                # Store queue metadata for linking gitops_workflow_detail to source entities
                if queue_item.code:
                    workflow_context.queue_metadata[queue_item.code] = {
                        'transaction_code': queue_item.transaction_code,
                        'table_name': queue_item.table_name,
                        'ticket_code': queue_item.ticket_code,
                        # "<route_group_key> · GET — 2 changes" for a gateway row.
                        # The PR card names the group from this; nothing else in the
                        # body path can, since the row points at a group code and
                        # stores only a change set.
                        'display_name': queue_item.display_name,
                    }

                # ====================================================================
                # STEP 3a: FILE LOCATION DETERMINATION
                # ====================================================================
                locate_start = time.monotonic()
                try:
                    await file_locator.locate(
                        tenant_code,
                        queue_dict,
                        workflow_context  # Pass context - gets updated by reference
                    )
                except Exception as e:
                    self.logger.error(
                        f"   ❌ File location determination failed for queue_id={queue_item.id}: {e}",
                        exc_info=True
                    )
                    raise

                # Get file locations from workflow context. An empty file list
                # is valid for post-action-only case_refs (e.g. `delete_bucket`)
                # where the queue item exists solely to drive the post-action.
                file_location_response: Optional[FileLocationResponse] = workflow_context.file_location_responses.get(queue_item.id)
                if not file_location_response:
                    error_msg = f"No file location response for queue_id={queue_item.id}"
                    self.logger.error(f"   ❌ {error_msg}")
                    raise ValueError(error_msg)
                files = file_location_response.files or []

                locate_ms = int((time.monotonic() - locate_start) * 1000)

                # ====================================================================
                # STEP 3b: SCRIPT GENERATION
                # ====================================================================
                gen_start = time.monotonic()
                try:
                    for file_idx, file_loc in enumerate(files, 1):
                        await script_gen.generate_script(
                            tenant=tenant_code,
                            queue_dict=queue_dict,
                            file_location=file_loc,
                            workflow_context=workflow_context,  # Pass context - gets updated by reference
                            gitops_queue_repo=self.queue_repo,
                            db=self.db,
                        )

                        # Note: Script responses are stored directly by components in workflow_context

                    # Run post-action once per queue item, dispatched by case_ref_code
                    # (silently skips queue items whose case_ref_code has no registered component)
                    await ResourcePostActionHandler.run(
                        tenant=tenant_code,
                        queue_dict=queue_dict,
                        workflow_context=workflow_context,
                        db=self.db,
                    )

                except Exception as e:
                    self.logger.error(
                        f"   ❌ Script generation failed for queue_id={queue_item.id}: {e}",
                        exc_info=True
                    )
                    raise
                gen_ms = int((time.monotonic() - gen_start) * 1000)
                item_ms = int((time.monotonic() - item_start) * 1000)
                self.logger.info(
                    "PR workflow queue done queue_id=%s files=%s locate_ms=%s gen_ms=%s total_ms=%s",
                    queue_item.id,
                    len(files),
                    locate_ms,
                    gen_ms,
                    item_ms
                )

            except Exception as e:
                self.logger.error(
                    f"❌ Unexpected error processing queue_id={queue_item.id}: {e}",
                    exc_info=True
                )
                raise

        # ====================================================================
        # STEP 3c-J: PROVISION JENKINS PIPELINES (for ci_provider="jenkins")
        # ====================================================================
        # For Jenkins tenants, no files are staged (file locator skips them).
        # Provision Jenkins pipeline jobs directly from config_snapshot.
        await self._provision_jenkins_pipelines_from_queue(
            queue_items=queue_items,
            workflow_context=workflow_context,
        )

        # ====================================================================
        # STEP 3c-K: APPLY K8s JOBS (for mode="kubectl")
        # ====================================================================
        # One-time operational jobs (CREATE DATABASE, CREATE USER, etc.) are
        # applied directly to the cluster — no Jenkins pipeline involved.
        await self._apply_k8s_jobs_from_queue(
            queue_items=queue_items,
            workflow_context=workflow_context,
        )

        # ====================================================================
        # STEP 3c: CREATE COMMITS (BATCHED PER REPO/BRANCH)
        # ====================================================================
        self.logger.info(
            "PR workflow commit start staged_files=%s",
            len(workflow_context.staged_files)
        )

        if not workflow_context.staged_files:
            self.logger.info("ℹ️  No staged files to commit")
        else:
            staged_by_repo_branch: Dict[str, Dict[str, Any]] = {}
            for entry in workflow_context.staged_files:
                # Skip pure Jenkins-mode entries (no repo/file_path — handled by inline provisioning).
                # Jenkins entries WITH repo/file_path are also committed to git for DR.
                if entry.get("mode") == "jenkins" and not entry.get("repo"):
                    continue

                repo = entry.get("repo")
                base_branch = entry.get("base_branch")
                feature_branch = entry.get("feature_branch")
                file_path = entry.get("file_path")
                content = entry.get("content")

                if not repo or not base_branch or not file_path:
                    self.logger.warning(
                        "Skipping staged file with missing data: %s",
                        entry
                    )
                    continue

                key = f"{repo}|||{base_branch}"
                if key not in staged_by_repo_branch:
                    staged_by_repo_branch[key] = {
                        "repo": repo,
                        "base_branch": base_branch,
                        "feature_branch": feature_branch,
                        "files": {}
                    }

                if feature_branch and not staged_by_repo_branch[key].get("feature_branch"):
                    staged_by_repo_branch[key]["feature_branch"] = feature_branch

                staged_by_repo_branch[key]["files"][file_path] = content

            for repo_branch_key, data in staged_by_repo_branch.items():
                repo = data["repo"]
                base_branch = data["base_branch"]
                _fb_val = data.get("feature_branch") or workflow_context.feature_branches.get(repo_branch_key)
                feature_branch = _fb_val["branch"] if isinstance(_fb_val, dict) else _fb_val

                if not feature_branch:
                    self.logger.error(
                        "Missing feature branch for %s; cannot commit staged files",
                        repo_branch_key
                    )
                    raise ValueError(f"Missing feature branch for {repo_branch_key}")

                files_to_commit = [
                    {"path": path, "content": content}
                    for path, content in data["files"].items()
                ]
                if not files_to_commit:
                    continue

                commit_message = workflow_context.commit_messages.get(repo_branch_key)
                if not commit_message:
                    commit_message = f"Update files for {repo}:{base_branch}"

                repo_parts = repo.split('/')
                owner = repo_parts[0] if len(repo_parts) > 1 else None
                repo_name = repo_parts[1] if len(repo_parts) > 1 else repo

                commit_start = time.monotonic()
                await GitOpsHandler.create_commit(
                    tenant=tenant_code,
                    owner=owner,
                    repo=repo_name,
                    base_branch=base_branch,
                    feature_branch=feature_branch,
                    files=files_to_commit,
                    commit_message=commit_message,
                    workflow_context=workflow_context,
                    db=self.db,
                )
                commit_ms = int((time.monotonic() - commit_start) * 1000)
                self.logger.info(
                    "PR workflow commit done repo=%s base_branch=%s files=%s ms=%s",
                    repo,
                    base_branch,
                    len(files_to_commit),
                    commit_ms
                )

        # ========================================================================
        # STEP 4: CREATE FEATURE BRANCHES AND PRS
        # ========================================================================
        self.logger.info(
            "PR workflow PR start branches=%s",
            len(workflow_context.feature_branches)
        )

        for idx, (repo_branch_key, fb_data) in enumerate(workflow_context.feature_branches.items(), 1):
            feature_branch = fb_data["branch"] if isinstance(fb_data, dict) else fb_data
            pr_type = fb_data.get("type", "infrastructure") if isinstance(fb_data, dict) else "infrastructure"
            try:
                pr_start = time.monotonic()
                # Create workflow, mappings, and PR
                await self._create_workflow_and_pr(
                    repo_branch_key=repo_branch_key,
                    feature_branch=feature_branch,
                    pr_type=pr_type,
                    tenant_code=tenant_code,
                    user_code=user_code,
                    workflow_context=workflow_context
                )
                pr_ms = int((time.monotonic() - pr_start) * 1000)
                self.logger.info(
                    "PR workflow PR done key=%s feature_branch=%s ms=%s",
                    repo_branch_key,
                    feature_branch,
                    pr_ms
                )

            except Exception as e:
                self.logger.error(
                    f"   ❌ Workflow creation failed for {repo_branch_key}: {e}",
                    exc_info=True
                )
                raise

        # ========================================================================
        # STEP 5: PaaS — auto-merge infra repo PRs (for disaster recovery copies)
        # Also fires per-resource + IAM Jenkins infra-apply pipelines after merge.
        # ========================================================================
        await self._auto_merge_paas_infra_prs(tenant_code, workflow_context, queue_items)

        # ========================================================================
        # STEP 6: TRIGGER JENKINS BUILDS (after manifests are merged into infra repo)
        # ========================================================================
        await self._trigger_jenkins_builds_post_merge(workflow_context)

        # ========================================================================
        # STEP 7: FLUSH QUEUE STATUS DECISIONS
        # Single point where every staged queue-status / soft-delete decision
        # made earlier in the workflow lands in the DB. One commit for all.
        # ========================================================================
        await self._apply_queue_status_updates(workflow_context)

        # ========================================================================
        # WORKFLOW COMPLETION SUMMARY
        # ========================================================================
        total_ms = int((time.monotonic() - overall_start) * 1000)
        prs_created = len([v for v in workflow_context.gitops_responses.values() if 'pr' in v])
        self.logger.info(
            "PR workflow done queues=%s branches=%s filesets=%s prs=%s total_ms=%s",
            len(queue_items),
            len(workflow_context.feature_branches),
            len(workflow_context.file_location_responses),
            prs_created,
            total_ms
        )

        # Return aggregated results from workflow context
        return {
            'feature_branches': workflow_context.feature_branches,
            'file_location_responses': workflow_context.file_location_responses,
            'script_gen_responses': workflow_context.script_gen_responses,
            'gitops_responses': workflow_context.gitops_responses,
            'jenkins_results': getattr(workflow_context, 'jenkins_results', []),
            'infra_apply_results': getattr(workflow_context, 'infra_apply_results', []),
            'kubectl_results': workflow_context.kubectl_results,
        }

    async def _get_github_token(self, owner: str) -> str:
        """Get GitHub App installation token for the given org."""
        from app.utils.github_app_token import get_token_for_org
        return await get_token_for_org(owner, self.db)

    async def preview_by_queue_id(self, queue_id: int, user_code: str, tenant_code: str) -> Dict:
        """
        Preview scripts for a single queue item without creating feature branches or PRs.

        This is a simplified preview that only generates script content.
        No feature branches are created, no database records are saved.

        Args:
            queue_id: Queue item ID to preview
            user_code: User code for authorization
            tenant_code: Tenant code for authorization

        Returns:
            Dictionary with script_gen_responses containing generated scripts

        Raises:
            ValueError: If queue item not found or invalid
        """
        from app.db.models.transaction_queue_model import TransactionQueueStatusEnum

        # Fetch the queue item
        queue_items = await self.queue_repo.get_selected_queues(
            user_code=user_code,
            tenant_code=tenant_code,
            selected_queue_ids=[queue_id],
            # status=TransactionQueueStatusEnum.APPROVED
        )
        # Log fetched queue items with joined data
        self.logger.info(f"Fetched {len(queue_items)} queue items from get_selected_queues (preview)")
        for idx, item in enumerate(queue_items):
            self.logger.info("#####################")
            self.logger.info(f"QUEUE ITEM [{idx}] - ALL FIELDS AND VALUES")
            self.logger.info("#####################")

            # Log all queue item fields
            for attr_name in dir(item):
                # Skip private/magic methods and SQLAlchemy internals
                if not attr_name.startswith('_') and attr_name not in ['metadata', 'registry']:
                    try:
                        attr_value = getattr(item, attr_name)
                        # Skip methods/functions
                        if not callable(attr_value):
                            self.logger.info(f"  {attr_name}: {attr_value}")
                    except Exception as e:
                        self.logger.debug(f"  {attr_name}: <Error accessing: {e}>")

            # Log source_entity details if present
            if hasattr(item, 'source_entity') and item.source_entity:
                self.logger.info("  ---")
                self.logger.info("  SOURCE_ENTITY (Joined Table Data):")
                source_entity = item.source_entity
                self.logger.info(f"    Type: {type(source_entity).__name__}")

                # Log all source_entity fields
                for attr_name in dir(source_entity):
                    if not attr_name.startswith('_') and attr_name not in ['metadata', 'registry']:
                        try:
                            attr_value = getattr(source_entity, attr_name)
                            if not callable(attr_value):
                                self.logger.info(f"    {attr_name}: {attr_value}")
                        except Exception as e:
                            self.logger.debug(f"    {attr_name}: <Error accessing: {e}>")
            else:
                self.logger.info("  source_entity: None (no joined data)")

            self.logger.info("#####################")

        if not queue_items:
            raise ValueError(f"Queue item {queue_id} not found")

        queue_item = queue_items[0]

        self.logger.info(f"Generating preview for queue_id={queue_item.id}, code={queue_item.code}")
        self.logger.info(f"Queue item details: table_name={queue_item.table_name}, transaction_code={queue_item.transaction_code}, case_ref_code={queue_item.case_ref_code}")
        self.logger.info(f"Config snapshot keys: {list(queue_item.config_snapshot.keys()) if queue_item.config_snapshot else 'None'}")

        # Initialize handlers and workflow context (skip commits for preview)
        file_locator = FileLocationHandler()
        script_gen = ScriptGenHandler()
        workflow_context = PRWorkflowContext(skip_commit=True)  # Skip Git commits

        # Extract placement parameters from source_entity (if available)
        environment = None
        geo_loc_mst_code = None
        infra_vendor_accounts_mst_code = None

        if hasattr(queue_item, 'source_entity') and queue_item.source_entity:
            source_entity = queue_item.source_entity
            # Extract placement params from joined table
            if hasattr(source_entity, 'environments_enum'):
                environment = source_entity.environments_enum.value if hasattr(source_entity.environments_enum, 'value') else str(source_entity.environments_enum)
            if hasattr(source_entity, 'geo_loc_mst_code'):
                geo_loc_mst_code = source_entity.geo_loc_mst_code
            if hasattr(source_entity, 'infra_vendor_accounts_mst_code'):
                infra_vendor_accounts_mst_code = source_entity.infra_vendor_accounts_mst_code

        self.logger.info(f"Extracted placement parameters: environment={environment}, geo_loc_mst_code={geo_loc_mst_code}, infra_vendor_accounts_mst_code={infra_vendor_accounts_mst_code}")

        # Convert queue_item to dict for handlers, including placement parameters
        queue_dict = {
            'id': queue_item.id,
            'code': queue_item.code,
            'user_code': queue_item.user_code,
            'transaction_code': queue_item.transaction_code,
            'case_ref_code': queue_item.case_ref_code,
            'table_name': queue_item.table_name,
            'config_snapshot': queue_item.config_snapshot,
            'tenant_code': queue_item.tenant_code,
            'status': queue_item.status.value if hasattr(queue_item.status, 'value') else queue_item.status,
            # Placement parameters from joined source table
            'environment': environment or "",
            'geo_loc_mst_code': geo_loc_mst_code or "",
            'infra_vendor_accounts_mst_code': infra_vendor_accounts_mst_code or ""
        }
        await self._apply_gateway_routing(queue_item, queue_dict)

        # Enrich config_snapshot with infrastructure values (tenant-specific)
        # This is required for workflow generation to have ECR repository, ECS service name, etc.
        if queue_item.config_snapshot and queue_item.config_snapshot.get('infrastructure_mst_code'):
            # Use tenant-specific enrichment logic
            if queue_item.tenant_code in ['aspora', 'vance']:
                from app.plugin.aspora.config_enrichment import enrich_config_with_infrastructure_values
                await enrich_config_with_infrastructure_values(
                    db=self.db,
                    config_snapshot=queue_item.config_snapshot,
                    tenant_code=queue_item.tenant_code
                )
            # Other tenants can add their own enrichment logic here

        # Store config_snapshot in workflow context for PR body generation
        if queue_item.id and queue_item.config_snapshot:
            workflow_context.config_snapshots[queue_item.id] = queue_item.config_snapshot

        # Step 1: Call FileLocationHandler
        try:
            await file_locator.locate(
                queue_item.tenant_code,
                queue_dict,
                workflow_context
            )
        except Exception as e:
            self.logger.error(f"File location determination failed for queue_id={queue_item.id}: {e}", exc_info=True)
            raise

        # Get file locations from workflow context
        file_location_response = workflow_context.file_location_responses.get(queue_item.id)
        if not file_location_response or not file_location_response.files:
            raise ValueError(f"No file locations determined for queue_id={queue_item.id}")

        # Step 2: Call ScriptGenHandler
        try:
            for file_loc in file_location_response.files:
                self.logger.info(
                    f"Generating script for queue_id={queue_item.id}, "
                    f"repo={file_loc.repo}, branch={file_loc.base_branch}, path={file_loc.file_path}"
                )

                await script_gen.generate_script(
                    tenant=queue_item.tenant_code,
                    queue_dict=queue_dict,
                    file_location=file_loc,
                    workflow_context=workflow_context,
                    gitops_queue_repo=self.queue_repo,
                    # Same reason as the deploy path — the preview would otherwise
                    # render v1's output for an item v2 owns.
                    db=self.db,
                    # A preview is a READ. Without this it re-uploaded the
                    # generated file to S3 and rewrote script_access_key — so
                    # merely LOOKING at a draft's preview overwrote the
                    # artifact that represented the deployed content.
                    upload_to_s3=False,
                )

            self.logger.info(f"Successfully generated preview for queue_id={queue_item.id}")

        except Exception as e:
            self.logger.error(f"Script generation failed for queue_id={queue_item.id}: {e}", exc_info=True)
            raise

        # Return only the script responses (no feature branches, no gitops responses)
        return {
            'queue_id': queue_id,
            'script_gen_responses': workflow_context.script_gen_responses,
            'file_location_responses': workflow_context.file_location_responses
        }

    async def conflict_resolve(
        self,
        queue_codes: List[str],
        pr_number: int,
        git_repository: str,
        feature_branch: str,
        user_code: str,
        tenant_code: str
    ) -> Dict:
        """
        Resolve conflicts for an existing PR by rebasing and regenerating files.

        Flow:
        1. Fetch PR info from GitHub → validate state, get base_branch
        2. Fetch queue items with source_entity from DB using queue_codes
        3. Get latest base branch SHA and reset feature branch
        4. Process queue items → FileLocator → ScriptGen
        5. Commit staged files
        6. Reopen PR if auto-closed
        7. Update workflow records with new commit SHA

        Args:
            queue_codes: Queue codes to process (fetches items internally)
            pr_number: GitHub PR number
            git_repository: GitHub repository (e.g., "owner/repo")
            feature_branch: Existing feature branch name
            user_code: User code
            tenant_code: Tenant code

        Returns:
            Dict with conflict resolve result

        Raises:
            ValueError: If PR is merged, no queue items found, or operation fails
        """
        self.logger.info(
            "conflict_resolve start pr_number=%s git_repository=%s feature_branch=%s queue_codes=%s",
            pr_number, git_repository, feature_branch, queue_codes
        )

        # Parse owner/repo
        repo_parts = git_repository.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo_name = repo_parts[1] if len(repo_parts) > 1 else git_repository

        # ========================================================================
        # STEP 1: FETCH PR INFO FROM GITHUB & VALIDATE
        # ========================================================================
        pr_info = await GitOpsHandler.get_pull_request(
            tenant=tenant_code,
            owner=owner,
            repo=repo_name,
            pr_number=pr_number,
            db=self.db,
        )

        if pr_info.get("status") == "error":
            raise ValueError(f"Failed to fetch PR #{pr_number}: {pr_info.get('error')}")

        if pr_info.get("merged"):
            raise ValueError(f"Cannot resolve conflicts for merged PR #{pr_number}")

        base_branch = pr_info.get("base_branch")
        if not base_branch:
            raise ValueError(f"Could not determine base branch for PR #{pr_number}")

        self.logger.info(
            "conflict_resolve PR info state=%s base_branch=%s merged=%s",
            pr_info.get("state"), base_branch, pr_info.get("merged")
        )

        # ========================================================================
        # STEP 2: FETCH QUEUE ITEMS WITH SOURCE ENTITY
        # ========================================================================
        queue_items = await self.queue_repo.get_selected_queues_by_codes(
            queue_codes=queue_codes,
            tenant_code=tenant_code
        )

        if not queue_items:
            raise ValueError(f"No queue items found for codes: {queue_codes}")

        self.logger.info("conflict_resolve fetched queue items count=%s", len(queue_items))

        # ========================================================================
        # STEP 3: RESET FEATURE BRANCH TO LATEST BASE
        # ========================================================================
        base_sha = await GitOpsHandler.get_branch_sha(
            tenant=tenant_code,
            owner=owner,
            repo=repo_name,
            branch=base_branch,
            db=self.db,
        )

        if not base_sha:
            raise ValueError(f"Could not get SHA for base branch '{base_branch}'")

        is_branch_newly_created = False

        # ========================================================================
        # STEP 4: INITIALIZE WORKFLOW CONTEXT (CONFLICT RESOLVE MODE)
        # ========================================================================
        file_locator = FileLocationHandler()
        script_gen = ScriptGenHandler()
        workflow_context = PRWorkflowContext(
            skip_commit=False,
            is_conflict_resolve=True
        )

        # Pre-populate feature_branches with the target repo|||base_branch
        repo_branch_key = f"{git_repository}|||{base_branch}"
        workflow_context.feature_branches[repo_branch_key] = {"branch": feature_branch, "type": "infrastructure"}

        # ========================================================================
        # STEP 5: PROCESS QUEUE ITEMS (FileLocator → ScriptGen)
        # ========================================================================
        for queue_item in queue_items:
            try:
                # Extract placement parameters from source_entity
                environment = None
                geo_loc_mst_code = None
                infra_vendor_accounts_mst_code = None

                if hasattr(queue_item, 'source_entity') and queue_item.source_entity:
                    source_entity = queue_item.source_entity
                    if hasattr(source_entity, 'environments_enum'):
                        environment = source_entity.environments_enum.value if hasattr(source_entity.environments_enum, 'value') else str(source_entity.environments_enum)
                    if hasattr(source_entity, 'geo_loc_mst_code'):
                        geo_loc_mst_code = source_entity.geo_loc_mst_code
                    if hasattr(source_entity, 'infra_vendor_accounts_mst_code'):
                        infra_vendor_accounts_mst_code = source_entity.infra_vendor_accounts_mst_code

                # Convert queue_item to dict for handlers
                queue_dict = {
                    'id': queue_item.id,
                    'code': queue_item.code,
                    'user_code': queue_item.user_code,
                    'transaction_code': queue_item.transaction_code,
                    'case_ref_code': queue_item.case_ref_code,
                    'table_name': queue_item.table_name,
                    'config_snapshot': queue_item.config_snapshot,
                    'tenant_code': queue_item.tenant_code,
                    'status': queue_item.status.value if hasattr(queue_item.status, 'value') else queue_item.status,
                    'environment': environment or "",
                    'geo_loc_mst_code': geo_loc_mst_code or "",
                    'infra_vendor_accounts_mst_code': infra_vendor_accounts_mst_code or ""
                }
                await self._apply_gateway_routing(queue_item, queue_dict)

                # Enrich config_snapshot with infrastructure values (tenant-specific)
                if queue_item.config_snapshot and queue_item.config_snapshot.get('infrastructure_mst_code'):
                    if queue_item.tenant_code in ['aspora', 'vance']:
                        from app.plugin.aspora.config_enrichment import enrich_config_with_infrastructure_values
                        await enrich_config_with_infrastructure_values(
                            db=self.db,
                            config_snapshot=queue_item.config_snapshot,
                            tenant_code=queue_item.tenant_code
                        )

                # Store config_snapshot in workflow context
                if queue_item.id and queue_item.config_snapshot:
                    workflow_context.config_snapshots[queue_item.id] = queue_item.config_snapshot

                # Store queue metadata
                if queue_item.code:
                    workflow_context.queue_metadata[queue_item.code] = {
                        'transaction_code': queue_item.transaction_code,
                        'table_name': queue_item.table_name,
                        'ticket_code': queue_item.ticket_code,
                        # "<route_group_key> · GET — 2 changes" for a gateway row.
                        # The PR card names the group from this; nothing else in the
                        # body path can, since the row points at a group code and
                        # stores only a change set.
                        'display_name': queue_item.display_name,
                    }

                # File location determination
                try:
                    await file_locator.locate(
                        tenant_code,
                        queue_dict,
                        workflow_context
                    )
                except Exception as e:
                    self.logger.error(
                        f"conflict_resolve file location failed for queue_id={queue_item.id}: {e}",
                        exc_info=True
                    )
                    raise

                file_location_response: Optional[FileLocationResponse] = workflow_context.file_location_responses.get(queue_item.id)
                if not file_location_response or not file_location_response.files:
                    self.logger.warning(
                        "conflict_resolve no file locations for queue_id=%s (may be skipped for non-target repo)",
                        queue_item.id
                    )
                    continue

                # Script generation
                try:
                    for file_loc in file_location_response.files:
                        await script_gen.generate_script(
                            tenant=tenant_code,
                            queue_dict=queue_dict,
                            file_location=file_loc,
                            workflow_context=workflow_context,
                            gitops_queue_repo=self.queue_repo,
                            # REQUIRED, not optional. The dispatcher resolves an
                            # add_route item's generator by looking its
                            # transaction_code up in kong_route_groups, and v2 needs
                            # a session of its own. Without db it silently falls back
                            # to v1, which generates nothing for a group-keyed item —
                            # so the deploy "succeeds" having written no file and the
                            # queue row is binned as having no changes to commit.
                            db=self.db,
                        )
                except Exception as e:
                    self.logger.error(
                        f"conflict_resolve script generation failed for queue_id={queue_item.id}: {e}",
                        exc_info=True
                    )
                    raise

            except Exception as e:
                self.logger.error(
                    f"conflict_resolve error processing queue_id={queue_item.id}: {e}",
                    exc_info=True
                )
                raise

        # ========================================================================
        # STEP 6: CREATE COMMITS (BATCHED PER REPO/BRANCH)
        # ========================================================================
        if not workflow_context.staged_files:
            self.logger.info("conflict_resolve no staged files to commit")
        else:
            staged_by_repo_branch: Dict[str, Dict[str, Any]] = {}
            for entry in workflow_context.staged_files:
                repo = entry.get("repo")
                base_br = entry.get("base_branch")
                feat_br = entry.get("feature_branch")
                file_path = entry.get("file_path")
                content = entry.get("content")

                if not repo or not base_br or not file_path:
                    continue

                key = f"{repo}|||{base_br}"
                if key not in staged_by_repo_branch:
                    staged_by_repo_branch[key] = {
                        "repo": repo,
                        "base_branch": base_br,
                        "feature_branch": feat_br,
                        "files": {}
                    }

                if feat_br and not staged_by_repo_branch[key].get("feature_branch"):
                    staged_by_repo_branch[key]["feature_branch"] = feat_br

                staged_by_repo_branch[key]["files"][file_path] = content

            for key, data in staged_by_repo_branch.items():
                repo = data["repo"]
                base_br = data["base_branch"]
                _fb_val2 = data.get("feature_branch") or workflow_context.feature_branches.get(key)
                feat_br = _fb_val2["branch"] if isinstance(_fb_val2, dict) else _fb_val2

                if not feat_br:
                    raise ValueError(f"Missing feature branch for {key}")

                files_to_commit = [
                    {"path": path, "content": content}
                    for path, content in data["files"].items()
                ]
                if not files_to_commit:
                    continue

                commit_message = workflow_context.commit_messages.get(key)
                if not commit_message:
                    commit_message = f"Conflict resolve: update files for {repo}:{base_br}"

                r_parts = repo.split('/')
                r_owner = r_parts[0] if len(r_parts) > 1 else None
                r_name = r_parts[1] if len(r_parts) > 1 else repo

                try:
                    fc_result = await GitOpsHandler.force_commit_from_parent(
                        tenant=tenant_code,
                        owner=r_owner,
                        repo=r_name,
                        branch=feat_br,
                        parent_sha=base_sha,
                        files=files_to_commit,
                        message=commit_message,
                        db=self.db,
                    )
                    repo_key = f"{r_name}|||{base_br}"
                    if repo_key not in workflow_context.gitops_responses:
                        workflow_context.gitops_responses[repo_key] = {}
                    workflow_context.gitops_responses[repo_key]["commit"] = {
                        "status": "success" if fc_result.get("success") else "error",
                        "commit_sha": fc_result.get("commit_sha"),
                    }
                except Exception as fc_err:
                    # Branch might have been deleted — recreate from base then commit normally
                    self.logger.warning(
                        "conflict_resolve force_commit_from_parent failed for %s, recreating branch: %s",
                        feat_br, fc_err,
                    )
                    component = GitOpsHandler.get_component(tenant_code, self.db)
                    recreate_result = await component.create_branch(
                        owner=r_owner,
                        repo=r_name,
                        base_branch=base_br,
                        feature_branch=feat_br,
                    )
                    if recreate_result.get("status") == "error":
                        raise ValueError(
                            f"Failed to force-commit or recreate feature branch '{feat_br}': "
                            f"{recreate_result.get('error')}"
                        )
                    is_branch_newly_created = True
                    await GitOpsHandler.create_commit(
                        tenant=tenant_code,
                        owner=r_owner,
                        repo=r_name,
                        base_branch=base_br,
                        feature_branch=feat_br,
                        files=files_to_commit,
                        commit_message=commit_message,
                        workflow_context=workflow_context,
                        db=self.db,
                    )

        # ========================================================================
        # STEP 7: REOPEN PR OR CREATE NEW PR
        # ========================================================================
        needs_new_pr = is_branch_newly_created

        if not needs_new_pr:
            # Branch was reset (not recreated) — check if PR needs reopening
            pr_check = await GitOpsHandler.get_pull_request(
                tenant=tenant_code,
                owner=owner,
                repo=repo_name,
                pr_number=pr_number,
                db=self.db,
            )

            if pr_check.get("state") == "closed" and not pr_check.get("merged"):
                self.logger.info("conflict_resolve PR is closed, attempting to reopen...")
                reopen_result = await GitOpsHandler.update_pull_request(
                    tenant=tenant_code,
                    owner=owner,
                    repo=repo_name,
                    pr_number=pr_number,
                    db=self.db,
                    state="open",
                )

                if reopen_result.get("status") == "error":
                    # Reopen failed (e.g., PR was manually closed) — fall back to new PR
                    self.logger.warning(
                        "conflict_resolve reopen failed for PR #%s (%s), will create new PR",
                        pr_number, reopen_result.get("error")
                    )
                    needs_new_pr = True
                else:
                    # Reopen succeeded (auto-closed by force-push) — stage
                    # PR_RAISED for the bulk-applier at end of conflict_resolve.
                    self._stage_queue_status(
                        workflow_context,
                        queue_codes,
                        status=TransactionQueueStatusEnum.PR_RAISED,
                    )
                    self.logger.info(
                        "conflict_resolve staged %s queue items → PR_RAISED after reopen",
                        len(queue_codes),
                    )

                    from app.core.enum import PRStatusEnum
                    from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
                    reopen_workflow_repo = GitopsWorkflowDetailRepository(self.db)
                    updated_wf_count = await reopen_workflow_repo.update_pr_status_by_pr_and_repository(
                        pr_number=pr_number,
                        git_repository=git_repository,
                        new_status=PRStatusEnum.PR_OPEN
                    )
                    await self.db.commit()
                    self.logger.info(
                        "conflict_resolve updated %s workflow records to PR_OPEN after reopen",
                        updated_wf_count
                    )

        if needs_new_pr:
            # Branch was recreated or PR could not be reopened — create a new PR
            original_pr_number = pr_number
            reason = "branch was recreated" if is_branch_newly_created else "reopen failed"
            self.logger.info(
                "conflict_resolve %s, creating new PR instead of reopening old PR #%s",
                reason, original_pr_number
            )

            user_email = user_code
            try:
                user = await self.user_repo.get_by_code(user_code)
                if user and user.email_id:
                    user_email = user.email_id
            except Exception as e:
                self.logger.warning(f"Could not fetch user email for {user_code}: {e}")

            pr_title, pr_body = await self._generate_pr_title_and_body(
                workflow_context=workflow_context,
                repo=git_repository,
                base_branch=base_branch,
                feature_branch=feature_branch,
                user_email=user_email,
                tenant_code=tenant_code
            )
            pr_body += f"\n\n---\nRegenerated from conflict resolve of original PR #{original_pr_number}."

            new_pr_result = await GitOpsHandler.create_pr(
                tenant=tenant_code,
                owner=owner,
                repo=repo_name,
                base_branch=base_branch,
                feature_branch=feature_branch,
                pr_title=pr_title,
                pr_body=pr_body,
                workflow_context=workflow_context,
                db=self.db,
                draft=False,
            )

            if new_pr_result.get("status") == "error":
                raise ValueError(
                    f"Failed to create new PR after conflict resolve: {new_pr_result.get('error')}"
                )

            pr_number = new_pr_result.get("pr_number")
            self.logger.info("conflict_resolve created new PR #%s", pr_number)

            from app.core.enum import PRStatusEnum
            from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
            new_pr_workflow_repo = GitopsWorkflowDetailRepository(self.db)
            all_workflows = await new_pr_workflow_repo.get_all_by_pr_and_repository(
                pr_number=original_pr_number,
                git_repository=git_repository
            )
            for wf in all_workflows:
                wf.pr_number = pr_number
                wf.pr_url = new_pr_result.get("pr_url")
                wf.pr_status = PRStatusEnum.PR_OPEN
                self.db.add(wf)

            await self.db.commit()
            self.logger.info(
                "conflict_resolve updated %s workflow records to new PR #%s",
                len(all_workflows), pr_number
            )

            # Stage PR_RAISED for the bulk-applier at end of conflict_resolve.
            self._stage_queue_status(
                workflow_context,
                queue_codes,
                status=TransactionQueueStatusEnum.PR_RAISED,
            )
            self.logger.info(
                "conflict_resolve staged %s queue items → PR_RAISED",
                len(queue_codes),
            )

        # ========================================================================
        # STEP 8: UPDATE WORKFLOW RECORDS WITH NEW COMMIT SHA
        # ========================================================================
        from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
        workflow_repo = GitopsWorkflowDetailRepository(self.db)

        commit_sha = None
        for key, resp in workflow_context.gitops_responses.items():
            commit_result = resp.get("commit")
            if commit_result and commit_result.get("status") == "success":
                commit_sha = commit_result.get("commit_sha")
                break

        if commit_sha:
            all_workflows = await workflow_repo.get_all_by_pr_and_repository(
                pr_number=pr_number,
                git_repository=git_repository
            )
            for wf in all_workflows:
                wf.git_commit_sha = commit_sha
                self.db.add(wf)

            await self.db.commit()
            self.logger.info(
                "conflict_resolve updated %s workflow records with commit_sha=%s",
                len(all_workflows), commit_sha[:8] if commit_sha else None
            )

        # Flush staged queue-status decisions (PR_RAISED for the queue items
        # whose PR was reopened or recreated). One commit for all.
        await self._apply_queue_status_updates(workflow_context)

        self.logger.info("conflict_resolve done pr_number=%s", pr_number)

        return {
            'pr_number': pr_number,
            'git_repository': git_repository,
            'feature_branch': feature_branch,
            'base_branch': base_branch,
            'commit_sha': commit_sha,
            'queue_count': len(queue_items),
            'feature_branches': workflow_context.feature_branches,
            'gitops_responses': workflow_context.gitops_responses
        }

    async def _apply_gateway_routing(self, queue_item, queue_dict: dict) -> None:
        """
        Fill in where a gateway item's terragrunt lives — product, environment,
        region — resolved LIVE from whatever its transaction_code points at.

        The gateway path is
        `environment/{product}-{env}-{version}/{region}/gateway/terragrunt.hcl`.
        A gateway queue row deliberately stores only the change set and its scope,
        so product_name is on neither shape of row; it is read from the service's
        application at deploy time instead.

        Not stored on the row on purpose. A saved copy is a photocopy: rename the
        application after saving and the row would keep targeting a folder that no
        longer exists. queue_dict is rebuilt for every deploy and thrown away, and
        the file locator reads it BEFORE falling back to config_snapshot — so this
        takes effect without persisting anything.

        Both gateway shapes are handled, but not here — resolve_routing takes
        either a route-group code or a service_configs code. This method exists
        TWICE (the other copy is inlined in multiple_deploy_activities), so any
        shape knowledge kept at this level is knowledge the two copies can drift
        on. Keeping it all behind resolve_routing is what stops that.

        Silent no-op for non-gateway items and for anything whose code resolves
        to neither shape.
        """
        from app.db.models.transaction_queue_model import is_gateway_row

        if not is_gateway_row(queue_item):
            return
        if not queue_item.transaction_code:
            return

        from app.repository.kong_route_groups_repository import KongRouteGroupsRepository
        routing = await KongRouteGroupsRepository(self.db).resolve_routing(
            queue_item.transaction_code
        )
        if not routing:
            return

        queue_dict['environment'] = routing['environment'] or ""
        queue_dict['region'] = routing['region'] or ""
        queue_dict['geo_loc_mst_code'] = routing['region'] or ""
        queue_dict['product_name'] = routing['product_name'] or ""
        queue_dict['service_name'] = routing['service_name'] or ""
        queue_dict['service_mst_code'] = routing['service_mst_code'] or ""
        self.logger.info(
            "gateway routing resolved: queue=%s group=%s product=%s env=%s region=%s",
            queue_item.code, queue_item.transaction_code,
            routing['product_name'], routing['environment'], routing['region'],
        )

    async def _create_workflow_record(
        self,
        repo: str,
        base_branch: str,
        feature_branch: str,
        tenant_code: str,
        user_code: str,
        queue_codes: set,
        transaction_code: str = None,
        table_name = None
    ) -> str:
        """
        Create workflow record and mappings in database (before PR creation).

        Database-First Pattern: Save to database BEFORE creating PR in GitHub.
        This ensures we have a record even if PR creation fails.

        Args:
            repo: Repository (owner/repo)
            base_branch: Base branch name
            feature_branch: Feature branch name
            tenant_code: Tenant code
            user_code: User code
            queue_codes: Set of queue codes to map to this workflow
            transaction_code: Source entity code for linking (e.g., service_config.code, pipeline.code)
            table_name: Source table enum for PR history lookup

        Returns:
            workflow_code: The created workflow code
        """
        import uuid
        from app.core.enum import PRStatusEnum

        from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
        from app.repository.transaction_queue_workflow_mapping_repository import TransactionQueueWorkflowMappingRepository

        workflow_repo = GitopsWorkflowDetailRepository(self.db)
        mapping_repo = TransactionQueueWorkflowMappingRepository(self.db)

        # Create workflow record
        workflow_code = f"WORKFLOW-{uuid.uuid4().hex[:8].upper()}"

        workflow_payload = {
            "code": workflow_code,
            "name": f"PR Workflow - {feature_branch}",
            "description": f"PR for {repo}:{base_branch} -> {feature_branch}",
            "git_repository": repo,
            "git_branch": feature_branch,
            "pr_status": PRStatusEnum.PR_OPEN,
            "tenant_mst_code": tenant_code,
            "user_mst_code": user_code,
            "transaction_code": transaction_code,
            "table_name": table_name,
        }
        workflow = await workflow_repo.create(
            **workflow_payload
        )

        await self.db.flush()

        # Create mappings
        for idx, queue_code in enumerate(queue_codes, 1):
            mapping_code = f"MAPPING-{uuid.uuid4().hex[:8].upper()}"

            await mapping_repo.create(
                code=mapping_code,
                name=f"Mapping: {queue_code} -> {workflow_code}",
                description=f"Links queue {queue_code} to workflow {workflow_code}",
                transaction_queue_code=queue_code,
                gitops_workflow_code=workflow_code
            )

        await self.db.flush()
        # Commit to database (safe to crash after this)
        await self.db.commit()

        return workflow_code

    async def _update_workflow_with_pr_details(
        self,
        workflow_code: str,
        pr_result: dict
    ):
        """
        Update workflow record with PR details after successful PR creation.

        Args:
            workflow_code: Workflow code to update
            pr_result: PR result from GitHub API
        """
        from app.core.enum import PRStatusEnum
        from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository

        self.logger.info(
            "update_workflow_pr entry workflow_code=%s pr_number=%s pr_url=%s",
            workflow_code, pr_result.get('pr_number'), pr_result.get('pr_url'),
        )

        workflow_repo = GitopsWorkflowDetailRepository(self.db)

        workflow = await workflow_repo.get_by(code=workflow_code)
        if workflow:
            update_payload = {
                "pr_number": pr_result.get('pr_number'),
                "pr_url": pr_result.get('pr_url'),
                "git_commit_sha": pr_result.get('head_sha'),
                "pr_status": PRStatusEnum.PR_OPEN,
            }
            workflow.pr_number = pr_result.get('pr_number')
            workflow.pr_url = pr_result.get('pr_url')
            workflow.git_commit_sha = pr_result.get('head_sha')  # Latest commit on PR branch
            workflow.pr_status = PRStatusEnum.PR_OPEN

            await self.db.commit()
            self.logger.info("update_workflow_pr committed workflow_code=%s", workflow_code)
        else:
            self.logger.warning(f"      ⚠️  Workflow {workflow_code} not found for update")

    async def _post_pr_message_to_chat(
        self,
        queue_codes: set,
        pr_result: dict,
        workflow_context,
        tenant_code: str,
    ) -> None:
        """
        Insert an assistant message into conversation_message_model for each
        unique ticket linked to this PR's queue items.

        Only runs for tenants registered in `_MOCK_CANVAS_DATA`; looks up the
        chat session by ticket_code and skips if no session exists.
        """
        from app.repository.chat_session_repository import ChatSessionRepository
        from app.repository.conversation_message_repository import ConversationMessageRepository
        from app.services.vpc_and_resource_discovery_service import _MOCK_CANVAS_DATA

        self.logger.info(
            "post_pr_message entry tenant=%s queue_codes=%s pr_number=%s pr_url=%s",
            tenant_code, queue_codes, pr_result.get('pr_number'), pr_result.get('pr_url'),
        )

        if tenant_code not in _MOCK_CANVAS_DATA:
            self.logger.info(
                "post_pr_message: tenant=%s not in canvas registry; skipping",
                tenant_code,
            )
            return

        pr_url = pr_result.get('pr_url')
        pr_number = pr_result.get('pr_number')
        if not pr_url:
            self.logger.warning("post_pr_message: pr_url missing in pr_result, skipping")
            return

        ticket_codes = set()
        for queue_code in queue_codes:
            metadata = workflow_context.queue_metadata.get(queue_code) or {}
            ticket_code = metadata.get('ticket_code')
            if ticket_code:
                ticket_codes.add(ticket_code)

        self.logger.info(
            "post_pr_message resolved ticket_codes=%s from queue_codes=%s",
            ticket_codes, queue_codes,
        )

        if not ticket_codes:
            self.logger.warning(
                "post_pr_message: no ticket_codes found in queue_metadata; skipping"
            )
            return

        session_repo = ChatSessionRepository(self.db)
        message_repo = ConversationMessageRepository(self.db)

        link_text = f"#{pr_number}" if pr_number else "View PR"
        message = f"Pull request raised: [{link_text}]({pr_url})"

        inserted_count = 0
        for ticket_code in ticket_codes:
            session_obj = await session_repo.find_by_ticket(ticket_code)
            if not session_obj:
                self.logger.info(
                    "      ℹ️  No chat session for ticket=%s; skipping PR message insert",
                    ticket_code
                )
                continue

            await message_repo.create(
                session_id=session_obj.id,
                role="assistant",
                message=message,
            )
            inserted_count += 1
            self.logger.info(
                "post_pr_message inserted for ticket=%s session_id=%s",
                ticket_code, session_obj.id,
            )

        await self.db.commit()
        self.logger.info(
            "post_pr_message commit done inserted_count=%s of %s tickets",
            inserted_count, len(ticket_codes),
        )

    async def _update_workflow_with_pipeline_details(
        self,
        workflow_code: str,
        pipeline_code: str
    ) -> None:
        """
        Update workflow record to represent a PIPELINE entry.

        Used when a repo/branch contains only pipeline/dockerfile files and the
        primary workflow entry should track the pipeline transaction.
        """
        from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository

        workflow_repo = GitopsWorkflowDetailRepository(self.db)
        workflow = await workflow_repo.get_by(code=workflow_code)
        if not workflow:
            self.logger.warning(
                "      ⚠️  Workflow %s not found for pipeline update",
                workflow_code
            )
            return

        workflow.transaction_code = pipeline_code
        workflow.table_name = WorkflowSourceTableEnum.PIPELINE
        if workflow.name and workflow.name.startswith("PR Workflow - "):
            workflow.name = f"Pipeline PR {pipeline_code}"

        await self.db.commit()

    async def _create_secondary_workflow_details(
        self,
        workflow_code: str,
        repo: str,
        base_branch: str,
        tenant_code: str,
        user_code: str,
        workflow_context,
        skip_pipeline_codes: Optional[set] = None
    ) -> None:
        """
        Create additional gitops_workflow_detail entries for Dockerfile and Pipeline PRs.

        Uses transaction_code + table_name for history lookups.
        """
        from app.core.enum import WorkflowSourceTableEnum
        from app.domain.factories.gitops_workflow_detail_factory import make_gitops_workflow_detail
        from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository

        repo_branch_key = f"{repo}|||{base_branch}"
        dockerfile_codes = workflow_context.dockerfile_workflow_codes.get(repo_branch_key, set())
        pipeline_codes = workflow_context.pipeline_codes.get(repo_branch_key, set())

        if not dockerfile_codes and not pipeline_codes:
            return

        workflow_repo = GitopsWorkflowDetailRepository(self.db)
        workflow = await workflow_repo.get_by(code=workflow_code)
        if not workflow or not workflow.pr_number:
            self.logger.warning(
                "Workflow missing PR details for secondary records: %s",
                workflow_code
            )
            return

        created_count = 0

        for code in sorted(dockerfile_codes):
            existing = await workflow_repo.get_by_transaction_and_pr(
                transaction_code=code,
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
                pr_number=workflow.pr_number,
                tenant_code=tenant_code
            )
            if existing:
                continue

            workflow_data = make_gitops_workflow_detail(
                git_repository=workflow.git_repository,
                git_branch=workflow.git_branch,
                git_commit_sha=workflow.git_commit_sha or "",
                pr_number=workflow.pr_number,
                pr_url=workflow.pr_url,
                tenant_mst_code=tenant_code,
                user_mst_code=user_code,
                workflow_name=f"Dockerfile PR {code}",
                transaction_code=code,
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE
            )
            await workflow_repo.create(**workflow_data)
            created_count += 1

        skip_pipeline_codes = skip_pipeline_codes or set()
        for code in sorted(pipeline_codes):
            if code in skip_pipeline_codes:
                continue
            existing = await workflow_repo.get_by_transaction_and_pr(
                transaction_code=code,
                table_name=WorkflowSourceTableEnum.PIPELINE,
                pr_number=workflow.pr_number,
                tenant_code=tenant_code
            )
            if existing:
                continue

            workflow_data = make_gitops_workflow_detail(
                git_repository=workflow.git_repository,
                git_branch=workflow.git_branch,
                git_commit_sha=workflow.git_commit_sha or "",
                pr_number=workflow.pr_number,
                pr_url=workflow.pr_url,
                tenant_mst_code=tenant_code,
                user_mst_code=user_code,
                workflow_name=f"Pipeline PR {code}",
                transaction_code=code,
                table_name=WorkflowSourceTableEnum.PIPELINE
            )
            await workflow_repo.create(**workflow_data)
            created_count += 1

        if created_count:
            await self.db.commit()

    def _stage_queue_status(
        self,
        workflow_context,
        queue_codes,
        *,
        status=None,
        is_deleted: bool = False,
    ) -> None:
        """
        Record a queue-status decision in workflow_context.queue_status_updates.
        Does NOT touch the DB — actual writes happen later in
        `_apply_queue_status_updates`, called once at the end of the workflow.

        Last-write-wins per queue_code.
        """
        if not queue_codes:
            return
        for code in queue_codes:
            if not code:
                continue
            # MERGED, not replaced. One queue row fans out over several repos
            # (k8s + infra + workflow), and a no-change repo stages
            # is_deleted while the changed repo stages PR_RAISED — replacing
            # let repo iteration order decide which survived, and a row that
            # HAD raised a real PR could end up soft-deleted: its later
            # pr-closed revert then flipped status on a row no query can see,
            # which is exactly the "history vanished after a failed deploy"
            # production bug.
            entry = workflow_context.queue_status_updates.setdefault(code, {})
            if status is not None:
                entry["status"] = status
            if is_deleted:
                entry["is_deleted"] = True

    async def _apply_queue_status_updates(self, workflow_context) -> None:
        """
        Flush queue-status decisions accumulated in
        ``workflow_context.queue_status_updates`` to the database in bulk
        (one statement per status value, one for soft-deletes), then commit.

        Single source of write for queue lifecycle within a workflow run.
        """
        updates: Dict[str, Dict[str, Any]] = workflow_context.queue_status_updates
        if not updates:
            return

        by_status: Dict[Any, list] = defaultdict(list)
        deleted_codes: list = []
        for code, entry in updates.items():
            status_val = entry.get("status")
            if status_val is not None:
                by_status[status_val].append(code)
            # A row that gained a real status this run SHIPPED something —
            # some repo skipped it (no changes there), but another raised its
            # PR. Retiring it would orphan that PR's whole lifecycle: the row
            # would keep moving statuses (pr-closed revert included) while
            # invisible to every is_deleted-filtered read.
            if entry.get("is_deleted") and status_val is None:
                deleted_codes.append(code)

        for status_val, codes in by_status.items():
            updated = await self.queue_repo.bulk_update_status_by_codes(
                queue_codes=codes,
                status=status_val,
            )
            self.logger.info(
                "      📝  Queue status → %s for %d items: %s",
                status_val.value if hasattr(status_val, "value") else status_val,
                updated,
                codes,
            )

        if deleted_codes:
            deleted_count = await self.queue_repo.bulk_soft_delete_by_codes(deleted_codes)
            self.logger.info(
                "      🗑️  Soft-deleted %d queue items: %s",
                deleted_count,
                deleted_codes,
            )

        # A raised PR is the only signal that a resource has left "untouched
        # draft": the prod create-PR entry point runs this service directly
        # rather than the Temporal deployment workflow, so nothing else writes
        # infrastructure_mst.status, and tenants whose pre-action component is a
        # no-op (Aspora/Vance) would sit at DRAFT until the PR merges and the
        # Jenkins apply reports SUCCESS. Flip it here instead.
        pr_raised_codes = [
            code
            for code, entry in updates.items()
            if getattr(entry.get("status"), "value", entry.get("status"))
            == TransactionQueueStatusEnum.PR_RAISED.value
            and not entry.get("is_deleted")
        ]
        if pr_raised_codes:
            await self._mark_infra_pr_raised(pr_raised_codes)

        await self.db.commit()

    async def _mark_infra_pr_raised(self, queue_codes: list) -> None:
        """Set ``infrastructure_mst.status`` = PR_RAISED for the resources behind
        the given queue items.

        Without it a resource with an open PR is indistinguishable from one that
        was saved and abandoned, so the Add-Reference picker (which lists every
        resource real enough to reference) reads it as a draft and hides it.

        PR_RAISED rather than INITIALISING deliberately: on prod nothing is
        running at this point — the resource is waiting on a human to review and
        merge, which can take hours. INITIALISING means "deploy triggered,
        waiting for pipeline", so every consumer reading it as in-progress (the
        settings-panel spinner, the deploy button gating) would claim work was
        happening and lock the panel for the whole review.

        Delete cases are skipped — their resource is heading for SOFT_DELETING
        and the deletion lifecycle owns that column. Service configs are left
        alone: their surfaces already drive status from the deploy workflow.
        """
        from app.core.enum import ResourceStatusEnum
        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository

        refs = await self.queue_repo.get_entity_refs_by_codes(queue_codes)
        infra_codes = [
            r["transaction_code"]
            for r in refs
            if r["transaction_code"]
            and getattr(r["table_name"], "value", r["table_name"])
            == WorkflowSourceTableEnum.INFRASTRUCTURE.value
            and not (r["case_ref_code"] or "").startswith("delete_")
        ]
        if not infra_codes:
            return

        updated = await InfrastructureMstRepository(self.db).bulk_update_status(
            infra_codes,
            ResourceStatusEnum.PR_RAISED,
        )
        self.logger.info(
            "      🚦  Resource status → PR_RAISED for %d infra row(s): %s",
            updated,
            infra_codes,
        )

    async def _record_postgres_server_defaults(
        self,
        config_snapshot: dict,
        infrastructure_mst_code: str,
        tenant_code: str,
        environment: str,
    ) -> None:
        """
        Record default database object and postgres admin permission created by
        Bitnami PostgreSQL on first Helm install.

        Idempotent — checks if records already exist before inserting, so safe
        to call on re-runs (Helm upgrade --install).

        Records:
          - db_object_mst  : default database (auth.database / devlift_db)
          - db_permission_mst: postgres superuser (server-level, db_object_mst_id=null)
        """
        import uuid
        from app.repository.db_object_mst_repository import DbObjectMstRepository
        from app.repository.db_permission_mst_repository import DbPermissionMstRepository

        db_obj_repo = DbObjectMstRepository(self.db)
        db_perm_repo = DbPermissionMstRepository(self.db)

        database_name = (
            config_snapshot.get("database")
            or config_snapshot.get("auth.database")
            or "devlift_db"
        )

        try:
            # ── 1. Default database (insert only if not exists) ───────────────
            existing_db = await db_obj_repo.get_by_server_name_type(
                infrastructure_mst_code=infrastructure_mst_code,
                name=database_name,
                object_type="database",
            )
            db_obj_id = None
            if not existing_db:
                db_obj = await db_obj_repo.create(
                    code=f"dbobj-{uuid.uuid4().hex[:12]}",
                    name=database_name,
                    infrastructure_mst_code=infrastructure_mst_code,
                    type="database",
                    parent_id=None,
                    metadata_json={},
                    tenant_code=tenant_code,
                    environment=environment,
                )
                db_obj_id = db_obj.id
                self.logger.info(
                    "Recorded default database: server=%s db=%s",
                    infrastructure_mst_code, database_name,
                )
            else:
                db_obj_id = existing_db.id
                self.logger.debug(
                    "Default database already recorded, skipping: server=%s db=%s",
                    infrastructure_mst_code, database_name,
                )

            # ── 2. postgres superuser — server-level (insert only if not exists) ─
            existing_admin = await db_perm_repo.get_by_object_and_user(
                db_object_mst_id=None,
                username="postgres",
                infrastructure_mst_code=infrastructure_mst_code,
            )
            if not existing_admin:
                await db_perm_repo.create(
                    code=f"dbperm-{uuid.uuid4().hex[:12]}",
                    name="postgres",
                    infrastructure_mst_code=infrastructure_mst_code,
                    db_object_mst_id=None,
                    username="postgres",
                    permissions="admin",
                    tenant_code=tenant_code,
                    environment=environment,
                )
                self.logger.info(
                    "Recorded postgres admin permission: server=%s",
                    infrastructure_mst_code,
                )
            else:
                self.logger.debug(
                    "Postgres admin permission already recorded, skipping: server=%s",
                    infrastructure_mst_code,
                )

        except Exception as e:
            # Non-fatal — provisioning already succeeded, don't block the response
            self.logger.error(
                "Failed to record postgres server defaults: server=%s error=%s",
                infrastructure_mst_code, e, exc_info=True,
            )

    async def _record_postgres_server_variables(
        self,
        config_snapshot: dict,
        infrastructure_mst_code: str,
        tenant_code: str,
        environment: str,
        release_name: str,
    ) -> None:
        """
        Record postgres connection variables into variable_mst after
        a successful k8s_postgres_create_server Helm install.

        SECRET-type variables (e.g. POSTGRES_PASSWORD) are stored in
        AWS Secrets Manager via ProjectVariablesService's consolidated
        secret pattern, and the ARN is persisted in
        variable_mst.variable_cloud_identifier.

        Idempotent — upserts by (table_name, transaction_code, key).
        Also updates infrastructure_mst.variable_ids.
        """
        from app.core.enum import VariableTypeEnum, WorkflowSourceTableEnum
        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
        from app.integrations.secret_config_client import SecretConfigClient

        namespace = f"{tenant_code}-ns"

        variables = [
            {"key": "POSTGRES_USER",     "value": config_snapshot.get("username") or "postgres",                              "type": VariableTypeEnum.VARIABLE},
            {"key": "POSTGRES_DATABASE", "value": config_snapshot.get("database") or config_snapshot.get("auth.database") or "devlift_db", "type": VariableTypeEnum.VARIABLE},
            {"key": "POSTGRES_PASSWORD", "value": config_snapshot.get("postgresPassword") or "Dvl!ftPg#S3cur3@2025",          "type": VariableTypeEnum.SECRET},
            {"key": "POSTGRES_PORT",     "value": str(config_snapshot.get("port") or 5432),                                   "type": VariableTypeEnum.VARIABLE},
            {"key": "POSTGRES_HOST",     "value": f"{release_name}-postgresql.{namespace}.svc.cluster.local",                 "type": VariableTypeEnum.VARIABLE},
        ]

        try:
            infra_repo = InfrastructureMstRepository(self.db)
            infra = await infra_repo.get_by_code(infrastructure_mst_code)
            application_code = infra.applications_mst_code if infra else None

            # AWS consolidated-secret write + row upserts run inside
            # devlift-secret-config-manager (variable-mst-isolation-spec) —
            # obs_tool keeps only item-building and the variable_ids merge.
            result = await SecretConfigClient().record_resource_secret(
                table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
                transaction_code=infrastructure_mst_code,
                environment=environment,
                tenant_code=tenant_code,
                application_code=application_code,
                resource_name="postgres-server",
                items=[
                    {
                        "key": v["key"],
                        "value": v["value"],
                        "variable_type": v["type"].value,
                        "description": f"{v['key']} for postgres server {infrastructure_mst_code}",
                    }
                    for v in variables
                ],
            )
            recorded_ids: list[int] = result.get("ids") or []

            if recorded_ids:
                if not infra:
                    infra = await infra_repo.get_by_code(infrastructure_mst_code)
                if infra:
                    existing_ids = infra.variable_ids or []
                    merged = list(set(existing_ids + recorded_ids))
                    infra.variable_ids = merged
                    self.db.add(infra)
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(infra, "variable_ids")
                    await self.db.flush()

            self.logger.info(
                "Recorded %d postgres variables for server=%s",
                len(recorded_ids), infrastructure_mst_code,
            )

        except Exception as e:
            self.logger.error(
                "Failed to record postgres server variables: server=%s error=%s",
                infrastructure_mst_code, e, exc_info=True,
            )

    async def _apply_k8s_jobs_from_queue(
        self,
        queue_items,
        workflow_context,
    ) -> None:
        """
        Apply Kubernetes Job manifests staged with mode="kubectl".

        Iterates workflow_context.staged_files for kubectl entries (generated by
        K8sJobScriptGenComponent) and applies each Job YAML directly to the EKS
        cluster via `aws eks update-kubeconfig` + `kubectl apply`.

        Called after Jenkins provisioning, before the git commit step.
        """
        import asyncio

        kubectl_staged = [
            entry for entry in workflow_context.staged_files
            if entry.get("mode") == "kubectl"
        ]

        if not kubectl_staged:
            return

        self.logger.info(
            "K8s Job apply: %d staged file(s) with mode=kubectl",
            len(kubectl_staged),
        )

        queue_item_map = {qi.id: qi for qi in queue_items}

        for entry in kubectl_staged:
            queue_id = entry.get("queue_id")
            queue_item = queue_item_map.get(queue_id)
            job_yaml = entry.get("content", "")
            job_name = entry.get("job_name", "unknown")
            namespace = entry.get("namespace", "")
            cluster_name = entry.get("cluster_name", "")
            aws_region = entry.get("aws_region", "")

            if queue_item:
                await self.queue_repo.update_status(
                    queue_item.id, TransactionQueueStatusEnum.PROVISIONING.value
                )

            try:
                # Configure kubectl for the target EKS cluster
                configure_proc = await asyncio.create_subprocess_exec(
                    "aws", "eks", "update-kubeconfig",
                    "--region", aws_region,
                    "--name", cluster_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, cfg_stderr = await configure_proc.communicate()
                if configure_proc.returncode != 0:
                    raise RuntimeError(
                        f"aws eks update-kubeconfig failed: {cfg_stderr.decode()}"
                    )

                # Apply the Job manifest via stdin
                apply_proc = await asyncio.create_subprocess_exec(
                    "kubectl", "apply", "-f", "-",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await apply_proc.communicate(input=job_yaml.encode())

                if apply_proc.returncode != 0:
                    raise RuntimeError(f"kubectl apply failed: {stderr.decode()}")

                self.logger.info(
                    "K8s Job applied: job=%s namespace=%s output=%s",
                    job_name, namespace, stdout.decode().strip(),
                )

                if queue_item:
                    await self.queue_repo.update_status(
                        queue_item.id, TransactionQueueStatusEnum.BUILDING.value
                    )

                workflow_context.kubectl_results.append({
                    "job_name": job_name,
                    "namespace": namespace,
                    "status": "success",
                })

            except Exception as e:
                if queue_item:
                    await self.queue_repo.update_status(
                        queue_item.id, TransactionQueueStatusEnum.FAILED.value
                    )

                workflow_context.kubectl_results.append({
                    "job_name": job_name,
                    "namespace": namespace,
                    "status": "error",
                    "error": str(e),
                })

                self.logger.error(
                    "K8s Job apply failed: job=%s error=%s",
                    job_name, e, exc_info=True,
                )
                raise

    async def _auto_merge_paas_infra_prs(
        self,
        tenant_code: str,
        workflow_context,
        queue_items: Optional[List] = None,
    ) -> None:
        """
        Auto-merge infrastructure repo PRs for PaaS tenants.

        PaaS infra PRs contain system-generated files (Jenkinsfiles, values.yaml)
        committed for disaster recovery. No manual review needed.

        After a successful merge, fires per-resource Jenkins ``infra-resource-apply``
        builds plus a single IAM apply build for that tenant — see
        ``InfraApplyOrchestratorService``. Pipelines run in parallel.

        Enterprise tenants (in _MOCK_CANVAS_DATA) are skipped.
        """
        from app.services.vpc_and_resource_discovery_service import _MOCK_CANVAS_DATA

        if tenant_code in _MOCK_CANVAS_DATA:
            return

        from app.services.gitops.github_component import GithubComponent
        from app.integrations.github_integration import GitHubIntegration
        from app.services.infra_apply_orchestrator_service import (
            InfraApplyOrchestratorService,
        )

        for repo_branch_key, fb_data in workflow_context.feature_branches.items():
            feature_branch = fb_data["branch"] if isinstance(fb_data, dict) else fb_data
            repo, base_branch = repo_branch_key.split("|||", 1)
            repo_parts = repo.split("/")
            owner = repo_parts[0] if len(repo_parts) > 1 else None
            repo_name = repo_parts[1] if len(repo_parts) > 1 else repo

            if not repo_name.endswith("-infrastructure"):
                continue

            repo_key = f"{repo_name}|||{base_branch}"
            pr_info = workflow_context.gitops_responses.get(repo_key, {}).get("pr", {})
            pr_number = pr_info.get("pr_number")

            # If a PR was created, merge it. If commit returned no_changes
            # (HCL identical to main) there's no PR to merge — the tenant repo
            # already holds the desired state, so we fall through to the
            # orchestrator anyway and re-apply.
            should_trigger_apply = False
            if pr_number:
                try:
                    gh = GithubComponent(self.db)
                    token = await gh._get_github_token(owner)
                    base_url = settings.github_base_url.rstrip("/")

                    merge_result = await GitHubIntegration.merge_pull_request(
                        token=token,
                        base_url=base_url,
                        owner=owner,
                        repo=repo_name,
                        pull_number=pr_number,
                        merge_method="squash",
                    )
                    self.logger.info(
                        "Auto-merged PR #%s on %s/%s (sha: %s)",
                        pr_number, owner, repo_name,
                        merge_result.get("sha", "")[:8],
                    )
                    should_trigger_apply = True
                except Exception as e:
                    self.logger.warning(
                        "Failed to auto-merge PR #%s on %s/%s: %s — skipping infra-apply",
                        pr_number, owner, repo_name, e,
                    )
                    continue
            else:
                self.logger.info(
                    "No PR created for %s/%s (no_changes) — re-triggering apply against current main",
                    owner, repo_name,
                )
                should_trigger_apply = True

            if should_trigger_apply and queue_items:
                try:
                    orchestrator = InfraApplyOrchestratorService(self.db)
                    orch_results = await orchestrator.trigger_for_merged_infra_pr(
                        tenant_code=tenant_code,
                        infra_repo=f"{owner}/{repo_name}",
                        infra_branch=base_branch,
                        queue_items=queue_items,
                    )

                    # Surface per-resource infra-apply run_codes so the MCP
                    # polling path (get_deployment_status → Redis draft →
                    # pipeline_run_track) can find them. For S3/SQS/DynamoDB
                    # on PaaS the orchestrator IS the only Jenkins activity;
                    # without this the Redis draft never gets a
                    # pipeline_run_track_code and the LLM stops polling on
                    # the first tick with `no_deployments`. We store in a
                    # separate field rather than `jenkins_results` so
                    # `_trigger_jenkins_builds_post_merge` doesn't re-trigger
                    # these already-fired builds.
                    if orch_results:
                        if not hasattr(workflow_context, 'infra_apply_results'):
                            workflow_context.infra_apply_results = []
                        for r in orch_results:
                            if not isinstance(r, dict):
                                continue
                            if r.get("status") != "success" or not r.get("run_code"):
                                continue
                            if r.get("queue_id") is None:
                                continue  # IAM build — no queue scope to correlate
                            workflow_context.infra_apply_results.append({
                                "queue_id": r.get("queue_id"),
                                "queue_code": r.get("queue_code"),
                                "job_name": r.get("job_name"),
                                "pipeline_run_track_code": r["run_code"],
                                "status": "success",
                                "kind": "infra_apply",
                            })
                except Exception as exc:
                    self.logger.error(
                        "Infra-apply orchestration failed for %s/%s: %s",
                        owner, repo_name, exc, exc_info=True,
                    )

    async def _provision_jenkins_pipelines_from_queue(
        self,
        queue_items,
        workflow_context,
    ) -> None:
        """
        Provision Jenkins pipeline jobs from staged files with mode="jenkins".

        Iterates workflow_context.staged_files (populated by script gen components)
        and routes each entry:
          - script_gen_key="eks_jenkins_pipeline" or "model_serving_jenkins_pipeline" → provision_pipeline() (EKS service)
          - anything else (e.g. k8s_postgres_create_server, stop/restart/delete) → provision_pipeline_job()

        Called after queue processing, before the commit/PR step.
        """
        jenkins_staged = [
            entry for entry in workflow_context.staged_files
            if entry.get("mode") == "jenkins"
        ]

        if not jenkins_staged:
            return

        self.logger.info(
            "Jenkins provisioning: %d staged file(s) with mode=jenkins",
            len(jenkins_staged),
        )

        # Build queue_id → queue_item lookup for status updates
        queue_item_map = {qi.id: qi for qi in queue_items}

        from app.services.jenkins_provisioning_service import JenkinsProvisioningService
        jenkins_svc = JenkinsProvisioningService(self.db)

        jenkins_results = []

        for entry in jenkins_staged:
            script_gen_key = entry.get("script_gen_key", "")
            job_name = entry.get("job_name", "unknown")
            config_snapshot = entry.get("config_snapshot") or {}
            queue_id = entry.get("queue_id")
            queue_item = queue_item_map.get(queue_id)

            service_name = config_snapshot.get("service_name") or config_snapshot.get("name", "unknown")
            tenant_code = config_snapshot.get("tenant_code") or (queue_items[0].tenant_code if queue_items else "")
            environment = config_snapshot.get("environment") or "stage"
            service_code = config_snapshot.get("services_mst_code") or config_snapshot.get("service_mst_code")
            resource_group_code = config_snapshot.get("resource_group_mst_code")
            application_code = config_snapshot.get("applications_mst_code")
            infrastructure_mst_code = config_snapshot.get("infrastructure_mst_code")

            # Set status to PROVISIONING
            if queue_item:
                await self.queue_repo.update_status(queue_item.id, TransactionQueueStatusEnum.PROVISIONING.value)

            try:
                if script_gen_key in ("eks_jenkins_pipeline", "model_serving_jenkins_pipeline"):
                    result = await jenkins_svc.provision_pipeline(
                        service_name=service_name,
                        config_snapshot=config_snapshot,
                        tenant_code=tenant_code,
                        transaction_code=queue_item.transaction_code if queue_item else config_snapshot.get("code"),
                        table_name=str(queue_item.table_name.value) if queue_item else "SERVICE_CONFIG",
                        service_code=service_code,
                        resource_group_code=resource_group_code,
                        application_code=application_code,
                        trigger_first_build=False,
                        queue_codes=[queue_item.code] if queue_item else [],
                    )
                else:
                    jenkinsfile_content = entry.get("content", "")
                    result = await jenkins_svc.provision_pipeline_job(
                        job_name=job_name,
                        jenkinsfile_content=jenkinsfile_content,
                        tenant_code=tenant_code,
                        environment=environment,
                        transaction_code=queue_item.transaction_code if queue_item else config_snapshot.get("code"),
                        table_name=str(queue_item.table_name.value) if queue_item else "INFRASTRUCTURE",
                        infrastructure_mst_code=infrastructure_mst_code,
                        trigger_build=False,
                        queue_codes=[queue_item.code] if queue_item else [],
                    )

                # Update status to BUILDING if build was triggered
                if queue_item and result.get("first_build_triggered"):
                    await self.queue_repo.update_status(queue_item.id, TransactionQueueStatusEnum.BUILDING.value)

                # Defence-in-depth: strip env variable values from persisted config_snapshot.
                # The frontend no longer sends secret values (they come from AWS Secrets
                # Manager via CSI driver), but we still strip as a safety net.
                if queue_item and queue_item.config_snapshot:
                    env_vars = queue_item.config_snapshot.get("env_variables")
                    if isinstance(env_vars, list):
                        queue_item.config_snapshot["env_variables"] = [
                            {"name": v.get("name"), "is_secret": v.get("is_secret", "false")}
                            for v in env_vars if isinstance(v, dict) and v.get("name")
                        ]
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(queue_item, "config_snapshot")
                        await self.db.flush()

                jenkins_results.append({
                    "service_name": service_name,
                    "job_name": result.get("job_name"),
                    "job_url": result.get("job_url"),
                    "pipeline_code": result.get("pipeline_code"),
                    "status": result.get("status", "error"),
                    "first_build_triggered": False,
                    "config_snapshot": config_snapshot,
                    "tenant_code": tenant_code,
                    "queue_code": queue_item.code if queue_item else None,
                    "queue_id": queue_id,
                })
                self.logger.info(
                    "Jenkins job provisioned: script_gen_key=%s job=%s",
                    script_gen_key, result.get("job_name"),
                )

                # Record default db objects + postgres admin on first postgres server install
                queue_case_ref_code = queue_item.case_ref_code if queue_item else None
                if queue_case_ref_code == "k8s_postgres_create_server" and infrastructure_mst_code:
                    await self._record_postgres_server_defaults(
                        config_snapshot=config_snapshot,
                        infrastructure_mst_code=infrastructure_mst_code,
                        tenant_code=tenant_code,
                        environment=environment,
                    )
                    # Derive the helm release name the same way
                    # k8s_helm_script_gen_component does (line 279-283):
                    #   identifier = config_snapshot.get("identifier") or server_name
                    #   release_name = identifier.lower().replace(" ", "-")
                    # Do NOT use job_name — that's the Jenkins pipeline name
                    # (e.g. "k8s-postgres-billing-pg-server-be919c44"), not the
                    # helm release (e.g. "billing-pg-server").
                    helm_release = (
                        config_snapshot.get("identifier")
                        or config_snapshot.get("server_name", "")
                    ).lower().replace(" ", "-") or job_name
                    await self._record_postgres_server_variables(
                        config_snapshot=config_snapshot,
                        infrastructure_mst_code=infrastructure_mst_code,
                        tenant_code=tenant_code,
                        environment=environment,
                        release_name=helm_release,
                    )
            except Exception as e:
                if queue_item:
                    await self.queue_repo.update_status(queue_item.id, TransactionQueueStatusEnum.FAILED.value)

                jenkins_results.append({
                    "service_name": service_name,
                    "status": "error",
                    "error": str(e),
                })
                self.logger.error(
                    "Jenkins provisioning failed: script_gen_key=%s job=%s: %s",
                    script_gen_key, job_name, e, exc_info=True,
                )

        # Store results on workflow_context for the response and post-merge build trigger
        if not hasattr(workflow_context, 'jenkins_results'):
            workflow_context.jenkins_results = []
        workflow_context.jenkins_results.extend(jenkins_results)

    async def _trigger_jenkins_builds_post_merge(self, workflow_context) -> None:
        """
        Trigger Jenkins builds for provisioned pipelines AFTER infra repo PRs are merged.

        This ensures K8s manifests exist in the infra repo before Jenkins tries to
        fetch them at runtime.
        """
        jenkins_results = getattr(workflow_context, 'jenkins_results', [])
        pending = [r for r in jenkins_results if r.get("status") == "success" and r.get("job_name")]
        if not pending:
            return

        self.logger.info("Triggering %d Jenkins build(s) post-merge", len(pending))

        from app.services.jenkins_provisioning_service import JenkinsProvisioningService
        jenkins_svc = JenkinsProvisioningService(self.db)

        for entry in pending:
            job_name = entry["job_name"]
            pipeline_code = entry.get("pipeline_code")
            config_snapshot = entry.get("config_snapshot", {})
            tenant_code = entry.get("tenant_code", "")
            queue_codes = [entry["queue_code"]] if entry.get("queue_code") else []
            repo_url = config_snapshot.get("repository") or ""

            try:
                result = await jenkins_svc.trigger_build_for_pipeline(
                    job_name=job_name,
                    pipeline_code=pipeline_code,
                    config_snapshot=config_snapshot,
                    tenant_code=tenant_code,
                    repo_url=repo_url,
                    queue_codes=queue_codes,
                )
                entry["first_build_triggered"] = result.get("status") == "success"
                if result.get("run_code"):
                    entry["pipeline_run_track_code"] = result["run_code"]

                # Update queue status to BUILDING
                if entry.get("queue_id") and entry["first_build_triggered"]:
                    await self.queue_repo.update_status(
                        entry["queue_id"], TransactionQueueStatusEnum.BUILDING.value,
                    )

                self.logger.info(
                    "Jenkins build triggered post-merge: job=%s result=%s run_code=%s",
                    job_name, result.get("status"), result.get("run_code"),
                )
            except Exception as e:
                self.logger.error(
                    "Failed to trigger Jenkins build post-merge: job=%s: %s",
                    job_name, e, exc_info=True,
                )

    async def _link_dockerfile_workflows(
        self,
        workflow_code: str,
        repo: str,
        base_branch: str,
        tenant_code: str,
        workflow_context
    ) -> None:
        """
        Link Dockerfile PR workflows to service_config_dockerfile_workflows.

        Uses config snapshots (queue_id -> snapshot) and file locations
        to resolve service_config_id and branch/repo for Dockerfile items.
        """
        import hashlib
        from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
        from app.repository.service_config_dockerfile_workflow_repository import ServiceConfigDockerfileWorkflowRepository
        from app.repository.service_config_repository import ServiceConfigRepository
        from app.core.enum import WorkflowSourceTableEnum

        workflow_repo = GitopsWorkflowDetailRepository(self.db)
        dockerfile_repo = ServiceConfigDockerfileWorkflowRepository(self.db)
        service_config_repo = ServiceConfigRepository(self.db)

        workflow = await workflow_repo.get_by(code=workflow_code)
        if not workflow:
            self.logger.warning(f"Workflow not found for dockerfile linking: {workflow_code}")
            return

        unique_links = set()
        for queue_id, file_loc_response in workflow_context.file_location_responses.items():
            for file_item in file_loc_response.files:
                if file_item.repo != repo or file_item.base_branch != base_branch:
                    continue
                if file_item.script_gen_key != "ecs_dockerfile":
                    continue

                snapshot = workflow_context.config_snapshots.get(queue_id) or {}
                service_config_id = snapshot.get("id")
                service_config_code = snapshot.get("code")
                services_mst_code = snapshot.get("services_mst_code")

                if not service_config_id:
                    queue_code = file_item.queue_code
                    metadata = workflow_context.queue_metadata.get(queue_code or "")
                    table_name = metadata.get("table_name") if metadata else None
                    transaction_code = metadata.get("transaction_code") if metadata else None
                    if table_name == WorkflowSourceTableEnum.SERVICE_CONFIG and transaction_code:
                        service_config = await service_config_repo.get_by_code_and_tenant(
                            code=transaction_code,
                            tenant_code=tenant_code
                        )
                        if service_config:
                            service_config_id = service_config.id
                            service_config_code = service_config.code
                            services_mst_code = service_config.services_mst_code
                        else:
                            self.logger.warning(
                                "Service config not found for dockerfile link "
                                f"(transaction_code={transaction_code}, tenant={tenant_code})"
                            )
                    if not service_config_id:
                        self.logger.warning(
                            "Missing service_config_id for dockerfile link "
                            f"(queue_id={queue_id}, repo={repo}, branch={base_branch})"
                        )
                        continue

                unique_links.add((
                    service_config_id,
                    service_config_code,
                    services_mst_code,
                    file_item.repo,
                    file_item.base_branch
                ))

        if not unique_links:
            return
        for (
            service_config_id,
            service_config_code,
            services_mst_code,
            service_repo,
            branch_name
        ) in unique_links:
            unique_key = f"{service_config_code or service_config_id}_{branch_name}_{service_repo}"
            dockerfile_workflow_code = f"SCDF_{hashlib.md5(unique_key.encode()).hexdigest()[:8]}"
            dockerfile_workflow_name = f"Dockerfile: {services_mst_code or service_config_code} - {branch_name}"

            repo_branch_key = f"{repo}|||{base_branch}"
            workflow_context.dockerfile_workflow_codes[repo_branch_key].add(
                dockerfile_workflow_code
            )

            dockerfile_link_payload = {
                "service_config_id": service_config_id,
                "gitops_workflow_id": workflow.id,
                "branch": branch_name,
                "repository": service_repo,
                "code": dockerfile_workflow_code,
                "name": dockerfile_workflow_name,
            }
            await dockerfile_repo.link_workflow(
                **dockerfile_link_payload
            )

    async def _link_pipeline_workflows(
        self,
        workflow_code: str,
        repo: str,
        base_branch: str,
        tenant_code: str,
        workflow_context
    ) -> None:
        """
        Link pipeline_mst records to the PR workflow when pipeline files are changed.

        Matches on transaction_code + repo_url and then refines by:
        - repo_branch == base_branch
        - deployment_config.selected_branches includes base_branch
        - deployment_config.workflow_file_path matches the pipeline workflow file
        - deployment_config.config_file_path matches the EKS config file
        """
        import hashlib
        from sqlalchemy import select
        from app.core.enum import EnvironmentEnum
        from app.db.models.pipeline_mst_model import PipelineMstModel
        from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
        from app.repository.service_config_repository import ServiceConfigRepository

        def _normalize_repo_url(raw_repo: str) -> str:
            """
            Normalize repo URLs to owner/repo for matching.
            Accepts full URLs or already-shortened owner/repo strings.
            """
            if not raw_repo:
                return ""
            normalized = raw_repo.strip()
            # Drop protocol and host if present
            if "://" in normalized:
                normalized = normalized.split("://", 1)[1]
            if "/" in normalized:
                parts = normalized.split("/")
                if len(parts) >= 2:
                    return "/".join(parts[-2:])
            return normalized

        normalized_repo = _normalize_repo_url(repo)
        workflow_repo = GitopsWorkflowDetailRepository(self.db)
        workflow = await workflow_repo.get_by(code=workflow_code)
        if not workflow:
            self.logger.warning(f"Workflow not found for pipeline linking: {workflow_code}")
            return

        service_config_repo = ServiceConfigRepository(self.db)

        pipeline_contexts = {}
        pipeline_keys = {"ecs_pipeline", "eks_pipeline_workflow", "eks_pipeline_config"}
        pipeline_file_types = {"pipeline", "eks_workflow", "eks_config"}

        for queue_id, file_loc_response in workflow_context.file_location_responses.items():
            for file_item in file_loc_response.files:
                file_repo = _normalize_repo_url(file_item.repo)
                if file_repo != normalized_repo or file_item.base_branch != base_branch:
                    continue

                file_type = None
                if getattr(file_item, "config", None):
                    file_type = file_item.config.get("file_type")

                if (
                    file_item.script_gen_key not in pipeline_keys
                    and file_type not in pipeline_file_types
                ):
                    continue

                # Resolve transaction_code (service_config.code) for pipeline linking
                snapshot = workflow_context.config_snapshots.get(queue_id) or {}
                svc_config_code = snapshot.get("service_config_code")

                if not svc_config_code:
                    queue_code = file_item.queue_code
                    metadata = workflow_context.queue_metadata.get(queue_code or "")
                    transaction_code = metadata.get("transaction_code") if metadata else None
                    table_name_val = metadata.get("table_name") if metadata else None
                    if table_name_val == WorkflowSourceTableEnum.SERVICE_CONFIG and transaction_code:
                        svc_config_code = transaction_code

                if not svc_config_code:
                    self.logger.warning(
                        "Missing service_config_code for pipeline link "
                        f"(queue_id={queue_id}, repo={repo}, branch={base_branch})"
                    )
                    continue

                key = (
                    svc_config_code,
                    file_repo,
                    file_item.base_branch
                )
                context = pipeline_contexts.setdefault(
                    key,
                    {
                        "workflow_file_path": None,
                        "config_file_path": None,
                        "file_type": None,
                        "queue_id": queue_id
                    }
                )
                if file_type in ("pipeline", "eks_workflow"):
                    context["workflow_file_path"] = file_item.file_path
                if file_type == "eks_config":
                    context["config_file_path"] = file_item.file_path
                context["file_type"] = file_type

        if not pipeline_contexts:
            return

        linked_count = 0
        created_count = 0
        for (
            svc_config_code,
            repo_url,
            branch_name
        ), context in pipeline_contexts.items():
            stmt = (
                select(PipelineMstModel)
                .where(
                    PipelineMstModel.transaction_code == svc_config_code,
                    PipelineMstModel.table_name == "SERVICE_CONFIG",
                    PipelineMstModel.is_deleted == False
                )
            )
            result = await self.db.execute(stmt)
            candidates = result.scalars().all()

            matching_candidates = [
                pipeline for pipeline in candidates
                if _normalize_repo_url(pipeline.repo_url) == repo_url
            ]
            linked = False
            for pipeline in candidates:
                if pipeline not in matching_candidates:
                    continue
                deployment_config = pipeline.deployment_config or {}
                selected_branches = deployment_config.get("selected_branches", [])
                workflow_file_path = deployment_config.get("workflow_file_path")
                config_file_path = deployment_config.get("config_file_path")

                branch_match = (
                    pipeline.repo_branch == branch_name
                    or (isinstance(selected_branches, list) and branch_name in selected_branches)
                )
                path_match = (
                    (workflow_file_path and workflow_file_path == context.get("workflow_file_path"))
                    or (config_file_path and config_file_path == context.get("config_file_path"))
                )
                requires_path_match = bool(
                    context.get("workflow_file_path") or context.get("config_file_path")
                )

                # Require branch match; use path match only to disambiguate within a branch.
                if not branch_match:
                    continue
                if requires_path_match and not path_match:
                    continue

                if pipeline.gitops_workflow_id != workflow.id:
                    update_payload = {
                        "pipeline_code": pipeline.code,
                        "pipeline_id": pipeline.id,
                        "repo_branch": pipeline.repo_branch,
                        "old_gitops_workflow_id": pipeline.gitops_workflow_id,
                        "new_gitops_workflow_id": workflow.id,
                    }
                    pipeline.gitops_workflow_id = workflow.id
                    self.db.add(pipeline)
                    linked_count += 1
                linked = True
                repo_branch_key = f"{repo}|||{base_branch}"
                workflow_context.pipeline_codes[repo_branch_key].add(pipeline.code)

            if linked:
                continue

            pipeline = await self._create_pipeline_mst_for_workflow(
                workflow=workflow,
                svc_config_code=svc_config_code,
                repo_url=repo_url,
                branch_name=branch_name,
                workflow_file_path=context.get("workflow_file_path"),
                config_file_path=context.get("config_file_path"),
                tenant_code=tenant_code,
                queue_id=context.get("queue_id"),
                workflow_context=workflow_context
            )
            if pipeline:
                created_count += 1
                repo_branch_key = f"{repo}|||{base_branch}"
                workflow_context.pipeline_codes[repo_branch_key].add(pipeline.code)

        if linked_count or created_count:
            await self.db.commit()

    async def _create_pipeline_mst_for_workflow(
        self,
        workflow,
        svc_config_code: str,
        repo_url: str,
        branch_name: str,
        workflow_file_path: str,
        config_file_path: str,
        tenant_code: str,
        queue_id: int,
        workflow_context
    ):
        """
        Create pipeline_mst entry and link to gitops workflow when missing.

        Returns the created PipelineMstModel or None if creation is not possible.
        """
        import hashlib
        from app.repository.pipeline_mst_repository import PipelineMstRepository
        from app.repository.pipeline_vendor_mst_repository import PipelineVendorMstRepository
        from app.repository.services_mst_repository import ServicesMstRepository
        from app.repository.service_config_repository import ServiceConfigRepository

        snapshot = workflow_context.config_snapshots.get(queue_id) or {}
        language_ref_code = snapshot.get("language_ref_code")
        infra_type = snapshot.get("infrastructuretype_ref_code")
        service_name = snapshot.get("service_name") or svc_config_code
        geo_loc_mst_code = snapshot.get("geo_loc_mst_code")

        if not language_ref_code:
            self.logger.warning(
                "Missing language_ref_code for pipeline create "
                f"(transaction_code={svc_config_code})"
            )
            return None

        # Look up service_config to resolve service and environment
        svc_config_repo = ServiceConfigRepository(self.db)
        svc_config = await svc_config_repo.get_by_code_and_tenant(svc_config_code, tenant_code)
        if not svc_config:
            self.logger.warning(
                "Service config not found for pipeline create: %s",
                svc_config_code
            )
            return None

        service_mst_code = svc_config.services_mst_code
        env_enum = svc_config.environment

        services_repo = ServicesMstRepository(self.db)
        service = await services_repo.get_by_code(service_mst_code)
        if not service:
            self.logger.warning(
                "Service not found for pipeline create: %s",
                service_mst_code
            )
            return None

        if service.tenants_mst_code != tenant_code:
            self.logger.warning(
                "Tenant mismatch for pipeline create (service=%s, tenant=%s)",
                service_mst_code,
                tenant_code
            )
            return None

        vendor_repo = PipelineVendorMstRepository(self.db)
        pipeline_vendor = await vendor_repo.get_by_service_hierarchy(
            service_code=service.code,
            resource_group_code=service.resource_group_mst_code,
            application_code=service.applications_mst_code,
            tenant_code=service.tenants_mst_code,
            environment=env_enum
        )
        if not pipeline_vendor:
            self.logger.warning(
                "No pipeline vendor config found for pipeline create "
                f"(transaction_code={svc_config_code})"
            )
            return None

        environment_str = env_enum.value if hasattr(env_enum, 'value') else str(env_enum)
        unique_hash = hashlib.md5(
            f"{svc_config_code}_{environment_str}_{branch_name}_{repo_url}".encode()
        ).hexdigest()[:8]
        pipeline_code = f"pipeline-{svc_config_code[:8]}-{environment_str}-{unique_hash}"
        pipeline_name = f"Pipeline: {service_name} - {environment_str} - {branch_name}"

        deployment_config = {
            "selected_branches": [branch_name],
            "workflow_file_path": workflow_file_path,
            "config_file_path": config_file_path,
        }
        if infra_type:
            deployment_config["infrastructure_type"] = infra_type
        if geo_loc_mst_code:
            deployment_config["geo_loc_mst_code"] = geo_loc_mst_code

        pipeline_repo = PipelineMstRepository(self.db)
        pipeline_payload = {
            "code": pipeline_code,
            "name": pipeline_name,
            "transaction_code": svc_config_code,
            "table_name": "SERVICE_CONFIG",
            "tenant_code": tenant_code,
            "pipeline_vendor_mst_code": pipeline_vendor.code,
            "repo_url": repo_url,
            "repo_branch": branch_name,
            "language_ref_code": language_ref_code,
            "authentication_config": {},
            "deployment_config": deployment_config,
            "gitops_workflow_id": workflow.id,
        }
        pipeline = await pipeline_repo.create(**pipeline_payload)
        return pipeline
    
    async def _create_workflow_and_pr(
        self,
        repo_branch_key: str,
        feature_branch: str,
        pr_type: str = "infrastructure",
        tenant_code: str = None,
        user_code: str = None,
        workflow_context=None,
    ):
        """
        Create workflow record, mappings, and PR (Database-First Pattern).
        Also links Dockerfile workflows when Dockerfile files are part of this PR.
        Also links pipeline_mst records when pipeline files are part of this PR.

        Args:
            repo_branch_key: "{repo}-{base_branch}" key
            feature_branch: Feature branch name
            tenant_code: Tenant code
            user_code: User code
            workflow_context: Workflow context
        """
        # Parse repo and base_branch from key
        # Key format: "{repo}|||{base_branch}" using triple-pipe delimiter
        repo, base_branch = repo_branch_key.split('|||', 1)

        # Extract owner and repo name from "owner/repo"
        repo_parts = repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo_name = repo_parts[1] if len(repo_parts) > 1 else repo

        # Collect queue codes for this repo+base_branch
        queue_codes = set()
        for file_loc_response in workflow_context.file_location_responses.values():
            for file_item in file_loc_response.files:
                if file_item.repo == repo and file_item.base_branch == base_branch:
                    if file_item.queue_code:
                        queue_codes.add(file_item.queue_code)

        # Get transaction_code and table_name from first queue item's metadata
        # All items in a single workflow PR should have the same table_name
        transaction_code = None
        table_name = None

        # Check if this repo/branch contains ONLY Dockerfile and/or Pipeline files (no Terragrunt HCL)
        has_terragrunt_hcl = False
        has_dockerfile_or_pipeline = False
        primary_pipeline_only = False

        for file_loc_response in workflow_context.file_location_responses.values():
            for file_item in file_loc_response.files:
                if file_item.repo == repo and file_item.base_branch == base_branch:
                    script_gen_key = file_item.script_gen_key or ""

                    # Check if this is Dockerfile or Pipeline
                    if script_gen_key in ["ecs_dockerfile", "ecs_pipeline", "eks_pipeline_workflow", "eks_pipeline_config", "eks_pipeline_deployment", "default_eks_deployment", "default_eks_workflow", "default_dockerfile"]:
                        has_dockerfile_or_pipeline = True
                    # Check if this is Terragrunt HCL or other infrastructure files
                    elif script_gen_key and script_gen_key not in ["atlantis"]:
                        # Any script_gen_key that's not Dockerfile/Pipeline/atlantis is considered HCL
                        has_terragrunt_hcl = True

        primary_pipeline_only = has_dockerfile_or_pipeline and not has_terragrunt_hcl

        if queue_codes and workflow_context.queue_metadata:
            first_queue_code = next(iter(queue_codes))
            metadata = workflow_context.queue_metadata.get(first_queue_code, {})
            transaction_code = metadata.get('transaction_code')
            table_name = metadata.get('table_name')

            # If this repo contains ONLY Dockerfile/Pipeline files (no HCL), skip the PRIMARY SERVICE_CONFIG entry
            # The secondary workflow details (_create_secondary_workflow_details) will handle these cases
            if primary_pipeline_only:
                transaction_code = None
                table_name = None

        # ====================================================================
        # STEP 4a: Create workflow and mappings in database (BEFORE PR)
        # ====================================================================
        workflow_code = await self._create_workflow_record(
            repo=repo,
            base_branch=base_branch,
            feature_branch=feature_branch,
            tenant_code=tenant_code,
            user_code=user_code,
            queue_codes=queue_codes,
            transaction_code=transaction_code,
            table_name=table_name
        )

        # Link Dockerfile workflows to service config junction table (if applicable)
        await self._link_dockerfile_workflows(
            workflow_code=workflow_code,
            repo=repo,
            base_branch=base_branch,
            tenant_code=tenant_code,
            workflow_context=workflow_context
        )

        # Link pipeline workflows to pipeline_mst (if pipeline files are part of this PR)
        await self._link_pipeline_workflows(
            workflow_code=workflow_code,
            repo=repo,
            base_branch=base_branch,
            tenant_code=tenant_code,
            workflow_context=workflow_context
        )

        # If this repo has only pipeline/dockerfile files, reuse the primary workflow entry for PIPELINE
        skip_pipeline_codes = set()
        if primary_pipeline_only:
            repo_branch_key = f"{repo}|||{base_branch}"
            pipeline_codes = sorted(workflow_context.pipeline_codes.get(repo_branch_key, set()))
            if pipeline_codes:
                primary_pipeline_code = pipeline_codes[0]
                skip_pipeline_codes.add(primary_pipeline_code)
                await self._update_workflow_with_pipeline_details(
                    workflow_code=workflow_code,
                    pipeline_code=primary_pipeline_code
                )
            else:
                self.logger.warning(
                    "      ⚠️  No pipeline codes found to update primary workflow "
                    f"(repo={repo}, branch={base_branch})"
                )

        # ====================================================================
        # STEP 4b: Create PR in GitHub
        # ====================================================================
        # Fetch user email for PR body
        user_email = user_code  # Fallback to user_code if email not found
        try:
            user = await self.user_repo.get_by_code(user_code)
            if user and user.email_id:
                user_email = user.email_id
        except Exception as e:
            self.logger.warning(f"Could not fetch user email for {user_code}: {e}")

        # Generate rich PR title and body (AI from the actual changes, deterministic fallback)
        pr_title, pr_body = await self._generate_pr_title_and_body(
            workflow_context=workflow_context,
            repo=repo,
            base_branch=base_branch,
            feature_branch=feature_branch,
            user_email=user_email,
            tenant_code=tenant_code
        )

        self.logger.info(f"         ├─ PR Title: {pr_title}")
        self.logger.info(f"         ├─ Queue Items: {len(queue_codes)}")
        self.logger.info(f"         └─ Base Branch: {base_branch} → Feature Branch: {feature_branch}")

        # Skip PR creation if no commit was made for this repo/branch
        repo_key = f"{repo_name}|||{base_branch}"
        commit_result = workflow_context.gitops_responses.get(repo_key, {}).get("commit")
        if not commit_result:
            self.logger.info(
                "      ℹ️  Skipping PR creation for %s because no commit result was recorded",
                repo_key
            )
            workflow_context.gitops_responses.setdefault(repo_key, {})["pr_skipped"] = {
                "status": "no_commit",
                "message": "No commit result recorded for repo/branch",
                "base_branch": base_branch,
                "feature_branch": feature_branch
            }
            self._stage_queue_status(workflow_context, queue_codes, is_deleted=True)
            return

        commit_status = commit_result.get("status")
        if commit_status in ("no_changes", "skipped"):
            message = commit_result.get("message") or commit_result.get("error") or "No changes detected"
            self.logger.info(
                "      ℹ️  Skipping PR creation for %s because commit status=%s (%s)",
                repo_key,
                commit_status,
                message
            )
            workflow_context.gitops_responses.setdefault(repo_key, {})["pr_skipped"] = {
                "status": commit_status,
                "message": message,
                "base_branch": base_branch,
                "feature_branch": feature_branch
            }
            self._stage_queue_status(workflow_context, queue_codes, is_deleted=True)
            return

        pr_result = await GitOpsHandler.create_pr(
            tenant=tenant_code,
            owner=owner,
            repo=repo_name,
            base_branch=base_branch,
            feature_branch=feature_branch,
            pr_title=pr_title,
            pr_body=pr_body,
            workflow_context=workflow_context,
            db=self.db,
            draft=False
        )

        # Stamp PR type onto gitops_responses so deploy_activities can classify primary vs secondary PRs
        repo_key = f"{repo_name}|||{base_branch}"
        workflow_context.gitops_responses.setdefault(repo_key, {})["type"] = pr_type
        # …and the queue codes that went into it, so create_with_run_track can
        # close the right resource's run-track row. gitops_responses is keyed by
        # repo|||branch, which says nothing about which resources a PR covers —
        # and one PR legitimately covers several when they share a repo and base
        # branch (bucket + queue + dynamo in one infra repo = one PR).
        # list(), not the raw set: this goes out in the API response and a set is
        # not JSON.
        workflow_context.gitops_responses.setdefault(repo_key, {})["queue_codes"] = list(queue_codes)

        # ====================================================================
        # STEP 4c: Update workflow with PR details
        # ====================================================================
        await self._update_workflow_with_pr_details(
            workflow_code=workflow_code,
            pr_result=pr_result
        )

        # ====================================================================
        # STEP 4c-bis: Post PR notification into chat conversation per ticket
        # ====================================================================
        await self._post_pr_message_to_chat(
            queue_codes=queue_codes,
            pr_result=pr_result,
            workflow_context=workflow_context,
            tenant_code=tenant_code,
        )

        # Create additional workflow detail records for dockerfile/pipeline history
        await self._create_secondary_workflow_details(
            workflow_code=workflow_code,
            repo=repo,
            base_branch=base_branch,
            tenant_code=tenant_code,
            user_code=user_code,
            workflow_context=workflow_context,
            skip_pipeline_codes=skip_pipeline_codes
        )

        # ====================================================================
        # STEP 4d: Stage queue status → PR_RAISED. Actual DB write happens in
        # `_apply_queue_status_updates` at the end of the workflow.
        # ====================================================================
        self._stage_queue_status(
            workflow_context,
            queue_codes,
            status=TransactionQueueStatusEnum.PR_RAISED,
        )

    # =============================================================================
    # PR Title and Body Generation (using shared helpers)
    # =============================================================================

    async def _generate_pr_title_and_body(
        self,
        workflow_context: PRWorkflowContext,
        repo: str,
        base_branch: str,
        feature_branch: str,
        user_email: str,
        tenant_code: str
    ) -> tuple:
        """
        Generate PR title + body, preferring AI generation from the actual changes.

        Deterministic title/body first (guaranteed fallback), then — when PR_AI_NAMING_ENABLED
        and changes are available — asks the LLM to write both from the real diff (feature vs
        the PR's base branch), falling back to the workflow content, then deterministic on error.
        """
        pr_title = self._generate_pr_title(workflow_context, repo, base_branch)
        pr_body = self._generate_pr_body(
            workflow_context=workflow_context,
            repo=repo,
            base_branch=base_branch,
            user_email=user_email,
            tenant_code=tenant_code
        )

        if not settings.pr_ai_naming_enabled:
            return pr_title, pr_body

        try:
            changes = await self._collect_ai_changes(
                workflow_context, repo, base_branch, feature_branch
            )
            if changes:
                from app.services.openai_service import OpenAIService
                ai_content = await OpenAIService().generate_pr_content_from_changes(
                    changes=changes,
                    context={"tenant": tenant_code},
                )
                if ai_content.get("title"):
                    pr_title = ai_content["title"]
                if ai_content.get("body"):
                    # Hybrid body: AI summary on top, then keep the deterministic details
                    # (per-resource cards, Atlantis plan/apply commands, "Requested by").
                    pr_body = ai_content["body"] + "\n\n---\n\n" + pr_body
                self.logger.info(f"AI-generated PR title: {pr_title}")
        except Exception as ai_err:
            self.logger.warning(
                f"AI PR naming failed, using deterministic title/body: {ai_err}"
            )

        return pr_title, pr_body

    async def _collect_ai_changes(
        self,
        workflow_context: PRWorkflowContext,
        repo: str,
        base_branch: str,
        feature_branch: str
    ) -> list:
        """
        Collect normalized, secret-redacted changes to describe. Prefers the real diff
        (feature vs base branch) via the shared collect_pr_changes; falls back to the
        generated content/config held in the workflow context. Returns NormalizedChange list.
        """
        from app.utils.pr_diff_helpers import collect_pr_changes, normalize_diff

        fallback = self._collect_changes_from_context(workflow_context, repo, base_branch)

        repo_parts = repo.split("/")
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo_name = repo_parts[1] if len(repo_parts) > 1 else repo
        if not owner or not feature_branch:
            return normalize_diff(fallback)

        try:
            token = await self._get_github_token(owner)
        except Exception as e:
            self.logger.warning(f"AI diff: token fetch failed, using workflow content: {e}")
            return normalize_diff(fallback)

        return await collect_pr_changes(
            token=token,
            base_url=settings.github_base_url,
            owner=owner,
            repo=repo_name,
            base=base_branch,
            head=feature_branch,
            fallback_entries=fallback,
        )

    def _collect_changes_from_context(
        self,
        workflow_context: PRWorkflowContext,
        repo: str,
        base_branch: str
    ) -> list:
        """
        Build the changes list from the workflow context (generated preview content and
        config), deduplicated by queue_code — mirrors the dedup logic in _generate_pr_body.
        """
        changes = []
        seen = set()
        for queue_id, file_loc_response in workflow_context.file_location_responses.items():
            config_snapshot = workflow_context.config_snapshots.get(queue_id, {})
            gen = workflow_context.script_gen_responses.get(queue_id) or {}
            content = "\n".join(
                v.get("preview_contents", "")
                for v in gen.values()
                if isinstance(v, dict) and v.get("preview_contents")
            ).strip()

            for file_item in file_loc_response.files:
                if file_item.repo != repo or file_item.base_branch != base_branch:
                    continue
                key = file_item.queue_code or file_item.file_path
                if key in seen:
                    continue
                seen.add(key)
                changes.append({
                    "path": file_item.file_path,
                    "content": content or None,
                    "config": {**config_snapshot, **(file_item.config or {})},
                    "status": "added",
                })
        return changes

    def _generate_pr_title(
        self,
        workflow_context: PRWorkflowContext,
        repo: str,
        base_branch: str
    ) -> str:
        """
        Generate PR title with [DevLift] prefix using shared helper.

        Deduplicates items by queue_code to get accurate item count.
        """
        environments = set()
        infra_types = set()
        seen_queue_codes = set()  # Track seen queue codes to deduplicate

        for queue_id, file_loc_response in workflow_context.file_location_responses.items():
            config_snapshot = workflow_context.config_snapshots.get(queue_id, {})
            for file_item in file_loc_response.files:
                if file_item.repo == repo and file_item.base_branch == base_branch:
                    # Deduplicate by queue_code
                    queue_code = file_item.queue_code
                    if queue_code and queue_code in seen_queue_codes:
                        continue
                    if queue_code:
                        seen_queue_codes.add(queue_code)

                    config = file_item.config or {}
                    env = (
                        config_snapshot.get('environment') or
                        config.get('environment') or
                        config.get('environments_enum') or
                        ''
                    )
                    if env:
                        env_normalized = str(env).lower()
                        if env_normalized in ("staging", "stage"):
                            env_normalized = "stage"
                        environments.add(env_normalized)

                    infra_type = (
                        config_snapshot.get('infra_type') or
                        file_item.infra_type_ref or
                        config.get('infra_type') or
                        ''
                    )
                    infra_type = self._resolve_service_infra_type(
                        infra_type, config_snapshot, config
                    )
                    if infra_type:
                        infra_types.add(str(infra_type))

        # Item count is the number of unique queue codes
        item_count = len(seen_queue_codes)
        return generate_pr_title(item_count, environments, infra_types)

    @staticmethod
    def _resolve_service_infra_type(infra_type, config_snapshot, config) -> str:
        """'update_service'/'create_service' are case codes, not infra types —
        the display map assumes ECS for them, mislabeling EKS services. When the
        snapshot's real infrastructure type is EKS, resolve to 'eks' so PR
        titles/bodies say EKS."""
        if str(infra_type) in ('update_service', 'create_service'):
            ref = (
                config_snapshot.get('infrastructuretype_ref_code') or
                config.get('infrastructuretype_ref_code') or
                ''
            )
            if 'eks' in str(ref).lower():
                return 'eks'
        return infra_type

    def _generate_pr_body(
        self,
        workflow_context: PRWorkflowContext,
        repo: str,
        base_branch: str,
        user_email: str,
        tenant_code: str
    ) -> str:
        """
        Generate PR description body using shared helper.

        Deduplicates items by queue_code to avoid showing multiple entries
        for the same queue item (e.g., HCL file + atlantis.yaml).
        """
        items_data = []
        environments = set()
        infra_types = set()
        seen_queue_codes = set()  # Track seen queue codes to deduplicate

        # Which queue items have an atlantis.yaml project, by queue code. Collected
        # up-front because the atlantis item is appended AFTER the terragrunt one in a
        # queue's file list, and the row below is built from the FIRST file seen — a
        # single pass would decide the flag before it had seen the evidence.
        queue_codes_with_atlantis = {
            f.queue_code
            for resp in workflow_context.file_location_responses.values()
            for f in resp.files
            if f.queue_code and f.file_path and 'atlantis.yaml' in f.file_path
            and f.repo == repo and f.base_branch == base_branch
        }

        for queue_id, file_loc_response in workflow_context.file_location_responses.items():
            # Get config_snapshot for this queue_id
            config_snapshot = workflow_context.config_snapshots.get(queue_id, {})

            for file_item in file_loc_response.files:
                if file_item.repo == repo and file_item.base_branch == base_branch:
                    # Deduplicate by queue_code - each queue item may have multiple files
                    queue_code = file_item.queue_code
                    if queue_code and queue_code in seen_queue_codes:
                        continue
                    if queue_code:
                        seen_queue_codes.add(queue_code)

                    # Use config_snapshot (from queue) as primary source, fall back to file_item.config
                    config = file_item.config or {}

                    # Extract infra type from config_snapshot first
                    infra_type = config_snapshot.get('infra_type') or file_item.infra_type_ref or config.get('infra_type') or ''
                    infra_type = str(self._resolve_service_infra_type(infra_type, config_snapshot, config))
                    if infra_type:
                        infra_types.add(infra_type)

                    # Extract service/resource name - prefer config_snapshot
                    service_name = (
                        config_snapshot.get('identifier') or
                        config_snapshot.get('name') or
                        config_snapshot.get('service_name') or
                        config.get('identifier') or
                        config.get('name') or
                        config.get('service_name') or
                        config.get('queue_name') or
                        config.get('bucket_name') or
                        config.get('table_name') or
                        ''
                    )
                    # Only fall back to queue_code if no meaningful name found
                    if not service_name:
                        service_name = file_item.queue_code or 'unknown'

                    # Extract environment from config_snapshot first
                    environment = config_snapshot.get('environment') or config.get('environment') or config.get('environments_enum') or ''
                    if environment:
                        environment = str(environment).lower()
                        environments.add(environment)

                    # Extract region from config_snapshot first
                    geo_loc = config_snapshot.get('geo_loc_mst_code') or config_snapshot.get('region') or config.get('geo_loc_mst_code') or config.get('region') or ''
                    region = get_region_display(geo_loc)

                    # Extract product_name from config_snapshot first
                    product_name = (
                        config_snapshot.get('product_name') or
                        config_snapshot.get('product') or
                        config.get('product_name') or
                        config.get('product') or
                        config.get('applications_mst_code') or
                        ''
                    )

                    # Try to get atlantis name from script_gen_responses (generated by atlantis component)
                    # This ensures PR description matches the actual atlantis.yaml entry
                    atlantis_name = None
                    script_gen_responses = workflow_context.script_gen_responses.get(queue_id, {})
                    atlantis_response = script_gen_responses.get('atlantis', {})
                    if atlantis_response and isinstance(atlantis_response, dict):
                        atlantis_name = atlantis_response.get('atlantis_project_name')

                    # Fallback to computing if not available from script_gen_responses
                    if not atlantis_name:
                        normalized_infra = normalize_infra_type(infra_type)
                        atlantis_name = compute_atlantis_project_name(
                            infra_type=normalized_infra,
                            service_name=service_name,
                            environment=environment,
                            tenant_code=tenant_code,
                            product_name=product_name,
                            geo_loc=geo_loc
                        )

                    # A gateway row's snapshot IS its change set, so the card can say
                    # what moved. Everything else already reads as a named resource.
                    changes = []
                    if normalize_infra_type(infra_type) == 'Kong Gateway':
                        group_label = (
                            (workflow_context.queue_metadata.get(queue_code or "") or {})
                            .get('display_name') or ''
                        ).split(' — ')[0].strip()
                        change_line = self._describe_gateway_delta(group_label, config_snapshot)
                        if change_line:
                            changes.append(change_line)

                    items_data.append({
                        'infra_type': infra_type,
                        'service_name': service_name,
                        'environment': environment,
                        'region': region,
                        'atlantis_name': atlantis_name,
                        'has_atlantis_entry': bool(
                            queue_code and queue_code in queue_codes_with_atlantis
                        ),
                        'changes': changes,
                    })

        # atlantis_entries=None: every item above carries an explicit
        # has_atlantis_entry, which takes precedence over the name-matching gate.
        return generate_pr_body(
            self._collapse_gateway_items(items_data),
            environments, infra_types, user_email, atlantis_entries=None,
        )

    @staticmethod
    def _collapse_gateway_items(items_data: List[Dict]) -> List[Dict]:
        """One nameless card per gateway Atlantis project, not one per route group.

        Every group edited in a deploy is written into the SAME gateway terragrunt
        file and planned by the SAME Atlantis project, so a card per group repeats
        one identical pair of commands N times and buries the service rows under it.

        The per-group label goes with it. It named the change rather than a
        resource ("<group> · GET — 2 changes"), which grows with every edit and
        restates what the plan and the diff already show; the card carries the
        commands, and those are what a reader needs from this body.

        Keyed on the project rather than merged outright, so a batch that ever
        spans two scopes (different environment or region) still gets a card each.
        """
        collapsed: List[Dict] = []
        by_project: Dict[str, Dict] = {}

        for item in items_data:
            if normalize_infra_type(item.get('infra_type', '')) != 'Kong Gateway':
                collapsed.append(item)
                continue
            project = item.get('atlantis_name') or ''
            merged = by_project.get(project)
            if merged is None:
                merged = {**item, 'service_name': '', 'changes': list(item.get('changes') or [])}
                by_project[project] = merged
                collapsed.append(merged)
            else:
                # The groups keep their own lines under the one card — that list is
                # the only place the routes are legible, since the Atlantis plan
                # renders a gateway change as one kong_configs map diff.
                merged['changes'].extend(item.get('changes') or [])

        return collapsed

    @staticmethod
    def _describe_gateway_delta(group_label: str, delta: Optional[Dict]) -> Optional[str]:
        """One line saying what a route group changed, or None when nothing did.

        Built from the delta the queue row stores — the same change set the
        generator applies — so the line cannot claim something other than what
        ships. It stays one line however often the group was edited: the delta is
        always relative to what is DEPLOYED, not cumulative across saves.
        """
        delta = delta or {}
        added, removed, edited = [], [], []
        for action_item in (delta.get('paths') or []):
            action = (action_item.get('action') or '').strip().lower()
            path = action_item.get('route_path')
            if not path:
                continue
            if action == 'add':
                added.append(f"`{path}`")
            elif action == 'delete':
                removed.append(f"`{path}`")
            elif action == 'edit':
                edited.append(f"`{action_item.get('old_path') or '?'}` → `{path}`")

        clauses = []
        if added:
            clauses.append(f"added {', '.join(added)}")
        if removed:
            clauses.append(f"removed {', '.join(removed)}")
        if edited:
            clauses.append(f"changed {', '.join(edited)}")

        plugins_before = sorted(delta.get('plugins_before') or [])
        plugins_after = sorted(delta.get('plugins_after') or [])
        if plugins_before != plugins_after:
            clauses.append(
                f"plugins `{', '.join(plugins_before) or 'none'}` → "
                f"`{', '.join(plugins_after) or 'none'}`"
            )

        priority_before = delta.get('regex_priority_before') or 0
        priority_after = delta.get('regex_priority_after') or 0
        if priority_before != priority_after:
            clauses.append(f"priority {priority_before} → {priority_after}")

        if not clauses:
            return None
        joined = ', '.join(clauses)
        return f"`{group_label}` — {joined}" if group_label else joined

    async def trigger_rollback(
        self,
        queue_id: int,
        tenant_code: str,
    ) -> Dict:
        from app.handlers.file_location_handler import FileLocationHandler
        from app.handlers.script_gen_handler import ScriptGenHandler
        from app.schemas.pr_workflow_context import PRWorkflowContext

        queue_item = await self.queue_repo.get_by(id=queue_id)
        if not queue_item:
            raise ValueError(f"Queue item not found: queue_id={queue_id}")
        if queue_item.tenant_code != tenant_code:
            raise PermissionError("Access denied: queue item belongs to a different tenant")

        config_snapshot = queue_item.config_snapshot or {}
        if not config_snapshot.get("target_sha"):
            raise ValueError("target_sha missing from queue item config_snapshot")

        queue_dict = {
            "id": queue_item.id,
            "code": queue_item.code,
            "case_ref_code": "rollback_service",
            "tenant_code": tenant_code,
            "config_snapshot": config_snapshot,
        }

        # skip_commit=True → file locator sets feature_branch = base_branch (no new branch)
        file_location_response = await FileLocationHandler.locate(
            tenant=tenant_code,
            queue_item=queue_dict,
            workflow_context=PRWorkflowContext(skip_commit=True),
        )
        if not file_location_response or not file_location_response.files:
            raise ValueError("File locator returned no files for rollback_service")

        # script gen fetches current values.yaml, replaces tag, stages the file
        commit_ctx = PRWorkflowContext()
        await ScriptGenHandler.generate_script(
            tenant=tenant_code,
            queue_dict=queue_dict,
            file_location=file_location_response.files[0],
            workflow_context=commit_ctx,
            gitops_queue_repo=self.queue_repo,
            db=self.db,
        )

        if not commit_ctx.staged_files:
            raise ValueError("Script gen produced no staged files for rollback_service")

        staged = commit_ctx.staged_files[0]
        repo_parts = staged["repo"].split("/", 1)
        owner, repo_name = repo_parts[0], repo_parts[1]
        base_branch = staged["base_branch"]
        repo_branch_key = f"{staged['repo']}|||{base_branch}"
        commit_message = (
            commit_ctx.commit_messages.get(repo_branch_key)
            or f"rollback: {config_snapshot.get('service_name', '')} → {config_snapshot.get('target_sha', '')[:9]}"
        )

        # feature_branch == base_branch → direct commit, no PR
        commit_result = await GitOpsHandler.create_commit(
            tenant=tenant_code,
            owner=owner,
            repo=repo_name,
            base_branch=base_branch,
            feature_branch=staged["feature_branch"],
            files=[{"path": staged["file_path"], "content": staged["content"]}],
            commit_message=commit_message,
            workflow_context=commit_ctx,
            db=self.db,
        )

        return {
            "commit_result": commit_result,
            "env_file_path": staged["file_path"],
            "k8s_branch": base_branch,
            "k8s_repository": staged["repo"],
        }
