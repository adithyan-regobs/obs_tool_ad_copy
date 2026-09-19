"""get_deployment_status tool — surfaces deployment progress for the user.

Two backends behind one tool:
  • PaaS tenants → poll the Jenkins pipeline_run_track row via the
    `pipeline_run_track_code` cached on each in-progress draft.
  • Enterprise tenants → the trigger hands the queue item to the Temporal
    DeploymentWorkflow (PR → atlantis plan → apply → merge). Poll the
    pipeline_run_track rows the workflow writes under its workflow_id
    (cached on the draft) and join the PR from gitops_workflow_detail.
    Drafts from before the Temporal hand-off (no workflow_id) fall back to
    a PR-only lookup so an old conversation can still recover its link.

PaaS flow:
  1. Scan Redis for mcp:draft:{user_code}:* keys. Filter entries where
     deploy_in_progress=True and pipeline_run_track_code is set.
  2. For each, fetch the pipeline_run_track row by code.
  3. On terminal status, clear the deploy flags from the draft.
  4. Return per-resource statuses + `all_completed`.

Enterprise (Temporal) flow:
  1. Scan Redis drafts; keep entries with deploy_in_progress=True and a
     workflow_id, scoped to project_id when given.
  2. For each, read pipeline_run_track by vendor_deployment_id=workflow_id
     (status + build_stages) and the latest gitops_workflow_detail row for
     the draft's transaction_code (pr_url / pr_status).
  3. On terminal status, clear deploy_in_progress on the draft.
  4. Return per-resource `stage`, `status`, `pr_url` + `all_completed`.

Enterprise (legacy PR-only) flow:
  1. Collect transaction_codes from the user's drafts.
  2. Query gitops_workflow_detail for matching rows in this tenant.
  3. Return pr_number + pr_url + pr_status.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.enum import WorkflowSourceTableEnum
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.session import AsyncSessionLocal
from app.mcp_servers.devlift_mcp.auth import get_auth_context
from app.mcp_servers.devlift_mcp.meta_data import is_paas_tenant
from app.mcp_servers.devlift_mcp.session_cache import scan_user_drafts, update_draft_by_id
from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}

# How far back to look when project_id is omitted and we have no drafts to
# scope by. Bounds runaway results from a long-lived user account.
_ENTERPRISE_LOOKBACK_MINUTES = 30


async def _get_enterprise_status(
    *,
    user_code: str,
    tenant_code: str,
    project_id: str | None,
) -> dict:
    """Enterprise entry point: live Temporal progress when a workflow is
    running for one of the user's drafts, otherwise the PR-only lookup."""
    drafts = await scan_user_drafts(user_code)
    if project_id is not None:
        drafts = [d for d in drafts if d.get("project_id") == project_id]

    temporal_drafts = [
        d for d in drafts
        if d.get("deploy_in_progress") and d.get("workflow_id")
    ]
    if temporal_drafts:
        return await _get_temporal_status(
            user_code=user_code,
            tenant_code=tenant_code,
            drafts=temporal_drafts,
        )
    return await _get_enterprise_pr_status(
        user_code=user_code,
        tenant_code=tenant_code,
        drafts=drafts,
    )


def _stage_label(stages: list) -> tuple[str | None, str | None]:
    """(name, status) of the most recent build stage, or (None, None)."""
    if not stages:
        return None, None
    last = stages[-1] or {}
    return last.get("name"), last.get("status")


def _failed_stage_error(stages: list) -> str | None:
    for s in reversed(stages or []):
        if (s or {}).get("status") == "failed" and s.get("error"):
            return s["error"]
    return None


async def _latest_pr_for_transaction(db, *, tenant_code: str, transaction_code: str):
    stmt = (
        select(GitopsWorkflowDetailModel)
        .where(
            GitopsWorkflowDetailModel.tenant_mst_code == tenant_code,
            GitopsWorkflowDetailModel.transaction_code == transaction_code,
            GitopsWorkflowDetailModel.is_deleted == False,
        )
        .order_by(GitopsWorkflowDetailModel.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _get_temporal_status(
    *,
    user_code: str,
    tenant_code: str,
    drafts: list[dict],
) -> dict:
    """Per-resource progress of the Temporal DeploymentWorkflow behind each
    in-progress enterprise draft."""
    results = []

    async with AsyncSessionLocal() as db:
        run_track_repo = PipelineRunTrackRepository(db)

        for draft in drafts:
            workflow_id = draft["workflow_id"]
            resource_type = draft.get("resource_type", "unknown")
            identifier = draft.get("identifier", "unknown")
            draft_key = draft.get("_case_ref_code")

            try:
                rows = await run_track_repo.get_by_vendor_deployment_id(workflow_id)
            except Exception:
                logger.exception("Failed to fetch run_track rows workflow_id=%s", workflow_id)
                rows = []
            # deploy_temporal writes one row per queue item, each tagged with
            # its queue_code; a single-item MCP deploy has exactly one.
            run_track = next(
                (
                    r for r in rows
                    if draft.get("queue_code") in (r.transaction_queue_code or [])
                ),
                rows[0] if rows else None,
            )

            pr = None
            if draft.get("transaction_code"):
                try:
                    pr = await _latest_pr_for_transaction(
                        db, tenant_code=tenant_code, transaction_code=draft["transaction_code"]
                    )
                except Exception:
                    logger.exception("Failed to fetch PR for transaction=%s", draft["transaction_code"])

            if run_track is None:
                status_value = "PENDING"
                stages: list = []
                error_message = None
            else:
                status_value = (
                    run_track.status.value if hasattr(run_track.status, "value")
                    else str(run_track.status)
                )
                stages = list(run_track.build_stages or [])
                error_message = _failed_stage_error(stages) or run_track.error_message

            stage_name, stage_status = _stage_label(stages)
            is_terminal = status_value.upper() in _TERMINAL_STATUSES

            if is_terminal and draft_key:
                try:
                    updated = {**draft}
                    updated.pop("_case_ref_code", None)
                    updated.pop("deploy_in_progress", None)
                    updated["deploy_final_status"] = status_value
                    if pr is not None and pr.pr_url:
                        updated["pr_url"] = pr.pr_url
                    await update_draft_by_id(user_code, draft_key, draft["draft_id"], updated)
                except Exception:
                    logger.warning("Failed to clear deploy fields for draft_key=%s", draft_key)

            if status_value.upper() == "COMPLETED":
                message = f"'{identifier}' is deployed."
            elif status_value.upper() in _TERMINAL_STATUSES:
                message = (
                    f"Deployment of '{identifier}' failed"
                    + (f": {error_message}" if error_message else ".")
                )
                if resource_type == "eks_service":
                    # The lane puts a failed batch back to approved with the
                    # seal intact, so the same deploy is the retry.
                    message += " The request is back to approved — say 'deploy it' to retry."
            elif stage_name:
                message = f"'{identifier}': {stage_name} ({stage_status})."
            else:
                message = f"'{identifier}' is queued — waiting for the deployment to start."

            pr_status_value = None
            if pr is not None and pr.pr_status is not None:
                pr_status_value = (
                    pr.pr_status.value if hasattr(pr.pr_status, "value") else str(pr.pr_status)
                )

            results.append({
                "resource_type": resource_type,
                "identifier": identifier,
                "status": status_value,
                "stage": stage_name,
                "stage_status": stage_status,
                "message": message,
                "stages": [
                    {
                        "name": s.get("name"),
                        "status": s.get("status"),
                        "started_at": s.get("started_at"),
                    }
                    for s in stages
                ],
                "pr_url": pr.pr_url if pr is not None else None,
                "pr_number": pr.pr_number if pr is not None else None,
                "pr_status": pr_status_value,
                "error_message": error_message,
            })

    all_completed = all(r["status"].upper() in _TERMINAL_STATUSES for r in results)

    if all_completed:
        # A COMPLETED pipeline means DevLift is done, NOT that the app is
        # serving: ArgoCD has usually not even looked at the change yet. For
        # EKS services the answer the user actually wants comes from the
        # second loop.
        eks_completed = any(
            r.get("resource_type") == "eks_service"
            and r["status"].upper() == "COMPLETED"
            for r in results
        )
        hand_off = (
            " These are EKS services, so DevLift being finished does not mean "
            "they are serving yet. Say the deployment finished and that you "
            "are checking whether the application is up, then start "
            "`/loop 20s get_application_status(project_id=<same project_id>)` "
            "in the SAME response."
            if eks_completed
            else ""
        )
        next_action = {
            "type": "present_completion",
            "instruction": (
                "Stop polling. For each deployment surface `message` verbatim. "
                "If `pr_url` is set, present it once as a clickable markdown "
                "link labelled with `identifier`. On FAILED, surface "
                "`error_message` and the PR link so the user can look."
                + hand_off
            ),
        }
    else:
        next_action = {
            "type": "continue_polling",
            "instruction": (
                "Deployment still running. Show `message` per resource (one "
                "line each). Present `pr_url` as a clickable link the FIRST "
                "time it appears, not on every tick. Let `/loop` call "
                "get_deployment_status again."
            ),
        }

    return {
        "status": "ok",
        "all_completed": all_completed,
        "next_action": next_action,
        "deployments": results,
    }


async def _get_enterprise_pr_status(
    *,
    user_code: str,
    tenant_code: str,
    drafts: list[dict],
) -> dict:
    """Resolve recent PR-based deployments for an enterprise user (drafts
    without a Temporal workflow, or after the workflow has finished)."""

    # Step 1 — find transaction_codes this user has touched via MCP.
    transaction_codes = [
        d["transaction_code"]
        for d in drafts
        if d.get("transaction_code")
    ]

    async with AsyncSessionLocal() as db:
        stmt = (
            select(GitopsWorkflowDetailModel)
            .where(
                GitopsWorkflowDetailModel.tenant_mst_code == tenant_code,
                GitopsWorkflowDetailModel.user_mst_code == user_code,
                GitopsWorkflowDetailModel.is_deleted == False,
            )
            .order_by(GitopsWorkflowDetailModel.created_at.desc())
            .limit(20)
        )

        if transaction_codes:
            # Scoped lookup — only PRs for the drafts the user has in this project.
            stmt = stmt.where(
                GitopsWorkflowDetailModel.transaction_code.in_(transaction_codes)
            )
        else:
            # No drafts to scope by — bound the query to recent activity so
            # we don't dump an entire workflow history at the LLM.
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=_ENTERPRISE_LOOKBACK_MINUTES
            )
            stmt = stmt.where(GitopsWorkflowDetailModel.created_at >= cutoff)

        result = await db.execute(stmt)
        workflows = list(result.scalars().all())

        # Resolve human-readable names for INFRASTRUCTURE rows in one query —
        # the LLM should never see internal codes like INFRA_S3_21545274 when
        # the user typed a bucket name like core-stage-mumbai-bucket.
        infra_codes = [
            wf.transaction_code for wf in workflows
            if wf.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE
            and wf.transaction_code
        ]
        infra_name_map: dict[str, str] = {}
        if infra_codes:
            name_stmt = (
                select(InfrastructureMstModel.code, InfrastructureMstModel.name)
                .where(InfrastructureMstModel.code.in_(infra_codes))
            )
            name_result = await db.execute(name_stmt)
            infra_name_map = {row[0]: row[1] for row in name_result.all() if row[1]}

    if not workflows:
        return {
            "status": "no_deployments",
            "message": (
                "No recent PRs found for this conversation yet. If you just "
                "triggered a deployment and the request timed out, retry in "
                "a few seconds — the PR is still being created."
            ),
            "deployments": [],
        }

    deployments = []
    for wf in workflows:
        pr_status_value = (
            wf.pr_status.value if hasattr(wf.pr_status, "value")
            else (str(wf.pr_status) if wf.pr_status else None)
        )
        table_name_value = (
            wf.table_name.value if hasattr(wf.table_name, "value")
            else (str(wf.table_name) if wf.table_name else None)
        )

        status = "completed" if wf.pr_url else "in_progress"
        identifier = infra_name_map.get(wf.transaction_code) or wf.transaction_code

        deployments.append({
            "resource_type": table_name_value,
            "identifier": identifier,
            "status": status,
            "pr_number": wf.pr_number,
            "pr_url": wf.pr_url,
            "pr_status": pr_status_value,
            "git_repository": wf.git_repository,
            "created_at": wf.created_at.isoformat() if wf.created_at else None,
        })

    all_completed = all(d["status"] == "completed" for d in deployments)

    if all_completed:
        next_action = {
            "type": "present_pr_links",
            "instruction": (
                "For each deployment, present `pr_url` to the user as a "
                "clickable markdown link labelled with the `identifier` "
                "(e.g. `[core-stage-mumbai-bucket](pr_url)`). Do not "
                "paraphrase the URL. Do not list `git_repository` or "
                "internal codes."
            ),
        }
    else:
        next_action = {
            "type": "wait_and_retry",
            "instruction": (
                "At least one PR is still being created. Tell the user the "
                "deployment is in progress, wait ~10 seconds, then call "
                "get_deployment_status once more with the same project_id."
            ),
        }

    return {
        "status": "ok",
        "all_completed": all_completed,
        "next_action": next_action,
        "deployments": deployments,
    }


async def get_deployment_status_impl(project_id: str | None = None) -> dict:
    """Return deployment progress for the user.

    When `project_id` is provided, scopes to that project (PaaS: drafts;
    enterprise: PRs derived from those drafts). When omitted, returns all
    recent in-progress drafts (PaaS) or recent PR activity (enterprise).
    """

    auth_ctx = await get_auth_context()
    if not auth_ctx:
        return {
            "status": "error",
            "message": "Authentication required — please run the 'authenticate' tool first.",
        }

    if not is_paas_tenant(auth_ctx.tenant_code):
        return await _get_enterprise_status(
            user_code=auth_ctx.user_code,
            tenant_code=auth_ctx.tenant_code,
            project_id=project_id,
        )

    user_code = auth_ctx.user_code

    drafts = await scan_user_drafts(user_code)
    in_progress = [
        d for d in drafts
        if d.get("deploy_in_progress")
        and d.get("pipeline_run_track_code")
        and (project_id is None or d.get("project_id") == project_id)
    ]

    if not in_progress:
        return {
            "status": "no_deployments",
            "message": "No deployments currently in progress.",
            "deployments": [],
        }

    results = []

    async with AsyncSessionLocal() as db:
        run_track_repo = PipelineRunTrackRepository(db)

        for draft in in_progress:
            run_track_code = draft["pipeline_run_track_code"]
            resource_type = draft.get("resource_type", "unknown")
            identifier = draft.get("identifier", "unknown")
            draft_key = draft.get("_case_ref_code")

            try:
                run_track = await run_track_repo.get_by_code(run_track_code)
            except Exception:
                logger.exception("Failed to fetch run_track code=%s", run_track_code)
                run_track = None

            if run_track is None:
                results.append({
                    "resource_type": resource_type,
                    "identifier": identifier,
                    "status": "PENDING",
                    "message": "Build is queued — Jenkins has not started yet.",
                    "stages": [],
                    "log_url": None,
                })
                continue

            status_value = run_track.status.value if hasattr(run_track.status, "value") else str(run_track.status)
            deploy_result = run_track.deploy_result or {}
            build_stages = run_track.build_stages or []

            # Clear deploy_in_progress and pipeline_run_track_code from Redis once terminal
            if status_value.upper() in _TERMINAL_STATUSES and draft_key:
                try:
                    updated = {**draft}
                    updated.pop("_case_ref_code", None)
                    updated.pop("deploy_in_progress", None)
                    updated.pop("pipeline_run_track_code", None)
                    await update_draft_by_id(user_code, draft_key, draft["draft_id"], updated)
                except Exception:
                    logger.warning("Failed to clear deploy fields for draft_key=%s", draft_key)

            alb_url = deploy_result.get("alb_url")

            results.append({
                "resource_type": resource_type,
                "identifier": identifier,
                "status": status_value,
                "log_url": run_track.log_url,
                "stages": [
                    {
                        "name": s.get("name"),
                        "status": s.get("status"),
                        "started_at": s.get("started_at"),
                    }
                    for s in build_stages
                ],
                "build_url": deploy_result.get("build_url"),
                **(
                    {
                        "alb_url": alb_url,
                        "alb_url_note": "Application URL — present as a clickable link to the user",
                    }
                    if alb_url
                    else {}
                ),
                "build_result": deploy_result.get("build_result"),
                "completed_at": deploy_result.get("completed_at"),
                "error_message": deploy_result.get("error_message") or run_track.error_message,
            })

    all_completed = all(
        r["status"].upper() in _TERMINAL_STATUSES for r in results
    )

    return {
        "status": "ok",
        "all_completed": all_completed,
        "deployments": results,
    }
