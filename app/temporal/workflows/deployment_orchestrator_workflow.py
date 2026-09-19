"""
DeploymentOrchestratorWorkflow — parent for the /multiple-deploy flow.

One instance per multiple-deploy request. Owns the coordinator locks for the
WHOLE journey and runs the stages strictly in order:

  1. Activity: prepare_multiple_deploy — resolve queue items, derive
     project_dirs (variable + infra parts share the same key set), create
     pipeline_mst / pipeline_run_track rows, pre-generate the infra child id.
  2. Signal TenantCoordinator: acquire_locks(orchestrator_id, project_dirs)
     — all-or-nothing; queued behind any running deploy of the same dirs.
  3. Child: DeploymentWorkflow — started with parent_holder_id=orchestrator_id
     so the coordinator grants it passage over the dirs THIS workflow already
     holds without transferring ownership (the child's own release calls are
     holder-mismatch no-ops). Awaited to completion (incl. PR merge).
  4. Child: VariableDeployWorkflow (no locks) — secrets/configs pushed to AWS.
     Runs only after infra succeeded.
  5. finally: release_locks(orchestrator_id) — the single real release, on
     every exit path.

Ordering (infra before variables) comes from this code sequence: Terraform —
the infra PR — OWNS creating the Secrets Manager secret / SSM structure, and
the variable stage only inserts keys/values into existing containers. Pushing
variables first would make deploy_variables' create-fallback manufacture the
secret and collide with Terraform's aws_secretsmanager_secret on apply. The
lock only provides atomicity against concurrent deploys of the same
service/dirs. Pods pick up the new values without a second rollout
(external-secrets refresh), so restarting before the values land is safe.

Standalone s3/sqs/dynamo deploys do NOT come through here — they keep starting
DeploymentWorkflow top-level via /transaction-queue/deploy, unchanged.
"""

import asyncio
from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.temporal.activities.multiple_deploy_activities import prepare_multiple_deploy
    from app.temporal.activities.deploy_activities import (
        update_pipeline_run_track_stage,
        update_deployment_status,
        update_queue_status,
    )
    from app.temporal.activities.admin_alert_activities import report_stale_queue_wait
    from app.temporal.error_utils import error_message
    from app.core.enum import ResourceDeploymentStatusEnum as _DS

LOCK_WAIT_TIMEOUT = timedelta(hours=24)
QUEUE_CHECK_INTERVAL = timedelta(minutes=10)  # re-check the lock holder this often while queued (admin alert if it is dead)
STALE_REALERT_INTERVAL = timedelta(hours=1)  # after the first stale-lock alert, remind admins this often while still blocked

_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
)


@workflow.defn
class DeploymentOrchestratorWorkflow:

    def __init__(self):
        self._locks_granted: bool = False
        self._lock_abort_reason: Optional[str] = None
        self._step: str = "starting"
        self._result: Optional[str] = None
        self._variable_result: Optional[dict] = None
        self._infra_child_id: Optional[str] = None
        self._track_id: Optional[str] = None
        self._extra_results: list = []
        # ── Production (promotion) mode ──────────────────────────────────────
        self._tenant_code: str = ""
        self._user_code: str = ""
        # Dirs the batch releases while a child is PARKED: the promotion key +
        # the batch's shared (kong/gateway) dirs. Owner-unique dirs stay held.
        self._park_dirs: list = []
        # Child waiting for a locks_granted forward after a resume re-acquire.
        self._pending_resume_child: Optional[str] = None

    @workflow.run
    async def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params: {tenant_code, user_code,
                 variables: {transaction_code, table_name, environment} | None,
                 infra:     {environment, item_ids} | None}
        """
        tenant_code = params["tenant_code"]
        user_code = params["user_code"]
        self._tenant_code = tenant_code
        self._user_code = user_code
        orchestrator_id = workflow.info().workflow_id
        coordinator = workflow.get_external_workflow_handle(f"coordinator-{tenant_code}")

        workflow.upsert_search_attributes({
            "DeployTenantCode": [tenant_code],
            "DeployUserCode": [user_code],
        })

        # ── 1. Prep (no locks yet) ────────────────────────────────────────────
        self._step = "preparing"
        prep = await workflow.execute_activity(
            prepare_multiple_deploy,
            args=[{**params, "orchestrator_id": orchestrator_id}],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY,
        )
        if prep.get("error"):
            self._step = "failed"
            self._result = prep["error"]
            return {"status": "FAILED", "error": prep["error"]}

        project_dirs = prep["project_dirs"]
        self._infra_child_id = prep.get("infra_child_id")
        self._track_id = prep.get("track_id")
        track_id = prep["track_id"]
        # ── Production (promotion) mode ──────────────────────────────────────
        # prep detects a prod-environment batch and returns:
        #   deploy_mode="production", repo_full_name, source/target_branch,
        #   service_dirs (the service child's own dirs),
        #   park_release_dirs (promotion key + shared kong/gateway dirs —
        #   what child_parked releases; prep also put the key into
        #   project_dirs so the batch's initial acquire covers it).
        prod_mode = prep.get("deploy_mode") == "production"
        self._park_dirs = prep.get("park_release_dirs") or []
        act_opts_stage = dict(
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        act_opts_short = dict(
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY,
        )

        # ── 2. Acquire locks (all-or-nothing; queued if any dir is held) ─────
        # 'waiting in queue' (no artifact prefix — the WHOLE deployment waits):
        # ~0s on an uncontended lock; when another deployment holds the dirs,
        # its duration is exactly the time spent queued behind them.
        self._step = "acquiring_locks"
        workflow.logger.info(
            "[%s] requesting locks for dirs: %s", orchestrator_id, project_dirs
        )
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[track_id, "waiting in queue", "running", workflow.now().isoformat()],
            **act_opts_stage,
        )
        await coordinator.signal(
            "acquire_locks", args=[orchestrator_id, project_dirs, user_code]
        )
        try:
            # Wait in QUEUE_CHECK_INTERVAL legs up to the 24h cap. Each leg that
            # times out asks report_stale_queue_wait whether the lock holder is
            # still running: queued behind a live deploy is normal (no alert);
            # a dead holder means the lock is stale, so admins are told in the
            # report channel — once right away, then a reminder every
            # STALE_REALERT_INTERVAL for as long as the run stays blocked.
            # Deterministic: timers + activities only.
            waited = timedelta(0)
            last_alert_at: timedelta | None = None  # `waited` when the last alert was actually sent
            while True:
                try:
                    await workflow.wait_condition(
                        lambda: self._locks_granted or self._lock_abort_reason is not None,
                        timeout=min(QUEUE_CHECK_INTERVAL, LOCK_WAIT_TIMEOUT - waited),
                    )
                    break
                except asyncio.TimeoutError:
                    waited += QUEUE_CHECK_INTERVAL
                    if waited >= LOCK_WAIT_TIMEOUT:
                        raise
                    if last_alert_at is not None and waited - last_alert_at < STALE_REALERT_INTERVAL:
                        continue  # alerted recently — hold the reminder until the interval passes
                    # Alerting is best-effort telemetry. A Slack outage or an unreachable
                    # coordinator must never kill a deploy that is only waiting its turn:
                    # an ActivityError is NOT an asyncio.TimeoutError, so without this guard
                    # it escapes the handler below, the run dies without withdrawing from the
                    # coordinator, and its hold_queue entry is later granted to a corpse --
                    # leaving the dirs locked to a workflow that will never release them.
                    try:
                        sent = await workflow.execute_activity(
                            report_stale_queue_wait,
                            args=[tenant_code, orchestrator_id, project_dirs, user_code, int(waited.total_seconds() // 60), last_alert_at is not None],
                            **act_opts_short,
                        )
                    except Exception:  # noqa: BLE001 - telemetry must never fail the deploy
                        workflow.logger.warning(
                            "stale-queue check failed; retrying on the next leg", exc_info=True
                        )
                        sent = False
                    if sent:
                        last_alert_at = waited
        except asyncio.TimeoutError:
            # Withdraw from the coordinator before dying, else our stale
            # hold_queue entry gets granted later to a completed workflow and
            # the dirs stay locked to a corpse. Both signals: whichever state
            # we raced into (still queued, or granted at the last instant) one
            # cleans it up and the other is a no-op.
            await coordinator.signal("release_hold", args=[orchestrator_id])
            await coordinator.signal("release_locks", args=[orchestrator_id, project_dirs])
            _timeout_err = f"Timed out waiting for locks on {project_dirs}"
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[track_id, "waiting in queue", "failed", workflow.now().isoformat(), _timeout_err],
                **act_opts_stage,
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[track_id, "completed", "failed", workflow.now().isoformat(), _timeout_err],
                **act_opts_stage,
            )
            self._step = "failed"
            self._result = "lock_timeout"
            return {
                "status": "FAILED",
                "error": f"Timed out waiting for locks on {project_dirs}",
            }
        if not self._locks_granted and self._lock_abort_reason is not None:
            # abort_queued_for_dirs already removed our hold_queue entry — a
            # grant can never come. Fail fast with the true reason, exactly
            # like a queued DeploymentWorkflow does (aborted_lock_conflict).
            _abort_err = f"Lock request aborted: {self._lock_abort_reason}"
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[track_id, "waiting in queue", "failed", workflow.now().isoformat(), _abort_err],
                **act_opts_stage,
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[track_id, "completed", "failed", workflow.now().isoformat(), _abort_err],
                **act_opts_stage,
            )
            self._step = "failed"
            self._result = "aborted_lock_conflict"
            return {
                "status": "FAILED",
                "error": f"Lock request aborted: {self._lock_abort_reason}",
            }
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[track_id, "waiting in queue", "success", workflow.now().isoformat()],
            **act_opts_stage,
        )

        # ── 3 + 4. Stages under the lock; 5. finally releases it ─────────────
        # ORDER: infra FIRST, variables SECOND. Terraform (the infra PR) OWNS
        # creating the Secrets Manager secret / SSM structure; the variable
        # stage only inserts keys/values into containers that already exist.
        # Pushing variables first would make deploy_variables' create-fallback
        # manufacture the secret manually and collide with Terraform's
        # aws_secretsmanager_secret on apply ("resource already exists").
        try:
            infra_result: Optional[dict] = None
            if prep.get("queue_ids"):
                self._step = "deploying_infra"
                try:
                    if prod_mode:
                        infra_result = await workflow.execute_child_workflow(
                            "ProductionDeploymentWorkflow",
                            args=[{
                                "tenant_code": tenant_code,
                                "user_code": user_code,
                                "queue_ids": prep["queue_ids"],
                                "project_dirs": prep.get("service_dirs") or [],
                                "shared_dirs": [],  # service dirs stay held on park
                                "repo_full_name": prep.get("repo_full_name"),
                                "source_branch": prep.get("source_branch"),
                                "target_branch": prep.get("target_branch"),
                                "parent_holder_id": orchestrator_id,
                                "track_id": track_id,
                                "pr_group": "service",
                            }],
                            id=prep["infra_child_id"],
                        )
                    else:
                        infra_result = await workflow.execute_child_workflow(
                            "DeploymentWorkflow",
                            args=[
                                tenant_code,
                                project_dirs,
                                prep["queue_ids"],
                                user_code,
                                orchestrator_id,  # parent_holder_id — reentrant passage
                                track_id,         # shared run-track key for this batch
                                "service",        # pr_group — tags this child's PRs
                            ],
                            id=prep["infra_child_id"],
                        )
                except Exception as exc:
                    # Infra failed → SKIP variables: on a first deploy the
                    # containers may not exist yet, and pushing values would
                    # recreate the Terraform-ownership collision above. The
                    # staged file survives for a retry. Extra groups (kong etc.)
                    # never got a child started — mark their items skipped so
                    # they don't sit in STARTING_DEPLOYMENT forever.
                    await self._mark_skipped(
                        prep.get("extra_groups") or [],
                        "an earlier deployment in this batch (service infra) failed",
                        act_opts_short,
                    )
                    await workflow.execute_activity(
                        update_pipeline_run_track_stage,
                        args=[track_id, "completed", "failed", workflow.now().isoformat(), error_message(exc)],
                        **act_opts_stage,
                    )
                    self._step = "failed"
                    self._result = "infra_failed"
                    return {
                        "status": "FAILED",
                        "stage": "infra",
                        "variable_result": None,
                        "infra_child_id": prep.get("infra_child_id"),
                        "extra_results": self._extra_results,
                        "error": error_message(exc),
                    }

            if prep.get("has_variables"):
                self._step = "deploying_variables"
                self._variable_result = await workflow.execute_child_workflow(
                    "VariableDeployWorkflow",
                    prep["variable_params"],
                    id=f"vars-{orchestrator_id}",
                )
                if not (self._variable_result or {}).get("all_success"):
                    # Variables failed → extra groups never got a child started.
                    await self._mark_skipped(
                        prep.get("extra_groups") or [],
                        "an earlier deployment in this batch (variables) failed",
                        act_opts_short,
                    )
                    self._step = "failed"
                    self._result = "variables_failed"
                    return {
                        "status": "FAILED",
                        "stage": "variables",
                        "variable_result": self._variable_result,
                        "infra_result": infra_result,
                        "infra_child_id": prep.get("infra_child_id"),
                        "extra_results": self._extra_results,
                        "error": (self._variable_result or {}).get("error")
                        or "variable deployment failed",
                    }

            # ── 3c. Extra groups (kong today, more resource types later) ─────
            # Every non-service queue item, grouped by terragrunt dir in prep,
            # deployed as its own DeploymentWorkflow child AFTER variables.
            # Sequential, not parallel: a child can park for hours (manual plan
            # fix / merge wait) and concurrent children would race branch-name
            # generation and run_script_pr_workflow's PR-reuse idempotency —
            # see deployment_workflow.py's header notes on that idempotency.
            for group in prep.get("extra_groups") or []:
                self._step = f"deploying_extra:{group['project_dir']}"
                try:
                    if prod_mode:
                        group_result = await workflow.execute_child_workflow(
                            "ProductionDeploymentWorkflow",
                            args=[{
                                "tenant_code": tenant_code,
                                "user_code": user_code,
                                "queue_ids": group["queue_ids"],
                                "project_dirs": [],
                                # A gateway dir IS shared — released on park.
                                "shared_dirs": [group["project_dir"]],
                                "repo_full_name": prep.get("repo_full_name"),
                                "source_branch": prep.get("source_branch"),
                                "target_branch": prep.get("target_branch"),
                                "parent_holder_id": orchestrator_id,
                                "track_id": track_id,
                                "pr_group": group.get("pr_group", "other"),
                            }],
                            id=group["child_id"],
                        )
                    else:
                        group_result = await workflow.execute_child_workflow(
                            "DeploymentWorkflow",
                            args=[
                                tenant_code,
                                project_dirs,
                                group["queue_ids"],
                                user_code,
                                orchestrator_id,  # parent_holder_id — reentrant passage
                                track_id,         # shared run-track key for this batch
                                group.get("pr_group", "other"),  # tags this child's PRs
                            ],
                            id=group["child_id"],
                        )
                    self._extra_results.append({
                        "project_dir": group["project_dir"],
                        "child_id": group["child_id"],
                        "status": "SUCCESS",
                        "result": group_result,
                    })
                except Exception as exc:
                    self._extra_results.append({
                        "project_dir": group["project_dir"],
                        "child_id": group["child_id"],
                        "status": "FAILED",
                        "error": error_message(exc),
                    })
                    _all_groups = prep.get("extra_groups") or []
                    _remaining = _all_groups[_all_groups.index(group) + 1:]
                    await self._mark_skipped(
                        _remaining,
                        f"an earlier deployment in this batch ({group['project_dir']}) failed",
                        act_opts_short,
                    )
                    await workflow.execute_activity(
                        update_pipeline_run_track_stage,
                        args=[track_id, "completed", "failed", workflow.now().isoformat(), error_message(exc)],
                        **act_opts_stage,
                    )
                    self._step = "failed"
                    self._result = "extra_group_failed"
                    return {
                        "status": "FAILED",
                        "stage": "extra",
                        "variable_result": self._variable_result,
                        "infra_result": infra_result,
                        "infra_child_id": prep.get("infra_child_id"),
                        "extra_results": self._extra_results,
                        "error": error_message(exc),
                    }

            self._step = "completed"
            self._result = "success"
            # The single, truly-final closure: the infra child writes
            # 'infra: complete' under a parent, so 'completed' lands here —
            # AFTER the variables stage — flipping the row to COMPLETED and
            # stopping the FE poller only when everything is done.
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[track_id, "completed", "success", workflow.now().isoformat()],
                **act_opts_stage,
            )
            return {
                "status": "COMPLETED",
                "variable_result": self._variable_result,
                "infra_result": infra_result,
                "infra_child_id": prep.get("infra_child_id"),
                "extra_results": self._extra_results,
            }
        finally:
            # The single real release — child releases are holder-mismatch
            # no-ops because ownership never left this workflow.
            await coordinator.signal("release_locks", args=[orchestrator_id, project_dirs])
            workflow.logger.info("[%s] released locks: %s", orchestrator_id, project_dirs)

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _mark_skipped(groups: list, reason: str, act_opts_short: dict) -> None:
        """Extra groups that never got a DeploymentWorkflow child started
        because an earlier stage in the batch failed first. Their queue items
        were flipped to STARTING_DEPLOYMENT by prep before anything ran, so
        without this they would sit there forever — neither failed nor able
        to be re-approved for a retry. Marks them FAILED / ERROR instead.

        Run-track is NOT touched here — the caller's own 'completed: failed'
        write (keyed by the batch's shared track_id) already updates every
        row in the batch, including these groups' rows, in one shot.
        """
        all_ids = [qid for g in groups for qid in g["queue_ids"]]
        if not all_ids:
            return
        await workflow.execute_activity(
            update_deployment_status,
            args=[all_ids, _DS.ERROR, reason],
            **act_opts_short,
        )
        await workflow.execute_activity(
            update_queue_status,
            args=[all_ids, "FAILED"],
            **act_opts_short,
        )

    # ── Signals / queries ─────────────────────────────────────────────────────

    @workflow.signal
    async def locks_granted(self) -> None:
        self._locks_granted = True
        # A grant arriving while a parked child is resuming is the re-acquire
        # ack — forward it so the child continues. (In-flight replays: the
        # field is unset in old histories, so this branch never runs there.)
        if self._pending_resume_child:
            child_id = self._pending_resume_child
            self._pending_resume_child = None
            try:
                await workflow.get_external_workflow_handle(child_id).signal("locks_granted")
                workflow.logger.info("resume ack forwarded to %s", child_id)
            except Exception as exc:
                workflow.logger.error("failed to forward resume ack to %s: %s", child_id, exc)

    @workflow.signal
    async def child_parked(self, child_id: str, released_dirs: list) -> None:
        """A prod child parked: release the promotion key + the batch's
        SHARED dirs so the prod queue and gateway waiters move on. The
        orchestrator (lock owner) decides the release set from prep's
        knowledge — the child's suggestion is logged, not trusted."""
        workflow.logger.info(
            "child_parked: %s (suggested %s) — releasing %s",
            child_id, released_dirs, self._park_dirs,
        )
        if self._park_dirs:
            coordinator = workflow.get_external_workflow_handle(f"coordinator-{self._tenant_code}")
            await coordinator.signal(
                "release_locks", args=[workflow.info().workflow_id, self._park_dirs]
            )

    @workflow.signal
    async def child_resuming(self, child_id: str, released_dirs: list) -> None:
        """A parked child's wake condition fired: re-acquire the key+shared
        dirs (tail of the coordinator FIFO). The grant lands on OUR
        locks_granted, which forwards the ack to the child."""
        workflow.logger.info("child_resuming: %s — re-acquiring %s", child_id, self._park_dirs)
        self._pending_resume_child = child_id
        self._locks_granted = False
        if not self._park_dirs:
            # Nothing was released (defensive) — ack immediately.
            self._pending_resume_child = None
            await workflow.get_external_workflow_handle(child_id).signal("locks_granted")
            return
        coordinator = workflow.get_external_workflow_handle(f"coordinator-{self._tenant_code}")
        await coordinator.signal(
            "acquire_locks",
            args=[workflow.info().workflow_id, self._park_dirs, self._user_code],
        )

    @workflow.signal
    def atlas_lock_abort(self, reason: str) -> None:
        """Sent by the coordinator's abort_queued_for_dirs when an Atlantis
        lock conflict exhausts its alert schedule — our queued lock request has
        been dropped, so wake up and fail fast instead of sleeping to the 24h
        timeout. Same contract as DeploymentWorkflow.atlas_lock_abort."""
        workflow.logger.warning("Signal: atlas_lock_abort reason=%s", reason)
        self._lock_abort_reason = reason or "aborted_lock_conflict"

    @workflow.query
    def get_state(self) -> dict:
        return {
            "step": self._step,
            "result": self._result,
            "locks_granted": self._locks_granted,
            "variable_result": self._variable_result,
            "infra_child_id": self._infra_child_id,
            "track_id": self._track_id,
            "extra_results": self._extra_results,
        }