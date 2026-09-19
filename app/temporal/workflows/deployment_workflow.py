"""
DeploymentWorkflow — one instance per deploy request.

Flow (using Atlantis for plan/apply):
  1. Signal TenantCoordinator: acquire_locks for my project_dirs
  2. Wait for locks_granted signal from coordinator
  3. Activity: run_script_pr_workflow (file gen + commit + create PR in GitHub)
  3b. Activity: post_plan_comment — posts "atlantis plan -p <project>" to trigger plan (autoplan disabled)
  4. Wait for plan signal — Atlantis posts plan comment → webhook → our signal
     Fallback: poll GitHub PR comments every PLAN_POLL_INTERVAL
     On failure: retry up to PLAN_MAX_RETRIES, then P0 alert + keep waiting
  5. Activity: post_apply_comment — posts "atlantis apply" on the PR
  6. Wait for apply signal — Atlantis applies → webhook → our signal
     Fallback: poll GitHub PR comments every APPLY_POLL_INTERVAL
     On failure: P0 alert + go back to step 4 (user pushes fix → auto-plan)
  7. Wait for pr_merged signal — Atlantis auto-merges → webhook → our signal
  8. Activity: update_queue_status → mark DEPLOYED
  9. Signal coordinator: release_locks

At any waiting step:
  - pr_merged → mark DEPLOYED, release locks, done
  - pr_closed → mark FAILED, release locks, done

No auto-close. No close-on-timeout. User decides when to fix, close, or merge.
P0 alerts sent when stuck or failed beyond retry threshold.
"""

import asyncio
from datetime import timedelta
from typing import List
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.temporal.activities.deploy_activities import (
        run_script_pr_workflow,
        post_apply_comment,
        post_plan_comment,
        approve_pr,
        update_queue_status,
        update_deployment_status,
        update_resource_status,
        record_gateway_routes_pr_created,
        check_for_manual_commit,
        poll_for_plan_status,
        poll_for_apply_status,
        poll_pr_approval_status,
        poll_pr_state,
        check_and_merge_pr,
        resolve_conflict_and_merge,
        send_p0_alert,
        send_atlantis_lock_alert,
        close_pr_activity,
        get_waiting_users_for_dirs,
        send_lock_timeout_queued_alert,
    )
    from app.temporal.activities.admin_alert_activities import report_stale_queue_wait
    from app.temporal.activities.deploy_activities import (
        mark_iac_locked,
        clear_iac_locked,
        merge_secondary_pr,
        send_deployment_started_dm,
        send_deployment_completed_dm,
        update_pipeline_run_track_stage,
        update_pipeline_run_track_deploy_result,
        save_service_alb_url,
        verify_plan_with_ai,
        post_plan_verification_comment,
    )
    from app.core.config import settings as _settings
    from app.core.enum import ResourceDeploymentStatusEnum as _DS
    from app.temporal.error_utils import error_message

# --- Timeouts and intervals ---
LOCK_WAIT_TIMEOUT   = timedelta(hours=24)
QUEUE_CHECK_INTERVAL = timedelta(minutes=10)  # re-check the lock holder this often while queued (admin alert if it is dead)
STALE_REALERT_INTERVAL = timedelta(hours=1)  # after the first stale-lock alert, remind admins this often while still blocked
PLAN_POLL_INTERVAL  = timedelta(seconds=30)
APPLY_POLL_INTERVAL = timedelta(seconds=60)
MERGE_POLL_INTERVAL = timedelta(seconds=60)  # poll PR state every 5 min while waiting for merge
PLAN_ALERT_AFTER    = timedelta(minutes=60)  # P0 alert if no plan result after 60 min
APPLY_ALERT_AFTER   = timedelta(minutes=60)  # P0 alert if no apply result after 60 min
MERGE_ALERT_AFTER   = timedelta(hours=2)     # P0 alert if not merged after 2 h
STUCK_MAX_ALERTS    = 24                     # auto-terminate after 24 hourly stuck alerts (≈ 24 h); applies to plan, apply, approval
PLAN_MAX_RETRIES    = 2                      # auto-retry plan this many times before P0 alert
# Atlantis lock-conflict alert schedule: T+5 min, T+30 min, T+60 min (1 hr), then every 60 min → terminate at T+25h
LOCK_CHECK_INTERVALS = [
    timedelta(minutes=5),
    timedelta(minutes=25),
    timedelta(minutes=30),
    *([timedelta(minutes=60)] * 24),
]
LOCK_RECHECK_INTERVAL = timedelta(minutes=5)  # plan re-check cadence (independent of alert schedule)
# Precomputed cumulative alert thresholds in minutes: [5, 30, 60, 120, 180, ...]
_LOCK_ALERT_MINUTES: list[int] = []
_cumul = 0
for _iv in LOCK_CHECK_INTERVALS:
    _cumul += int(_iv.total_seconds() // 60)
    _LOCK_ALERT_MINUTES.append(_cumul)
LOCK_TOTAL_MINUTES = _cumul  # 1500 min (25 h)
APPLY_MAX_RETRIES   = 2                      # auto-retry apply this many times before P0 alert
MERGE_MAX_RETRIES   = 3                      # retry programmatic merge this many times before falling back to manual

RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
)

# Stage-entry error texts reused across multiple failure paths.
_MANUAL_COMMIT_ERR = "Manual commit detected during deployment. Workflow terminated."
_PR_CLOSED_ERR = "PR was closed before the deployment completed."


@workflow.defn
class DeploymentWorkflow:

    def __init__(self):
        self._locks_granted: bool = False
        self._plan_result: str | None = None    # "success" | "failed"
        self._plan_error: str | None = None     # error excerpt from the failed plan comment
        self._plan_verification_result: str | None = None  # "clean" | "destructive"
        self._apply_result: str | None = None   # "success" | "failed"
        self._apply_error: str | None = None    # error excerpt from the failed apply comment
        self._merge_result: str | None = None   # "merged"
        self._pr_closed: bool = False
        self._merge_conflict: bool = False
        self._pr_approved: bool = False
        self._step: str = "starting"
        self._pr_number: int | None = None
        self._repo_full_name: str | None = None
        self._project_name: str | None = None
        self._result: str | None = None
        self._tenant_code: str = ""
        self._user_code: str = ""
        self._devlift_commit_sha: str | None = None
        self._manual_commit_detected: bool = False
        self._force_terminate: bool = False
        self._force_terminate_reason: str = ""
        self._secondary_prs: list = []
        self._pr_group: str = "service"

    @workflow.run
    async def run(
        self,
        tenant_code: str,
        project_dirs: List[str],
        queue_ids: List[int],
        user_code: str,
        parent_holder_id: str | None = None,
        track_id: str | None = None,
        pr_group: str | None = None,
    ) -> dict:
        self._tenant_code = tenant_code
        self._user_code = user_code
        workflow_id = workflow.info().workflow_id
        # pr_group — tags every PR this run creates (deploy_result.prs[].group)
        # so a shared multi-deploy run-track row can tell the service's PRs
        # apart from kong's (or a future extra group's). Standalone deploys and
        # the primary multi-deploy child default to "service" — the only kind
        # of PR set they ever produce.
        self._pr_group = pr_group or "service"
        # track_id — same contract as VariableDeployWorkflow's track_id: the
        # vendor_deployment_id value used to find the pipeline_run_track row(s)
        # for every stage / deploy-result write.
        #   - Standalone deploy: omitted → rows were tagged with THIS
        #     workflow's own id by deploy_temporal. Behavior unchanged.
        #   - Multi-deploy: the orchestrator tags the batch's single row with
        #     ITS OWN workflow id and passes it here, so the service child,
        #     variables and every extra child write onto one shared timeline.
        # run_track_key is used ONLY for run-track / deploy-result writes.
        # Coordinator lock signals and search attributes MUST keep the real
        # workflow_id — the coordinator's passage / no-op-release logic
        # depends on the child's true identity.
        run_track_key = track_id or workflow_id
        coordinator = workflow.get_external_workflow_handle(f"coordinator-{tenant_code}")
        # Under an orchestrator parent the FINAL 'completed' stage belongs to
        # the parent (written after the variables stage) — this child closes its
        # own phase as 'infra: complete'. Standalone keeps 'completed', which is
        # what flips pipeline_run_track to COMPLETED and stops the FE poller.
        _completed_stage = "infra: complete" if parent_holder_id else "completed"

        # Index by tenant, user and queue IDs so deployments can be listed/joined
        workflow.upsert_search_attributes({
            "DeployTenantCode": [tenant_code],
            "DeployUserCode": [user_code],
            "DeployQueueCodes": [",".join(str(i) for i in queue_ids)],
        })

        act_opts_short = dict(start_to_close_timeout=timedelta(minutes=5),  retry_policy=RETRY)
        act_opts_long  = dict(start_to_close_timeout=timedelta(minutes=30), retry_policy=RETRY)
        act_opts_stage = dict(start_to_close_timeout=timedelta(seconds=10), retry_policy=RetryPolicy(maximum_attempts=1))

        # ── Step 1: Acquire locks ────────────────────────────────────────────
        self._step = "acquiring_locks"
        workflow.logger.info("Requesting locks for dirs: %s", project_dirs)

        # When started as a child of DeploymentOrchestratorWorkflow, the parent
        # already holds these dirs — pass its id so the coordinator grants
        # passage without transferring ownership (child releases stay no-ops;
        # the parent's finally-release is the single real release). Standalone
        # starts keep the exact 3-arg signal so in-flight replays are unchanged.
        if parent_holder_id:
            await coordinator.signal(
                "acquire_locks",
                args=[workflow_id, project_dirs, user_code, parent_holder_id],
            )
        else:
            await coordinator.signal("acquire_locks", args=[workflow_id, project_dirs, user_code])

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
                    # Alerting is best-effort telemetry. A Slack outage or an unreachable
                    # coordinator must never kill a deploy that is only waiting its turn:
                    # an ActivityError is NOT an asyncio.TimeoutError, so without this guard
                    # it escapes the handler below, the run dies without withdrawing from the
                    # coordinator, and its hold_queue entry is later granted to a corpse --
                    # leaving the dirs locked to a workflow that will never release them.
                    try:
                        sent = await workflow.execute_activity(
                            report_stale_queue_wait,
                            args=[tenant_code, workflow_id, project_dirs, user_code, int(waited.total_seconds() // 60), last_alert_at is not None],
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
            await coordinator.signal("release_hold", args=[workflow_id])
            await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(),
                      f"Timed out waiting for locks on {project_dirs}", self._pr_group],
                **act_opts_stage,
            )
            self._step = "failed"
            self._result = "lock_wait_timeout"
            raise ApplicationError("lock_wait_timeout", non_retryable=True)

        if self._force_terminate:
            await workflow.execute_activity(
                update_deployment_status,
                args=[queue_ids, _DS.ERROR, self._force_terminate_reason],
                **act_opts_short,
            )
            await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(),
                      self._force_terminate_reason or "aborted_lock_conflict", self._pr_group],
                **act_opts_stage,
            )
            self._step = "failed"
            self._result = "aborted_lock_conflict"
            raise ApplicationError("aborted_lock_conflict", non_retryable=True)

        workflow.logger.info("Locks granted for: %s", project_dirs)

        # ── Step 2: Create PR (file gen + commit + PR) ───────────────────────
        self._step = "creating_pr"
        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.DEPLOYING], **act_opts_short)
        await workflow.execute_activity(update_queue_status, args=[queue_ids, "STARTING_DEPLOYMENT"], **act_opts_short)
        # Opened as running BEFORE the creation activity (file gen + commits +
        # PR creation take a while) — resolved to success / skipped / failed by
        # outcome below, so the run track always shows what's in progress.
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[run_track_key, "infra: create pr", "running", workflow.now().isoformat(), None, self._pr_group],
            **act_opts_stage,
        )
        try:
            pr_info = await workflow.execute_activity(
                run_script_pr_workflow,
                args=[user_code, tenant_code, queue_ids],
                **act_opts_long,
            )
        except Exception as e:
            workflow.logger.error("run_script_pr_workflow failed: %s", error_message(e))
            await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.PR_CREATION_FAILED, error_message(e)], **act_opts_short)
            await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
            await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: create pr", "failed", workflow.now().isoformat(), error_message(e), self._pr_group],
                **act_opts_stage,
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), error_message(e), self._pr_group],
                **act_opts_stage,
            )
            self._step = "failed"
            self._result = "pr_creation_failed"
            raise ApplicationError("pr_creation_failed", non_retryable=True)

        pr_number = pr_info.get("pr_number")
        pr_url = pr_info.get("pr_url") or None
        repo_full_name = pr_info.get("repo_full_name") or ""
        project_name = pr_info.get("project_name") or None
        self._pr_number = pr_number
        self._repo_full_name = repo_full_name
        self._project_name = project_name
        self._devlift_commit_sha = pr_info.get("commit_sha") or None
        self._secondary_prs = pr_info.get("secondary_prs", [])

        if not pr_number and self._secondary_prs:
            # ── No infrastructure (terragrunt) diff, but secondary PRs exist ──────
            # For ECS/EKS a deployment renders more than the infra terragrunt.hcl:
            # it also produces a workflow PR and (EKS) a k8s-manifests PR. When the
            # terragrunt.hcl already matches desired state there is NO infrastructure
            # PR, so there is nothing to plan/apply. But the secondary PRs may still
            # carry real changes. Instead of terminating, skip the plan→apply→merge
            # state machine entirely and merge those secondary PRs directly, then
            # mark the deployment deployed.
            #
            # (For terragrunt-only resources — s3/sqs/dynamodb — there are no
            # secondary PRs, so this branch is skipped and the no-diff case falls
            # through to the PR-creation-failed terminate below.)
            workflow.logger.info(
                "No infrastructure PR (terragrunt has no diff) but %d secondary PR(s) "
                "present — merging secondary PRs directly and completing.",
                len(self._secondary_prs),
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: create pr", "skipped", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )
            await self._record_created_prs(run_track_key, False, act_opts_stage)
            await self._merge_secondary_prs(run_track_key, act_opts_short, act_opts_stage)
            await workflow.execute_activity(send_deployment_completed_dm, args=[user_code, None, repo_full_name, None, project_name, queue_ids], **act_opts_short)
            await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.DEPLOYED], **act_opts_short)
            await workflow.execute_activity(update_resource_status, args=[queue_ids, "ONLINE"], **act_opts_short)
            await workflow.execute_activity(update_queue_status, args=[queue_ids, "DEPLOYED"], **act_opts_short)
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, _completed_stage, "success", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )
            await self._save_service_alb_url(run_track_key, queue_ids, act_opts_short)
            await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
            workflow.logger.info("Locks released for: %s", project_dirs)
            workflow.logger.info("Deployment DEPLOYED ✓ (secondary PRs only, no infra diff)")
            self._step = "active"
            self._result = "success"
            return {"status": "DEPLOYED", "pr_number": None}

        if not pr_number:
            # No PR created (primary AND secondary both empty) — no diff anywhere
            # or PR creation failed silently. Mark PR creation failed and terminate.
            workflow.logger.warning("No PR created (pr_number=None, no secondary PRs) — failing deployment.")
            await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.PR_CREATION_FAILED, "No pull request was created — no diff detected or PR creation failed silently."], **act_opts_short)
            await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
            await workflow.execute_activity(
                send_p0_alert,
                args=[
                    user_code, None,
                    (
                        "*Reason:* No pull request was created for this deployment.\n\n"
                        "This usually means the infrastructure files already match the desired state "
                        "(no diff detected) or the PR creation encountered an issue.\n\n"
                        "⚠️ *Action Required*\n"
                        "• Verify the resource state in the infrastructure repository.\n"
                        "• If this is unexpected, start a new deployment from DevLift."
                    ),
                    "Deployment Failed — No PR Created",
                    None,
                    True,
                ],
                **act_opts_short,
            )
            await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
            await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, "infra: create pr", "failed", workflow.now().isoformat(), "No pull request was created — no diff detected or PR creation failed silently.", self._pr_group], **act_opts_stage)
            await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), "No pull request was created — no diff detected or PR creation failed silently.", self._pr_group], **act_opts_stage)
            self._step = "failed"
            self._result = "no_pr_created"
            raise ApplicationError("no_pr_created", non_retryable=True)

        workflow.upsert_search_attributes({"DeployPrNumber": [pr_number]})

        workflow.logger.info(
            "PR #%s created (project=%s). Posting initial plan comment.",
            pr_number, project_name,
        )

        # Notify user that their deployment has started
        await workflow.execute_activity(
            send_deployment_started_dm,
            args=[user_code, pr_number, pr_url, project_name, queue_ids],
            **act_opts_short,
        )

        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.STARTING_PLANNING], **act_opts_short)
        # The PR exists, so the gateway change is real and gets recorded — at
        # PR_CREATED, not ACTIVE. Nothing wrote these tables before this point:
        # save, submit and approve all leave them alone, which is what keeps them
        # a record of what has actually been shipped. The merge promotes them.
        # Self-skips for non-gateway deploys, and never fails one.
        await workflow.execute_activity(
            record_gateway_routes_pr_created, args=[queue_ids], **act_opts_short,
        )
        await self._record_created_prs(run_track_key, True, act_opts_stage)
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[run_track_key, "infra: plan pr", "running", workflow.now().isoformat(), None, self._pr_group],
            **act_opts_stage,
        )

        # Post initial plan comment — autoplan is disabled server-side, so we trigger it explicitly
        if pr_number and repo_full_name:
            await workflow.execute_activity(
                post_plan_comment, args=[pr_number, repo_full_name, project_name], **act_opts_short,
            )

        # ── State machine ────────────────────────────────────────────────────
        # Outer loop handles two back-to-plan cases:
        #   - apply_failed  → user pushes fix → auto-plan → plan_completed → apply again
        #   - merge_conflict → coordinator resolves → force-push → auto-plan → plan again
        apply_exhausted = False   # suppress PLANNING flash when re-entering plan phase after apply exhaustion
        plan_not_before: str | None = None  # cutoff for plan poll — skip stale plan comments from before last trigger
        while True:

            # ── WAIT_PLAN ────────────────────────────────────────────────────
            # Retries up to PLAN_MAX_RETRIES via "atlantis plan" comment.
            # After max retries: P0 alert, reset counter, keep waiting indefinitely.
            # Exits on: plan_completed | pr_merged | pr_closed
            plan_attempt = 0
            plan_elapsed = timedelta(0)
            plan_stuck_count = 0
            self._plan_result = None
            while True:
                # _plan_result is NOT reset here — it may already be set by a signal
                # that arrived during the previous post_plan_comment or update_queue_status
                # activity. Resetting here would silently lose that signal.
                self._step = "waiting_for_plan"

                # Only update status to PLANNING when we don't already have a result
                # (avoids a brief PLANNING flash when a fast retry result arrived
                # during the previous activity).
                # Hold the current status (don't flash PLANNING) while parked on a
                # destructive-plan block waiting for a fix — same as apply_exhausted
                # holds APPLY_FAILED while waiting in this same WAIT_PLAN loop.
                if (self._plan_result is None and plan_attempt < PLAN_MAX_RETRIES
                        and not apply_exhausted and self._plan_verification_result != "destructive"):
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.PLANNING], **act_opts_short)

                # Wait only if no result arrived during the activities above
                while self._plan_result is None and not (self._merge_result or self._pr_closed or self._manual_commit_detected):
                    try:
                        await workflow.wait_condition(
                            lambda: self._plan_result is not None or self._merge_result or self._pr_closed or self._manual_commit_detected,
                            timeout=PLAN_POLL_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        plan_elapsed += PLAN_POLL_INTERVAL
                        if pr_number and repo_full_name:
                            pr_state = await workflow.execute_activity(
                                poll_pr_state, args=[pr_number, repo_full_name], **act_opts_short,
                            )
                            if pr_state == "closed":
                                self._pr_closed = True
                                await coordinator.signal("pr_merge_completed", args=[pr_number])
                            else:
                                if self._manual_commit_detected or self._devlift_commit_sha:
                                    manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                                    if manual:
                                        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                                        await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                                        await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                                        await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                                        _fail_stage = "infra: waiting for manual apply" if apply_exhausted else ("infra: waiting for manual plan" if plan_attempt >= PLAN_MAX_RETRIES else "infra: plan pr")
                                        await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                                        await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                                        self._step = "failed"
                                        self._result = "manual_commit_detected"
                                        raise ApplicationError("manual_commit_detected", non_retryable=True)
                                if pr_state == "merged":
                                    self._merge_result = "merged"
                                    await coordinator.signal("pr_merge_completed", args=[pr_number])
                                else:
                                    poll_result = await workflow.execute_activity(
                                        poll_for_plan_status, args=[pr_number, repo_full_name, plan_not_before], **act_opts_short,
                                    )
                                    _pr_parts = poll_result.split(":", 1) if poll_result else []
                                    _pr = _pr_parts[0] if _pr_parts else poll_result
                                    if _pr == "failed":
                                        # "failed:<error excerpt>" — keep the excerpt for the stage record
                                        self._plan_result = "failed"
                                        if len(_pr_parts) > 1 and _pr_parts[1]:
                                            self._plan_error = _pr_parts[1]
                                    elif _pr == "success" or (poll_result and poll_result.startswith("lock_conflict")):
                                        self._plan_result = poll_result
                        if self._plan_result is None and plan_elapsed >= PLAN_ALERT_AFTER:
                            plan_stuck_count += 1
                            if plan_stuck_count >= STUCK_MAX_ALERTS:
                                workflow.logger.warning(
                                    "plan stuck >24h PR#%s — auto-terminating deployment",
                                    pr_number,
                                )
                                await workflow.execute_activity(
                                    update_deployment_status,
                                    args=[queue_ids, _DS.ERROR, "Plan stuck for 24 hours. Deployment auto-terminated."],
                                    **act_opts_short,
                                )
                                await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                                await workflow.execute_activity(
                                    send_p0_alert,
                                    args=[
                                        user_code, pr_number,
                                        (
                                            "*Reason:* Plan stuck for 24 hours with no result from Atlantis.\n\n"
                                            "⚠️ *Action Required*\n"
                                            "• Start a new deployment from DevLift."
                                        ),
                                        "Deployment Auto-Terminated",
                                        pr_url,
                                    ],
                                    **act_opts_short,
                                )
                                await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                                _fail_stage = "infra: waiting for manual apply" if apply_exhausted else ("infra: waiting for manual plan" if plan_attempt >= PLAN_MAX_RETRIES else "infra: plan pr")
                                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), "Plan stuck for 24 hours with no result from Atlantis. Deployment auto-terminated.", self._pr_group], **act_opts_stage)
                                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), "Plan stuck for 24 hours with no result from Atlantis. Deployment auto-terminated.", self._pr_group], **act_opts_stage)
                                self._step = "failed"
                                self._result = "plan_stuck_timeout"
                                raise ApplicationError("plan_stuck_timeout", non_retryable=True)
                            else:
                                workflow.logger.warning(
                                    "P0 ALERT: plan stuck >60min PR#%s — %d project(s) waiting (%d/%d alerts)",
                                    pr_number, len(project_dirs), plan_stuck_count, STUCK_MAX_ALERTS,
                                )
                                await workflow.execute_activity(
                                    send_p0_alert,
                                    args=[
                                        user_code, pr_number,
                                        (
                                            "*Status:* In progress >60 min\n\n"
                                            "⚠️ *Action Required*\n"
                                            "• Check Atlantis for the current status.\n"
                                            "• Trigger atlantis plan manually if needed."
                                        ),
                                        "Plan Stuck Alert",
                                        pr_url,
                                        False,
                                        True,  # channel_only
                                    ],
                                    **act_opts_short,
                                )
                                plan_elapsed = timedelta(0)

                if self._merge_result or self._pr_closed or self._manual_commit_detected:
                    break  # terminal — handled below

                if self._plan_result and self._plan_result.startswith("lock_conflict"):
                    # Parse locking PR from "lock_conflict:119" or "lock_conflict"
                    _parts = self._plan_result.split(":", 1)
                    _locking_pr: int | None = int(_parts[1]) if len(_parts) > 1 and _parts[1].isdigit() else None
                    self._plan_result = "lock_conflict"

                    lock_elapsed = timedelta(0)
                    next_alert_idx = 0
                    _lock_total_min = LOCK_TOTAL_MINUTES
                    _lock_next_min = _LOCK_ALERT_MINUTES[0]  # first in-loop alert at T+5 min

                    await workflow.execute_activity(mark_iac_locked, args=[queue_ids], **act_opts_short)

                    # Fetch queued users and send initial alert at T+0
                    _waiting_users = await workflow.execute_activity(
                        get_waiting_users_for_dirs, args=[tenant_code, project_dirs], **act_opts_short,
                    )
                    await workflow.execute_activity(
                        send_atlantis_lock_alert,
                        args=[user_code, pr_number, project_dirs, 0, False, _waiting_users, _lock_next_min, _lock_total_min, _locking_pr, repo_full_name],
                        **act_opts_short,
                    )
                    while self._plan_result == "lock_conflict":
                        _elapsed_min = int(lock_elapsed.total_seconds() // 60)
                        if _elapsed_min >= LOCK_TOTAL_MINUTES:
                            # Timeout exhausted — terminate current + abort all queued for these dirs
                            await workflow.execute_activity(
                                update_deployment_status,
                                args=[queue_ids, _DS.ERROR, f"Atlantis lock conflict not resolved in {_lock_total_min} minutes. Deployment terminated."],
                                **act_opts_short,
                            )
                            await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                            # Fetch queued users before aborting
                            _timeout_queued = await workflow.execute_activity(
                                get_waiting_users_for_dirs, args=[tenant_code, project_dirs], **act_opts_short,
                            )
                            # Timeout alert — same format as lock conflict, channel + DM to blocked user
                            await workflow.execute_activity(
                                send_atlantis_lock_alert,
                                args=[user_code, pr_number, project_dirs, _elapsed_min, False, _timeout_queued, 0, _lock_total_min, _locking_pr, repo_full_name, True],
                                **act_opts_short,
                            )
                            # DM each queued user
                            if _timeout_queued:
                                await workflow.execute_activity(
                                    send_lock_timeout_queued_alert,
                                    args=[_timeout_queued, project_dirs, _locking_pr, _lock_total_min],
                                    **act_opts_short,
                                )
                            # Close the blocked PR
                            if pr_number and repo_full_name:
                                await workflow.execute_activity(
                                    close_pr_activity,
                                    args=[pr_number, f"Deployment terminated: IaC state lock held by PR #{_locking_pr} was not resolved within {_lock_total_min} minutes.", repo_full_name],
                                    **act_opts_short,
                                )
                            await coordinator.signal("abort_queued_for_dirs", args=[workflow_id, project_dirs, f"Atlantis lock conflict not resolved in {_lock_total_min} minutes"])
                            await workflow.execute_activity(clear_iac_locked, args=[queue_ids], **act_opts_short)
                            _fail_stage = "infra: waiting for manual apply" if apply_exhausted else ("infra: waiting for manual plan" if plan_attempt >= PLAN_MAX_RETRIES else "infra: plan pr")
                            await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), f"Atlantis lock conflict not resolved in {_lock_total_min} minutes. Deployment terminated.", self._pr_group], **act_opts_stage)
                            await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), f"Atlantis lock conflict not resolved in {_lock_total_min} minutes. Deployment terminated.", self._pr_group], **act_opts_stage)
                            self._step = "failed"
                            self._result = "atlantis_lock_timeout"
                            raise ApplicationError("atlantis_lock_timeout", non_retryable=True)

                        # Re-check plan every 5 min regardless of alert schedule.
                        # The wait is SLICED into PLAN_POLL_INTERVAL pieces, each
                        # slice ending with a PR-state poll: webhook signals do
                        # not reach this environment, so polling is the only way
                        # a close/merge is noticed — sliced, it lands within ~30s
                        # instead of the full recheck interval. Only the plan
                        # RE-POST keeps the 5-minute cadence, so the PR is not
                        # spammed with "atlantis plan" comments.
                        self._plan_result = None
                        _sliced = timedelta(0)
                        _interrupted = False
                        while _sliced < LOCK_RECHECK_INTERVAL and not _interrupted:
                            try:
                                await workflow.wait_condition(
                                    lambda: self._plan_result is not None or self._merge_result or self._pr_closed or self._manual_commit_detected,
                                    timeout=PLAN_POLL_INTERVAL,
                                )
                                _interrupted = True
                            except asyncio.TimeoutError:
                                _sliced += PLAN_POLL_INTERVAL
                                lock_elapsed += PLAN_POLL_INTERVAL
                                if pr_number and repo_full_name:
                                    _lock_pr_state = await workflow.execute_activity(
                                        poll_pr_state, args=[pr_number, repo_full_name], **act_opts_short,
                                    )
                                    if _lock_pr_state == "closed":
                                        self._pr_closed = True
                                        await coordinator.signal("pr_merge_completed", args=[pr_number])
                                        _interrupted = True
                                    elif _lock_pr_state == "merged":
                                        self._merge_result = "merged"
                                        await coordinator.signal("pr_merge_completed", args=[pr_number])
                                        _interrupted = True

                        # Something happened mid-wait (signal, close, merge) —
                        # skip the re-post and let the loop head route it, same
                        # as the old non-timeout path did.
                        if not _interrupted:
                            if self._merge_result or self._pr_closed or self._manual_commit_detected:
                                break

                            # Re-post plan to check if lock was released (every 5 min)
                            if pr_number and repo_full_name:
                                _plan_repost_time = workflow.now().isoformat()
                                await workflow.execute_activity(
                                    post_plan_comment, args=[pr_number, repo_full_name, project_name], **act_opts_short,
                                )
                                await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.PLANNING], **act_opts_short)
                                # Wait for Atlantis to respond before polling
                                self._plan_result = None
                                try:
                                    await workflow.wait_condition(
                                        lambda: self._plan_result is not None or self._merge_result or self._pr_closed,
                                        timeout=PLAN_POLL_INTERVAL,
                                    )
                                except asyncio.TimeoutError:
                                    pass
                                # Poll with not_before cutoff — ignore pre-repost comments
                                if self._plan_result is None:
                                    poll_result = await workflow.execute_activity(
                                        poll_for_plan_status, args=[pr_number, repo_full_name, _plan_repost_time], **act_opts_short,
                                    )
                                    _poll_parts = poll_result.split(":", 1) if poll_result else []
                                    _poll_base = _poll_parts[0] if _poll_parts else poll_result
                                    if _poll_base == "lock_conflict" and len(_poll_parts) > 1 and _poll_parts[1].isdigit():
                                        _locking_pr = int(_poll_parts[1])
                                    if _poll_base == "failed" and len(_poll_parts) > 1 and _poll_parts[1]:
                                        self._plan_error = _poll_parts[1]
                                    self._plan_result = _poll_base if _poll_base in ("success", "failed", "lock_conflict") else "lock_conflict"
                                elif self._plan_result and self._plan_result.startswith("lock_conflict:"):
                                    _sig_parts = self._plan_result.split(":", 1)
                                    if _sig_parts[1].isdigit():
                                        _locking_pr = int(_sig_parts[1])
                                    self._plan_result = "lock_conflict"

                            if self._plan_result != "lock_conflict":
                                break

                            # Fire alert only when elapsed crosses the next scheduled threshold
                            _elapsed_min = int(lock_elapsed.total_seconds() // 60)
                            if next_alert_idx < len(_LOCK_ALERT_MINUTES) and _elapsed_min >= _LOCK_ALERT_MINUTES[next_alert_idx]:
                                is_final = next_alert_idx >= len(_LOCK_ALERT_MINUTES) - 1
                                _next_alert_min = (
                                    _LOCK_ALERT_MINUTES[next_alert_idx + 1] - _elapsed_min
                                    if not is_final else 0
                                )
                                _waiting_users = await workflow.execute_activity(
                                    get_waiting_users_for_dirs, args=[tenant_code, project_dirs], **act_opts_short,
                                )
                                await workflow.execute_activity(
                                    send_atlantis_lock_alert,
                                    args=[user_code, pr_number, project_dirs, _elapsed_min, is_final, _waiting_users, _next_alert_min, _lock_total_min, _locking_pr, repo_full_name],
                                    **act_opts_short,
                                )
                                next_alert_idx += 1
                            continue

                        # Signal arrived during wait — parse locking PR if signal carried it
                        if self._plan_result and self._plan_result.startswith("lock_conflict:"):
                            _sig_parts = self._plan_result.split(":", 1)
                            if _sig_parts[1].isdigit():
                                _locking_pr = int(_sig_parts[1])
                            self._plan_result = "lock_conflict"
                            continue  # loop back — still locked
                        if self._merge_result or self._pr_closed or self._manual_commit_detected:
                            break
                        if self._plan_result in ("success", "failed"):
                            break

                    # After lock loop — clear IaC lock regardless of how we exited
                    await workflow.execute_activity(clear_iac_locked, args=[queue_ids], **act_opts_short)
                    # Fall through to terminal / success / failed handling
                    if self._merge_result or self._pr_closed or self._manual_commit_detected:
                        break
                    if self._plan_result == "success":
                        break  # plan succeeded — exit plan outer loop
                    # _plan_result == "failed" → fall through to existing failed handling

                if self._plan_result == "failed":
                    plan_attempt += 1
                    if plan_attempt < PLAN_MAX_RETRIES:
                        workflow.logger.warning(
                            "Plan failed (attempt %d/%d) — retrying via 'atlantis plan' comment",
                            plan_attempt, PLAN_MAX_RETRIES,
                        )
                        # Reset BEFORE the activity so any signal that arrives
                        # during post_plan_comment is captured, not overwritten.
                        apply_exhausted = False  # actively retrying plan now — resume PLANNING status
                        self._plan_result = None
                        self._plan_error = None  # each attempt records its own error
                        if pr_number and repo_full_name:
                            await workflow.execute_activity(
                                post_plan_comment, args=[pr_number, repo_full_name, project_name], **act_opts_short,
                            )
                        plan_elapsed = timedelta(0)
                        continue  # retry plan — _plan_result may already be set if Atlantis is fast
                    else:
                        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.PLAN_FAILED], **act_opts_short)
                        await workflow.execute_activity(
                            update_pipeline_run_track_stage,
                            args=[run_track_key, "infra: plan pr", "failed", workflow.now().isoformat(),
                                  self._plan_error or f"Terraform plan failed after {plan_attempt} attempts — review the plan error details on the PR.", self._pr_group],
                            **act_opts_stage,
                        )
                        await workflow.execute_activity(
                            update_pipeline_run_track_stage,
                            args=[run_track_key, "infra: waiting for manual plan", "running", workflow.now().isoformat(), None, self._pr_group],
                            **act_opts_stage,
                        )
                        workflow.logger.warning(
                            "P0 ALERT: plan failed %d times PR#%s — waiting for user to push fix or plan manually",
                            plan_attempt, pr_number,
                        )
                        await workflow.execute_activity(
                            send_p0_alert,
                            args=[
                                user_code, pr_number,
                                (
                                    "⚠️ *Action Required*\n"
                                    "• Review the plan error details on the PR.\n"
                                    "• Trigger atlantis plan manually to retry."
                                ),
                                "Plan Failure Alert",
                                pr_url,
                            ],
                            **act_opts_short,
                        )
                        plan_elapsed = timedelta(0)
                        plan_not_before = workflow.now().isoformat()  # ignore old failure comments in subsequent polls
                        # Reset BEFORE continuing to wait so the next plan signal is captured
                        self._plan_result = None
                        self._plan_error = None  # recorded on the stage above — next failure brings its own
                        continue          # stay in waiting_for_plan

                break  # plan succeeded

            # Terminal: pr_closed
            if self._pr_closed:
                await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "PR was closed"], **act_opts_short)
                await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                _fail_stage = "infra: waiting for manual apply" if apply_exhausted else ("infra: waiting for manual plan" if plan_attempt >= PLAN_MAX_RETRIES else "infra: plan pr")
                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), _PR_CLOSED_ERR, self._pr_group], **act_opts_stage)
                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _PR_CLOSED_ERR, self._pr_group], **act_opts_stage)
                self._step = "failed"
                self._result = "pr_closed"
                raise ApplicationError("pr_closed", non_retryable=True)

            if self._manual_commit_detected or (self._devlift_commit_sha and pr_number and repo_full_name):
                manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                if manual:
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                    await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                    await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                    await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                    _fail_stage = "infra: waiting for manual apply" if apply_exhausted else ("infra: waiting for manual plan" if plan_attempt >= PLAN_MAX_RETRIES else "infra: plan pr")
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                    self._step = "failed"
                    self._result = "manual_commit_detected"
                    raise ApplicationError("manual_commit_detected", non_retryable=True)

            # Terminal: pr_merged (user merged before/without apply)
            if self._merge_result:
                await self._merge_secondary_prs(run_track_key, act_opts_short, act_opts_stage)
                await workflow.execute_activity(send_deployment_completed_dm, args=[user_code, pr_number, repo_full_name, pr_url, project_name, queue_ids], **act_opts_short)
                await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.DEPLOYED], **act_opts_short)
                await workflow.execute_activity(update_resource_status, args=[queue_ids, "ONLINE"], **act_opts_short)
                await workflow.execute_activity(update_queue_status, args=[queue_ids, "DEPLOYED"], **act_opts_short)
                await self._save_service_alb_url(run_track_key, queue_ids, act_opts_short)
                await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                self._step = "active"
                self._result = "success"
                return {"status": "DEPLOYED", "pr_number": pr_number}

            workflow.logger.info("Plan succeeded ✓")
            # Close the WAIT_PLAN stage if we were parked there — either after plan
            # retries were exhausted, or after a destructive-plan block (user pushed a fix).
            if plan_attempt >= PLAN_MAX_RETRIES or self._plan_verification_result == "destructive":
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[run_track_key, "infra: waiting for manual plan", "success", workflow.now().isoformat(), None, self._pr_group],
                    **act_opts_stage,
                )
            if apply_exhausted:
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[run_track_key, "infra: waiting for manual apply", "success", workflow.now().isoformat(), None, self._pr_group],
                    **act_opts_stage,
                )
            apply_exhausted = False  # clear: plan succeeded, apply phase is a fresh start
            await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.PLANNED_SUCCESSFULLY], **act_opts_short)
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: plan pr", "success", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )
            if self._manual_commit_detected or (self._devlift_commit_sha and pr_number and repo_full_name):
                manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                if manual:
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                    await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                    await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                    await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                    self._step = "failed"
                    self._result = "manual_commit_detected"
                    raise ApplicationError("manual_commit_detected", non_retryable=True)

            # ── VERIFY PLAN (AI destructive-change check) ─────────────────────
            # After a successful plan and before approval, an LLM reads the full
            # Atlantis plan output. If it detects destructive (destroy / replace)
            # changes, we post the findings on the PR and loop back to WAIT_PLAN —
            # exactly like apply-exhausted: hold the status (DESTRUCTIVE_PLAN_DETECTED)
            # and wait for the user to push a fix (Atlantis re-plans → we re-verify).
            # Otherwise we proceed to approval. On LLM/API error we fail OPEN (proceed)
            # so an OpenAI outage can't block every deployment.
            if pr_number and repo_full_name:
                self._step = "verifying_plan"
                await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.STARTING_PLAN_VERIFICATION], **act_opts_short)
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[run_track_key, "infra: devlift ai review", "running", workflow.now().isoformat(), None, self._pr_group],
                    **act_opts_stage,
                )
                try:
                    verdict = await workflow.execute_activity(
                        verify_plan_with_ai,
                        args=[pr_number, repo_full_name, plan_not_before],
                        **act_opts_long,
                    )
                except Exception as e:
                    workflow.logger.warning("verify_plan_with_ai failed for PR#%s — failing open (proceeding): %s", pr_number, e)
                    verdict = {"is_destructive": False}

                if verdict.get("is_destructive"):
                    workflow.logger.warning(
                        "Destructive plan detected for PR#%s (severity=%s) — posting findings, back to WAIT_PLAN",
                        pr_number, verdict.get("severity"),
                    )
                    await workflow.execute_activity(
                        post_plan_verification_comment,
                        args=[pr_number, repo_full_name, verdict],
                        **act_opts_short,
                    )
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.DESTRUCTIVE_PLAN_DETECTED], **act_opts_short)
                    await workflow.execute_activity(
                        update_pipeline_run_track_stage,
                        args=[run_track_key, "infra: devlift ai review", "failed", workflow.now().isoformat(),
                              ("Destructive plan detected: " + (verdict.get("summary") or "the plan destroys or replaces existing resources."))[:500], self._pr_group],
                        **act_opts_stage,
                    )
                    await workflow.execute_activity(
                        update_pipeline_run_track_stage,
                        args=[run_track_key, "infra: waiting for manual plan", "running", workflow.now().isoformat(), None, self._pr_group],
                        **act_opts_stage,
                    )
                    # Slack alert → on-call channel (deployer @mentioned) + DM to the deployer.
                    _sev = (verdict.get("severity") or "unknown").upper()
                    _sensitive = verdict.get("sensitive_resources") or []
                    await workflow.execute_activity(
                        send_p0_alert,
                        args=[
                            user_code, pr_number,
                            (
                                f"*Severity:* {_sev}\n"
                                f"*Summary:* {verdict.get('summary') or 'Destructive changes detected in the plan.'}\n"
                                + (f"*Sensitive resources:* {', '.join(_sensitive)}\n" if _sensitive else "")
                                + "\n⚠️ *Action Required*\n"
                                "• The plan will DESTROY or REPLACE existing resources — review the PR before applying.\n"
                                "• Push a fix; the deployment is paused before approval and will re-verify automatically."
                            ),
                            "Destructive Plan Detected",
                            pr_url,
                        ],
                        **act_opts_short,
                    )
                    self._plan_verification_result = "destructive"
                    plan_not_before = workflow.now().isoformat()  # ignore this plan's comments on re-poll
                    self._plan_result = None  # wait for a fresh plan after the user's fix
                    continue  # outer loop → back to WAIT_PLAN
                else:
                    self._plan_verification_result = "clean"
                    # Only post the ✅ "verified" comment when we actually verified a real
                    # plan — not on the fail-open path (LLM error) or when no plan was found,
                    # where we proceed without a genuine verdict.
                    if verdict.get("plan_found"):
                        await workflow.execute_activity(
                            post_plan_verification_comment,
                            args=[pr_number, repo_full_name, verdict],
                            **act_opts_short,
                        )
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.PLAN_VERIFIED_SUCCESSFULLY], **act_opts_short)
                    await workflow.execute_activity(
                        update_pipeline_run_track_stage,
                        args=[run_track_key, "infra: devlift ai review", "success", workflow.now().isoformat(), None, self._pr_group],
                        **act_opts_stage,
                    )

            # ── APPROVE PR (feature-flagged) ──────────────────────────────────
            # Approves the PR using the approval GitHub App so that Atlantis apply
            # doesn't fail on required-review branch protection rules.
            # On failure: P0 alert, update status to APPROVAL_FAILED, wait for user to
            # manually approve or close the PR. Does NOT fail the workflow.
            if pr_number and repo_full_name and _settings.github_approval_enabled:
                self._step = "waiting_for_approval"
                # Reset each plan cycle — ATLANTIS_DISCARD_APPROVAL_ON_PLAN=true discards
                # the previous cycle's approval on every new plan, so a stale True would
                # skip the approve call entirely.
                self._pr_approved = False
                await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.STARTING_APPROVAL], **act_opts_short)
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[run_track_key, "infra: approve pr", "running", workflow.now().isoformat(), None, self._pr_group],
                    **act_opts_stage,
                )

                approval_elapsed = timedelta(0)
                approval_stuck_count = 0
                approval_first_alert_sent = False
                while not (self._merge_result or self._pr_closed or self._pr_approved or self._manual_commit_detected):
                    try:
                        await workflow.execute_activity(
                            approve_pr, args=[pr_number, repo_full_name], **act_opts_short,
                        )
                        self._pr_approved = True  # automated approval succeeded
                        approval_elapsed = timedelta(0)
                        approval_stuck_count = 0
                        approval_first_alert_sent = False
                    except Exception as e:
                        _cause = getattr(e, 'cause', None) or e
                        _err_str = str(_cause)
                        approval_elapsed += PLAN_POLL_INTERVAL
                        await workflow.execute_activity(
                            update_deployment_status, args=[queue_ids, _DS.APPROVAL_FAILED], **act_opts_short,
                        )
                        if not approval_first_alert_sent:
                            approval_first_alert_sent = True
                            await workflow.execute_activity(
                                update_pipeline_run_track_stage,
                                args=[run_track_key, "infra: approve pr", "failed", workflow.now().isoformat(), _err_str[:500], self._pr_group],
                                **act_opts_stage,
                            )
                            await workflow.execute_activity(
                                update_pipeline_run_track_stage,
                                args=[run_track_key, "infra: waiting for approval", "running", workflow.now().isoformat(), None, self._pr_group],
                                **act_opts_stage,
                            )
                            await workflow.execute_activity(
                                send_p0_alert,
                                args=[
                                    user_code, pr_number,
                                    (
                                        f"*Error:* {_err_str}\n\n"
                                        f"⚠️ *Action Required*\n"
                                        f"• Approve the PR manually on GitHub to unblock the deployment."
                                    ),
                                    "Approval Failed",
                                    pr_url,
                                ],
                                **act_opts_short,
                            )
                        if approval_elapsed >= APPLY_ALERT_AFTER:
                            approval_stuck_count += 1
                            if approval_stuck_count >= STUCK_MAX_ALERTS:
                                workflow.logger.warning(
                                    "approval stuck >24h PR#%s — auto-terminating deployment",
                                    pr_number,
                                )
                                await workflow.execute_activity(
                                    update_deployment_status,
                                    args=[queue_ids, _DS.ERROR, "Approval stuck for 24 hours. Deployment auto-terminated."],
                                    **act_opts_short,
                                )
                                await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                                await workflow.execute_activity(
                                    send_p0_alert,
                                    args=[
                                        user_code, pr_number,
                                        (
                                            "*Reason:* PR approval stuck for 24 hours.\n\n"
                                            "⚠️ *Action Required*\n"
                                            "• Start a new deployment from DevLift."
                                        ),
                                        "Deployment Auto-Terminated",
                                        pr_url,
                                    ],
                                    **act_opts_short,
                                )
                                await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                                _fail_stage = "infra: waiting for approval" if approval_first_alert_sent else "infra: approve pr"
                                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), "Approval stuck for 24 hours. Deployment auto-terminated.", self._pr_group], **act_opts_stage)
                                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), "Approval stuck for 24 hours. Deployment auto-terminated.", self._pr_group], **act_opts_stage)
                                self._step = "failed"
                                self._result = "approval_stuck_timeout"
                                raise ApplicationError("approval_stuck_timeout", non_retryable=True)
                            else:
                                workflow.logger.warning(
                                    "P0 ALERT: approve_pr stuck >60min PR#%s — %s (%d/%d alerts)",
                                    pr_number, _err_str, approval_stuck_count, STUCK_MAX_ALERTS,
                                )
                                await workflow.execute_activity(
                                    send_p0_alert,
                                    args=[
                                        user_code, pr_number,
                                        (
                                            f"*Error:* {_err_str}\n\n"
                                            f"⚠️ *Action Required*\n"
                                            f"• Approve the PR manually on GitHub to unblock the deployment."
                                        ),
                                        "Approval Stuck Alert",
                                        pr_url,
                                        False,
                                        True,  # channel_only
                                    ],
                                    **act_opts_short,
                                )
                                approval_elapsed = timedelta(0)
                        try:
                            # Wait for manual approval (pr_approved signal), PR close, or PR merge.
                            # On timeout, poll GitHub reviews API in case the webhook was missed.
                            await workflow.wait_condition(
                                lambda: self._merge_result or self._pr_closed or self._pr_approved or self._manual_commit_detected,
                                timeout=PLAN_POLL_INTERVAL,
                            )
                        except asyncio.TimeoutError:
                            if pr_number and repo_full_name:
                                pr_state = await workflow.execute_activity(
                                    poll_pr_state, args=[pr_number, repo_full_name], **act_opts_short,
                                )
                                if pr_state == "closed":
                                    self._pr_closed = True
                                    await coordinator.signal("pr_merge_completed", args=[pr_number])
                                else:
                                    if self._manual_commit_detected or self._devlift_commit_sha:
                                        manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                                        if manual:
                                            await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                                            await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                                            await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                                            await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                                            _fail_stage = "infra: waiting for approval" if approval_first_alert_sent else "infra: approve pr"
                                            await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                                            await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                                            self._step = "failed"
                                            self._result = "manual_commit_detected"
                                            raise ApplicationError("manual_commit_detected", non_retryable=True)
                                    if pr_state == "merged":
                                        self._merge_result = "merged"
                                        await coordinator.signal("pr_merge_completed", args=[pr_number])
                                    else:
                                        already_approved = await workflow.execute_activity(
                                            poll_pr_approval_status, args=[pr_number, repo_full_name], **act_opts_short,
                                        )
                                        if already_approved:
                                            self._pr_approved = True

                if self._pr_approved and not (self._merge_result or self._pr_closed):
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.APPROVED_SUCCESSFULLY], **act_opts_short)
                    if approval_first_alert_sent:
                        await workflow.execute_activity(
                            update_pipeline_run_track_stage,
                            args=[run_track_key, "infra: waiting for approval", "success", workflow.now().isoformat(), None, self._pr_group],
                            **act_opts_stage,
                        )
                    else:
                        await workflow.execute_activity(
                            update_pipeline_run_track_stage,
                            args=[run_track_key, "infra: approve pr", "success", workflow.now().isoformat(), None, self._pr_group],
                            **act_opts_stage,
                        )

                if self._pr_closed:
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "PR was closed"], **act_opts_short)
                    await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                    await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                    _fail_stage = "infra: waiting for approval" if approval_first_alert_sent else "infra: approve pr"
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), _PR_CLOSED_ERR, self._pr_group], **act_opts_stage)
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _PR_CLOSED_ERR, self._pr_group], **act_opts_stage)
                    self._step = "failed"
                    self._result = "pr_closed"
                    raise ApplicationError("pr_closed", non_retryable=True)

                if self._manual_commit_detected or (self._devlift_commit_sha and pr_number and repo_full_name):
                    manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                    if manual:
                        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                        await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                        await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                        await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                        _fail_stage = "infra: waiting for approval" if approval_first_alert_sent else "infra: approve pr"
                        await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                        await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                        self._step = "failed"
                        self._result = "manual_commit_detected"
                        raise ApplicationError("manual_commit_detected", non_retryable=True)

                if self._merge_result:
                    await self._merge_secondary_prs(run_track_key, act_opts_short, act_opts_stage)
                    await workflow.execute_activity(send_deployment_completed_dm, args=[user_code, pr_number, repo_full_name, pr_url, project_name, queue_ids], **act_opts_short)
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.DEPLOYED], **act_opts_short)
                    await workflow.execute_activity(update_resource_status, args=[queue_ids, "ONLINE"], **act_opts_short)
                    await workflow.execute_activity(update_queue_status, args=[queue_ids, "DEPLOYED"], **act_opts_short)
                    await self._save_service_alb_url(run_track_key, queue_ids, act_opts_short)
                    await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                    self._step = "active"
                    self._result = "success"
                    return {"status": "DEPLOYED", "pr_number": pr_number}

            # ── CHECK: manual commit before apply ────────────────────────────
            if self._manual_commit_detected or (self._devlift_commit_sha and pr_number and repo_full_name):
                manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                if manual:
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                    await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                    await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                    await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                    self._step = "failed"
                    self._result = "manual_commit_detected"
                    raise ApplicationError("manual_commit_detected", non_retryable=True)

            # ── APPLY (with up to APPLY_MAX_RETRIES retries) ─────────────────
            await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.STARTING_APPLYING], **act_opts_short)
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: apply pr", "running", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )
            apply_attempt = 0
            self._apply_result = None
            while True:
                # _apply_result is NOT reset here — same race condition fix as plan loop.
                # Reset happens before each activity where a signal could arrive early.
                self._step = "posting_apply_comment"
                # Reset before the activity so a fast apply result is captured, not lost
                self._apply_result = None
                self._apply_error = None  # each attempt records its own error
                if pr_number and repo_full_name:
                    await workflow.execute_activity(
                        post_apply_comment, args=[pr_number, repo_full_name, project_name], **act_opts_short,
                    )
                if self._apply_result is None:
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.APPLYING], **act_opts_short)
                workflow.logger.info("Apply comment posted. Waiting for apply signal...")

                self._step = "waiting_for_apply"
                apply_elapsed = timedelta(0)
                apply_stuck_count = 0

                while self._apply_result is None and not (self._merge_result or self._pr_closed or self._manual_commit_detected):
                    try:
                        await workflow.wait_condition(
                            lambda: self._apply_result is not None or self._merge_result or self._pr_closed or self._manual_commit_detected,
                            timeout=APPLY_POLL_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        apply_elapsed += APPLY_POLL_INTERVAL
                        if pr_number and repo_full_name:
                            pr_state = await workflow.execute_activity(
                                poll_pr_state, args=[pr_number, repo_full_name], **act_opts_short,
                            )
                            if pr_state == "closed":
                                self._pr_closed = True
                                await coordinator.signal("pr_merge_completed", args=[pr_number])
                            else:
                                if self._manual_commit_detected or self._devlift_commit_sha:
                                    manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                                    if manual:
                                        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                                        await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                                        await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                                        await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                                        await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, "infra: apply pr", "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                                        await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                                        self._step = "failed"
                                        self._result = "manual_commit_detected"
                                        raise ApplicationError("manual_commit_detected", non_retryable=True)
                                if pr_state == "merged":
                                    self._merge_result = "merged"
                                    await coordinator.signal("pr_merge_completed", args=[pr_number])
                                else:
                                    poll_result = await workflow.execute_activity(
                                        poll_for_apply_status, args=[pr_number, repo_full_name], **act_opts_short,
                                    )
                                    _ap_parts = poll_result.split(":", 1) if poll_result else []
                                    _ap = _ap_parts[0] if _ap_parts else poll_result
                                    if _ap in ("success", "failed"):
                                        self._apply_result = _ap
                                        if _ap == "failed" and len(_ap_parts) > 1 and _ap_parts[1]:
                                            self._apply_error = _ap_parts[1]
                        if self._apply_result is None and apply_elapsed >= APPLY_ALERT_AFTER:
                            apply_stuck_count += 1
                            if apply_stuck_count >= STUCK_MAX_ALERTS:
                                workflow.logger.warning(
                                    "apply stuck >24h PR#%s — auto-terminating deployment",
                                    pr_number,
                                )
                                await workflow.execute_activity(
                                    update_deployment_status,
                                    args=[queue_ids, _DS.ERROR, "Apply stuck for 24 hours. Deployment auto-terminated."],
                                    **act_opts_short,
                                )
                                await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                                await workflow.execute_activity(
                                    send_p0_alert,
                                    args=[
                                        user_code, pr_number,
                                        (
                                            "*Reason:* Apply stuck for 24 hours with no result from Atlantis.\n\n"
                                            "⚠️ *Action Required*\n"
                                            "• Start a new deployment from DevLift."
                                        ),
                                        "Deployment Auto-Terminated",
                                        pr_url,
                                    ],
                                    **act_opts_short,
                                )
                                await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, "infra: apply pr", "failed", workflow.now().isoformat(), "Apply stuck for 24 hours with no result from Atlantis. Deployment auto-terminated.", self._pr_group], **act_opts_stage)
                                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), "Apply stuck for 24 hours with no result from Atlantis. Deployment auto-terminated.", self._pr_group], **act_opts_stage)
                                self._step = "failed"
                                self._result = "apply_stuck_timeout"
                                raise ApplicationError("apply_stuck_timeout", non_retryable=True)
                            else:
                                workflow.logger.warning(
                                    "P0 ALERT: apply stuck >60min PR#%s — %d project(s) waiting (%d/%d alerts)",
                                    pr_number, len(project_dirs), apply_stuck_count, STUCK_MAX_ALERTS,
                                )
                                await workflow.execute_activity(
                                    send_p0_alert,
                                    args=[
                                        user_code, pr_number,
                                        (
                                            "*Status:* In progress >60 min\n\n"
                                            "⚠️ *Action Required*\n"
                                            "• Check Atlantis for the current status.\n"
                                            "• Trigger atlantis apply manually if needed."
                                        ),
                                        "Apply Stuck Alert",
                                        pr_url,
                                        False,
                                        True,  # channel_only
                                    ],
                                    **act_opts_short,
                                )
                                apply_elapsed = timedelta(0)

                # Terminal: pr_closed
                if self._pr_closed:
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "PR was closed"], **act_opts_short)
                    await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                    await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, "infra: apply pr", "failed", workflow.now().isoformat(), _PR_CLOSED_ERR, self._pr_group], **act_opts_stage)
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _PR_CLOSED_ERR, self._pr_group], **act_opts_stage)
                    self._step = "failed"
                    self._result = "pr_closed"
                    raise ApplicationError("pr_closed", non_retryable=True)

                if self._manual_commit_detected or (self._devlift_commit_sha and pr_number and repo_full_name):
                    manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                    if manual:
                        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                        await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                        await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                        await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                        await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, "infra: apply pr", "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                        await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                        self._step = "failed"
                        self._result = "manual_commit_detected"
                        raise ApplicationError("manual_commit_detected", non_retryable=True)

                # Terminal: pr_merged (user merged during apply wait)
                if self._merge_result:
                    await self._merge_secondary_prs(run_track_key, act_opts_short, act_opts_stage)
                    await workflow.execute_activity(send_deployment_completed_dm, args=[user_code, pr_number, repo_full_name, pr_url, project_name, queue_ids], **act_opts_short)
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.DEPLOYED], **act_opts_short)
                    await workflow.execute_activity(update_resource_status, args=[queue_ids, "ONLINE"], **act_opts_short)
                    await workflow.execute_activity(update_queue_status, args=[queue_ids, "DEPLOYED"], **act_opts_short)
                    await self._save_service_alb_url(run_track_key, queue_ids, act_opts_short)
                    await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                    self._step = "active"
                    self._result = "success"
                    return {"status": "DEPLOYED", "pr_number": pr_number}

                if self._apply_result == "failed":
                    apply_attempt += 1
                    if apply_attempt < APPLY_MAX_RETRIES:
                        workflow.logger.warning(
                            "Apply failed (attempt %d/%d) — retrying via 'atlantis apply' comment",
                            apply_attempt, APPLY_MAX_RETRIES,
                        )
                        apply_elapsed = timedelta(0)
                        # _apply_result is reset at the top of the loop (before post_apply_comment)
                        continue  # retry apply
                    else:
                        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.APPLY_FAILED], **act_opts_short)
                        await workflow.execute_activity(
                            update_pipeline_run_track_stage,
                            args=[run_track_key, "infra: apply pr", "failed", workflow.now().isoformat(),
                                  self._apply_error or f"Terraform apply failed after {apply_attempt} attempts — review the apply error details on the PR.", self._pr_group],
                            **act_opts_stage,
                        )
                        await workflow.execute_activity(
                            update_pipeline_run_track_stage,
                            args=[run_track_key, "infra: waiting for manual apply", "running", workflow.now().isoformat(), None, self._pr_group],
                            **act_opts_stage,
                        )
                        workflow.logger.warning(
                            "P0 ALERT: apply failed %d times PR#%s — waiting for user to push fix (Atlantis will auto-plan)",
                            apply_attempt, pr_number,
                        )
                        await workflow.execute_activity(
                            send_p0_alert,
                            args=[
                                user_code, pr_number,
                                (
                                    "⚠️ *Action Required*\n"
                                    "• Review the apply error details on the PR.\n"
                                    "• Atlantis will auto-plan on your next commit."
                                ),
                                "Apply Failure Alert",
                                pr_url,
                            ],
                            **act_opts_short,
                        )
                        break  # exit apply retry loop

                break  # apply succeeded

            if self._apply_result == "failed":
                apply_exhausted = True  # stay at APPLY_FAILED until user pushes fix and plan actually starts
                plan_not_before = workflow.now().isoformat()  # ignore old plan-success comments when re-entering plan wait
                continue  # outer loop → back to WAIT_PLAN

            workflow.logger.info("Apply completed ✓. Checking freshness and merging PR...")
            await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.APPLIED_SUCCESSFULLY], **act_opts_short)
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: apply pr", "success", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: merge pr", "running", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )

            if self._manual_commit_detected or (self._devlift_commit_sha and pr_number and repo_full_name):
                manual = self._manual_commit_detected or await workflow.execute_activity(check_for_manual_commit, args=[pr_number, repo_full_name, self._devlift_commit_sha], **act_opts_short)
                if manual:
                    await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "Manual commit detected during deployment. Workflow terminated."], **act_opts_short)
                    await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                    await workflow.execute_activity(
                                        send_p0_alert,
                                        args=[
                                            user_code, pr_number,
                                            (
                                                f"*Reason:* Manual commit detected during deployment\n\n"
                                                f"⚠️ *Action Required*\n"
                                                f"• Start a new deployment from DevLift."
                                            ),
                                            "Deployment Terminated",
                                            pr_url,
                                        ],
                                        **act_opts_short,
                                    )
                    await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, "infra: merge pr", "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                    await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _MANUAL_COMMIT_ERR, self._pr_group], **act_opts_stage)
                    self._step = "failed"
                    self._result = "manual_commit_detected"
                    raise ApplicationError("manual_commit_detected", non_retryable=True)

            # ── PROGRAMMATIC MERGE (with freshness check) ────────────────────
            # check_and_merge_pr:
            #   "stale"    → new commit arrived during apply → re-plan
            #   "merged"   → merged successfully → done
            #   "conflict" → merge conflict → resolve_conflict_and_merge → back to plan (no alert)
            #   "failed"   → non-conflict merge failure → retry up to MERGE_MAX_RETRIES, then P0 alert
            merge_attempt = 0
            merge_outcome = ""
            conflict_triggered_replan = False
            while merge_attempt < MERGE_MAX_RETRIES and not (self._merge_result or self._pr_closed) and pr_number and repo_full_name:
                merge_attempt += 1
                try:
                    merge_outcome = await workflow.execute_activity(
                        check_and_merge_pr,
                        args=[pr_number, repo_full_name],
                        **act_opts_short,
                    )
                except Exception as _merge_exc:
                    workflow.logger.warning(
                        "check_and_merge_pr raised on attempt %d/%d for PR #%s: %s",
                        merge_attempt, MERGE_MAX_RETRIES, pr_number, _merge_exc,
                    )
                    merge_outcome = "failed"

                if merge_outcome == "conflict":
                    workflow.logger.info(
                        "PR #%s has merge conflicts — resolving and posting plan comment",
                        pr_number,
                    )
                    await workflow.execute_activity(
                        resolve_conflict_and_merge,
                        args=[pr_number, repo_full_name, tenant_code, user_code],
                        **act_opts_short,
                    )
                    await workflow.execute_activity(
                        post_plan_comment, args=[pr_number, repo_full_name, project_name], **act_opts_short,
                    )
                    # The pre-resolve plan succeeded, so without these the poll matches
                    # that old comment and applies before Atlantis has answered the plan
                    # just requested — the apply then hits the project's own plan lock.
                    plan_not_before = workflow.now().isoformat()
                    self._plan_result = None
                    # Close out the merge stage before looping back to plan. Left
                    # "running" it sits alongside the re-running plan stage, and a
                    # workflow that dies mid-replan strands it there forever. The
                    # retry sets it back to running, then success.
                    await workflow.execute_activity(
                        update_pipeline_run_track_stage,
                        args=[run_track_key, "infra: merge pr", "failed", workflow.now().isoformat(),
                              "Merge blocked by a conflict with the base branch — resolved, re-planning.", self._pr_group],
                        **act_opts_stage,
                    )
                    conflict_triggered_replan = True
                    break  # exit merge loop → outer loop → back to WAIT_PLAN
                elif merge_outcome == "stale":
                    workflow.logger.warning(
                        "PR #%s is stale (new commit after last plan) — re-planning", pr_number
                    )
                    if pr_number and repo_full_name:
                        await workflow.execute_activity(
                            post_plan_comment, args=[pr_number, repo_full_name, project_name], **act_opts_short,
                        )
                        # Same staleness as the conflict branch above.
                        plan_not_before = workflow.now().isoformat()
                        self._plan_result = None
                        await workflow.execute_activity(
                            update_pipeline_run_track_stage,
                            args=[run_track_key, "infra: merge pr", "failed", workflow.now().isoformat(),
                                  "Base branch moved after the last plan — re-planning before merge.", self._pr_group],
                            **act_opts_stage,
                        )
                    break  # exit merge retry → outer loop → back to WAIT_PLAN
                elif merge_outcome == "merged":
                    self._merge_result = "merged"
                    break
                else:
                    workflow.logger.warning(
                        "Merge attempt %d/%d failed for PR #%s — retrying",
                        merge_attempt, MERGE_MAX_RETRIES, pr_number,
                    )

            if merge_outcome == "conflict" or conflict_triggered_replan:
                conflict_triggered_replan = False
                continue  # outer loop → back to plan phase (conflict resolved, Atlantis will re-plan)

            if merge_outcome == "stale":
                continue  # outer loop → back to plan phase

            if not (self._merge_result or self._pr_closed) and merge_attempt >= MERGE_MAX_RETRIES:
                workflow.logger.warning(
                    "P0 ALERT: merge exhausted %d attempts for PR #%s — waiting for manual merge",
                    MERGE_MAX_RETRIES, pr_number,
                )
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[run_track_key, "infra: merge pr", "failed", workflow.now().isoformat(),
                          f"Automatic merge failed after {MERGE_MAX_RETRIES} attempts — merge the PR manually on GitHub.", self._pr_group],
                    **act_opts_stage,
                )
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[run_track_key, "infra: waiting for merge", "running", workflow.now().isoformat(), None, self._pr_group],
                    **act_opts_stage,
                )
                await workflow.execute_activity(
                    send_p0_alert,
                    args=[
                        user_code, pr_number,
                        (
                            "⚠️ *Action Required*\n"
                            "• Merge the PR manually on GitHub."
                        ),
                        "Merge Failed",
                        pr_url,
                    ],
                    **act_opts_short,
                )

            await workflow.execute_activity(update_queue_status, args=[queue_ids, "DEPLOYING"], **act_opts_short)

            # ── WAIT_MERGE (manual fallback if programmatic merge exhausted) ─
            self._merge_conflict = False
            self._step = "waiting_for_merge"
            merge_elapsed = timedelta(0)

            while not (self._merge_result or self._merge_conflict or self._pr_closed or self._manual_commit_detected):
                try:
                    await workflow.wait_condition(
                        lambda: self._merge_result or self._merge_conflict or self._pr_closed or self._manual_commit_detected,
                        timeout=MERGE_POLL_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    merge_elapsed += MERGE_POLL_INTERVAL
                    if pr_number and repo_full_name:
                        pr_state = await workflow.execute_activity(
                            poll_pr_state, args=[pr_number, repo_full_name], **act_opts_short,
                        )
                        if pr_state == "merged":
                            self._merge_result = "merged"
                            await coordinator.signal("pr_merge_completed", args=[pr_number])
                        elif pr_state == "closed":
                            self._pr_closed = True
                            await coordinator.signal("pr_merge_completed", args=[pr_number])
                    if not (self._merge_result or self._merge_conflict or self._pr_closed) and merge_elapsed >= MERGE_ALERT_AFTER:
                        workflow.logger.warning(
                            "P0 ALERT: merge stuck >%s PR#%s — %d project(s) waiting",
                            merge_elapsed, pr_number, len(project_dirs),
                        )
                        await workflow.execute_activity(
                            send_p0_alert,
                            args=[
                                user_code, pr_number,
                                (
                                    "⚠️ *Action Required*\n"
                                    "• Merge the PR manually on GitHub."
                                ),
                                "Merge Stuck",
                                pr_url,
                            ],
                            **act_opts_short,
                        )
                        merge_elapsed = timedelta(0)

            # Terminal: pr_closed
            if self._pr_closed:
                await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.ERROR, "PR was closed"], **act_opts_short)
                await workflow.execute_activity(update_queue_status, args=[queue_ids, "FAILED"], **act_opts_short)
                await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
                _fail_stage = "infra: waiting for merge" if merge_attempt >= MERGE_MAX_RETRIES else "infra: merge pr"
                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _fail_stage, "failed", workflow.now().isoformat(), _PR_CLOSED_ERR, self._pr_group], **act_opts_stage)
                await workflow.execute_activity(update_pipeline_run_track_stage, args=[run_track_key, _completed_stage, "failed", workflow.now().isoformat(), _PR_CLOSED_ERR, self._pr_group], **act_opts_stage)
                self._step = "failed"
                self._result = "pr_closed"
                raise ApplicationError("pr_closed", non_retryable=True)

            if self._merge_result:
                if merge_attempt >= MERGE_MAX_RETRIES:
                    await workflow.execute_activity(
                        update_pipeline_run_track_stage,
                        args=[run_track_key, "infra: waiting for merge", "success", workflow.now().isoformat(), None, self._pr_group],
                        **act_opts_stage,
                    )
                break  # merged — exit outer loop → success

            # Merge conflict: coordinator handles resolve_conflict_and_merge and force-pushes
            # the feature branch. With autoplan disabled the coordinator must post a plan
            # comment after the force-push; for now we loop back and wait for the plan signal.
            workflow.logger.info("Merge conflict on PR #%s — coordinator resolving, going back to plan", pr_number)

        # ── SUCCESS ──────────────────────────────────────────────────────────
        self._step = "active"
        self._result = "success"

        # Merge secondary PRs (k8s manifest + workflow) after infra PR is done
        await self._merge_secondary_prs(run_track_key, act_opts_short, act_opts_stage)

        await workflow.execute_activity(send_deployment_completed_dm, args=[user_code, pr_number, repo_full_name, pr_url, project_name, queue_ids], **act_opts_short)
        await workflow.execute_activity(update_deployment_status, args=[queue_ids, _DS.DEPLOYED], **act_opts_short)
        await workflow.execute_activity(update_resource_status, args=[queue_ids, "ONLINE"], **act_opts_short)
        await workflow.execute_activity(update_queue_status, args=[queue_ids, "DEPLOYED"], **act_opts_short)
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[run_track_key, "infra: merge pr", "success", workflow.now().isoformat(), None, self._pr_group],
            **act_opts_stage,
        )
        await workflow.execute_activity(
            update_pipeline_run_track_stage,
            args=[run_track_key, _completed_stage, "success", workflow.now().isoformat(), None, self._pr_group],
            **act_opts_stage,
        )
        await self._save_service_alb_url(run_track_key, queue_ids, act_opts_short)
        await coordinator.signal("release_locks", args=[workflow_id, project_dirs])
        workflow.logger.info("Locks released for: %s", project_dirs)
        workflow.logger.info("Deployment ACTIVE ✓  PR #%s", pr_number)
        return {"status": "ACTIVE", "pr_number": pr_number}

    # ── Stage helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _secondary_artifact(pr_type: str) -> str:
        """Run-track artifact label for a secondary PR: k8s-manifests PRs are
        'k8s', service-repo (workflow YAML) PRs are 'service'."""
        return {"k8s_manifest": "k8s", "workflow": "service"}.get(pr_type, pr_type)

    def _ordered_secondary_prs(self) -> list:
        """Secondary PRs in stage-display order: k8s before service."""
        order = {"k8s_manifest": 0, "workflow": 1}
        return sorted(
            self._secondary_prs,
            key=lambda p: order.get(p.get("pr_type", "workflow"), 2),
        )

    async def _record_created_prs(
        self, run_track_key: str, include_infra: bool, act_opts_stage: dict
    ) -> None:
        """Per-artifact 'create pr' run-track entries derived from what
        run_script_pr_workflow actually produced — the stage list mirrors the
        deploy's real shape (no infra entry on a no-diff run, etc.).
        Also persists the created PRs into deploy_result.prs so the run
        carries the PR links: [{name, url, repo, number, group}]. "group"
        (self._pr_group) tells apart which child produced each PR when several
        DeploymentWorkflow children share ONE multi-deploy run-track row —
        update_pipeline_run_track_deploy_result appends rather than replaces,
        so every child's PRs survive on the shared row."""
        prs: list = []
        if include_infra:
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, "infra: create pr", "success", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )
            if self._pr_number and self._repo_full_name:
                prs.append({
                    "name": "INFRA PR",
                    "url": f"https://github.com/{self._repo_full_name}/pull/{self._pr_number}",
                    "repo": self._repo_full_name,
                    "number": self._pr_number,
                    "group": self._pr_group,
                })
        _PR_LABEL = {"k8s": "K8S PR", "service": "SERVICE PR"}
        for sec_pr in self._ordered_secondary_prs():
            if not (sec_pr.get("pr_number") and sec_pr.get("repo_full_name")):
                continue
            artifact = self._secondary_artifact(sec_pr.get("pr_type", "workflow"))
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, f"{artifact}: create PR", "success", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )
            prs.append({
                "name": _PR_LABEL.get(artifact, f"{artifact.upper()} PR"),
                "url": f"https://github.com/{sec_pr['repo_full_name']}/pull/{sec_pr['pr_number']}",
                "repo": sec_pr["repo_full_name"],
                "number": sec_pr["pr_number"],
                "group": self._pr_group,
            })
        if prs:
            await workflow.execute_activity(
                update_pipeline_run_track_deploy_result,
                args=[run_track_key, {"prs": prs}],
                **act_opts_stage,
            )

    async def _merge_secondary_prs(
        self, run_track_key: str, act_opts_short: dict, act_opts_stage: dict
    ) -> None:
        """Merge every secondary PR (k8s manifests / service workflow YAML),
        writing a per-artifact 'merge pr' stage around each merge."""
        for sec_pr in self._ordered_secondary_prs():
            _sec_num = sec_pr.get("pr_number")
            _sec_repo = sec_pr.get("repo_full_name")
            _sec_type = sec_pr.get("pr_type", "workflow")
            if not (_sec_num and _sec_repo):
                continue
            artifact = self._secondary_artifact(_sec_type)
            workflow.logger.info(
                "Merging secondary PR #%s (%s) repo=%s", _sec_num, _sec_type, _sec_repo
            )
            if _sec_type == "k8s_manifest":
                # k8s-manifests repo has required-review branch protection — approve
                # with the same codeowner PAT used for the infra PR before merging.
                # (approve_pr self-skips when GITHUB_APPROVAL_ENABLED=false.)
                # Best-effort: an approval failure surfaces via the merge attempt.
                try:
                    await workflow.execute_activity(
                        approve_pr, args=[_sec_num, _sec_repo], **act_opts_short,
                    )
                except Exception as e:
                    workflow.logger.warning(
                        "approve_pr failed for k8s manifest PR #%s (%s): %s — proceeding to merge",
                        _sec_num, _sec_repo, error_message(e),
                    )
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[run_track_key, f"{artifact}: merge pr", "running", workflow.now().isoformat(), None, self._pr_group],
                **act_opts_stage,
            )
            # merge_secondary_pr never raises on failure — it logs and returns
            # "failed" so one secondary PR's problem can't crash the whole
            # deployment. That means the result MUST be checked explicitly;
            # ignoring it (as before) let a real "failed" silently get
            # recorded as "success" on the run track.
            merge_outcome = await workflow.execute_activity(
                merge_secondary_pr,
                args=[_sec_num, _sec_repo, _sec_type],
                **act_opts_short,
            )
            if merge_outcome in ("merged", "already_merged"):
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[run_track_key, f"{artifact}: merge pr", "success", workflow.now().isoformat(), None, self._pr_group],
                    **act_opts_stage,
                )
            else:
                _sec_pr_url = f"https://github.com/{_sec_repo}/pull/{_sec_num}"
                await workflow.execute_activity(
                    update_pipeline_run_track_stage,
                    args=[run_track_key, f"{artifact}: merge pr", "failed", workflow.now().isoformat(),
                          f"Automatic merge failed — merge PR #{_sec_num} manually on GitHub.", self._pr_group],
                    **act_opts_stage,
                )
                # Record the truth and move on — no retry, no blocking wait.
                await workflow.execute_activity(
                    send_p0_alert,
                    args=[
                        self._user_code, _sec_num,
                        (
                            f"*Reason:* {artifact} PR #{_sec_num} failed to merge automatically.\n\n"
                            f"⚠️ *Action Required*\n"
                            f"• Merge {_sec_repo}#{_sec_num} manually on GitHub."
                        ),
                        "Secondary PR Merge Failed",
                        _sec_pr_url,
                    ],
                    **act_opts_short,
                )

    async def _save_service_alb_url(
        self, run_track_key: str, queue_ids: List[int], act_opts_short: dict
    ) -> None:
        """Best-effort: after a successful infra deployment, persist the
        shared application ALB URL + service path into deploy_result.
        Never fails the deployment."""
        try:
            await workflow.execute_activity(
                save_service_alb_url, args=[run_track_key, queue_ids], **act_opts_short,
            )
        except Exception as e:
            workflow.logger.warning("save_service_alb_url failed (non-fatal): %s", e)

    # ── Signals ──────────────────────────────────────────────────────────────

    @workflow.signal
    def locks_granted(self) -> None:
        workflow.logger.info("locks_granted received from coordinator ✓")
        self._locks_granted = True

    @workflow.signal
    def plan_completed(self) -> None:
        workflow.logger.info("Signal: plan_completed ✓")
        self._plan_result = "success"

    @workflow.signal
    def plan_failed(self, error: str | None = None) -> None:
        workflow.logger.info("Signal: plan_failed ✗")
        self._plan_result = "failed"
        if error:
            self._plan_error = error

    @workflow.signal
    def lock_conflict_detected(self, locking_pr: int | None = None) -> None:
        workflow.logger.info("Signal: lock_conflict_detected locking_pr=%s", locking_pr)
        self._plan_result = f"lock_conflict:{locking_pr}" if locking_pr else "lock_conflict"

    @workflow.signal
    def atlas_lock_abort(self, reason: str) -> None:
        workflow.logger.warning("Signal: atlas_lock_abort reason=%s", reason)
        self._force_terminate = True
        self._force_terminate_reason = reason

    @workflow.signal
    def apply_completed(self) -> None:
        workflow.logger.info("Signal: apply_completed ✓")
        self._apply_result = "success"

    @workflow.signal
    def apply_failed(self, error: str | None = None) -> None:
        workflow.logger.info("Signal: apply_failed ✗")
        self._apply_result = "failed"
        if error:
            self._apply_error = error

    @workflow.signal
    def pr_merged(self) -> None:
        workflow.logger.info("Signal: pr_merged ✓")
        self._merge_result = "merged"

    @workflow.signal
    def pr_closed(self) -> None:
        workflow.logger.info("Signal: pr_closed")
        self._pr_closed = True

    @workflow.signal
    def pr_approved(self) -> None:
        workflow.logger.info("Signal: pr_approved (manual review) ✓")
        self._pr_approved = True

    @workflow.signal
    def push_detected(self, new_sha: str) -> None:
        if self._devlift_commit_sha and new_sha != self._devlift_commit_sha:
            workflow.logger.warning(
                "push_detected: SHA mismatch (expected=%s got=%s) — manual commit flagged",
                self._devlift_commit_sha[:8], new_sha[:8],
            )
            self._manual_commit_detected = True

    @workflow.signal
    async def merge_conflict_detected(self, git_repository: str) -> None:
        workflow.logger.info("Signal: merge_conflict_detected PR #%s", self._pr_number)
        self._merge_conflict = True
        if self._pr_number is None:
            return
        coordinator = workflow.get_external_workflow_handle(f"coordinator-{self._tenant_code}")
        await coordinator.signal(
            "enqueue_merge",
            args=[self._pr_number, git_repository, self._tenant_code, self._user_code],
        )

    # ── Query ────────────────────────────────────────────────────────────────

    @workflow.query
    def get_state(self) -> dict:
        return {
            "step": self._step,
            "pr_number": self._pr_number,
            "repo_full_name": self._repo_full_name,
            "locks_granted": self._locks_granted,
            "result": self._result,
        }
