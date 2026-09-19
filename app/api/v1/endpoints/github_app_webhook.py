"""
GitHub App Webhook Endpoint

Receives webhook events from GitHub when:
- A tenant installs the GitHub App on their org
- A tenant uninstalls the GitHub App
- A push event occurs (triggers Jenkins build for services using Jenkins CI)
"""

import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.services.github_app_installation_service import GitHubAppInstallationService
from app.utils.atlantis_helpers import extract_atlantis_error

router = APIRouter()
logger = logging.getLogger(__name__)


async def _handle_push_event(payload: dict, db: AsyncSession) -> dict:
    """
    Handle GitHub push events.

    Enterprise (Temporal) tenants: signals the running DeploymentWorkflow with
    the new commit SHA so it can detect manual commits immediately.

    Standard (Jenkins) tenants: looks up pipeline_mst records and triggers
    Jenkins builds.
    """
    repo_full_name = payload.get("repository", {}).get("full_name", "")
    ref = payload.get("ref", "")
    branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ""
    commit_sha = payload.get("after", "")
    pusher_name = payload.get("pusher", {}).get("name", "")

    if not repo_full_name or not branch:
        return {"status": "ignored", "reason": "missing repo or branch"}

    # Skip tag pushes and deleted branches
    if not ref.startswith("refs/heads/") or payload.get("deleted", False):
        return {"status": "ignored", "reason": "not a branch push"}

    from app.core.config import settings

    # ── Enterprise path (Temporal) ───────────────────────────────────────────
    if settings.temporal_enabled:
        # Skip branch creation events — GitHub fires a push event when a new branch is created,
        # with before=000...0 and after=base-branch-HEAD. This SHA is not the devlift commit
        # and would cause a false "manual commit detected" mismatch in the workflow.
        before_sha = payload.get("before", "")
        if before_sha.strip("0") == "":
            logger.info("push_event: skipping push_detected signal — branch creation event (before=%s)", before_sha[:8])
            return {"status": "ok", "handled_by": "temporal", "skipped": "branch_creation"}
        # Skip push signal if the pusher is the GitHub App bot (e.g. resolve_conflict_and_merge force-push)
        if pusher_name in settings.devlift_git_bots_list:
            logger.info("push_event: skipping push_detected signal — pusher is devlift bot (%s)", pusher_name)
            return {"status": "ok", "handled_by": "temporal", "skipped": "bot_push"}
        await _signal_temporal_push_for_branch(branch, repo_full_name, commit_sha, db)
        return {"status": "ok", "handled_by": "temporal"}

    # ── Standard path (Jenkins) ──────────────────────────────────────────────
    # Find Jenkins pipeline_mst records for this repo + branch
    from app.db.models.pipeline_mst_model import PipelineMstModel
    from app.db.models.pipeline_vendor_mst_model import PipelineVendorMstModel
    from sqlalchemy.orm import joinedload

    stmt = (
        select(PipelineMstModel)
        .join(
            PipelineVendorMstModel,
            PipelineMstModel.pipeline_vendor_mst_code == PipelineVendorMstModel.code,
        )
        .options(joinedload(PipelineMstModel.pipeline_vendor))
        .where(
            and_(
                # Match both "owner/repo" and "https://github.com/owner/repo" formats
                PipelineMstModel.repo_url.in_([
                    repo_full_name,
                    f"https://github.com/{repo_full_name}",
                    f"https://github.com/{repo_full_name}.git",
                ]),
                PipelineMstModel.repo_branch == branch,
                PipelineVendorMstModel.pipeline_agent_enum == "jenkins",
                PipelineMstModel.is_deleted == False,
            )
        )
    )
    result = await db.execute(stmt)
    pipelines = result.unique().scalars().all()

    if not pipelines:
        logger.debug(
            "No Jenkins pipelines found for %s branch %s", repo_full_name, branch
        )
        return {"status": "ignored", "reason": "no matching Jenkins pipelines"}

    # Deduplicate by jenkins_job_name — only trigger each job once per push
    seen_jobs: dict[str, object] = {}
    for pipeline in pipelines:
        deployment_config = pipeline.deployment_config or {}
        job_name = deployment_config.get("jenkins_job_name")
        if not job_name:
            logger.warning("Pipeline %s has no jenkins_job_name in deployment_config", pipeline.code)
            continue
        if job_name not in seen_jobs:
            seen_jobs[job_name] = pipeline

    logger.info(
        "Found %d unique Jenkins job(s) for %s branch %s",
        len(seen_jobs), repo_full_name, branch,
    )

    from app.services.jenkins_provisioning_service import JenkinsProvisioningService
    from app.integrations.jenkins_integration import JenkinsIntegration
    from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository

    triggered = []
    for jenkins_job_name, pipeline in seen_jobs.items():
        try:
            # Generate separate tokens for infra repo and source repo
            jenkins_service = JenkinsProvisioningService(db)
            build_params: dict[str, str] = {}

            # Source repo token (for checkout)
            source_repo = pipeline.repo_url or repo_full_name
            if source_repo:
                build_params["SOURCE_GITHUB_TOKEN"] = await jenkins_service._generate_github_token(source_repo)

            # Infra repo token (for manifest fetch)
            from app.utils.tenant_config import get_tenant_config
            tenant_cfg = await get_tenant_config(pipeline.tenant_code, db)
            infra_repo = tenant_cfg.github_infra_repository
            if infra_repo:
                build_params["GITHUB_TOKEN"] = await jenkins_service._generate_github_token(
                    f"https://github.com/{infra_repo}"
                )

            # Trigger build directly via Jenkins API (avoids pipeline_mst re-lookup
            # which can fail with MultipleResultsFound on duplicate records)
            client = JenkinsIntegration(
                settings.jenkins_url, settings.jenkins_user, settings.jenkins_api_token,
            )
            build_result = await client.trigger_build(
                jenkins_job_name,
                parameters=build_params,
            )

            # Track the run
            if build_result.get("status") == "success":
                import uuid as _uuid
                deployment_config = pipeline.deployment_config or {}
                queue_codes = deployment_config.get("queue_codes", [])
                run_track_repo = PipelineRunTrackRepository(db)
                await run_track_repo.create(
                    code=f"run-{_uuid.uuid4().hex[:12]}",
                    pipeline_mst_code=pipeline.code,
                    status="RUNNING",
                    commit_sha=commit_sha,
                    log_url=f"{settings.jenkins_url}/job/{jenkins_job_name}",
                    transaction_queue_code=queue_codes,
                )

            triggered.append({
                "pipeline_code": pipeline.code,
                "job_name": jenkins_job_name,
                "result": build_result.get("status"),
            })
            logger.info(
                "Triggered Jenkins build for %s (commit: %s)",
                jenkins_job_name, commit_sha[:8],
            )
        except Exception as exc:
            logger.error(
                "Failed to trigger Jenkins build for pipeline %s: %s",
                pipeline.code, exc, exc_info=True,
            )
            triggered.append({
                "pipeline_code": pipeline.code,
                "job_name": jenkins_job_name,
                "result": "error",
                "error": str(exc),
            })

    await db.commit()
    return {"status": "ok", "builds_triggered": len(triggered), "details": triggered}


async def _signal_temporal_push_for_branch(
    branch: str, repo_full_name: str, commit_sha: str, db: AsyncSession
) -> None:
    """
    Finds the DevLift PR for this pushed branch (using tenant's infra_branch as base)
    and signals the running DeploymentWorkflow with the new SHA.
    Fire-and-forget — errors are logged but never raised.
    """
    from app.core.config import settings
    if not settings.temporal_enabled or not commit_sha:
        return
    try:
        parts = repo_full_name.split("/")
        if len(parts) != 2:
            return
        owner, repo = parts

        # Resolve tenant from infra repo → get base_branch
        from app.db.models.tenants_mst_model import TenantsMstModel
        tenant_row = (await db.execute(
            select(TenantsMstModel).where(
                TenantsMstModel.config["github"]["infra_repository"].astext == repo_full_name,
                TenantsMstModel.is_deleted == False,
            ).limit(1)
        )).scalars().first()
        if not tenant_row:
            return
        base_branch: str = (tenant_row.config or {}).get("github", {}).get("infra_branch", "")
        if not base_branch:
            return

        # Use head + base to pinpoint the exact DevLift PR
        from app.handlers.gitops_handler import GitOpsHandler
        pr_number = await GitOpsHandler.find_open_pr_by_branch(
            tenant=tenant_row.code,
            owner=owner,
            repo=repo,
            feature_branch=branch,
            base_branch=base_branch,
            db=db,
        )
        if not pr_number:
            return

        from app.temporal.client import get_temporal_client
        client = await get_temporal_client()
        async for wf in client.list_workflows(
            f'DeployPrNumber={pr_number} AND ExecutionStatus="Running"'
        ):
            handle = client.get_workflow_handle(wf.id)
            await handle.signal("push_detected", commit_sha)
            logger.info(
                "push_detected signal sent: branch=%s pr=%s wf=%s sha=%s",
                branch, pr_number, wf.id, commit_sha[:8],
            )
            break
    except Exception as exc:
        logger.error("push_detected signal failed branch=%s: %s", branch, exc)


import re as _project_re

# Atlantis result comments name their project: "Ran Plan for project: `x` dir: ..."
_ATLANTIS_PROJECT_PATTERN = _project_re.compile(
    r'project:\s*`?([A-Za-z0-9._/-]+)`?', _project_re.IGNORECASE
)


def _extract_atlantis_project(body: str) -> str | None:
    """Project name from an Atlantis comment body, or None (some bare error
    comments carry no project)."""
    m = _ATLANTIS_PROJECT_PATTERN.search(body or "")
    return m.group(1) if m else None


async def _signal_temporal_by_pr(
    pr_number: int,
    signal_name: str,
    signal_args: list | None = None,
    project_name: str | None = None,
    broadcast: bool = False,
) -> dict:
    """
    Route a signal to the workflow(s) working this PR.

    Comment events (plan/apply results):
      1. project parsed from the comment → query
         (DeployPrNumber, DeployProjectName) → the exact
         ProductionDeploymentWorkflow (several projects share the one prod
         promotion PR; serialization guarantees at most one active match).
      2. Miss, or no project in the comment → fallback: pr-number +
         WorkflowType="DeploymentWorkflow" → stage resolves byte-identically
         to the old behavior, and a prod workflow can never receive another
         project's comment.

    PR-level events (merged/closed/review) have no project → broadcast=True
    delivers to EVERY Running workflow on the PR (both types); each decides
    what the event means for its own state.

    Returns a status dict — never raises.
    """
    from app.core.config import settings
    from app.temporal.client import get_temporal_client

    if not settings.temporal_enabled:
        return {"status": "skipped", "reason": "temporal_disabled"}

    async def _deliver(handle) -> None:
        if signal_args is not None:
            await handle.signal(signal_name, *signal_args)
        else:
            await handle.signal(signal_name)

    try:
        client = await get_temporal_client()
        delivered: list[str] = []

        if broadcast:
            async for wf in client.list_workflows(
                f'DeployPrNumber={pr_number} AND ExecutionStatus="Running"'
            ):
                await _deliver(client.get_workflow_handle(wf.id))
                delivered.append(wf.id)
        else:
            if project_name:
                async for wf in client.list_workflows(
                    f'DeployPrNumber={pr_number} AND DeployProjectName="{project_name}" '
                    f'AND ExecutionStatus="Running"'
                ):
                    await _deliver(client.get_workflow_handle(wf.id))
                    delivered.append(wf.id)
                    break  # serialization: at most one active per (pr, project)
            if not delivered:
                async for wf in client.list_workflows(
                    f'DeployPrNumber={pr_number} AND WorkflowType="DeploymentWorkflow" '
                    f'AND ExecutionStatus="Running"'
                ):
                    await _deliver(client.get_workflow_handle(wf.id))
                    delivered.append(wf.id)
                    break  # one active stage deployment per PR

        if not delivered:
            logger.debug(
                "No running workflow for pr=%s project=%s (signal=%s ignored)",
                pr_number, project_name, signal_name,
            )
            return {"status": "ignored", "reason": "no_running_workflow"}

        logger.info(
            "Temporal signal sent: pr=%s project=%s signal=%s -> %s",
            pr_number, project_name, signal_name, delivered,
        )
        return {"status": "signalled", "signal": signal_name, "pr_number": pr_number,
                "delivered": delivered}

    except Exception as exc:
        logger.error(
            "Failed to signal Temporal workflow for pr_number=%s signal=%s: %s",
            pr_number, signal_name, exc,
        )
        return {"status": "error", "error": str(exc)}


async def _handle_atlantis_comment(payload: dict) -> dict:
    """
    Handle issue_comment events posted by Atlantis on a PR.

    Detects plan and apply results from the comment body and signals the
    matching DeploymentWorkflow via the DeployPrNumber search attribute.

    Atlantis comment patterns:
      Plan success : body contains "Ran Atlantis Plan" — no error/failed lines
      Plan failure : body contains "Ran Atlantis Plan" — and error/failed
      Apply success: body contains "Ran Atlantis Apply" — no error/failed lines
      Apply failure: body contains "Ran Atlantis Apply" — and error/failed
    """
    # Only care about PR issue comments (issue comments on PRs, not plain issues)
    issue = payload.get("issue", {})
    if "pull_request" not in issue:
        return {"status": "ignored", "reason": "not_a_pr_comment"}

    action = payload.get("action", "")
    if action != "created":
        return {"status": "ignored", "reason": f"action={action}"}

    pr_number = issue.get("number")
    if not pr_number:
        return {"status": "ignored", "reason": "missing_pr_number"}

    body_raw = payload.get("comment", {}).get("body") or ""
    body = body_raw.lower()

    logger.info(
        "Atlantis comment received on PR #%s — preview: %s",
        pr_number, repr(body_raw[:120]),
    )
    # Prod promotion PRs carry several projects' results — route precisely.
    project = _extract_atlantis_project(body_raw)

    # ── Atlantis plan result ───────────────────────────────────────────────
    # Atlantis posts "Ran Plan for dir:" or "Ran Plan for project:"
    # Failure signals:
    #   "Plan Error"   — terragrunt/terraform init or plan exited non-zero
    #                    (also posted standalone when project name not found)
    #   "Plan Failed:" — Atlantis project locked by another PR
    #   "exit code: 1" — explicit exit code in output
    # Do NOT use "error" or "failed" broadly — terragrunt warning logs contain
    # "level=error" even on successful plans, causing false positives.
    if ("plan error" in body or "plan failed" in body) and "ran plan for" not in body and "ran atlantis plan" not in body:
        logger.info("Atlantis standalone plan error on PR #%s → signal=plan_failed", pr_number)
        return await _signal_temporal_by_pr(
            pr_number, "plan_failed", signal_args=[extract_atlantis_error(body_raw, "plan")],
            project_name=project,
        )
    if "ran plan for" in body or "ran atlantis plan" in body:
        is_lock = ("locked by" in body or "lock on this plan" in body) and "for this pull request" not in body
        is_failure = "plan error" in body or "plan failed" in body
        if is_lock:
            import re as _re
            m = _re.search(r'from pull\s+#?(\d+)', body_raw, _re.IGNORECASE)
            locking_pr = int(m.group(1)) if m else None
            logger.info("Atlantis plan comment detected on PR #%s → signal=lock_conflict_detected locking_pr=%s", pr_number, locking_pr)
            return await _signal_temporal_by_pr(pr_number, "lock_conflict_detected", signal_args=[locking_pr], project_name=project)
        elif is_failure:
            logger.info("Atlantis plan comment detected on PR #%s → signal=plan_failed", pr_number)
            return await _signal_temporal_by_pr(
                pr_number, "plan_failed", signal_args=[extract_atlantis_error(body_raw, "plan")],
                project_name=project,
            )
        logger.info("Atlantis plan comment detected on PR #%s → signal=plan_completed", pr_number)
        return await _signal_temporal_by_pr(pr_number, "plan_completed", project_name=project)

    # ── Atlantis apply result ──────────────────────────────────────────────
    # Atlantis posts "Ran Apply for dir:" or "Ran Apply for project:"
    # Failure signals: "Apply Error", "Apply Failed:"
    # Also catches standalone "Apply Error" (e.g. project name not found)
    if ("apply error" in body or "apply failed" in body) and "ran apply for" not in body and "ran atlantis apply" not in body:
        logger.info("Atlantis standalone apply error on PR #%s → signal=apply_failed", pr_number)
        return await _signal_temporal_by_pr(
            pr_number, "apply_failed", signal_args=[extract_atlantis_error(body_raw, "apply")],
            project_name=project,
        )
    if "ran apply for" in body or "ran atlantis apply" in body:
        is_self_lock = ("apply error" in body or "apply failed" in body) and "for this pull request" in body
        if is_self_lock:
            logger.info("Atlantis apply self-lock (same PR) on PR #%s — no signal", pr_number)
            return {"status": "ok", "skipped": "apply_self_lock"}
        is_failure = "apply error" in body or "apply failed" in body
        logger.info("Atlantis apply comment detected on PR #%s → signal=%s", pr_number, "apply_failed" if is_failure else "apply_completed")
        if is_failure:
            return await _signal_temporal_by_pr(
                pr_number, "apply_failed", signal_args=[extract_atlantis_error(body_raw, "apply")],
                project_name=project,
            )
        return await _signal_temporal_by_pr(pr_number, "apply_completed", project_name=project)

    # ── Atlantis automerge failure ────────────────────────────────────────
    # Atlantis posts "Automerging failed:\n```\n...\n```" when automerge
    # fails due to merge conflicts (GitHub 405 "Pull Request is not mergeable").
    if "automerging failed" in body:
        repo_full_name = payload.get("repository", {}).get("full_name", "")
        logger.info("Atlantis automerge failure detected on PR #%s repo=%s", pr_number, repo_full_name)
        from app.core.config import settings
        from app.temporal.client import get_temporal_client
        if not settings.temporal_enabled:
            return {"status": "skipped", "reason": "temporal_disabled"}
        try:
            client = await get_temporal_client()
            async for wf in client.list_workflows(
                f'DeployPrNumber={pr_number} AND ExecutionStatus="Running"'
            ):
                handle = client.get_workflow_handle(wf.id)
                await handle.signal("merge_conflict_detected", repo_full_name)
                logger.info("Signalled merge_conflict_detected: pr=%s wf=%s", pr_number, wf.id)
                break
        except Exception as exc:
            logger.error("Failed to signal merge_conflict_detected pr=%s: %s", pr_number, exc)
        return {"status": "signalled", "signal": "merge_conflict_detected", "pr_number": pr_number}

    return {"status": "ignored", "reason": "not_an_atlantis_comment"}


async def _handle_pr_review(payload: dict) -> dict:
    """
    Handle pull_request_review events.

    When a reviewer approves a PR, signal the matching DeploymentWorkflow so
    the approval waiting loop can unblock without waiting for the next retry.
    """
    action = payload.get("action", "")
    review_state = payload.get("review", {}).get("state", "")
    pr_number = payload.get("pull_request", {}).get("number")
    logger.info(
        "PR review event received on PR #%s — action=%s state=%s",
        pr_number, action, review_state,
    )

    if action != "submitted":
        return {"status": "ignored", "reason": f"action={action}"}

    if review_state != "approved":
        return {"status": "ignored", "reason": f"review_state={review_state}"}

    if not pr_number:
        return {"status": "ignored", "reason": "missing_pr_number"}

    logger.info("PR #%s approved by reviewer — signalling pr_approved (broadcast)", pr_number)
    return await _signal_temporal_by_pr(pr_number, "pr_approved", broadcast=True)


async def _handle_pr_merged(payload: dict) -> dict:
    """
    Handle pull_request events.

    On any close (merged or not), signal the coordinator to release the PR from the
    merge queue so the next conflict resolution can proceed.
    When merged=true, also signal the DeploymentWorkflow so it can mark ACTIVE.
    """
    action = payload.get("action", "")
    if action != "closed":
        return {"status": "ignored", "reason": f"action={action}"}

    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    if not pr_number:
        return {"status": "ignored", "reason": "missing_pr_number"}

    # Release from coordinator merge queue on any close (merged or not)
    from app.core.config import settings
    from app.temporal.client import get_temporal_client
    if settings.temporal_enabled:
        try:
            client = await get_temporal_client()
            for tenant_code in settings.temporal_deploy_tenants_list:
                try:
                    handle = client.get_workflow_handle(f"coordinator-{tenant_code}")
                    await handle.signal("pr_merge_completed", pr_number)
                    logger.info("pr_merge_completed → coordinator-%s PR#%s", tenant_code, pr_number)
                except Exception as exc:
                    logger.debug("coordinator-%s signal skipped: %s", tenant_code, exc)
        except Exception as exc:
            logger.error("Failed to signal coordinator for PR#%s: %s", pr_number, exc)

    if not pr.get("merged"):
        logger.info("PR #%s closed (not merged) — signalling pr_closed (broadcast)", pr_number)
        return await _signal_temporal_by_pr(pr_number, "pr_closed", broadcast=True)

    logger.info("PR #%s merged — signalling pr_merged (broadcast)", pr_number)
    return await _signal_temporal_by_pr(pr_number, "pr_merged", broadcast=True)


@router.post("/github-app", summary="GitHub App Webhook")
async def github_app_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Webhook endpoint for GitHub App events.

    No JWT auth — authenticated via HMAC signature from GitHub.

    Handles:
    - installation.created / installation.deleted: GitHub App install lifecycle
    - push: Triggers Jenkins build for services using Jenkins CI provider
    - issue_comment: Detects Atlantis plan/apply results → signals DeploymentWorkflow
    - pull_request (closed+merged): Signals DeploymentWorkflow pr_merged
    """
    payload_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    event = request.headers.get("X-GitHub-Event", "")

    if not GitHubAppInstallationService.verify_webhook_signature(payload_body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()

    if event == "push":
        return await _handle_push_event(payload, db)

    if event == "issue_comment":
        return await _handle_atlantis_comment(payload)

    if event == "pull_request":
        return await _handle_pr_merged(payload)

    if event == "pull_request_review":
        return await _handle_pr_review(payload)

    # installation.created / installation.deleted and everything else
    service = GitHubAppInstallationService(db)
    result = await service.handle_webhook(event, payload)

    return result
