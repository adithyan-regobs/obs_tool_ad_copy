"""
TenantCoordinatorWorkflow — ONE instance per tenant, runs forever.

This is the lock manager. Every DeploymentWorkflow signals THIS coordinator
to acquire or release locks on project_dirs.

Why one coordinator per tenant?
  - A single Temporal workflow processes signals ONE AT A TIME (sequential).
  - So lock decisions are atomic — no two DeploymentWorkflows can see
    the same "free" state simultaneously.
  - This gives us the same all-or-nothing guarantee as a SQL transaction,
    without needing a database table.

Internal state (no DB, lives in Temporal):
  locks       → {project_dir: workflow_id}  — who holds which dir right now
  hold_queue  → [{workflow_id, required_dirs}] — who is waiting and for what
  merge_queue → [{pr_number, git_repository, tenant_code, user_code}]
                  — PRs waiting for conflict resolution (processed one at a time)

Structure:
  run()               — restores carried state, runs the merge loop, and parks
                        until the server suggests resetting history, then
                        continue-as-new (see below)
  _merge_queue_loop   — sequentially resolves merge conflicts via activity
  Lock handling is purely signal-driven (acquire_locks / release_locks
  handlers); a slow merge resolution never blocks lock grants.

Flow:
  DW-A signals acquire_locks → all free → grant → signal DW-A: locks_granted
  DW-B signals acquire_locks → some taken → add to hold_queue
  DW-A signals release_locks → check hold_queue → find anyone unblocked → grant them
  Webhook detects Atlantis "Merge Failed" → signals DW → DW signals coordinator enqueue_merge
  coordinator merge_queue_loop processes conflict resolutions sequentially

Continue-as-new (history reset):
  A forever-running workflow accumulates event history without bound. Temporal
  enforces hard per-execution limits — most bitingly 10,000 received signals,
  after which the server REJECTS every further signal: deployments then die
  instantly at acquire_locks (this took prod down on 2026-09-01 when
  coordinator-aspora crossed the limit at ~42k history events). Unbounded
  history is also why worker redeploys took 10-20 min to revive the
  coordinator (full-history replay).

  The fix is Temporal's standard one: when the server suggests it
  (is_continue_as_new_suggested — history grew large), run() snapshots
  {locks, hold_queue, merge_queue} — all plain JSON — and continue-as-new
  with that snapshot as the new run's input. Same workflow ID, fresh history,
  fresh signal counter, nothing lost:
    - running deployments: their locks are in the carried snapshot; their
      later release_locks signals route by workflow ID to the new run.
    - queued deployments: parked in their OWN workflows waiting for a
      locks_granted signal; their hold_queue entries are carried, and the new
      run's grant-scan signals them exactly as the old run would have.
    - signals racing the transition: the server refuses to complete a
      continue-as-new past an unprocessed signal (task retried with the
      signal included), and we gate on all_handlers_finished so no handler
      is ever chopped mid-flight.
  Safety gates before the reset: no merge item mid-flight (merge_queue empty)
  and no signal handler still running. A 30-day timer is the fallback trigger
  so history stays small even if the server suggestion never fires.

Changing this workflow:
  Temporal replays a run's ENTIRE history through the current code, so an
  edit that decides something the recorded history contradicts kills the run
  — and with it every deployment behind it. On 2026-09-02 the reset logic
  above shipped unguarded; the two prod coordinators (started 2026-06-05) had
  already passed _HISTORY_RESET_AT, so replay re-decided that crossing in the
  past and wedged them for nine days at ~39k events and 8.8k of the 10k
  signals. They were recovered on 2026-09-11 by briefly replaying the old
  shape, then continue-as-new into fresh runs.

  Before deploying ANY change here, replay it against a real history:
      curl .../temporal/workflows/coordinator-aspora/history > h.json
      python scripts/replay_coordinator_history.py h.json
"""

import asyncio
from datetime import timedelta
from typing import Dict, List, Optional
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.temporal.activities.deploy_activities import resolve_conflict_and_merge

# Our own continue-as-new trigger, independent of the server's suggestion flag.
# Far below the 10k per-execution signal cap and the replay-cost threshold.
_HISTORY_RESET_AT = 5_000

_MERGE_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
)


@workflow.defn
class TenantCoordinatorWorkflow:

    def __init__(self):
        # {project_dir: workflow_id} — who currently holds each dir
        self._locks: Dict[str, str] = {}

        # [{workflow_id, required_dirs}] — workflows waiting for locks
        self._hold_queue: List[Dict] = []

        # [{pr_number, git_repository, tenant_code, user_code}]
        self._merge_queue: List[Dict] = []

        self._terminate = False

    @workflow.run
    async def run(self, tenant_code: str, carried_state: Optional[Dict] = None) -> None:
        # carried_state — the previous run's snapshot when this run was started
        # by continue-as-new; None on a genuinely fresh start (app startup hook
        # passes only tenant_code, so the signature stays backward-compatible).
        if carried_state:
            self._locks = dict(carried_state.get("locks") or {})
            self._hold_queue = list(carried_state.get("hold_queue") or [])
            self._merge_queue = list(carried_state.get("merge_queue") or [])
            workflow.logger.info(
                "Coordinator resumed for tenant %s (continue-as-new): "
                "locks=%d hold_queue=%d merge_queue=%d",
                tenant_code, len(self._locks), len(self._hold_queue), len(self._merge_queue),
            )
        else:
            workflow.logger.info(f"Coordinator started for tenant: {tenant_code}")

        merge_loop = asyncio.create_task(self._merge_queue_loop())

        # Park until a history reset is due (re-evaluated on every new event as
        # signals arrive), with a 30-day timer as the last-resort trigger.
        #
        # TWO conditions on purpose. is_continue_as_new_suggested() is set by
        # the SERVER from a dynamic config (limit.historyCount.suggestContinueAsNew)
        # whose value we do not control on a self-hosted Temporal. If it is
        # tuned above the per-execution signal cap (10k signals — the limit that
        # took prod down on 2026-09-01 at ~42k events), the suggestion would
        # never fire in time and the 30-day timer would be the only guard.
        # _HISTORY_RESET_AT makes the trigger self-sufficient: well below both
        # the signal cap and the point where full-history replay gets slow.
        try:
            await workflow.wait_condition(
                lambda: (
                    workflow.info().is_continue_as_new_suggested()
                    or workflow.info().get_current_history_length() > _HISTORY_RESET_AT
                ),
                timeout=timedelta(days=30),
            )
            workflow.logger.info(
                "Coordinator history reset (history=%d events)",
                workflow.info().get_current_history_length(),
            )
        except asyncio.TimeoutError:
            workflow.logger.info("Coordinator history reset: 30-day fallback timer")

        # Safe point: no merge item mid-flight (its transient processing state
        # can't be carried) and no signal handler still mid-execution (its
        # half-applied changes must land in the snapshot, not be chopped).
        # Signals that arrive after this gate are protected by the server:
        # a continue-as-new past an unprocessed signal is refused and the
        # workflow task retried with the signal included.
        await workflow.wait_condition(
            lambda: not self._merge_queue and workflow.all_handlers_finished()
        )

        merge_loop.cancel()
        workflow.continue_as_new(
            args=[
                tenant_code,
                {
                    "locks": self._locks,
                    "hold_queue": self._hold_queue,
                    "merge_queue": self._merge_queue,
                },
            ]
        )

    async def _merge_queue_loop(self) -> None:
        while True:
            try:
                await workflow.wait_condition(
                    lambda: bool(self._merge_queue) or self._terminate,
                    timeout=timedelta(days=365),
                )
            except asyncio.TimeoutError:
                break
            if self._terminate:
                break

            item = self._merge_queue[0]
            item["_released"] = False
            pr_number = item["pr_number"]
            workflow.logger.info(
                "merge_queue: processing PR#%s repo=%s",
                pr_number, item["git_repository"],
            )

            try:
                await workflow.execute_activity(
                    resolve_conflict_and_merge,
                    args=[
                        pr_number,
                        item["git_repository"],
                        item["tenant_code"],
                        item["user_code"],
                    ],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_MERGE_RETRY,
                )
                workflow.logger.info("merge_queue: resolved PR#%s ✓", pr_number)
            except Exception as exc:
                workflow.logger.error(
                    "merge_queue: failed to resolve PR#%s: %s",
                    pr_number, exc,
                )

            # Wait for the PR to actually merge or close before processing next item
            workflow.logger.info("merge_queue: waiting for PR#%s to merge/close", pr_number)
            try:
                await workflow.wait_condition(
                    lambda: item["_released"] or self._terminate,
                    timeout=timedelta(minutes=30),
                )
            except asyncio.TimeoutError:
                workflow.logger.warning(
                    "merge_queue: timed out waiting for PR#%s — moving to next item", pr_number,
                )
            self._merge_queue.pop(0)

    @workflow.signal
    async def acquire_locks(
        self,
        requester_id: str,
        required_dirs: List[str],
        user_code: str = "",
        parent_holder_id: Optional[str] = None,
    ) -> None:
        """
        All-or-nothing lock grant. If ANY dir is taken → add to hold_queue,
        acquire nothing. If ALL free → grant all → signal requester.

        parent_holder_id: reentrant passage for a child workflow whose PARENT
        (orchestrator) already holds some/all of the dirs. Dirs held by that
        parent don't block the request, and ownership is NOT transferred —
        the parent stays the registered holder, so the child's release calls
        are no-ops and the parent's finally-release is the single real release.
        """
        workflow.logger.info(f"[{requester_id}] requesting locks: {required_dirs}")

        blocked = [
            d for d in required_dirs
            if d in self._locks and self._locks[d] != parent_holder_id
        ]

        if not blocked:
            for d in required_dirs:
                # Claim only genuinely free dirs — parent-held dirs KEEP the
                # parent as holder (passage granted, possession unchanged).
                if d not in self._locks:
                    self._locks[d] = requester_id

            workflow.logger.info(f"[{requester_id}] granted: {required_dirs}")
            workflow.logger.info(f"Current locks: {self._locks}")

            await workflow.get_external_workflow_handle(requester_id).signal("locks_granted")
        else:
            workflow.logger.info(f"[{requester_id}] blocked on {blocked} → added to hold queue")
            self._hold_queue.append({
                "workflow_id": requester_id,
                "required_dirs": required_dirs,
                "user_code": user_code,
                # Preserved so a queued reentrant request keeps its parent
                # context when the grant-scan evaluates it later.
                "parent_holder_id": parent_holder_id,
            })
            workflow.logger.info(f"Hold queue: {[h['workflow_id'] for h in self._hold_queue]}")

    @workflow.query
    def get_waiting_users_for_dirs(self, dirs: List[str]) -> List[str]:
        """Return user_codes of all hold_queue entries whose required_dirs overlap with dirs."""
        dirs_set = set(dirs)
        return [
            h["user_code"]
            for h in self._hold_queue
            if h.get("user_code") and any(d in dirs_set for d in h["required_dirs"])
        ]

    @workflow.signal
    async def release_hold(self, requester_id: str) -> None:
        """
        Withdraw a requester's QUEUED (never-granted) lock request.

        Sent by a waiter that gives up (lock-wait timeout) so its stale entry
        can't be granted later to a completed workflow ("corpse grant": dirs
        assigned to a dead workflow that will never release them). Callers send
        this TOGETHER with release_locks — signals process one at a time, so
        whichever state the waiter raced into (still queued, or granted at the
        last instant) one of the two signals cleans it up and the other is a
        no-op. Removing a waiter frees no dirs, so no grant-scan is needed.
        """
        before = len(self._hold_queue)
        self._hold_queue = [h for h in self._hold_queue if h["workflow_id"] != requester_id]
        if len(self._hold_queue) != before:
            workflow.logger.info(f"[{requester_id}] withdrew its queued lock request")

    @workflow.signal
    async def release_locks(self, releaser_id: str, released_dirs: Optional[List[str]] = None) -> None:
        """
        Release locks then scan hold_queue — grant any unblocked waiters.
        Multiple waiters can be unblocked simultaneously if their dirs don't overlap.
        If released_dirs is empty/None, all dirs held by releaser_id are released (admin use).
        """
        if not released_dirs:
            released_dirs = [d for d, wf in list(self._locks.items()) if wf == releaser_id]
        workflow.logger.info(f"[{releaser_id}] releasing: {released_dirs}")

        for d in released_dirs:
            if self._locks.get(d) == releaser_id:
                del self._locks[d]

        workflow.logger.info(f"Locks after release: {self._locks}")

        triggered = []
        for hold in self._hold_queue:
            all_free = all(d not in self._locks for d in hold["required_dirs"])
            if all_free:
                for d in hold["required_dirs"]:
                    self._locks[d] = hold["workflow_id"]

                workflow.logger.info(
                    f"[{hold['workflow_id']}] unblocked → granted: {hold['required_dirs']}"
                )

                await workflow.get_external_workflow_handle(
                    hold["workflow_id"]
                ).signal("locks_granted")

                triggered.append(hold)

        for t in triggered:
            self._hold_queue.remove(t)

        if triggered:
            workflow.logger.info(
                f"Triggered {len(triggered)} waiting workflow(s): "
                f"{[t['workflow_id'] for t in triggered]}"
            )

    @workflow.signal
    async def pr_merge_completed(self, pr_number: int) -> None:
        """
        Sent by the webhook when a PR is merged or closed.
        Unblocks _merge_queue_loop to process the next queued item.
        """
        if self._merge_queue and self._merge_queue[0]["pr_number"] == pr_number:
            self._merge_queue[0]["_released"] = True
            workflow.logger.info("merge_queue: PR#%s released (merged/closed)", pr_number)
        else:
            workflow.logger.debug("merge_queue: pr_merge_completed PR#%s not at front of queue, ignored", pr_number)

    @workflow.signal
    async def enqueue_merge(
        self,
        pr_number: int,
        git_repository: str,
        tenant_code: str,
        user_code: str,
    ) -> None:
        """
        Enqueue a PR for sequential conflict resolution.
        Called by DeploymentWorkflow when it receives a merge_conflict_detected signal
        (Atlantis posted "Merge Failed" on the PR).
        """
        self._merge_queue.append({
            "pr_number": pr_number,
            "git_repository": git_repository,
            "tenant_code": tenant_code,
            "user_code": user_code,
        })
        workflow.logger.info(
            "merge_queue: enqueued PR#%s repo=%s queue_size=%d",
            pr_number, git_repository, len(self._merge_queue),
        )

    @workflow.signal
    async def abort_queued_for_dirs(self, requester_id: str, dirs: List[str], reason: str) -> None:
        """
        Called when an Atlantis lock conflict hits the 60-min timeout.
        1. Signals all hold_queue entries that overlap with `dirs` to abort (they were never granted locks).
        2. Releases the requester's own locks.
        3. Grants locks to any remaining unblocked waiters.
        """
        dirs_set = set(dirs)
        aborted = []
        remaining = []
        for hold in self._hold_queue:
            if any(d in dirs_set for d in hold["required_dirs"]):
                try:
                    await workflow.get_external_workflow_handle(hold["workflow_id"]).signal(
                        "atlas_lock_abort", reason
                    )
                    workflow.logger.info(
                        "abort_queued_for_dirs: signalled %s to abort (reason=%s)",
                        hold["workflow_id"], reason,
                    )
                except Exception as exc:
                    workflow.logger.error(
                        "abort_queued_for_dirs: failed to signal %s: %s",
                        hold["workflow_id"], exc,
                    )
                aborted.append(hold)
            else:
                remaining.append(hold)

        self._hold_queue = remaining

        # Release requester's own locks
        for d in dirs:
            if self._locks.get(d) == requester_id:
                del self._locks[d]

        workflow.logger.info(
            "abort_queued_for_dirs: aborted=%d requester=%s locks_now=%s",
            len(aborted), requester_id, list(self._locks.keys()),
        )

        # Grant to any remaining waiters that are now unblocked
        triggered = []
        for hold in self._hold_queue:
            all_free = all(d not in self._locks for d in hold["required_dirs"])
            if all_free:
                for d in hold["required_dirs"]:
                    self._locks[d] = hold["workflow_id"]
                await workflow.get_external_workflow_handle(hold["workflow_id"]).signal("locks_granted")
                triggered.append(hold)
        for t in triggered:
            self._hold_queue.remove(t)

    @workflow.signal
    def reset_state(self) -> None:
        """Wipe all locks, hold queue, and merge queue — for test cleanup only."""
        self._locks = {}
        self._hold_queue = []
        self._merge_queue = []
        workflow.logger.info("Coordinator state reset")

    @workflow.query
    def get_state(self) -> dict:
        return {
            "active_locks": self._locks,
            "hold_queue": [
                {
                    "workflow_id": h["workflow_id"],
                    "waiting_for": h["required_dirs"],
                }
                for h in self._hold_queue
            ],
            "merge_queue": [
                {
                    "pr_number": m["pr_number"],
                    "git_repository": m["git_repository"],
                }
                for m in self._merge_queue
            ],
        }
