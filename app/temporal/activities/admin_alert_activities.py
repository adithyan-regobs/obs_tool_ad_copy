"""
Admin-only alerts raised from inside the deploy workflows.

report_stale_queue_wait — called by a workflow that has been "waiting in
queue" for a while. Looks at the coordinator's lock table: if every holder
is still RUNNING the wait is genuine and nothing is sent; if a holder is
gone (completed / terminated / not found) the lock will never be released on
its own, so admins are told in the report channel (deploy-tracker bot), and
reminded hourly while the run stays blocked. The deployer and the P0 channel
are deliberately NOT notified — this is an ops problem, not a deploy failure.
"""

import logging

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn
async def report_stale_queue_wait(
    tenant_code: str,
    waiting_workflow_id: str,
    project_dirs: list,
    user_code: str | None,
    waited_min: int,
    reminder: bool = False,
) -> bool:
    """Returns True when a stale lock was found AND the admin alert was sent
    (the workflow uses that to space out the hourly reminders). False =
    genuine wait, or the report bot is not configured.

    `reminder` is True on every alert after the first for the same run, so
    the message reads as "still blocked" rather than a fresh incident."""
    from app.services import temporal_dashboard_service as svc

    dirs, holders = await svc.check_stale_locks(tenant_code, waiting_workflow_id, list(project_dirs) or None)
    stale = [h for h in holders if h.stale]
    if not stale:
        logger.info("[%s] queued %d min behind running deploy(s) — genuine wait", waiting_workflow_id, waited_min)
        return False
    text, blocks = svc.stale_lock_alert(tenant_code, waiting_workflow_id, user_code, waited_min, dirs, holders, reminder=reminder)
    sent = await svc.post_admin_alert(text, blocks)
    logger.warning("[%s] stale lock(s) %s — admin %s %s", waiting_workflow_id, [h.dir for h in stale], "reminder" if reminder else "alert", "sent" if sent else "NOT sent (bot unconfigured)")
    return sent
