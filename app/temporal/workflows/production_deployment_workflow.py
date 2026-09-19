"""
ProductionDeploymentWorkflow — the prod half of the Temporal deploy flow.

Prod ships through ONE shared stage→main promotion PR per repo (Atlantis's
prod projects watch /main/ only). Stage/qa keep DeploymentWorkflow untouched.

Locking model (batch-atomic; see the prod-promotion design doc):
  - The promotion key "prod-promotion:{repo}" serializes prod deploys: one
    Atlantis actor per repo at a time (Atlantis breaks on mid-plan head
    moves; observed in production).
  - Under a DeploymentOrchestratorWorkflow parent (eks/ecs multiple-deploy),
    the PARENT owns the key together with the batch's real dirs — the whole
    batch (service → variables → kong) runs as ONE deployment, no
    interleaving. This child gets passage via parent_holder_id, same as any
    parent-held dir.
  - Standalone prod deploys (s3/sqs/dynamo via /transaction-queue/deploy)
    own their dirs + the key themselves.

PARK (plan/apply/approval failure after retries): the deployment stays
alive — "blocked" state, hourly alerts, 24h hard terminate — while:
  - releasing the promotion key + the SHARED dirs only (kong/gateway dirs):
    the prod queue and gateway waiters move on. Service/bucket/queue/
    db-cluster dirs STAY HELD, so a same-service redeploy during the park
    is impossible (it queues). Under a parent this child signals
    `child_parked` / `child_resuming` and the parent talks to the
    coordinator; standalone talks to it directly. Same wait code either way.
  - CLEARING its routing attributes (DeployPrNumber + DeployProjectName):
    only the ACTIVE deployment owns the PR address — two kong deploys share
    the same (pr, project) pair, so a parked one must not shadow the active
    one. Parked deployments are poll-only (2-min, project-scoped): a fresh
    plan-success comment for OUR project is the WAKE-UP HINT, never the
    evidence — Atlantis keeps ONE pending plan file per (PR, project) and
    apply consumes it, so on resume (locks re-granted at the FIFO tail,
    attributes restored) the workflow RE-POSTS its own `atlantis plan -p`
    and proceeds only on that fresh result (usually a no-change plan when
    someone else's apply already converged the dir).

Signal routing (webhook side, wiring item 11a): comment events resolve by
(DeployPrNumber, DeployProjectName); if that misses — or the comment names
no project — the fallback query is pr-number + WorkflowType =
"DeploymentWorkflow", so stage keeps resolving byte-identically and a prod
workflow can never receive another project's comment. PR-level events
(merged/closed/review) broadcast to every Running workflow on the PR. This
workflow additionally verifies before every signal-driven transition with a
project-scoped comment check — signals are hints, polls are proof.

Manual commits: the feature PR (phase 1, DevLift-owned) keeps stage's
one-shot SHA check. On the promotion PR SHA equality is meaningless (the
head is the shared stage branch), so the rule becomes dir-scoped AND
bot-aware: commits since our stage baseline that touch OUR dirs and are
not all authored by the DevLift bots = a manual edit of our generated
content → terminate + P0 (same escalate-and-drop policy as stage; bot
commits — conflict resolutions, another deploy's gateway regen — pass).
Checked on every active poll tick and immediately before apply.

Human merges the promotion PR over our head ("merged_before_apply" rule):
on pr_merged, one question — did MY apply succeed?
  - yes → the human performed our merge step: skip the gate, delete track
    rows, normal success closeout.
  - no (active pre-apply, or parked — content is now in main UNAPPLIED and
    this PR can no longer apply it) → terminate: statuses ERROR/FAILED,
    in-flight run-track stage + 'completed' failed, track rows FAILED,
    P0 with exact recovery instructions. Loud and manual on purpose — this
    only happens when a human overrides the gate our alerts told them about.

Flow:
  Phase 1 — GET ONTO STAGE:
    1. run_script_pr_workflow → feature PR to stage (+ secondary PRs)
    2. one-shot check_for_manual_commit
    3. production_track_upsert(IN_PROGRESS) — BEFORE the merge, so content
       can never reach the promotion diff without a track row
    4. approve_pr (branch protection; failure → poll_pr_approval_status
       until a human approves) → merge_pr_direct (NO plan) — conflict →
       resolve_conflict_and_merge. No DeployPrNumber for the feature PR:
       after the merge it is inert.

  Phase 2 — PROMOTION (stage→main PR):
    5. ensure_promotion_pr (reuse or create) → upsert DeployPrNumber +
       DeployProjectName → THEN reset signal state (stray earlier events
       either land before the reset and are wiped, or route nowhere).
    6. atlantis plan -p <project> → wait: webhook hint + 30s poll. Tick
       order: (1) poll_pr_state — merged/closed are terminal,
       (2) manual-commit check, (3) poll_for_plan_status (project-scoped).
    7. Plan failed after retries / destructive AI verdict → PARK. Resume →
       track row RESUMING → locks → attributes → RE-PLAN → continue.
       24h → hard terminate, track row FAILED, content stays on stage —
       humans own it from there (alerted throughout).
    8. AI verify → re-approve (every plan discards approvals) →
       manual-commit check → atlantis apply -p → wait, same structure.
    9. Apply success → track row APPLIED → MERGE GATE (evaluate_merge_gate):
       every changed PRODUCTION path must be mine or APPLIED.
         - mergeable → merge_promotion_pr (SHA-conditional, merge commit);
           "head_moved" → re-run the gate against the NEW head: non-prod
           noise (stage envs, atlantis.yaml) still passes → merge with the
           new SHA; new prod content can only be manual (every other prod
           deploy is queued) → blocked. Bounded by GATE_MAX_RETRIES, then
           leave open + alert (next deploy sweeps).
           merged → delete track rows for merged dirs (ours + others'
           APPLIED riding along).
         - blocked → NO merge; loud alert naming each blocker (manual
           content / parked / failed DevLift deploy) and the applied dirs
           now waiting. The PR stays open for the blocker's owner.
    10. CLOSEOUT (merged or blocked — the apply already made the infra
       live): merge secondary PRs, completed DM, DEPLOYED / ONLINE / queue
       DEPLOYED, ALB URL, run-track stage, release own locks (standalone;
       under a parent the parent's finally releases).

Under a parent the final 'completed' run-track stage belongs to the parent;
this child writes 'infra: complete' — same contract as DeploymentWorkflow.
"""

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.temporal.activities.deploy_activities import (
        run_script_pr_workflow,
        post_plan_comment,
        post_apply_comment,
        approve_pr,
        update_queue_status,
        update_deployment_status,
        update_resource_status,
        check_for_manual_commit,
        poll_for_plan_status,
        poll_for_apply_status,
        poll_pr_approval_status,
        poll_pr_state,
        resolve_conflict_and_merge,
        send_p0_alert,
        merge_secondary_pr,
        send_deployment_started_dm,
        send_deployment_completed_dm,
        update_pipeline_run_track_stage,
        update_pipeline_run_track_deploy_result,
        save_service_alb_url,
        verify_plan_with_ai,
        post_plan_verification_comment,
    )
    from app.temporal.activities.admin_alert_activities import report_stale_queue_wait
    from app.temporal.activities.production_deploy_activities import (
        ensure_promotion_pr,
        merge_pr_direct,
        production_track_upsert,
        production_track_set_status,
        production_track_delete_dirs,
        evaluate_merge_gate,
        merge_promotion_pr,
        check_manual_commit_on_dirs,
        resolve_deployment_dirs,
    )
    from app.core.config import settings as _settings
    from app.core.enum import ResourceDeploymentStatusEnum as _DS
    from app.temporal.error_utils import error_message

# ── Timeouts / cadence ───────────────────────────────────────────────────────
LOCK_WAIT_TIMEOUT    = timedelta(hours=24)    # coordinator queue wait (initial + resume)
QUEUE_CHECK_INTERVAL = timedelta(minutes=10)  # re-check the lock holder this often while queued (admin alert if it is dead)
STALE_REALERT_INTERVAL = timedelta(hours=1)  # after the first stale-lock alert, remind admins this often while still blocked
POLL_INTERVAL        = timedelta(seconds=30)  # active waits: signal hint + poll fallback
PARKED_POLL_INTERVAL = timedelta(minutes=2)   # parked waits: slow enough for a 24h park
                                              # to stay far under Temporal's history cap
ALERT_AFTER          = timedelta(minutes=60)  # stuck/parked alert cadence (time-based)
STUCK_MAX_ALERTS     = 24                     # ≈24h of hourly alerts → hard terminate
PLAN_MAX_RETRIES     = 2                      # auto plan retries before parking
APPLY_MAX_RETRIES    = 2                      # auto apply retries before parking
GATE_MAX_RETRIES     = 5                      # head_moved re-evaluations before giving up

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
)


def promotion_lock_key(repo_full_name: str) -> str:
    """The repo-wide serialization key. Just another dir string to the
    coordinator — importable so prepare_multiple_deploy / deploy_temporal can
    add it to the lock set they build."""
    return f"prod-promotion:{repo_full_name}"


@workflow.defn
class ProductionDeploymentWorkflow:

    def __init__(self):
        # Locks
        self._locks_granted: bool = False
        self._force_terminate: bool = False
        self._force_terminate_reason: str = ""

        # Phase 1 (feature PR — polls only, no webhook routing)
        self._feature_pr_number: Optional[int] = None
        self._feature_pr_url: Optional[str] = None
        self._devlift_commit_sha: Optional[str] = None
        self._secondary_prs: list = []
        self._project_name: Optional[str] = None

        # Phase 2 (promotion PR — webhook hints + project-scoped polls)
        self._promotion_pr_number: Optional[int] = None
        self._promotion_pr_url: Optional[str] = None
        self._stage_baseline_sha: Optional[str] = None  # stage head after our merge
        self._plan_head_sha: Optional[str] = None       # promotion head at plan success
        self._plan_result: Optional[str] = None         # "success" | "failed"
        self._plan_error: Optional[str] = None
        self._plan_verification_result: Optional[str] = None
        self._apply_result: Optional[str] = None        # "success" | "failed"
        self._apply_error: Optional[str] = None
        self._merge_result: Optional[str] = None        # "merged"
        self._pr_closed: bool = False
        self._pr_approved: bool = False

        # Identity / bookkeeping
        self._step: str = "starting"
        self._result: Optional[str] = None
        self._tenant_code: str = ""
        self._user_code: str = ""
        self._repo_full_name: str = ""
        self._my_dirs: List[str] = []
        self._shared_dirs: List[str] = []
        self._pr_group: str = "service"
        self._parent_holder_id: Optional[str] = None

    # ── Signals ──────────────────────────────────────────────────────────────
    # Same names as DeploymentWorkflow. Comment-event signals are HINTS: the
    # run loop verifies each with a project-scoped poll before acting.
    # locks_granted doubles as the park-resume ack — sent by the coordinator
    # on a direct grant, or by the parent orchestrator after re-acquiring the
    # batch's locks.

    @workflow.signal
    def locks_granted(self) -> None:
        workflow.logger.info("locks_granted ✓")
        self._locks_granted = True

    @workflow.signal
    def plan_completed(self) -> None:
        workflow.logger.info("Signal: plan_completed (hint)")
        self._plan_result = "success"

    @workflow.signal
    def plan_failed(self, error: str | None = None) -> None:
        workflow.logger.info("Signal: plan_failed (hint)")
        self._plan_result = "failed"
        if error:
            self._plan_error = error

    @workflow.signal
    def lock_conflict_detected(self, locking_pr: int | None = None) -> None:
        # Near-impossible on prod by policy (no other PRs target main), but
        # Atlantis can still report a leftover lock — treated as a plan
        # failure so the normal retry/park path handles it.
        workflow.logger.warning("Signal: lock_conflict_detected locking_pr=%s", locking_pr)
        self._plan_result = "failed"
        self._plan_error = (
            f"Atlantis lock conflict (held by PR #{locking_pr})" if locking_pr
            else "Atlantis lock conflict"
        )

    @workflow.signal
    def atlas_lock_abort(self, reason: str) -> None:
        workflow.logger.warning("Signal: atlas_lock_abort reason=%s", reason)
        self._force_terminate = True
        self._force_terminate_reason = reason

    @workflow.signal
    def apply_completed(self) -> None:
        workflow.logger.info("Signal: apply_completed (hint)")
        self._apply_result = "success"

    @workflow.signal
    def apply_failed(self, error: str | None = None) -> None:
        workflow.logger.info("Signal: apply_failed (hint)")
        self._apply_result = "failed"
        if error:
            self._apply_error = error

    @workflow.signal
    def pr_merged(self) -> None:
        workflow.logger.info("Signal: pr_merged")
        self._merge_result = "merged"

    @workflow.signal
    def pr_closed(self) -> None:
        workflow.logger.info("Signal: pr_closed")
        self._pr_closed = True

    @workflow.signal
    def pr_approved(self) -> None:
        workflow.logger.info("Signal: pr_approved (manual review) ✓")
        self._pr_approved = True

    # ── Run: phase 0 (locks) + phase 1 (get onto stage) ──────────────────────

    @workflow.run
    async def run(self, params: Dict[str, Any]) -> dict:
        """
        params:
          tenant_code, user_code, queue_ids: [int],
          project_dirs: [..]          # MY dirs (service/bucket/queue/db)
          shared_dirs: [..]           # kong/gateway dirs (released on park)
          repo_full_name: "owner/repo"
          source_branch, target_branch  # promotion pair (stage → main)
          parent_holder_id: str|None  # orchestrator id (batch mode)
          track_id: str|None          # shared run-track key (batch mode)
          pr_group: str|None          # tags this child's PRs
        """
        self._tenant_code = params["tenant_code"]
        self._user_code = params["user_code"]
        self._repo_full_name = params["repo_full_name"]
        self._my_dirs = list(params["project_dirs"])
        self._shared_dirs = list(params.get("shared_dirs") or [])
        self._pr_group = params.get("pr_group") or "service"
        self._parent_holder_id = params.get("parent_holder_id")
        queue_ids: List[int] = list(params.get("queue_ids") or [])
        source_branch = params.get("source_branch") or "stage"
        target_branch = params.get("target_branch") or "main"

        workflow_id = workflow.info().workflow_id
        coordinator = workflow.get_external_workflow_handle(f"coordinator-{self._tenant_code}")
        # Same run-track contract as DeploymentWorkflow: batch children write
        # onto the orchestrator's shared row; standalone uses its own id.
        run_track_key = params.get("track_id") or workflow_id
        completed_stage = "infra: complete" if self._parent_holder_id else "completed"

        workflow.upsert_search_attributes({
            "DeployTenantCode": [self._tenant_code],
            "DeployUserCode": [self._user_code],
            "DeployQueueCodes": [",".join(str(i) for i in queue_ids)],
        })

        act_short = dict(start_to_close_timeout=timedelta(minutes=5), retry_policy=RETRY)
        act_long = dict(start_to_close_timeout=timedelta(minutes=30), retry_policy=RETRY)
        act_stage = dict(
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        # Everything downstream needs these bundles + ids — stash once.
        self._ctx = {
            "queue_ids": queue_ids,
            "run_track_key": run_track_key,
            "completed_stage": completed_stage,
            "coordinator": coordinator,
            "act_short": act_short,
            "act_long": act_long,
            "act_stage": act_stage,
            "source_branch": source_branch,
            "target_branch": target_branch,
        }

        # ── Phase 0: acquire locks ───────────────────────────────────────────
        # One request for everything this deploy touches. Under a parent every
        # entry is parent-held → pure passage (grants immediately, ownership
        # stays with the orchestrator). Standalone owns them all.
        lock_set = self._my_dirs + self._shared_dirs + [promotion_lock_key(self._repo_full_name)]
        self._lock_set = lock_set
        self._step = "acquiring_locks"
        if self._parent_holder_id:
            await coordinator.signal(
                "acquire_locks", args=[workflow_id, lock_set, self._user_code, self._parent_holder_id]
            )
        else:
            await coordinator.signal("acquire_locks", args=[workflow_id, lock_set, self._user_code])

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
                        lambda: self._locks_granted or self._force_terminate,
                        timeout=min(QUEUE_CHECK_INTERVAL, LOCK_WAIT_TIMEOUT - waited),
                    )
                    break
                except asyncio.TimeoutError:
                    waited += QUEUE_CHECK_INTERVAL
                    if waited >= LOCK_WAIT_TIMEOUT:
                        raise
                    if last_alert_at is not None and waited - last_alert_at < STALE_REALERT_INTERVAL:
                        continue  # alerted recently — hold the reminder until the interval passes
                    if self._parent_holder_id:
                        continue  # a child passes through its parent's locks — the parent checks
                    # Alerting is best-effort telemetry. A Slack outage or an unreachable
                    # coordinator must never kill a deploy that is only waiting its turn:
                    # an ActivityError is NOT an asyncio.TimeoutError, so without this guard
                    # it escapes the handler below, the run dies without withdrawing from the
                    # coordinator, and its hold_queue entry is later granted to a corpse --
                    # leaving the dirs locked to a workflow that will never release them.
                    try:
                        sent = await workflow.execute_activity(
                            report_stale_queue_wait,
                            args=[self._tenant_code, workflow_id, lock_set, self._user_code, int(waited.total_seconds() // 60), last_alert_at is not None],
                            **act_short,
                        )
                    except Exception:  # noqa: BLE001 - telemetry must never fail the deploy
                        workflow.logger.warning(
                            "stale-queue check failed; retrying on the next leg", exc_info=True
                        )
                        sent = False
                    if sent:
                        last_alert_at = waited
        except asyncio.TimeoutError:
            # Withdraw both ways — whichever state we raced into, one signal
            # cleans it up and the other is a no-op (same as stage).
            await coordinator.signal("release_hold", args=[workflow_id])
            await coordinator.signal("release_locks", args=[workflow_id, lock_set])
            await self._fail_deployment(
                reason=f"Timed out waiting for locks on {lock_set}",
                result_code="lock_wait_timeout",
                fail_stage=None,
                release_locks=False,
            )
        if self._force_terminate:
            await self._fail_deployment(
                reason=self._force_terminate_reason or "aborted_lock_conflict",
                result_code="aborted_lock_conflict",
                fail_stage=None,
            )

        # ── Phase 1: get onto stage ──────────────────────────────────────────
        self._step = "creating_pr"
        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.DEPLOYING], **act_short)
        await workflow.execute_activity(update_queue_status, args=[queue_ids, "STARTING_DEPLOYMENT"], **act_short)
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[run_track_key, "infra: create pr", "running", workflow.now().isoformat(), None, self._pr_group],
            **act_stage,
        )
        try:
            pr_info = await workflow.execute_activity(
                run_script_pr_workflow,
                args=[self._user_code, self._tenant_code, queue_ids],
                **act_long,
            )
        except Exception as e:
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: create pr", "failed", workflow.now().isoformat(),
                      error_message(e), self._pr_group],
                **act_stage,
            )
            await self._fail_deployment(
                reason=f"PR creation failed: {error_message(e)}",
                result_code="pr_creation_failed",
                fail_stage=None,
                deployment_status=_DS.PR_CREATION_FAILED,
            )

        self._feature_pr_number = pr_info.get("pr_number")
        self._feature_pr_url = pr_info.get("pr_url") or None
        self._devlift_commit_sha = pr_info.get("commit_sha") or None
        self._project_name = pr_info.get("project_name") or None
        self._secondary_prs = pr_info.get("secondary_prs", [])
        # NOTE: deliberately NO DeployPrNumber upsert for the feature PR.

        # Record every PR this run produced, same as stage. The promotion PR
        # is added later, once phase 2 has found or created it.
        _label = {"k8s_manifest": "K8S PR", "workflow": "SERVICE PR"}
        _prs = []
        if self._feature_pr_number and self._repo_full_name:
            _prs.append(self._pr_entry(
                "INFRA PR", self._repo_full_name, self._feature_pr_number))
        for _sec in self._secondary_prs:
            _prs.append(self._pr_entry(
                _label.get(_sec.get("pr_type", "workflow"), "SERVICE PR"),
                _sec.get("repo_full_name"), _sec.get("pr_number")))
        await self._record_prs(_prs)

        if not self._feature_pr_number and self._secondary_prs:
            # No infra diff but secondary PRs (workflow yml / k8s manifests)
            # exist — nothing to promote, so phase 2 is skipped entirely:
            # merge the secondaries and complete (mirrors stage's shortcut).
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: create pr", "skipped", workflow.now().isoformat(), None, self._pr_group],
                **act_stage,
            )
            await self._merge_secondary_prs()
            await self._closeout_success(merged=False, note="no infra diff — secondary PRs only")
            return {"status": "DEPLOYED", "pr_number": None}

        if not self._feature_pr_number:
            # No diff anywhere. No resume path on purpose (policy): after a
            # 24h-terminated deploy the content on stage is the humans'.
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: create pr", "failed", workflow.now().isoformat(),
                      "No pull request was created — no diff detected.", self._pr_group],
                **act_stage,
            )
            await self._fail_deployment(
                reason="No pull request was created — no diff detected or PR creation failed silently.",
                result_code="no_pr_created",
                fail_stage=None,
                deployment_status=_DS.PR_CREATION_FAILED,
            )

        # One-shot manual-commit check — the feature PR is DevLift-owned, so
        # SHA equality works here exactly like stage.
        if self._devlift_commit_sha:
            manual = await workflow.execute_activity(
                check_for_manual_commit,
                args=[self._feature_pr_number, self._repo_full_name, self._devlift_commit_sha],
                **act_short,
            )
            if manual:
                await self._fail_deployment(
                    reason="Manual commit detected on the feature PR before merge to stage.",
                    result_code="manual_commit_detected",
                    fail_stage="infra: create pr",
                    alert_title="Deployment Terminated",
                )

        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[run_track_key, "infra: create pr", "success", workflow.now().isoformat(), None, self._pr_group],
            **act_stage,
        )

        # Replace the PREDICTED dirs with the ones this PR actually changed.
        # Prep guesses dirs before generation runs (lock keys need a value up
        # front) and the guess can be wrong — an EKS service's cluster segment
        # is resolved only during config enrichment, so prep said
        # `eks-workloads/backend/...` where the commit went to
        # `eks-workloads/application/...`. Everything below compares dirs to
        # real git paths (track rows, merge gate, manual-commit check, poller
        # dir needles), so from here on they must BE the git paths. Locks are
        # untouched — they were frozen into _lock_set at phase 0 and released
        # from there.
        real_dirs = await workflow.execute_activity(
            resolve_deployment_dirs,
            args=[{
                "tenant_code": self._tenant_code,
                "repo_full_name": self._repo_full_name,
                "pr_number": self._feature_pr_number,
            }],
            **act_short,
        )
        if real_dirs:
            # Drop anything already carried by _shared_dirs. A kong deploy's
            # own content IS the shared gateway dir, so the PR-derived list
            # contains it — and every call site passes
            # `_my_dirs + _shared_dirs`, which would then repeat that dir.
            # production_track_upsert SELECTs-then-INSERTs per dir without
            # flushing between iterations, so a repeat inserts the same dir
            # twice and trips its UNIQUE constraint.
            own = [d for d in real_dirs if d not in self._shared_dirs]
            if sorted(own) != sorted(self._my_dirs):
                workflow.logger.info(
                    "dirs corrected from feature PR #%s: predicted=%s actual=%s"
                    " (shared=%s)",
                    self._feature_pr_number, self._my_dirs, own, self._shared_dirs,
                )
            self._my_dirs = own
        else:
            # No production path in the PR — nothing to correct with. Keep the
            # predicted dirs rather than claiming none: an empty _my_dirs would
            # make the gate treat our own content as unowned.
            workflow.logger.warning(
                "feature PR #%s changed no environment/ paths — keeping "
                "predicted dirs %s", self._feature_pr_number, self._my_dirs,
            )

        # Claim the track rows BEFORE the merge — content must never be able
        # to reach the promotion diff without a row (the merge gate's
        # "no row = manual" rule depends on this ordering).
        await workflow.execute_activity(
            production_track_upsert,
            args=[{
                "dirs": self._my_dirs + self._shared_dirs,
                "workflow_id": workflow_id,
                "tenant_code": self._tenant_code,
                "repo_full_name": self._repo_full_name,
                "status": "IN_PROGRESS",
            }],
            **act_short,
        )

        # Approve (stage branch protection) + merge to stage. Approval failure
        # waits on 30s polls for a manual approval — no webhook on this PR.
        await self._approve_feature_pr()
        await self._merge_feature_pr_to_stage()

        # ── Phase 2 continues in the next chunk ──────────────────────────────
        return await self._run_promotion_phase()

    # ── Phase-1 helpers ──────────────────────────────────────────────────────

    async def _approve_feature_pr(self) -> None:
        """Approve the feature PR (stage branch protection). On failure: one
        P0, then 30s polls for a manual approval — no webhook routes to this
        PR, polls are the only listener. Hourly re-alerts; 24h → terminate."""
        ctx = self._ctx
        self._step = "approving_feature_pr"
        if not _settings.github_approval_enabled:
            return
        self._pr_approved = False
        alerted = False
        waited = timedelta(0)
        while not self._pr_approved:
            try:
                await workflow.execute_activity(
                    approve_pr, args=[self._feature_pr_number, self._repo_full_name],
                    **ctx["act_short"],
                )
                self._pr_approved = True
                break
            except Exception as e:
                if not alerted:
                    alerted = True
                    await workflow.execute_activity(
                        update_deployment_status,
                        args=[ctx["queue_ids"], _DS.APPROVAL_FAILED], **ctx["act_short"],
                    )
                    await workflow.execute_activity(
                        send_p0_alert,
                        args=[
                            self._user_code, self._feature_pr_number,
                            (
                                f"*Error:* {error_message(e)[:300]}\n\n"
                                "⚠️ *Action Required*\n"
                                "• Approve the feature PR manually on GitHub to unblock the prod deployment."
                            ),
                            "Prod Deploy: Approval Failed",
                            self._feature_pr_url,
                        ],
                        **ctx["act_short"],
                    )
            try:
                await workflow.wait_condition(lambda: self._pr_approved, timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                waited += POLL_INTERVAL
                approved = await workflow.execute_activity(
                    poll_pr_approval_status,
                    args=[self._feature_pr_number, self._repo_full_name], **ctx["act_short"],
                )
                if approved:
                    self._pr_approved = True
                elif waited >= ALERT_AFTER * STUCK_MAX_ALERTS:
                    await self._fail_deployment(
                        reason="Feature PR approval stuck for 24 hours.",
                        result_code="approval_stuck_timeout",
                        fail_stage="infra: merge to stage",
                    )
                elif waited and int(waited.total_seconds()) % int(ALERT_AFTER.total_seconds()) < int(POLL_INTERVAL.total_seconds()):
                    await workflow.execute_activity(
                        send_p0_alert,
                        args=[
                            self._user_code, self._feature_pr_number,
                            "⚠️ Still waiting for a manual approval on the feature PR.",
                            "Prod Deploy: Approval Stuck",
                            self._feature_pr_url, False, True,  # channel_only
                        ],
                        **ctx["act_short"],
                    )
        await workflow.execute_activity(
            update_deployment_status,
            args=[ctx["queue_ids"], _DS.APPROVED_SUCCESSFULLY], **ctx["act_short"],
        )

    async def _merge_feature_pr_to_stage(self) -> None:
        """Direct merge (no plan). Conflict → the same resolver stage uses
        (atlantis.yaml appends etc.), then retry — bounded at 3 attempts."""
        ctx = self._ctx
        self._step = "merging_to_stage"
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], "infra: merge to stage", "running",
                  workflow.now().isoformat(), None, self._pr_group],
            **ctx["act_stage"],
        )
        for attempt in range(3):
            outcome = await workflow.execute_activity(
                merge_pr_direct,
                args=[{
                    "tenant_code": self._tenant_code,
                    "repo_full_name": self._repo_full_name,
                    "pr_number": self._feature_pr_number,
                }],
                **ctx["act_short"],
            )
            if outcome in ("merged", "already_merged"):
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], "infra: merge to stage", "success",
                          workflow.now().isoformat(), None, self._pr_group],
                    **ctx["act_stage"],
                )
                return
            if outcome == "conflict":
                workflow.logger.warning(
                    "Feature PR #%s conflicts with stage — resolving (attempt %d/3)",
                    self._feature_pr_number, attempt + 1,
                )
                await workflow.execute_activity(
                    resolve_conflict_and_merge,
                    args=[self._feature_pr_number, self._repo_full_name,
                          self._tenant_code, self._user_code],
                    **ctx["act_short"],
                )
                continue
            workflow.logger.warning(
                "merge_pr_direct failed (attempt %d/3) for PR #%s",
                attempt + 1, self._feature_pr_number,
            )
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], "infra: merge to stage", "failed",
                  workflow.now().isoformat(),
                  "Could not merge the feature PR to stage after 3 attempts.", self._pr_group],
            **ctx["act_stage"],
        )
        await self._fail_deployment(
            reason=f"Could not merge feature PR #{self._feature_pr_number} to stage after 3 attempts.",
            result_code="stage_merge_failed",
            fail_stage=None,
        )

    # ── Phase 2: promotion ───────────────────────────────────────────────────

    async def _run_promotion_phase(self) -> dict:
        """Steps 5-10: promotion PR → plan → verify → approve → apply → gate
        → closeout. Entered once phase 1 has our content merged to stage."""
        ctx = self._ctx
        self._step = "opening_promotion_pr"
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], "infra: promotion pr", "running",
                  workflow.now().isoformat(), None, self._pr_group],
            **ctx["act_stage"],
        )
        promo = await workflow.execute_activity(
            ensure_promotion_pr,
            args=[{
                "tenant_code": self._tenant_code,
                "repo_full_name": self._repo_full_name,
                "source_branch": ctx["source_branch"],
                "target_branch": ctx["target_branch"],
            }],
            **ctx["act_short"],
        )
        self._promotion_pr_number = promo["pr_number"]
        self._promotion_pr_url = promo.get("pr_url")
        # The stage→main PR is where prod actually plans, applies and merges —
        # the link users need most. Prod-only: stage has no promotion step.
        await self._record_prs([self._pr_entry(
            "PROMOTION PR", self._repo_full_name, self._promotion_pr_number)])
        # Baseline for the dir-scoped manual-commit rule: the promotion head
        # right after our merge landed. Anything after this that touches OUR
        # dirs (and isn't all bot-authored) is a manual edit.
        self._stage_baseline_sha = promo.get("head_sha")

        # ORDER MATTERS: publish the routing address FIRST, then wipe the
        # signal slate — a stray earlier event either lands before the reset
        # (wiped) or after the upsert (routes by project, so not to us unless
        # it is genuinely ours).
        self._upsert_routing_attributes()
        self._plan_result = None
        self._plan_error = None
        self._apply_result = None
        self._apply_error = None
        self._merge_result = None
        self._pr_closed = False
        self._pr_approved = False

        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], "infra: promotion pr", "success",
                  workflow.now().isoformat(), None, self._pr_group],
            **ctx["act_stage"],
        )
        await workflow.execute_activity(
            send_deployment_started_dm,
            args=[self._user_code, self._promotion_pr_number, self._promotion_pr_url,
                  self._project_name, ctx["queue_ids"]],
            **ctx["act_short"],
        )
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], "infra: plan pr", "running",
                  workflow.now().isoformat(), None, self._pr_group],
            **ctx["act_stage"],
        )
        await self._post_plan()
        return await self._plan_apply_gate_loop()

    def _upsert_routing_attributes(self) -> None:
        """Publish (pr, project) so comment-event webhooks route exactly to
        us. Cleared on park — only the ACTIVE deployment owns the address."""
        workflow.upsert_search_attributes({
            "DeployPrNumber": [self._promotion_pr_number],
            "DeployProjectName": [self._project_name or ""],
        })

    def _clear_routing_attributes(self) -> None:
        """Parked deployments are poll-only; a parked kong must not shadow an
        active kong that shares the same (pr, project) pair."""
        workflow.upsert_search_attributes({
            "DeployPrNumber": [0],
            "DeployProjectName": [""],
        })

    async def _post_plan(self) -> None:
        """Post `atlantis plan -p <project>` on the promotion PR and reset the
        plan slate so the fresh result (signal or poll) is unambiguous."""
        self._plan_result = None
        self._plan_error = None
        self._step = "waiting_for_plan"
        await workflow.execute_activity(
            post_plan_comment,
            args=[self._promotion_pr_number, self._repo_full_name, self._project_name],
            **self._ctx["act_short"],
        )
        await workflow.execute_activity(
            update_deployment_status,
            args=[self._ctx["queue_ids"], _DS.PLANNING], **self._ctx["act_short"],
        )

    # ── Wait / park machinery (plan, apply and approve share it) ─────────────

    async def _check_manual_commit_tick(self) -> None:
        """Dir-scoped, bot-aware manual-commit check against the stage
        baseline. Touch by a non-bot → escalate and drop (terminate)."""
        touched = await workflow.execute_activity(
            check_manual_commit_on_dirs,
            args=[{
                "tenant_code": self._tenant_code,
                "repo_full_name": self._repo_full_name,
                "base_sha": self._stage_baseline_sha,
                "head_sha": self._ctx["source_branch"],  # branch ref = current stage head
                "dirs": self._my_dirs + self._shared_dirs,
            }],
            **self._ctx["act_short"],
        )
        if touched:
            await self._fail_deployment(
                reason="Manual commit detected on this deployment's directories during the prod deploy.",
                result_code="manual_commit_detected",
                fail_stage="infra: plan pr" if self._apply_result is None else "infra: apply pr",
                alert_title="Prod Deployment Terminated — Manual Commit",
            )

    async def _handle_pr_terminal(self, pr_state: str, fail_stage: str | None = None) -> None:
        """merged/closed on the promotion PR, seen by signal or poll.

        fail_stage is the run-track stage we were sitting in when the terminal
        event arrived ("infra: plan pr", "infra: apply pr", or a park's
        "infra: waiting for manual …"). It is closed on BOTH paths — failed
        when the deployment fails, success when the human's work is adopted —
        so the UI never keeps a stage spinning after the run has ended.

        merged with our own apply NOT done (e.g. humans completed a parked
        deployment manually and merged): adopt the human's work as success
        ONLY under three conditions — (1) an apply-success comment for OUR
        project newer than our stage baseline, (2) no manual (non-bot)
        commit touching our dirs since that baseline, (3) no plan comment
        for our project AFTER that apply-success (a later plan means the
        applied state was not the final word). All three hold → APPLIED →
        success closeout. Anything else → loud ERROR with recovery steps.
        (Adopting comments here is a forced exception to "never trust
        observed artifacts": the PR is merged — re-planning on it is
        impossible, and in doubt we fail loud, never silently succeed.)
        """
        if pr_state == "closed":
            await self._fail_deployment(
                reason="The promotion PR was closed before the deployment completed.",
                result_code="pr_closed",
                fail_stage=fail_stage,
            )
        if self._apply_result == "success":
            return  # caller handles the success closeout

        adopted = False
        try:
            applied = await workflow.execute_activity(
                poll_for_apply_status,
                args=[self._promotion_pr_number, self._repo_full_name,
                      self._project_name, self._my_dirs + self._shared_dirs],
                **self._ctx["act_short"],
            )
            if (applied or "").split(":", 1)[0] == "success":
                manual = await workflow.execute_activity(
                    check_manual_commit_on_dirs,
                    args=[{
                        "tenant_code": self._tenant_code,
                        "repo_full_name": self._repo_full_name,
                        "base_sha": self._stage_baseline_sha,
                        "head_sha": self._ctx["source_branch"],
                        "dirs": self._my_dirs + self._shared_dirs,
                    }],
                    **self._ctx["act_short"],
                )
                if not manual:
                    # Condition 3 — no plan for our project after the apply
                    # success: poll_for_plan_status with not_before=None
                    # returns the LATEST plan/apply-ordering evidence; the
                    # activity's project filter + comment ordering rules
                    # implement "plan after apply → not adoptable".
                    later_plan = await workflow.execute_activity(
                        poll_for_plan_status,
                        args=[self._promotion_pr_number, self._repo_full_name,
                              None, self._project_name, True,  # after_last_apply=True
                              self._my_dirs + self._shared_dirs],
                        **self._ctx["act_short"],
                    )
                    adopted = later_plan in (None, "", "none")
        except Exception as exc:
            workflow.logger.warning("merged-adoption evidence check failed: %s", exc)

        if adopted:
            workflow.logger.info(
                "PR merged with our apply done manually — adopting as success"
            )
            self._apply_result = "success"
            self._merge_result = "merged"
            # Our content is now on main, so the track rows have done their job
            # — they exist to tell the gate which pending dirs are DevLift's.
            # Deletion normally happens right after OUR merge; adoption never
            # reaches that branch, so without this every human-merged promotion
            # PR leaves rows behind for good. `dir` is UNIQUE, so the leftovers
            # make the next deploy to the same dir take over a stale row rather
            # than insert a fresh one, and leave dead workflow ids for the
            # gate's liveness healing to chew through.
            await workflow.execute_activity(
                production_track_delete_dirs,
                args=[{
                    "repo_full_name": self._repo_full_name,
                    "dirs": self._my_dirs + self._shared_dirs,
                }],
                **self._ctx["act_short"],
            )
            # Close the books on the run track. Adoption skips the plan →
            # apply → gate loop, which is what normally writes these stages,
            # so without this the stage we were sitting in stays "running"
            # forever (a spinner on a finished deploy) and the apply + merge
            # that DID happen never show up at all.
            _now = workflow.now().isoformat()
            _stages: List[str] = []
            for _s in (fail_stage, "infra: apply pr", "infra: merge pr"):
                # fail_stage IS one of the latter two when the merge arrives
                # during an active wait — write each name once.
                if _s and _s not in _stages:
                    _stages.append(_s)
            for _stage in _stages:
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[self._ctx["run_track_key"], _stage, "success",
                          _now, None, self._pr_group],
                    **self._ctx["act_stage"],
                )
            return  # caller proceeds to the success closeout

        await self._fail_deployment(
            reason=(
                "The promotion PR was merged BEFORE this deployment applied, and no "
                "clean apply evidence exists for our project. "
                f"The content for {self._my_dirs} is in {self._ctx['target_branch']} but may NOT be applied. "
                f"Recovery: on the next promotion PR run `atlantis plan -p {self._project_name}` "
                "and apply, or revert and redeploy from DevLift."
            ),
            result_code="merged_before_apply",
            # Close whichever stage was open (e.g. 'infra: waiting for manual
            # apply' during a park) — without it the run-track spinner on that
            # stage hangs forever (observed on the merged-while-parked pilot).
            fail_stage=fail_stage,
            alert_title="Prod Deploy: PR merged before apply",
        )

    async def _wait_for_result(self, kind: str, not_before: str) -> str:
        """Wait for a plan/apply outcome on the promotion PR.

        Trust model: only COMMENT-derived signals (plan/apply results) are
        hints — a comment naming our project is not necessarily the answer to
        OUR request (e.g. a human ran `atlantis plan -p our-project`
        mid-wait), so before acting we confirm with one project-scoped poll:
        "latest result for MY project newer than MY trigger". PR-level facts
        (merged/closed) and internal signals (locks) are authoritative as-is.

        A self-lock result ("workspace locked by another command on this
        PR") is "busy", not "failed": an in-flight operation (often a
        human's apply we raced) — retried every POLL_INTERVAL without
        consuming plan/apply retries, bounded by BUSY_BUDGET.

        Tick order: pr state → manual commit → result poll. Hourly stuck
        alerts; 24h stuck → terminate. Returns "success" | "failed" |
        "merged_early" (PR merged after/with adoptable apply).
        """
        ctx = self._ctx
        flag = "_plan_result" if kind == "plan" else "_apply_result"
        poll_activity = poll_for_plan_status if kind == "plan" else poll_for_apply_status
        BUSY_BUDGET = timedelta(minutes=20)
        # Busy = a self-lock REJECTED our command ("locked by another command
        # ... for this pull request") — no result will ever come from it, so
        # it must be re-sent once the blocking operation finishes. Re-post
        # every 2 minutes while busy (not every 30s tick — each posted
        # comment makes Atlantis attempt a run; spamming a busy workspace
        # just floods the PR). Everything non-busy keeps the 30s cadence.
        BUSY_REPOST_AFTER = timedelta(minutes=2)
        busy_waited = timedelta(0)
        since_busy_repost = timedelta(0)
        waited = timedelta(0)
        stuck_alerts = 0

        def _poll_args():
            args = [self._promotion_pr_number, self._repo_full_name]
            if kind == "plan":
                args.append(not_before)
            args.append(self._project_name)
            if kind == "plan":
                args.append(False)  # after_last_apply
            # Dir needles too: Atlantis error comments sometimes omit the
            # project name and key by dir ("Ran Plan for dir: ...").
            args.append(self._my_dirs + self._shared_dirs)
            return args

        async def _classify(raw: Optional[str]) -> Optional[str]:
            """success | failed | busy | None (no result yet)."""
            base = (raw or "").split(":", 1)[0]
            if base == "busy":
                return "busy"  # self-lock: our command was rejected, re-post
            if base.startswith("lock_conflict"):
                # Cross-PR Atlantis lock — near-impossible on prod by policy;
                # route through the normal failure/park path.
                if kind == "plan":
                    self._plan_error = raw
                return "failed"
            if base in ("success", "failed"):
                if base == "failed":
                    parts = (raw or "").split(":", 1)
                    err = parts[1] if len(parts) > 1 else None
                    if kind == "plan":
                        self._plan_error = err or self._plan_error
                    else:
                        self._apply_error = err or self._apply_error
                return base
            return None

        while True:
            if self._merge_result:
                await self._handle_pr_terminal("merged", fail_stage=f"infra: {kind} pr")
                return "merged_early"
            if self._pr_closed:
                await self._handle_pr_terminal("closed", fail_stage=f"infra: {kind} pr")

            if getattr(self, flag) is not None:
                # Hint received — verify with the authoritative poll.
                confirmed = await workflow.execute_activity(
                    poll_activity, args=_poll_args(), **ctx["act_short"],
                )
                verdict = await _classify(confirmed)
                if verdict in ("success", "failed"):
                    setattr(self, flag, verdict)
                    return verdict
                setattr(self, flag, None)  # unconfirmed or busy — keep waiting
                if verdict == "busy":
                    busy_waited += POLL_INTERVAL
                    since_busy_repost += POLL_INTERVAL

            try:
                await workflow.wait_condition(
                    lambda: getattr(self, flag) is not None or self._merge_result
                    or self._pr_closed or self._force_terminate,
                    timeout=POLL_INTERVAL,
                )
                continue  # a flag flipped — loop re-checks and verifies
            except asyncio.TimeoutError:
                pass

            waited += POLL_INTERVAL
            pr_state = await workflow.execute_activity(
                poll_pr_state,
                args=[self._promotion_pr_number, self._repo_full_name], **ctx["act_short"],
            )
            if pr_state == "merged":
                self._merge_result = "merged"
                continue
            if pr_state == "closed":
                self._pr_closed = True
                continue
            await self._check_manual_commit_tick()
            polled = await workflow.execute_activity(
                poll_activity, args=_poll_args(), **ctx["act_short"],
            )
            verdict = await _classify(polled)
            if verdict in ("success", "failed"):
                setattr(self, flag, verdict)
                return verdict
            if verdict == "busy":
                busy_waited += POLL_INTERVAL
                since_busy_repost += POLL_INTERVAL
                if busy_waited >= BUSY_BUDGET:
                    return "failed"  # something is wedged — normal failure path
                if since_busy_repost >= BUSY_REPOST_AFTER:
                    # Re-post the rejected command (plan OR apply) — the
                    # operation that blocked us has had time to finish.
                    since_busy_repost = timedelta(0)
                    post = post_plan_comment if kind == "plan" else post_apply_comment
                    await workflow.execute_activity(
                        post,
                        args=[self._promotion_pr_number, self._repo_full_name, self._project_name],
                        **ctx["act_short"],
                    )
                continue

            if waited >= ALERT_AFTER:
                stuck_alerts += 1
                waited = timedelta(0)
                if stuck_alerts >= STUCK_MAX_ALERTS:
                    await self._fail_deployment(
                        reason=f"{kind} stuck for 24 hours with no result from Atlantis.",
                        result_code=f"{kind}_stuck_timeout",
                        fail_stage=f"infra: {kind} pr",
                    )
                await workflow.execute_activity(
                    send_p0_alert,
                    args=[
                        self._user_code, self._promotion_pr_number,
                        f"*Status:* {kind} in progress >60 min on the promotion PR.\n"
                        "⚠️ Check Atlantis for the current status.",
                        f"Prod {kind.capitalize()} Stuck Alert",
                        self._promotion_pr_url, False, True,  # channel_only
                    ],
                    **ctx["act_short"],
                )

    async def _park(self, kind: str, reason: str) -> None:
        """Blocked state: step aside, stay alive, wait for a human fix.

        kind: "plan" | "apply" | "approve" — decides the wake condition:
          plan    → a fresh plan-success comment for OUR project
          apply   → a fresh plan-success OR apply-success for OUR project
          approve → the PR is approved (poll_pr_approval_status / signal)

        Releases the promotion key + SHARED dirs (parent does it for a batch;
        standalone talks to the coordinator) and clears routing attributes —
        parked deployments are poll-only. Resume ALWAYS re-enters at re-plan
        (uniform: the observed artifacts are wake hints, never evidence).
        Hourly re-alerts; 24h → hard terminate.
        """
        ctx = self._ctx
        workflow_id = workflow.info().workflow_id
        self._step = f"blocked_{kind}"
        status_map = {"plan": _DS.PLAN_FAILED, "apply": _DS.APPLY_FAILED, "approve": _DS.APPROVAL_FAILED}
        await workflow.execute_activity(
            update_deployment_status,
            args=[ctx["queue_ids"], status_map[kind]], **ctx["act_short"],
        )
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], f"infra: waiting for manual {kind}", "running",
                  workflow.now().isoformat(), None, self._pr_group],
            **ctx["act_stage"],
        )
        self._clear_routing_attributes()
        released = [promotion_lock_key(self._repo_full_name)] + self._shared_dirs
        if self._parent_holder_id:
            parent = workflow.get_external_workflow_handle(self._parent_holder_id)
            await parent.signal("child_parked", args=[workflow_id, released])
        else:
            await ctx["coordinator"].signal("release_locks", args=[workflow_id, released])
        parked_at = workflow.now().isoformat()
        fix_hint = (
            f"• Fix the cause, then comment `atlantis plan -p {self._project_name}` on the "
            "promotion PR — the deployment resumes automatically."
            if kind != "approve"
            else "• Approve the PR — the deployment resumes automatically."
        )
        await workflow.execute_activity(
            send_p0_alert,
            args=[
                self._user_code, self._promotion_pr_number,
                (
                    f"*Reason:* {reason}\n\n"
                    "This deployment is PARKED but STILL ALIVE — it resumes on its own; "
                    "you do NOT need to merge or complete it manually.\n"
                    "⚠️ *Action Required*\n"
                    f"{fix_hint}\n"
                    "• Do NOT hand-edit the generated files on stage — that terminates the "
                    "deployment (fix the source in DevLift and redeploy instead).\n"
                    "• If you do finish it manually anyway: apply BEFORE merging.\n"
                    "• Without a fix it hard-fails in 24 hours."
                ),
                f"Prod Deploy PARKED — {kind} failed",
                self._promotion_pr_url,
            ],
            **ctx["act_short"],
        )

        # ── Parked wait: 2-min project-scoped polls, hourly re-alerts ────────
        waited_total = timedelta(0)
        since_alert = timedelta(0)
        woke = False
        while not woke:
            try:
                await workflow.wait_condition(
                    lambda: self._merge_result or self._pr_closed or self._force_terminate
                    or (kind == "approve" and self._pr_approved),
                    timeout=PARKED_POLL_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass
            if self._merge_result:
                await self._handle_pr_terminal(
                    "merged", fail_stage=f"infra: waiting for manual {kind}")
                return  # adopted as success — caller sees _merge_result set
            if self._pr_closed:
                await self._handle_pr_terminal(
                    "closed", fail_stage=f"infra: waiting for manual {kind}")
            waited_total += PARKED_POLL_INTERVAL
            since_alert += PARKED_POLL_INTERVAL
            if waited_total >= timedelta(hours=24):
                await self._fail_deployment(
                    reason=f"Parked deployment not resolved within 24 hours ({kind} failure: {reason}).",
                    result_code=f"{kind}_park_expired",
                    fail_stage=f"infra: waiting for manual {kind}",
                    alert_title="Prod Deploy DROPPED — park expired",
                )
            pr_state = await workflow.execute_activity(
                poll_pr_state,
                args=[self._promotion_pr_number, self._repo_full_name], **ctx["act_short"],
            )
            if pr_state == "merged":
                await self._handle_pr_terminal(
                    "merged", fail_stage=f"infra: waiting for manual {kind}")
                return
            if pr_state == "closed":
                await self._handle_pr_terminal(
                    "closed", fail_stage=f"infra: waiting for manual {kind}")

            if kind == "approve":
                approved = await workflow.execute_activity(
                    poll_pr_approval_status,
                    args=[self._promotion_pr_number, self._repo_full_name], **ctx["act_short"],
                )
                woke = bool(approved) or self._pr_approved
            else:
                polled = await workflow.execute_activity(
                    poll_for_plan_status,
                    args=[self._promotion_pr_number, self._repo_full_name, parked_at,
                          self._project_name, False, self._my_dirs + self._shared_dirs],
                    **ctx["act_short"],
                )
                woke = (polled or "").split(":", 1)[0] == "success"
                if not woke and kind == "apply":
                    applied = await workflow.execute_activity(
                        poll_for_apply_status,
                        args=[self._promotion_pr_number, self._repo_full_name,
                              self._project_name, self._my_dirs + self._shared_dirs],
                        **ctx["act_short"],
                    )
                    woke = (applied or "").split(":", 1)[0] == "success"
            if woke:
                break
            if since_alert >= ALERT_AFTER:
                since_alert = timedelta(0)
                await workflow.execute_activity(
                    send_p0_alert,
                    args=[
                        self._user_code, self._promotion_pr_number,
                        f"⚠️ Still parked ({kind} failed): {reason}\n{fix_hint}",
                        "Prod Deploy still PARKED",
                        self._promotion_pr_url, False, True,  # channel_only
                    ],
                    **ctx["act_short"],
                )

        # ── Resume: rows RESUMING → re-acquire → restore address ─────────────
        await workflow.execute_activity(
            production_track_set_status,
            args=[{
                "dirs": self._my_dirs + self._shared_dirs,
                "workflow_id": workflow_id,
                "status": "RESUMING",
            }],
            **ctx["act_short"],
        )
        self._locks_granted = False
        self._step = f"resuming_{kind}"
        if self._parent_holder_id:
            parent = workflow.get_external_workflow_handle(self._parent_holder_id)
            await parent.signal("child_resuming", args=[workflow_id, released])
        else:
            await ctx["coordinator"].signal(
                "acquire_locks", args=[workflow_id, released, self._user_code]
            )
        try:
            # The fourth lock wait, and the one the initial-acquire change missed.
            # Same legged wait as run(): re-check the holder every
            # QUEUE_CHECK_INTERVAL and tell admins when it is dead. Without it a
            # resume blocked by a lock nobody will ever release sat silent for
            # the full 24h and then died with resume_lock_timeout -- and the
            # dashboard could not surface it either, because this run's stage is
            # the park stage, not "waiting in queue".
            waited = timedelta(0)
            last_alert_at: timedelta | None = None  # `waited` when the last alert was actually sent
            while True:
                try:
                    await workflow.wait_condition(
                        lambda: self._locks_granted or self._force_terminate,
                        timeout=min(QUEUE_CHECK_INTERVAL, LOCK_WAIT_TIMEOUT - waited),
                    )
                    break
                except asyncio.TimeoutError:
                    waited += QUEUE_CHECK_INTERVAL
                    if waited >= LOCK_WAIT_TIMEOUT:
                        raise
                    if last_alert_at is not None and waited - last_alert_at < STALE_REALERT_INTERVAL:
                        continue  # alerted recently — hold the reminder until the interval passes
                    if self._parent_holder_id:
                        continue  # the parent re-acquires on our behalf — it does the checking
                    # Alerting is best-effort telemetry. A Slack outage or an unreachable
                    # coordinator must never kill a deploy that is only waiting its turn:
                    # an ActivityError is NOT an asyncio.TimeoutError, so without this guard
                    # it escapes the handler below, the run dies without withdrawing from the
                    # coordinator, and its hold_queue entry is later granted to a corpse --
                    # leaving the dirs locked to a workflow that will never release them.
                    try:
                        sent = await workflow.execute_activity(
                            report_stale_queue_wait,
                            args=[self._tenant_code, workflow_id, released, self._user_code, int(waited.total_seconds() // 60), last_alert_at is not None],
                            **ctx["act_short"],
                        )
                    except Exception:  # noqa: BLE001 - telemetry must never fail the deploy
                        workflow.logger.warning(
                            "stale-queue check failed; retrying on the next leg", exc_info=True
                        )
                        sent = False
                    if sent:
                        last_alert_at = waited
        except asyncio.TimeoutError:
            if not self._parent_holder_id:
                await ctx["coordinator"].signal("release_hold", args=[workflow_id])
            await self._fail_deployment(
                reason="Timed out re-acquiring locks after park resume.",
                result_code="resume_lock_timeout",
                fail_stage=f"infra: waiting for manual {kind}",
            )
        self._upsert_routing_attributes()
        # Active again. The caller re-enters at RE-PLAN — uniformly, whatever
        # the park kind: the wake evidence is a hint, our own fresh plan is
        # the proof (usually "No changes" when a human already converged it).
        await workflow.execute_activity(
            production_track_set_status,
            args=[{
                "dirs": self._my_dirs + self._shared_dirs,
                "workflow_id": workflow_id,
                "status": "IN_PROGRESS",
            }],
            **ctx["act_short"],
        )
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], f"infra: waiting for manual {kind}", "success",
                  workflow.now().isoformat(), None, self._pr_group],
            **ctx["act_stage"],
        )

    # ── The plan → verify → approve → apply state machine ────────────────────

    async def _plan_apply_gate_loop(self) -> dict:
        """Outer loop: every park resumes back here at re-plan (uniform)."""
        ctx = self._ctx
        plan_attempt = 0
        while True:
            # ── PLAN ─────────────────────────────────────────────────────────
            plan_posted_at = workflow.now().isoformat()
            result = await self._wait_for_result("plan", plan_posted_at)
            if result == "merged_early":
                return await self._closeout_success(merged=True, note="merged with adopted apply")
            if result == "failed":
                plan_attempt += 1
                if plan_attempt < PLAN_MAX_RETRIES:
                    workflow.logger.warning(
                        "Plan failed (attempt %d/%d) — retrying", plan_attempt, PLAN_MAX_RETRIES,
                    )
                    await self._post_plan()
                    continue
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], "infra: plan pr", "failed",
                          workflow.now().isoformat(),
                          self._plan_error or f"Plan failed after {plan_attempt} attempts.",
                          self._pr_group],
                    **ctx["act_stage"],
                )
                await self._park("plan", self._plan_error or "Terraform plan failed")
                if self._merge_result:  # park exited via merged-adoption
                    return await self._closeout_success(merged=True, note="merged with adopted apply")
                plan_attempt = 0
                await self._post_plan()
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], "infra: plan pr", "running",
                          workflow.now().isoformat(), None, self._pr_group],
                    **ctx["act_stage"],
                )
                continue

            workflow.logger.info("Plan succeeded ✓")
            plan_attempt = 0
            await workflow.execute_activity(
                update_deployment_status,
                args=[ctx["queue_ids"], _DS.PLANNED_SUCCESSFULLY], **ctx["act_short"],
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[ctx["run_track_key"], "infra: plan pr", "success",
                      workflow.now().isoformat(), None, self._pr_group],
                **ctx["act_stage"],
            )

            # ── AI VERIFY (fail open on LLM errors, same as stage) ───────────
            self._step = "verifying_plan"
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[ctx["run_track_key"], "infra: devlift ai review", "running",
                      workflow.now().isoformat(), None, self._pr_group],
                **ctx["act_stage"],
            )
            try:
                verdict = await workflow.execute_activity(
                    verify_plan_with_ai,
                    args=[self._promotion_pr_number, self._repo_full_name, plan_posted_at],
                    **ctx["act_long"],
                )
            except Exception as e:
                workflow.logger.warning("verify_plan_with_ai failed — failing open: %s", e)
                verdict = {"is_destructive": False}

            if verdict.get("is_destructive"):
                await workflow.execute_activity(
                    post_plan_verification_comment,
                    args=[self._promotion_pr_number, self._repo_full_name, verdict],
                    **ctx["act_short"],
                )
                await workflow.execute_activity(
                    update_deployment_status,
                    args=[ctx["queue_ids"], _DS.DESTRUCTIVE_PLAN_DETECTED], **ctx["act_short"],
                )
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], "infra: devlift ai review", "failed",
                          workflow.now().isoformat(),
                          ("Destructive plan detected: "
                           + (verdict.get("summary") or "destroys/replaces resources."))[:500],
                          self._pr_group],
                    **ctx["act_stage"],
                )
                await self._park(
                    "plan",
                    "Destructive plan detected — the plan would DESTROY or REPLACE prod resources. "
                    "Fix the source in DevLift and redeploy, or re-plan after correcting.",
                )
                if self._merge_result:
                    return await self._closeout_success(merged=True, note="merged with adopted apply")
                await self._post_plan()
                continue

            if verdict.get("plan_found"):
                await workflow.execute_activity(
                    post_plan_verification_comment,
                    args=[self._promotion_pr_number, self._repo_full_name, verdict],
                    **ctx["act_short"],
                )
            await workflow.execute_activity(
                update_deployment_status,
                args=[ctx["queue_ids"], _DS.PLAN_VERIFIED_SUCCESSFULLY], **ctx["act_short"],
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[ctx["run_track_key"], "infra: devlift ai review", "success",
                      workflow.now().isoformat(), None, self._pr_group],
                **ctx["act_stage"],
            )

            # ── RE-APPROVE (every plan discards approvals) ───────────────────
            if _settings.github_approval_enabled:
                self._step = "approving_promotion_pr"
                try:
                    await workflow.execute_activity(
                        approve_pr,
                        args=[self._promotion_pr_number, self._repo_full_name],
                        **ctx["act_short"],
                    )
                except Exception as e:
                    workflow.logger.warning("approve_pr failed on promotion PR: %s", e)
                    await self._park("approve", f"PR approval failed: {error_message(e)[:200]}")
                    if self._merge_result:
                        return await self._closeout_success(merged=True, note="merged with adopted apply")
                    await self._post_plan()  # uniform resume: re-plan
                    continue

            # ── PRE-APPLY manual-commit checkpoint ───────────────────────────
            await self._check_manual_commit_tick()

            # ── APPLY ────────────────────────────────────────────────────────
            await workflow.execute_activity(
                update_deployment_status,
                args=[ctx["queue_ids"], _DS.APPLYING], **ctx["act_short"],
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[ctx["run_track_key"], "infra: apply pr", "running",
                      workflow.now().isoformat(), None, self._pr_group],
                **ctx["act_stage"],
            )
            apply_attempt = 0
            apply_ok = False
            while True:
                self._apply_result = None
                self._apply_error = None
                self._step = "waiting_for_apply"
                await workflow.execute_activity(
                    post_apply_comment,
                    args=[self._promotion_pr_number, self._repo_full_name, self._project_name],
                    **ctx["act_short"],
                )
                result = await self._wait_for_result("apply", plan_posted_at)
                if result == "merged_early":
                    return await self._closeout_success(merged=True, note="merged with adopted apply")
                if result == "success":
                    apply_ok = True
                    break
                apply_attempt += 1
                # Approval-blocked applies get a re-approve + retry without
                # consuming an attempt (approval churn is external). PRECISE
                # phrase only — Atlantis says "Apply Failed: Pull request must
                # be approved according to the project's approval rules…" — a
                # loose "approv" substring matched "--auto-approve" inside
                # every terragrunt error output and looped the deploy forever
                # (first pilot run). This branch can only repeat if approval
                # is genuinely dismissed again externally; the approval PAT is
                # a real human's, so it always satisfies the approval rules.
                if "must be approved" in (self._apply_error or "").lower():
                    workflow.logger.warning("Apply blocked on approval — re-approving and retrying")
                    try:
                        await workflow.execute_activity(
                            approve_pr,
                            args=[self._promotion_pr_number, self._repo_full_name],
                            **ctx["act_short"],
                        )
                        apply_attempt -= 1
                        continue
                    except Exception:
                        pass
                if apply_attempt < APPLY_MAX_RETRIES:
                    workflow.logger.warning(
                        "Apply failed (attempt %d/%d) — retrying", apply_attempt, APPLY_MAX_RETRIES,
                    )
                    continue
                break

            if not apply_ok:
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], "infra: apply pr", "failed",
                          workflow.now().isoformat(),
                          self._apply_error or f"Apply failed after {apply_attempt} attempts.",
                          self._pr_group],
                    **ctx["act_stage"],
                )
                await self._park("apply", self._apply_error or "Terraform apply failed")
                if self._merge_result:
                    return await self._closeout_success(merged=True, note="merged with adopted apply")
                await self._post_plan()  # uniform resume: re-plan
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], "infra: plan pr", "running",
                          workflow.now().isoformat(), None, self._pr_group],
                    **ctx["act_stage"],
                )
                continue

            # ── APPLIED ✓ → track rows → merge gate ──────────────────────────
            workflow.logger.info("Apply succeeded ✓ — infra is live")
            await workflow.execute_activity(
                update_deployment_status,
                args=[ctx["queue_ids"], _DS.APPLIED_SUCCESSFULLY], **ctx["act_short"],
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[ctx["run_track_key"], "infra: apply pr", "success",
                      workflow.now().isoformat(), None, self._pr_group],
                **ctx["act_stage"],
            )
            await workflow.execute_activity(
                production_track_set_status,
                args=[{
                    "dirs": self._my_dirs + self._shared_dirs,
                    "workflow_id": workflow.info().workflow_id,
                    "status": "APPLIED",
                }],
                **ctx["act_short"],
            )
            return await self._merge_gate_and_closeout()

    # ── Merge gate + closeout ────────────────────────────────────────────────

    async def _merge_gate_and_closeout(self) -> dict:
        """Apply is done — decide merge vs leave-open, then close out. Both
        paths end DEPLOYED: the infra is live either way."""
        ctx = self._ctx
        self._step = "merge_gate"
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], "infra: merge pr", "running",
                  workflow.now().isoformat(), None, self._pr_group],
            **ctx["act_stage"],
        )
        gate = None
        for attempt in range(GATE_MAX_RETRIES):
            gate = await workflow.execute_activity(
                evaluate_merge_gate,
                args=[{
                    "tenant_code": self._tenant_code,
                    "repo_full_name": self._repo_full_name,
                    "pr_number": self._promotion_pr_number,
                    "my_dirs": self._my_dirs + self._shared_dirs,
                }],
                **ctx["act_short"],
            )
            if not gate["mergeable"]:
                break
            outcome = await workflow.execute_activity(
                merge_promotion_pr,
                args=[{
                    "tenant_code": self._tenant_code,
                    "repo_full_name": self._repo_full_name,
                    "pr_number": self._promotion_pr_number,
                    "expected_head_sha": gate["head_sha"],
                }],
                **ctx["act_short"],
            )
            if outcome == "merged":
                self._merge_result = "merged"
                await workflow.execute_activity(
                    production_track_delete_dirs,
                    args=[{
                        "repo_full_name": self._repo_full_name,
                        "dirs": gate["merged_dirs"],
                    }],
                    **ctx["act_short"],
                )
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], "infra: merge pr", "success",
                          workflow.now().isoformat(), None, self._pr_group],
                    **ctx["act_stage"],
                )
                return await self._closeout_success(merged=True)
            if outcome == "head_moved":
                workflow.logger.info(
                    "Merge refused — head moved (attempt %d/%d), re-evaluating gate",
                    attempt + 1, GATE_MAX_RETRIES,
                )
                continue
            # outcome == "failed": leave open like a blocked gate — the apply
            # already succeeded, a human or the next deploy finishes the merge.
            workflow.logger.warning("merge_promotion_pr failed — leaving the PR open")
            break

        # ── Blocked (or merge gave up): leave open + loud alert ──────────────
        blockers = (gate or {}).get("blockers") or []
        blocker_lines = "\n".join(
            f"• `{b['dir']}` — "
            + ("manual content (not created by DevLift) — review & merge when ready"
               if b["kind"] == "manual"
               else f"DevLift deployment {b.get('status', '?')} (workflow {b.get('workflow_id') or 'unknown'})")
            for b in blockers
        ) or "• merge attempts exhausted (head kept moving / merge API failed)"
        waiting = (gate or {}).get("applied_waiting") or []
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], "infra: merge pr", "failed",
                  workflow.now().isoformat(),
                  "Applied but NOT merged — the promotion PR contains content that is not "
                  "DevLift-applied. See the alert for the blocker list.", self._pr_group],
            **ctx["act_stage"],
        )
        await workflow.execute_activity(
            send_p0_alert,
            args=[
                self._user_code, self._promotion_pr_number,
                (
                    "*This deployment APPLIED successfully and is DEPLOYED.* The promotion "
                    "PR was NOT merged because it contains content that is not DevLift-applied:\n"
                    f"{blocker_lines}\n"
                    + (f"\nApplied and waiting to ride the next merge: {', '.join(waiting)}\n" if waiting else "")
                    + "\n⚠️ Review the blockers, then merge the PR (or let the next clean "
                    "DevLift deploy sweep it)."
                ),
                "Prod promotion PR left open — review needed",
                self._promotion_pr_url,
            ],
            **ctx["act_short"],
        )
        return await self._closeout_success(merged=False, note="applied; promotion PR left open")

    async def _closeout_success(self, merged: bool, note: Optional[str] = None) -> dict:
        """DEPLOYED closeout — runs whether the PR merged or stayed open
        (the apply already made the infra live)."""
        ctx = self._ctx
        self._step = "closeout"
        await self._merge_secondary_prs()
        await workflow.execute_activity(
            send_deployment_completed_dm,
            args=[self._user_code, self._promotion_pr_number, self._repo_full_name,
                  self._promotion_pr_url, self._project_name, ctx["queue_ids"]],
            **ctx["act_short"],
        )
        await workflow.execute_activity(
            update_deployment_status, args=[ctx["queue_ids"], _DS.DEPLOYED], **ctx["act_short"],
        )
        await workflow.execute_activity(
            update_resource_status, args=[ctx["queue_ids"], "ONLINE"], **ctx["act_short"],
        )
        await workflow.execute_activity(
            update_queue_status, args=[ctx["queue_ids"], "DEPLOYED"], **ctx["act_short"],
        )
        try:
            await workflow.execute_activity(
                save_service_alb_url,
                args=[ctx["run_track_key"], ctx["queue_ids"]], **ctx["act_short"],
            )
        except Exception as e:
            workflow.logger.warning("save_service_alb_url failed (non-fatal): %s", e)
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], ctx["completed_stage"], "success",
                  workflow.now().isoformat(), None, self._pr_group],
            **ctx["act_stage"],
        )
        if not self._parent_holder_id:
            await ctx["coordinator"].signal(
                "release_locks", args=[workflow.info().workflow_id, self._lock_set]
            )
        self._step = "active"
        self._result = "success"
        workflow.logger.info(
            "Prod deployment DEPLOYED ✓ (merged=%s%s)", merged, f", {note}" if note else "",
        )
        return {
            "status": "DEPLOYED",
            "merged": merged,
            "pr_number": self._promotion_pr_number,
            "note": note,
        }

    async def _record_prs(self, prs: List[dict]) -> None:
        """Persist PR links into deploy_result.prs so the run card shows them,
        exactly as stage does. The activity is additive and deduped by
        (repo, number), so several children sharing ONE multi-deploy row all
        keep their PRs, and calling it again for the promotion PR appends
        rather than replacing what phase 1 wrote."""
        prs = [p for p in prs if p.get("repo") and p.get("number")]
        if not prs:
            return
        await workflow.execute_activity(
            update_pipeline_run_track_deploy_result,
            args=[self._ctx["run_track_key"], {"prs": prs}],
            **self._ctx["act_stage"],
        )

    def _pr_entry(self, name: str, repo: str, number: int) -> dict:
        return {
            "name": name,
            "url": f"https://github.com/{repo}/pull/{number}",
            "repo": repo,
            "number": number,
            "group": self._pr_group,
        }

    async def _merge_secondary_prs(self) -> None:
        """Merge k8s-manifests / workflow-YAML PRs — same per-artifact stages
        as stage's flow; a failure alerts and moves on, never blocks."""
        ctx = self._ctx
        order = {"k8s_manifest": 0, "workflow": 1}
        label = {"k8s_manifest": "k8s", "workflow": "service"}
        for sec in sorted(self._secondary_prs, key=lambda p: order.get(p.get("pr_type", "workflow"), 2)):
            num, repo = sec.get("pr_number"), sec.get("repo_full_name")
            if not (num and repo):
                continue
            artifact = label.get(sec.get("pr_type", "workflow"), sec.get("pr_type"))
            if sec.get("pr_type") == "k8s_manifest":
                try:
                    await workflow.execute_activity(
                        approve_pr, args=[num, repo], **ctx["act_short"],
                    )
                except Exception as e:
                    workflow.logger.warning(
                        "approve_pr failed for %s PR #%s: %s — merging anyway", artifact, num, e,
                    )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[ctx["run_track_key"], f"{artifact}: merge pr", "running",
                      workflow.now().isoformat(), None, self._pr_group],
                **ctx["act_stage"],
            )
            outcome = await workflow.execute_activity(
                merge_secondary_pr, args=[num, repo, sec.get("pr_type", "workflow")],
                **ctx["act_short"],
            )
            if outcome in ("merged", "already_merged"):
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], f"{artifact}: merge pr", "success",
                          workflow.now().isoformat(), None, self._pr_group],
                    **ctx["act_stage"],
                )
            else:
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[ctx["run_track_key"], f"{artifact}: merge pr", "failed",
                          workflow.now().isoformat(),
                          f"Automatic merge failed — merge PR #{num} manually.", self._pr_group],
                    **ctx["act_stage"],
                )
                await workflow.execute_activity(
                    send_p0_alert,
                    args=[
                        self._user_code, num,
                        f"*Reason:* {artifact} PR #{num} failed to merge automatically.\n"
                        f"⚠️ Merge {repo}#{num} manually on GitHub.",
                        "Secondary PR Merge Failed",
                        f"https://github.com/{repo}/pull/{num}",
                    ],
                    **ctx["act_short"],
                )

    # ── Shared terminate path ────────────────────────────────────────────────

    async def _fail_deployment(
        self,
        reason: str,
        result_code: str,
        fail_stage: Optional[str],
        deployment_status: Any = None,
        alert_title: str = "Prod Deployment Failed",
        release_locks: bool = True,
    ) -> None:
        """The one way this workflow dies on purpose: statuses → ERROR/FAILED,
        track rows → FAILED, run-track stages closed, P0 sent, own locks
        released (standalone — parent-held locks are the parent's to free),
        then a non-retryable ApplicationError ends the run."""
        ctx = self._ctx
        workflow.logger.warning("_fail_deployment: %s (%s)", result_code, reason)
        await workflow.execute_activity(
            update_deployment_status,
            args=[ctx["queue_ids"], deployment_status or _DS.ERROR, reason],
            **ctx["act_short"],
        )
        await workflow.execute_activity(
            update_queue_status, args=[ctx["queue_ids"], "FAILED"], **ctx["act_short"],
        )
        # Track rows: scoped to our workflow_id — if a successor already
        # re-claimed a dir, this write correctly touches nothing.
        await workflow.execute_activity(
            production_track_set_status,
            args=[{
                "dirs": self._my_dirs + self._shared_dirs,
                "workflow_id": workflow.info().workflow_id,
                "status": "FAILED",
            }],
            **ctx["act_short"],
        )
        if fail_stage:
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[ctx["run_track_key"], fail_stage, "failed",
                      workflow.now().isoformat(), reason, self._pr_group],
                **ctx["act_stage"],
            )
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[ctx["run_track_key"], ctx["completed_stage"], "failed",
                  workflow.now().isoformat(), reason, self._pr_group],
            **ctx["act_stage"],
        )
        await workflow.execute_activity(
            send_p0_alert,
            args=[
                self._user_code,
                self._promotion_pr_number or self._feature_pr_number,
                f"*Reason:* {reason}\n\n⚠️ *Action Required*\n• Fix the cause and start a new deployment from DevLift.",
                alert_title,
                self._promotion_pr_url or self._feature_pr_url,
            ],
            **ctx["act_short"],
        )
        if release_locks and not self._parent_holder_id:
            await ctx["coordinator"].signal(
                "release_locks", args=[workflow.info().workflow_id, self._lock_set]
            )
        self._step = "failed"
        self._result = result_code
        raise ApplicationError(result_code, non_retryable=True)

    # ── Query ────────────────────────────────────────────────────────────────

    @workflow.query
    def get_state(self) -> dict:
        return {
            "step": self._step,
            "result": self._result,
            "locks_granted": self._locks_granted,
            "feature_pr_number": self._feature_pr_number,
            "promotion_pr_number": self._promotion_pr_number,
            "promotion_pr_url": self._promotion_pr_url,
            "repo_full_name": self._repo_full_name,
            "project_name": self._project_name,
            "project_dirs": self._my_dirs,
        }
