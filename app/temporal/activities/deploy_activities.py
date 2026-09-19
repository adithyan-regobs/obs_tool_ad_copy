"""
Deploy Activities for the Temporal deployment workflow.

Each activity is an idempotent unit of work that can be retried safely:

  run_script_pr_workflow   — wraps ScriptPRWorkflowService.create() entirely:
                             file locations → script gen → commit → create PR
  post_apply_comment       — posts "atlantis apply" comment on the GitHub PR
  close_pr_activity        — closes a PR with a reason comment
  update_queue_status      — updates transaction_queue item statuses in DB
  poll_for_plan_status     — polls GitHub PR comments for Atlantis plan result
  poll_for_apply_status    — polls GitHub PR comments for Atlantis apply result

All activities that need DB access create their own session via AsyncSessionLocal.
All activities that need GitHub access get a token via get_token_for_org.
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional, Set

from pydantic import BaseModel, Field as PydanticField
from temporalio import activity

logger = logging.getLogger(__name__)


# ── AI plan verification ─────────────────────────────────────────────────────
# Sensitive resource list — taken verbatim from the `terragrunt-pr-review` skill's
# "Sensitive resource list" section. A destroy/replace of any of these types (or
# any address containing a prod-like prefix) is treated as high-risk.
# (`aws_lb`/`aws_lb_listener` are public-traffic risk on destroy; `vault_`/`sops_`
#  are matched as prefixes so they cover vault_generic_secret etc.)
SENSITIVE_RESOURCE_TYPES = (
    "aws_db_instance", "aws_db_cluster", "aws_db_snapshot", "aws_rds_cluster",
    "aws_s3_bucket", "aws_s3_bucket_policy", "aws_s3_object",
    "aws_kms_key", "aws_kms_alias",
    "aws_secretsmanager_secret", "aws_secretsmanager_secret_version",
    "aws_iam_role", "aws_iam_policy", "aws_iam_user", "aws_iam_group",
    "aws_route53_zone", "aws_route53_record",
    "aws_eks_cluster", "aws_eks_node_group",
    "aws_dynamodb_table",
    "aws_acm_certificate",
    "aws_lb", "aws_lb_listener",
    "vault_", "sops_", "vault_generic_secret",
)
# Flag any resource whose address contains one of these, regardless of type.
SENSITIVE_ADDRESS_PREFIXES = ("prod", "prd", "live", "payments", "wealth")


class PlanVerificationResult(BaseModel):
    """Structured verdict returned by the AI plan-verification agent."""
    is_destructive: bool = PydanticField(
        description="True if the plan will DESTROY (-) or REPLACE (-/+) at least one existing resource. "
                    "Pure additions (+) and in-place updates (~) are NOT destructive."
    )
    severity: str = PydanticField(
        description="Overall risk: one of 'none', 'low', 'medium', 'high', 'critical'. "
                    "Use 'high'/'critical' when stateful/sensitive resources (databases, S3, KMS, "
                    "secrets, IAM) or production-prefixed resources are destroyed or replaced."
    )
    destroyed_resources: List[str] = PydanticField(
        default_factory=list,
        description="Resource addresses marked for destruction (- destroy).",
    )
    replaced_resources: List[str] = PydanticField(
        default_factory=list,
        description="Resource addresses marked for replacement (-/+ destroy and recreate).",
    )
    sensitive_resources: List[str] = PydanticField(
        default_factory=list,
        description="Subset of destroyed/replaced addresses that are stateful or sensitive "
                    "(RDS, S3, KMS, Secrets Manager, IAM, Route53, EKS, vault/sops) or production-prefixed.",
    )
    summary: str = PydanticField(description="One-sentence plain-language summary of the verdict.")
    rationale: str = PydanticField(description="Short explanation of why the plan is or isn't destructive.")


_PLAN_VERIFY_SYSTEM_PROMPT = """You are an infrastructure safety reviewer for a Terraform/Terragrunt \
deployment pipeline driven by Atlantis. You are given the FULL output of a successful `atlantis plan`.

Your only job is to determine whether applying this plan would be DESTRUCTIVE.

How to read a Terraform plan:
- Each resource action is marked with a symbol:
    +    create        (NOT destructive)
    ~    update in-place (NOT destructive)
    -    destroy        (DESTRUCTIVE)
    -/+  replace        (DESTRUCTIVE — destroys then recreates; data loss risk)
    <=   read (data source) (NOT destructive)
- The summary line "Plan: X to add, Y to change, Z to destroy" tells you the counts.
  If Z (to destroy) is 0 AND there are no "-/+" replacements, the plan is NOT destructive.
- Replacements still appear in the destroy count, so always also scan the body for "-/+" and
  "# ... will be destroyed" / "must be replaced" annotations.

Treat a destroy or replace of these STATEFUL / SENSITIVE types as high severity:
{sensitive_types}
Also treat any resource address containing one of these prefixes as high severity regardless of type:
{sensitive_prefixes}

Rules:
- is_destructive = true ONLY if at least one existing resource is destroyed (-) or replaced (-/+).
- List the exact resource addresses you found for destroyed_resources and replaced_resources.
- sensitive_resources = the subset of those that match the sensitive types/prefixes above.
- Be precise and avoid false positives: a plan that only creates (+) or updates in place (~) is NOT destructive.
"""


def _classify_sensitive(addresses: List[str]) -> List[str]:
    """Deterministic backstop: flag addresses matching the skill's sensitive types/prefixes."""
    flagged: List[str] = []
    for addr in addresses:
        low = addr.lower()
        if any(t in low for t in SENSITIVE_RESOURCE_TYPES) or any(p in low for p in SENSITIVE_ADDRESS_PREFIXES):
            flagged.append(addr)
    return flagged


async def _get_latest_plan_comment_body(pr_number: int, repo_full_name: str, not_before: str | None = None) -> str | None:
    """
    Return the body of the most recent SUCCESSFUL Atlantis plan-output comment,
    or None if none is found.

    Race-safety: a concurrent or manual `atlantis plan` on the same PR can post a
    newer comment that is a plan failure or a lock / "currently busy" notice. Our
    plan's success was recorded from its own signal, so we must NOT just grab the
    newest plan comment. We skip any comment that is a failure / lock / busy notice
    and require an actual plan-result line, so we always verify OUR successful plan
    rather than whatever comment happens to be newest.
    """
    from datetime import datetime
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    owner, repo = repo_full_name.split("/", 1)
    nb_dt = None
    if not_before:
        nb_dt = datetime.fromisoformat(not_before.replace("Z", "+00:00"))

    async with AsyncSessionLocal() as db:
        comments = await GitOpsHandler.get_pr_comments(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
        )

    for comment in reversed(comments):  # newest first
        if nb_dt:
            created_at = comment.get("created_at", "")
            if created_at:
                comment_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if comment_dt <= nb_dt:
                    break  # remaining comments are older than the cutoff
        body_raw = comment.get("body") or ""
        body = body_raw.lower()
        if "ran plan for" not in body:
            continue  # not a plan comment
        if any(m in body for m in ("plan error", "plan failed", "locked by", "lock on this plan", "currently busy")):
            continue  # failure / lock / busy notice — not a real plan result
        if "plan:" in body or "no changes" in body:
            return body_raw  # a genuine successful plan
    return None


# ── Activity 1: run_script_pr_workflow ───────────────────────────────────────

@activity.defn
async def run_script_pr_workflow(
    user_code: str,
    tenant_code: str,
    queue_ids: List[int],
) -> Dict[str, Any]:
    """
    Wraps ScriptPRWorkflowService.create() as a single Temporal activity.

    Does: fetch queue items → sync secrets → generate HCL scripts →
          create feature branch → commit files → create PR in GitHub →
          save workflow records to DB.

    Idempotent: ScriptPRWorkflowService checks for an existing open PR on the
    same branch before creating a new one, so retries are safe.

    Returns dict with pr_number (first PR number found) and feature_branches.
    """
    from app.db.session import AsyncSessionLocal
    from app.services.script_pr_workflow_service import ScriptPRWorkflowService
    import app.db.models  # noqa: F401 — register models with SQLAlchemy

    activity.logger.info(
        f"run_script_pr_workflow: user={user_code} tenant={tenant_code} queues={queue_ids}"
    )

    async with AsyncSessionLocal() as db:
        service = ScriptPRWorkflowService(db)
        result = await service.create(
            user_code=user_code,
            tenant_code=tenant_code,
            queue_ids=queue_ids,
        )

    # Classify PRs by type from gitops_responses
    # gitops_responses = {"{repo_name}|||{base_branch}": {"pr": {...}, "type": "infrastructure"|"k8s_manifest"|"workflow"}}
    pr_number = None
    pr_url = None
    repo_full_name = None
    commit_sha = None
    secondary_prs = []  # list of {pr_number, repo_full_name, pr_type} for non-infrastructure PRs

    for _key, resp in result.get("gitops_responses", {}).items():
        pr_info = resp.get("pr") or {}
        if not isinstance(pr_info, dict):
            continue
        _pr_num = pr_info.get("number") or pr_info.get("pr_number")
        if not _pr_num:
            continue
        _pr_url = pr_info.get("url") or pr_info.get("html_url") or pr_info.get("pr_url")
        _pr_type = resp.get("type", "infrastructure")

        # Extract owner/repo from pr_url
        _repo_full = None
        if _pr_url:
            _parts = _pr_url.rstrip("/").split("/")
            _pull_idx = next((i for i, p in enumerate(_parts) if p == "pull"), -1)
            if _pull_idx >= 2:
                _repo_full = f"{_parts[_pull_idx - 2]}/{_parts[_pull_idx - 1]}"

        if _pr_type == "infrastructure":
            pr_number = _pr_num
            pr_url = _pr_url
            repo_full_name = _repo_full
            commit_sha = pr_info.get("head_sha")
        else:
            secondary_prs.append({
                "pr_number": _pr_num,
                "repo_full_name": _repo_full,
                "pr_type": _pr_type,
            })

    # NOTE: intentionally NO fallback to promote a secondary PR into the
    # infrastructure slot. If there is no infrastructure (terragrunt) PR, the
    # terragrunt.hcl had no diff. For terragrunt-only resources (s3/sqs/dynamo)
    # that means nothing to do. For ECS/EKS there may still be workflow /
    # k8s-manifest PRs in `secondary_prs`; the deployment workflow detects
    # pr_number=None + non-empty secondary_prs and merges those directly (no
    # plan/apply, since there is no infra change). Only when BOTH the primary PR
    # and secondary_prs are empty is this treated as "PR creation failed".
    # Promoting a secondary PR here would (a) route a non-atlantis PR through the
    # plan→apply state machine and (b) leave it duplicated in secondary_prs.

    # Extract atlantis_project_name from script_gen_responses
    project_name = None
    for _qid, responses in result.get("script_gen_responses", {}).items():
        atlantis_resp = responses.get("atlantis", {})
        project_name = atlantis_resp.get("atlantis_project_name")
        if project_name:
            break

    # Fallback: derive project name for db/gateway resources that don't use the atlantis component
    if not project_name:
        from app.repository.transaction_queue_repository import TransactionQueueRepository
        from app.services.terragrunt_sync_service import TerragruntSyncService

        DB_GATEWAY_CASES = {"add_route", "database_creation", "user_management", "user_management_separate_file"}

        async with AsyncSessionLocal() as db:
            queue_repo = TransactionQueueRepository(db)
            for qid in queue_ids:
                item = await queue_repo.get_by_id(qid)
                if not item:
                    continue
                case_ref = item.case_ref_code or ""
                if case_ref not in DB_GATEWAY_CASES:
                    continue

                snap = item.config_snapshot or {}
                product = snap.get("product_name") or snap.get("product") or snap.get("applications_mst_code") or ""
                env = snap.get("environment") or ""
                tenant = snap.get("tenant_code") or ""
                product_s = TerragruntSyncService._sanitize_name(product)
                env_s = TerragruntSyncService._normalize_environment_for_display(env, tenant)

                if case_ref == "add_route":
                    project_name = f"{product_s}-{env_s}-gateway"
                    break

                # database_creation / user_management — distinguish mysql vs pg
                db_server = snap.get("db_server_name", "")
                has_mysql = bool(snap.get("mysql_servers")) or "mysql" in db_server.lower()
                if has_mysql:
                    project_name = f"{product_s}-{env_s}-mysql-db"
                else:
                    project_name = f"{product_s}-{env_s}-pg-db"
                break

    _fb_branches = [
        (v["branch"] if isinstance(v, dict) else v)
        for v in result.get("feature_branches", {}).values()
    ]
    activity.logger.info(
        f"run_script_pr_workflow done: pr_number={pr_number} repo={repo_full_name} pr_url={pr_url} "
        f"project_name={project_name} branches={_fb_branches} secondary_prs={len(secondary_prs)}"
    )

    return {
        "pr_number": pr_number,
        "pr_url": pr_url,
        "repo_full_name": repo_full_name,
        "project_name": project_name,
        "commit_sha": commit_sha,
        "feature_branches": result.get("feature_branches", {}),
        "total_prs_created": len(result.get("gitops_responses", {})),
        "secondary_prs": secondary_prs,
    }


async def _post_pr_comment(pr_number: int, repo_full_name: str, tenant_code: str, body: str) -> None:
    """Post a comment on a GitHub PR via GitOpsHandler."""
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    if "/" not in repo_full_name:
        raise ValueError(f"repo_full_name must be 'owner/repo', got: {repo_full_name}")
    owner, repo = repo_full_name.split("/", 1)

    async with AsyncSessionLocal() as db:
        await GitOpsHandler.post_pr_comment(
            tenant=tenant_code, owner=owner, repo=repo, pr_number=pr_number, body=body, db=db,
        )


# ── Activity 2: post_apply_comment ───────────────────────────────────────────

@activity.defn
async def post_apply_comment(pr_number: int, repo_full_name: str, project_name: str | None = None) -> None:
    """Posts "atlantis apply -p <project_name>" (or generic "atlantis apply") on the GitHub PR."""
    cmd = f"atlantis apply -p {project_name}" if project_name else "atlantis apply"
    activity.logger.info(f"Posting '{cmd}' comment on PR #{pr_number} ({repo_full_name})")
    await _post_pr_comment(pr_number, repo_full_name, "default", cmd)
    activity.logger.info(f"'{cmd}' comment posted on PR #{pr_number} ✓")


# ── Activity 2b: post_plan_comment ───────────────────────────────────────────

@activity.defn
async def post_plan_comment(pr_number: int, repo_full_name: str, project_name: str | None = None) -> None:
    """Posts "atlantis plan -p <project_name>" (or generic "atlantis plan") on the GitHub PR."""
    cmd = f"atlantis plan -p {project_name}" if project_name else "atlantis plan"
    activity.logger.info(f"Posting '{cmd}' comment on PR #{pr_number} ({repo_full_name})")
    await _post_pr_comment(pr_number, repo_full_name, "default", cmd)
    activity.logger.info(f"'{cmd}' comment posted on PR #{pr_number} ✓")


# ── Activity 2c: approve_pr ──────────────────────────────────────────────────

@activity.defn
async def approve_pr(pr_number: int, repo_full_name: str) -> None:
    """Approves the PR using the GitHub Approval App (separate from the gitops app).

    Called after plan success and before posting the apply comment, so that
    Atlantis apply doesn't fail due to required PR review protection rules.
    Skipped automatically if GITHUB_APPROVAL_ENABLED=false.
    """
    from app.core.config import settings
    from app.integrations.github_integration import GitHubIntegration

    if not settings.github_approval_enabled:
        activity.logger.info(f"approve_pr: approval disabled (GITHUB_APPROVAL_ENABLED=false) — skipping PR #{pr_number}")
        return

    if "/" not in repo_full_name:
        raise ValueError(f"repo_full_name must be 'owner/repo', got: {repo_full_name}")
    owner, repo = repo_full_name.split("/", 1)

    activity.logger.info(f"approve_pr: approving PR #{pr_number} ({repo_full_name})")
    token = settings.github_codeowner_pat
    await GitHubIntegration.approve_pull_request(
        token=token,
        base_url=settings.github_base_url,
        owner=owner,
        repo=repo,
        pull_number=pr_number,
    )
    activity.logger.info(f"approve_pr: PR #{pr_number} approved ✓")


# ── Activity 3: close_pr_activity ────────────────────────────────────────────

@activity.defn
async def close_pr_activity(pr_number: int, reason: str, repo_full_name: str = "") -> None:
    """
    Closes a GitHub PR and adds a comment explaining why it was closed.
    repo_full_name: "Owner/repo-name" — optional, skips close if empty/unknown.
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    if not repo_full_name or "/" not in repo_full_name:
        activity.logger.warning(f"close_pr_activity: no repo_full_name, skipping close of PR #{pr_number}")
        return

    owner, repo = repo_full_name.split("/", 1)
    activity.logger.info(f"Closing PR #{pr_number} ({repo_full_name}) — reason: {reason}")

    async with AsyncSessionLocal() as db:
        await GitOpsHandler.close_pull_request(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
            comment=f"Deployment workflow closed this PR automatically — reason: {reason}",
        )

    activity.logger.info(f"PR #{pr_number} closed ✓")


# ── Activity 4: update_queue_status ──────────────────────────────────────────

@activity.defn
async def update_queue_status(queue_ids: List[int], new_status: str) -> None:
    """
    Updates the status column on transaction_queue rows identified by queue_ids.
    Called at key workflow milestones: PLAN_COMPLETED, APPLY_COMPLETED, ACTIVE, FAILED.

    On DEPLOYED it also settles gateway paths: a gateway KONG_ROUTE row's
    config_snapshot lists exactly the paths this deploy wrote into the terragrunt
    file, per group, and nothing else flips them — update_deployment_status and
    update_resource_status both skip KONG_ROUTE, so without this the routes sit
    INITIATED until the next deploy attempt's settle path finds them.
    """
    from app.db.session import AsyncSessionLocal
    from datetime import datetime, timezone

    from app.db.models.transaction_queue_model import TransactionQueueModel, TransactionQueueStatusEnum
    from sqlalchemy import select, update

    activity.logger.info(f"Updating queue {queue_ids} → status={new_status}")

    status_enum = TransactionQueueStatusEnum(new_status.lower())

    async with AsyncSessionLocal() as db:
        if status_enum == TransactionQueueStatusEnum.FAILED:
            # A failed deploy goes BACK to APPROVED, not to FAILED.
            #
            # Deploy accepts APPROVED rows only (validate_deployable_queue_items),
            # and nothing ever moved a row out of FAILED — so parking it there
            # stranded the change for good: its snapshot survived, but no retry
            # could reach it and the only way forward was to raise the whole
            # request again. A deploy that did not happen has not changed what
            # the reviewer approved, so the approval still stands and the row is
            # exactly as deployable as it was a moment earlier.
            #
            # The seal (approved_snapshot_hash) is deliberately left intact: it
            # fingerprints the reviewed content, which this did not touch, so the
            # retry verifies against the same decision rather than needing a new
            # one.
            #
            # The failure is NOT lost. It is written to each row's history below,
            # the run-track already carries 'completed: failed' with the error,
            # and update_deployment_status has put ERROR on the service_configs /
            # infrastructure_mst row. What changes is only whether the change can
            # be tried again.
            #
            # Rows that were never in flight are left alone — a draft or a
            # submitted request must not be promoted to APPROVED by a deploy
            # failure somewhere else in the batch.
            IN_FLIGHT = (
                TransactionQueueStatusEnum.APPROVED,
                TransactionQueueStatusEnum.STARTING_DEPLOYMENT,
                TransactionQueueStatusEnum.DEPLOYING,
                TransactionQueueStatusEnum.PR_RAISED,
                TransactionQueueStatusEnum.PR_MERGED,
                TransactionQueueStatusEnum.PROVISIONING,
                TransactionQueueStatusEnum.BUILDING,
                TransactionQueueStatusEnum.COMMIT,
                TransactionQueueStatusEnum.CHECKOUT,
            )
            rows = (
                await db.execute(
                    select(TransactionQueueModel).where(
                        TransactionQueueModel.id.in_(queue_ids)
                    )
                )
            ).scalars().all()
            for row in rows:
                if row.status not in IN_FLIGHT:
                    continue
                was = getattr(row.status, "value", str(row.status))
                row.status = TransactionQueueStatusEnum.APPROVED
                # The deploy is over, so nothing is in flight any more. Left
                # set, the gate's marker would refuse every later revoke on a
                # row that is once again merely approved and waiting.
                row.deploy_started_at = None
                # JSONB does not see in-place mutation: reassign, never append.
                row.history = (row.history or []) + [{
                    "at": datetime.now(timezone.utc).isoformat(),
                    "by": "rule",
                    "event": "deploy-failed",
                    "comment": f"deploy failed while {was}; returned to approved "
                               f"so it can be deployed again",
                }]
            activity.logger.info(
                "deploy failed — returned %s row(s) to APPROVED for retry",
                sum(1 for r in rows if r.status == TransactionQueueStatusEnum.APPROVED),
            )
        else:
            # Row path, not a bulk UPDATE: every pipeline transition goes onto
            # the row's history — the record used to hold only the approval
            # events, so the deploy's own moves (starting_deployment, deployed,
            # pr_raised, ...) never showed on the History tab.
            from app.repository.transaction_queue_repository import TransactionQueueRepository

            rows = (
                await db.execute(
                    select(TransactionQueueModel).where(
                        TransactionQueueModel.id.in_(queue_ids)
                    )
                )
            ).scalars().all()
            for row in rows:
                TransactionQueueRepository.append_status_event(row, status_enum)
                row.status = status_enum
        # Committed BEFORE the gateway settle below, and the settle is isolated in
        # its own transaction: this activity is on every deployment's critical path
        # (service and infra included), and the status flip is what the workflow
        # depends on. Sharing a transaction would let a gateway-only failure roll
        # the flip back for unrelated items, and the Temporal retry would hit the
        # same error and strand the deploy.
        await db.commit()

        if status_enum == TransactionQueueStatusEnum.DEPLOYED:
            # Step two: the PR merged, so the routes are live. FATAL on failure
            # — swallowing it would leave routes in the gateway that devlift has
            # no record of. Temporal retries; the write is idempotent, and the
            # status flip above is already committed so only this re-runs.
            try:
                await _write_deployed_gateway_routes(db, queue_ids)
                await db.commit()
            except Exception:
                await db.rollback()
                activity.logger.error(
                    "gateway route write FAILED for queue %s — retrying; routes are "
                    "live in the gateway but not yet recorded",
                    queue_ids, exc_info=True,
                )
                raise

            # The approved values become the live configuration, AFTER THE
            # MERGE. Hooked to the STATUS rather than to the workflow, because
            # DeploymentWorkflow reaches a merged outcome five different ways —
            # the no-diff/secondary-PR shortcut, two merged-before-apply exits
            # and the main apply→merge success path — and every one of them
            # passes through here. Wiring the save into each instead would be
            # five places to keep in step. The multiple-deploy orchestrator gets
            # it for free too: it runs DeploymentWorkflow as a child and only
            # ever sets FAILED itself.
            #
            # Its own transaction, for the same reason the gateway settle above
            # has one: this activity is on every deployment's critical path, and
            # a settings-save failure must not roll back the status flip the
            # workflow depends on.
            try:
                saved = await _save_deployed_settings(db, queue_ids)
                await db.commit()
                if saved:
                    activity.logger.info(
                        "settings saved for %s of queue %s", saved, queue_ids,
                    )
            except Exception as exc:
                # Never fatal — the PR is merged and the change is live by now,
                # so refusing here would not un-ship anything. A stale settings
                # row is something to chase, not a reason to fail a deploy that
                # has already happened.
                await db.rollback()
                activity.logger.error(
                    "settings save failed for queue %s (deploy unaffected) — the "
                    "live records are now stale: %s",
                    queue_ids, exc, exc_info=True,
                )

    activity.logger.info(f"Queue status updated → {new_status} ✓")


@activity.defn
async def record_gateway_routes_pr_created(queue_ids: List[int]) -> None:
    """Record a PROD gateway change's routes at PR_CREATED, once its pull
    request exists. Other environments write nothing here — they wait for the
    merge.

    Step one of two, and prod is the only environment that has a step one.
    That asymmetry follows how a deploy is judged to have succeeded: on prod
    the raised PR IS the outcome devlift produces — merging and applying it is
    manual work someone does afterwards on their own schedule — so waiting for
    a merge would mean prod routes were never recorded at all. Everywhere else
    the PR is merged as part of the deploy, so the merge is the real event and
    there is nothing to gain by writing ahead of it.

    PR_CREATED, not ACTIVE, because even on prod the PR is not the gateway: if
    it is closed instead of merged these rows stay put and no reader counts
    them as live (see _DEPLOYED_STATUSES). The merge promotes them to ACTIVE —
    see update_queue_status, which stays unfiltered, so a merge records every
    environment's routes including the prod ones this pass already wrote.

    Not fatal — the merge write records the same rows at ACTIVE if this fails.
    """
    from app.core.enum import DeploymentStatusEnum
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            await _write_deployed_gateway_routes(
                db, queue_ids, creation_status=DeploymentStatusEnum.PR_CREATED,
                only_environments={"prod"},
            )
            await db.commit()
        except Exception:
            await db.rollback()
            activity.logger.error(
                "gateway PR_CREATED write failed for queue %s — the merge write "
                "will record these routes instead; deploy unaffected",
                queue_ids, exc_info=True,
            )


async def _write_deployed_gateway_routes(
    db, queue_ids: List[int], creation_status=None,
    only_environments: Optional[Set[str]] = None,
) -> None:
    """Write each gateway queue row's snapshot into kong_route_groups /
    kong_route_configs, at `creation_status`.

    The only writer of those tables. Save, submit and approve leave them alone,
    so what they hold is what has actually been shipped.

    `only_environments` restricts the write to rows in those environments; None
    means every row. It exists for the PR_CREATED pass, which is prod-only —
    see record_gateway_routes_pr_created for why. The environment is read from
    the queue row's snapshot rather than a column, because transaction_queue
    has none; the snapshot is also what apply_deployed_gateway_snapshot reads
    it from, so the two cannot disagree about which environment a row is for.

    Raises on failure; both callers decide whether that is fatal.
    """
    from app.core.enum import DeploymentStatusEnum
    from app.db.models.transaction_queue_model import (
        TransactionQueueModel,
        gateway_rows_clause,
    )
    from app.db.models.user_mst_model import UserMstModel
    from app.services.kong_route_config_service import KongRouteConfigService
    from sqlalchemy import select

    if creation_status is None:
        creation_status = DeploymentStatusEnum.ACTIVE

    queue_rows = (await db.execute(
        select(TransactionQueueModel).where(
            TransactionQueueModel.id.in_(queue_ids),
            gateway_rows_clause(),
        )
    )).scalars().all()
    if not queue_rows:
        return

    if only_environments is not None:
        wanted = {e.lower() for e in only_environments}
        kept = []
        for q in queue_rows:
            env = (q.config_snapshot or {}).get("environment")
            env = getattr(env, "value", env)
            if str(env or "").lower() in wanted:
                kept.append(q)
        skipped = len(queue_rows) - len(kept)
        if skipped:
            # Said out loud rather than dropped quietly: "no rows written" and
            # "rows deliberately deferred to the merge" look identical in the
            # tables, and only one of them is a bug.
            activity.logger.info(
                "gateway routes deferred to the merge write for %d queue row(s) "
                "outside %s — nothing recorded for them at %s",
                skipped, sorted(wanted),
                getattr(creation_status, "value", creation_status),
            )
        queue_rows = kept
        if not queue_rows:
            return

    # Credited on every row written, so the tab can say who shipped a route.
    # One query for the batch; the user code stands in if the account has gone.
    user_codes = {q.user_code for q in queue_rows if q.user_code}
    emails = {}
    if user_codes:
        emails = dict((await db.execute(
            select(UserMstModel.code, UserMstModel.email_id)
            .where(UserMstModel.code.in_(user_codes))
        )).all())

    service = KongRouteConfigService(db)

    for q in queue_rows:
        totals = await service.apply_deployed_gateway_snapshot(
            q.config_snapshot or {},
            user_email=emails.get(q.user_code) or q.user_code,
            creation_status=creation_status,
        )
        activity.logger.info(
            "gateway routes recorded [%s]: queue=%s groups=%d "
            "created=%d updated=%d removed=%d promoted=%d",
            getattr(creation_status, "value", creation_status),
            q.code, totals["groups"], totals["created"],
            totals["updated"], totals["removed"], totals["promoted"],
        )

async def _save_deployed_settings(db, queue_ids: List[int]) -> int:
    """Write each merged queue item's approved snapshot into its live record.

    Save deliberately wrote nothing live: the proposal sat in config_snapshot
    while service_configs went on describing what was actually running, which is
    what made that row an honest baseline to diff against. This is the other
    half of that bargain, and it runs only once the PR is merged — so the live
    row moves when the change really shipped, not when someone asked for it.

    Dispatched per item on case_ref_code through ResourceSettingsSaverHandler,
    the same shape as the pre-action and post-action families, so a new resource
    type is a component file plus a map entry rather than a branch here. Items
    with nothing registered are skipped silently, which is most of them.

    Does not commit — the caller owns the transaction so a failure here rolls
    back on its own without touching the status flip.

    Returns how many rows were actually written.
    """
    from sqlalchemy import select
    from app.db.models.transaction_queue_model import TransactionQueueModel
    from app.handlers.resource_settings_saver_handler import (
        ResourceSettingsSaverHandler,
    )

    rows = (await db.execute(
        select(TransactionQueueModel).where(
            TransactionQueueModel.id.in_(queue_ids),
            TransactionQueueModel.is_deleted.isnot(True),
        )
    )).scalars().all()

    saved = 0
    for row in rows:
        # The same dict the pre/post-action handlers are handed, so a component
        # written against one works here unchanged.
        queue_dict = {
            "id": row.id,
            "code": row.code,
            "tenant_code": row.tenant_code,
            "transaction_code": row.transaction_code,
            "case_ref_code": row.case_ref_code,
            "table_name": row.table_name,
            "config_snapshot": row.config_snapshot,
        }
        # The handler swallows and logs a component's own failure, so one
        # resource's stale row never stops the rest of the batch being saved.
        if await ResourceSettingsSaverHandler.run(
            tenant=row.tenant_code, queue_dict=queue_dict, db=db
        ):
            saved += 1
    return saved


@activity.defn
async def update_deployment_status(
    queue_ids: List[int],
    new_status: str,
    error_message: str | None = None,
) -> None:
    """
    Updates deployment_status on infrastructure_mst or service_configs.
    Looks up entity refs from the queue rows, then routes to the correct repo.
    """
    from app.db.session import AsyncSessionLocal
    from app.repository.transaction_queue_repository import TransactionQueueRepository
    from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
    from app.repository.service_config_repository import ServiceConfigRepository
    from app.core.enum import ResourceDeploymentStatusEnum, WorkflowSourceTableEnum

    status_enum = ResourceDeploymentStatusEnum(new_status.lower())

    activity.logger.info("Updating deployment_status %s → %s", queue_ids, new_status)

    async with AsyncSessionLocal() as db:
        queue_repo = TransactionQueueRepository(db)
        entity_refs = await queue_repo.get_entity_refs_by_ids(queue_ids)

        infra_codes = [
            r["transaction_code"] for r in entity_refs
            if r["table_name"] == WorkflowSourceTableEnum.INFRASTRUCTURE and r["transaction_code"]
        ]
        svc_codes = [
            r["transaction_code"] for r in entity_refs
            if r["table_name"] in (
                WorkflowSourceTableEnum.SERVICE_CONFIG,
                WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
            )
            # Gateway rows sit on SERVICE_CONFIG too (case_ref_code
            # "add_route") but a route deploy is not a service deploy — they
            # were skipped when they carried KONG_ROUTE and stay skipped now.
            and r.get("case_ref_code") != "add_route"
            and r["transaction_code"]
        ]

        if infra_codes:
            infra_repo = InfrastructureMstRepository(db)
            await infra_repo.bulk_update_deployment_status(infra_codes, status_enum, error_message)

        if svc_codes:
            svc_repo = ServiceConfigRepository(db)
            await svc_repo.bulk_update_deployment_status(svc_codes, status_enum, error_message)

        await db.commit()

    activity.logger.info("deployment_status updated → %s ✓", new_status)


@activity.defn
async def update_resource_status(
    queue_ids: List[int],
    new_status: str,
) -> None:
    """
    Updates the UI-ready status column on infrastructure_mst or service_configs.
    Called at high-level milestones: ONLINE (after merge).
    """
    from app.db.session import AsyncSessionLocal
    from app.repository.transaction_queue_repository import TransactionQueueRepository
    from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
    from app.repository.service_config_repository import ServiceConfigRepository
    from app.core.enum import ResourceStatusEnum, WorkflowSourceTableEnum

    status_enum = ResourceStatusEnum(new_status.upper())

    activity.logger.info("Updating resource status %s → %s", queue_ids, new_status)

    async with AsyncSessionLocal() as db:
        queue_repo = TransactionQueueRepository(db)
        entity_refs = await queue_repo.get_entity_refs_by_ids(queue_ids)

        infra_codes = [
            r["transaction_code"] for r in entity_refs
            if r["table_name"] == WorkflowSourceTableEnum.INFRASTRUCTURE and r["transaction_code"]
        ]
        svc_codes = [
            r["transaction_code"] for r in entity_refs
            if r["table_name"] in (
                WorkflowSourceTableEnum.SERVICE_CONFIG,
                WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
            )
            # add_route rows are gateway changes, not service deploys — see
            # update_deployment_status for why they are skipped.
            and r.get("case_ref_code") != "add_route"
            and r["transaction_code"]
        ]

        if infra_codes:
            await InfrastructureMstRepository(db).bulk_update_status(infra_codes, status_enum)

        if svc_codes:
            await ServiceConfigRepository(db).bulk_update_status(svc_codes, status_enum)

        await db.commit()

    activity.logger.info("resource status updated → %s ✓", new_status)


@activity.defn
async def check_for_manual_commit(
    pr_number: int,
    repo_full_name: str,
    expected_sha: str,
) -> bool:
    """
    Returns True if the PR HEAD SHA no longer matches expected_sha AND the
    new commit was not authored by one of settings.devlift_git_bots_list.
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler
    from app.core.config import settings

    owner, repo = repo_full_name.split("/", 1)

    async with AsyncSessionLocal() as db:
        pr_info = await GitOpsHandler.get_pull_request(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
        )

    current_sha = (pr_info or {}).get("head_sha") or ""
    if not current_sha or current_sha == expected_sha:
        return False

    # SHA changed — check if the new commit was pushed by the devlift bot
    try:
        async with AsyncSessionLocal() as db:
            commit_detail = await GitOpsHandler.get_commit(
                tenant="default", owner=owner, repo=repo, sha=current_sha, db=db,
            )
        # author is null when the commit's email maps to no GitHub account, so
        # this cannot chain straight into .get("login").
        author_login = ((commit_detail or {}).get("author") or {}).get("login", "")
        if author_login in settings.devlift_git_bots_list:
            activity.logger.info(
                "check_for_manual_commit: SHA changed on PR #%s but author is devlift bot — not manual",
                pr_number,
            )
            return False
    except Exception as e:
        activity.logger.warning("check_for_manual_commit: could not fetch commit author for %s: %s", current_sha[:8], e)

    activity.logger.warning(
        "Manual commit detected on PR #%s: expected=%s current=%s",
        pr_number, expected_sha[:8], current_sha[:8],
    )
    return True


# ── Activity 5: poll_for_plan_status ─────────────────────────────────────────

@activity.defn
async def poll_for_plan_status(
    pr_number: int,
    repo_full_name: str,
    not_before: str | None = None,
    project_name: str | None = None,
    after_last_apply: bool = False,
    project_dirs: list | None = None,
) -> str:
    """
    Polls GitHub PR comments for an Atlantis plan result.
    repo_full_name: "Owner/repo-name"
    not_before: ISO timestamp — ignore plan comments created at or before this time.
                Used after apply exhaustion to skip stale plan-success comments.
    project_name: prod promotion PR — only consider comments mentioning this
                Atlantis project (the shared stage→main PR carries several
                projects' results; a stage feature PR is single-project so
                results are interchangeable and no filter is needed there).
                In project-scoped mode a same-PR self-lock returns "busy"
                (= our command was REJECTED, re-post it) instead of
                "pending" (= command ran, result not posted yet).
    project_dirs: additional match needles — Atlantis error comments sometimes
                omit the project name and key by DIR instead ("Ran Plan for
                dir: environment/..."), so a comment counts as ours when it
                names the project OR any of our dirs (observed: a prod plan
                failure went undetected because only the project was matched).
    after_last_apply: adoption-evidence mode (prod, PR merged before our
                apply): answers "is there a plan comment for this project
                NEWER than its last apply-success?" — "found" | "none".
                A later plan means the applied state wasn't the final word.
    Returns: "success" | "failed[:<error excerpt>]" | "pending" | "busy"
             | "lock_conflict[:<pr>]" | ("found"|"none" in adoption mode)

    Defaults keep every stage caller byte-identical.
    """
    from datetime import datetime
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler
    from app.utils.atlantis_helpers import extract_atlantis_error

    owner, repo = repo_full_name.split("/", 1)
    activity.logger.info(f"Polling plan status for PR #{pr_number} ({repo_full_name})")

    nb_dt = None
    if not_before:
        nb_dt = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
    project_needle = (project_name or "").lower()
    dir_needles = [d.lower() for d in (project_dirs or [])]
    needles = ([project_needle] if project_needle else []) + dir_needles

    async with AsyncSessionLocal() as db:
        comments = await GitOpsHandler.get_pr_comments(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
        )

    for comment in reversed(comments):  # newest first
        if nb_dt:
            created_at = comment.get("created_at", "")
            if created_at:
                comment_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if comment_dt <= nb_dt:
                    break  # all remaining comments are older than cutoff
        body_raw = comment.get("body") or ""
        body = body_raw.lower()
        if needles and not any(n in body for n in needles):
            continue  # another project's comment on the shared promotion PR

        if after_last_apply:
            # Newest-first: whichever appears first decides. An apply-success
            # first → nothing planned after it → "none". Any plan comment
            # first → the apply wasn't the last word → "found".
            if "ran apply for" in body or "apply complete" in body or "apply succeeded" in body:
                if not ("apply error" in body or "apply failed" in body):
                    return "none"
                continue
            if ("ran plan for" in body or "plan succeeded" in body
                    or "plan error" in body or "plan failed" in body):
                return "found"
            continue

        if "ran plan for" in body or "plan succeeded" in body:
            if "locked by" in body or "lock on this plan" in body:
                # Self-lock: Atlantis double-command on the same PR — transient, not a conflict
                if "for this pull request" in body:
                    activity.logger.info(f"Self-lock (same PR) detected for PR #{pr_number}")
                    return "busy" if project_needle else "pending"
                import re as _re
                m = _re.search(r'from pull\s+#?(\d+)', body_raw, _re.IGNORECASE)
                locking_pr_num = m.group(1) if m else ""
                activity.logger.warning(f"Atlantis lock conflict found in PR #{pr_number} comment (locking_pr={locking_pr_num or '?'})")
                return f"lock_conflict:{locking_pr_num}" if locking_pr_num else "lock_conflict"
            if "plan error" in body or "plan failed" in body:
                activity.logger.warning(f"Plan failure found in PR #{pr_number} comment")
                excerpt = extract_atlantis_error(body_raw, "plan")
                return f"failed:{excerpt}" if excerpt else "failed"
            activity.logger.info(f"Plan succeeded found in PR #{pr_number} comment ✓")
            return "success"
        if "plan error" in body or "plan failed" in body:
            activity.logger.warning(f"Plan failure found in PR #{pr_number} comment")
            excerpt = extract_atlantis_error(body_raw, "plan")
            return f"failed:{excerpt}" if excerpt else "failed"

    if after_last_apply:
        return "none"
    activity.logger.info(f"No plan comment found yet for PR #{pr_number} — pending")
    return "pending"


# ── Activity 6: poll_for_apply_status ────────────────────────────────────────

@activity.defn
async def poll_for_apply_status(
    pr_number: int,
    repo_full_name: str,
    project_name: str | None = None,
    project_dirs: list | None = None,
) -> str:
    """
    Polls GitHub PR comments for an Atlantis apply result.
    repo_full_name: "Owner/repo-name"
    project_name: prod promotion PR — only consider comments mentioning this
                Atlantis project (shared PR, several projects' results). In
                project-scoped mode a same-PR self-lock returns "busy" (our
                command was rejected, re-post) instead of "pending". None =
                today's behavior, stage byte-identical.
    Returns: "success" | "failed[:<error excerpt>]" | "pending" | "busy"
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler
    from app.utils.atlantis_helpers import extract_atlantis_error

    owner, repo = repo_full_name.split("/", 1)
    activity.logger.info(f"Polling apply status for PR #{pr_number} ({repo_full_name})")
    project_needle = (project_name or "").lower()
    dir_needles = [d.lower() for d in (project_dirs or [])]
    needles = ([project_needle] if project_needle else []) + dir_needles

    async with AsyncSessionLocal() as db:
        comments = await GitOpsHandler.get_pr_comments(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
        )

    for comment in reversed(comments):  # newest first
        body_raw = comment.get("body") or ""
        body = body_raw.lower()
        if needles and not any(n in body for n in needles):
            continue  # another project's comment on the shared promotion PR
        if "ran apply for" in body or "apply complete" in body or "apply succeeded" in body:
            if ("apply error" in body or "apply failed" in body) and "for this pull request" in body:
                activity.logger.info(f"Self-lock (same PR) on apply for PR #{pr_number}")
                return "busy" if project_needle else "pending"
            if "apply error" in body or "apply failed" in body:
                activity.logger.warning(f"Apply failure found in PR #{pr_number} comment")
                excerpt = extract_atlantis_error(body_raw, "apply")
                return f"failed:{excerpt}" if excerpt else "failed"
            activity.logger.info(f"Apply succeeded found in PR #{pr_number} comment ✓")
            return "success"
        if "apply error" in body or "apply failed" in body:
            activity.logger.warning(f"Apply failure found in PR #{pr_number} comment")
            excerpt = extract_atlantis_error(body_raw, "apply")
            return f"failed:{excerpt}" if excerpt else "failed"

    activity.logger.info(f"No apply comment found yet for PR #{pr_number} — pending")
    return "pending"


# ── Activity 7: poll_pr_approval_status ──────────────────────────────────────

@activity.defn
async def poll_pr_approval_status(pr_number: int, repo_full_name: str) -> bool:
    """
    Checks GitHub PR reviews API to see if any reviewer has approved the PR.
    Used as a fallback when the pull_request_review webhook is missed.
    Returns True if at least one APPROVED review exists, False otherwise.
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    owner, repo = repo_full_name.split("/", 1)
    activity.logger.info(f"Polling PR approval status for PR #{pr_number} ({repo_full_name})")

    async with AsyncSessionLocal() as db:
        reviews = await GitOpsHandler.get_pr_reviews(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
        )

    for review in reviews:
        if review.get("state") == "APPROVED":
            activity.logger.info(f"PR #{pr_number} has an approved review ✓")
            return True

    activity.logger.info(f"No approved review found for PR #{pr_number}")
    return False


# ── Activity 8: poll_pr_state ─────────────────────────────────────────────────

@activity.defn
async def poll_pr_state(pr_number: int, repo_full_name: str) -> str:
    """
    Checks the actual GitHub PR state via API (not comments).
    Used in all wait phases to recover from missed webhooks (e.g. obs_tool was down).
    Returns: "merged" | "closed" | "open"
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    owner, repo = repo_full_name.split("/", 1)
    activity.logger.info(f"Polling PR state for PR #{pr_number} ({repo_full_name})")

    async with AsyncSessionLocal() as db:
        pr_info = await GitOpsHandler.get_pull_request(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
        )

    if pr_info.get("status") == "error":
        activity.logger.warning(f"poll_pr_state: failed to fetch PR #{pr_number}: {pr_info.get('error')}")
        return "open"

    if pr_info.get("merged"):
        activity.logger.info(f"PR #{pr_number} is merged ✓")
        return "merged"
    if pr_info.get("state") == "closed":
        activity.logger.info(f"PR #{pr_number} is closed")
        return "closed"

    activity.logger.info(f"PR #{pr_number} is still open")
    return "open"


# ── Activity 8: check_and_merge_pr ───────────────────────────────────────────

@activity.defn
async def check_and_merge_pr(pr_number: int, repo_full_name: str) -> str:
    """
    1. Fetches the PR to get the feature branch name.
    2. Compares last Atlantis plan comment timestamp vs last commit on feature branch.
       If last_commit_time > last_plan_comment_time → new commit arrived after plan → return "stale".
    3. If fresh (or timestamp check inconclusive) → merge the PR via squash.

    Returns: "merged" | "stale" | "failed"
    """
    from datetime import datetime
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    owner, repo = repo_full_name.split("/", 1)
    activity.logger.info(f"check_and_merge_pr: PR #{pr_number} repo={repo_full_name}")

    async with AsyncSessionLocal() as db:
        # 1. Fetch PR to get feature branch name
        pr_info = await GitOpsHandler.get_pull_request(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
        )
        feature_branch = pr_info.get("head_branch") or ""
        activity.logger.info(f"check_and_merge_pr: PR #{pr_number} branch={feature_branch}")

        # 2. Find last "Ran Plan for" comment timestamp
        comments = await GitOpsHandler.get_pr_comments(
            tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
        )
        last_plan_time: datetime | None = None
        for comment in reversed(comments):  # newest first
            body = (comment.get("body") or "").lower()
            if "ran plan for" in body or "plan succeeded" in body:
                if "plan error" in body or "plan failed" in body or "workspace locked" in body:
                    # Failed plan (e.g. workspace locked during apply) — skip, keep searching
                    activity.logger.info(
                        f"check_and_merge_pr: PR #{pr_number} — skipping failed plan comment"
                    )
                    continue
                raw = comment.get("created_at", "")
                if raw:
                    last_plan_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                break

        # 3. Get last commit timestamp on feature branch
        last_commit_date: str | None = None
        if feature_branch:
            last_commit_date = await GitOpsHandler.get_branch_latest_commit_time(
                tenant="default", owner=owner, repo=repo, branch=feature_branch, db=db,
            )

    # 4. Freshness check
    if last_plan_time is not None and last_commit_date is not None:
        try:
            last_commit_time = datetime.fromisoformat(last_commit_date.replace("Z", "+00:00"))
            activity.logger.info(
                f"PR #{pr_number} last_plan={last_plan_time.isoformat()} last_commit={last_commit_time.isoformat()}"
            )
            if last_commit_time > last_plan_time:
                activity.logger.warning(
                    f"PR #{pr_number} STALE — commit at {last_commit_time} is after last plan at {last_plan_time}"
                )
                return "stale"
        except Exception as e:
            activity.logger.warning(f"check_and_merge_pr: freshness check error ({e}) — proceeding to merge")
    else:
        activity.logger.warning(
            f"check_and_merge_pr: PR #{pr_number} — "
            f"last_plan={'found' if last_plan_time else 'missing'} "
            f"last_commit={'found' if last_commit_date else 'missing'} — proceeding to merge"
        )

    # 5. Merge the PR
    try:
        async with AsyncSessionLocal() as db:
            result = await GitOpsHandler.merge_pull_request(
                tenant="default", owner=owner, repo=repo,
                pull_number=pr_number, db=db, merge_method="merge",
            )
    except Exception as e:
        err = str(e).lower()
        if "conflict" in err or "merge conflict" in err:
            activity.logger.warning(f"check_and_merge_pr: PR #{pr_number} has merge conflicts → returning 'conflict'")
            return "conflict"
        raise

    if result.get("merged"):
        activity.logger.info(f"PR #{pr_number} merged successfully ✓")
        return "merged"

    activity.logger.warning(f"check_and_merge_pr: PR #{pr_number} merge failed — {result}")
    return "failed"


# ── Activity 9: resolve_conflict_and_merge ───────────────────────────────────

@activity.defn
async def resolve_conflict_and_merge(
    pr_number: int,
    git_repository: str,
    tenant_code: str,
    user_code: str,
) -> None:
    """
    Resolves a merge conflict on an open PR by delegating to
    ScriptPRWorkflowService.conflict_resolve(), which:
      1. Regenerates all HCL + atlantis.yaml files from scratch
      2. force_commit_from_parent: new commit with parent=base_sha → merge base fixed
      3. Force-pushes the feature branch (PR stays open, Atlantis lock held)

    The force-push fires a push webhook → Atlantis autoplan triggers automatically.
    Atlantis plan → plan_completed → DeploymentWorkflow posts apply →
    apply → Atlantis automerges → pr_merged.
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler
    from app.repository.transaction_queue_repository import TransactionQueueRepository
    from app.services.script_pr_workflow_service import ScriptPRWorkflowService
    import app.db.models  # noqa: F401

    activity.logger.info(
        "resolve_conflict_and_merge: PR#%s repo=%s tenant=%s",
        pr_number, git_repository, tenant_code,
    )

    if "/" not in git_repository:
        raise ValueError(f"git_repository must be 'owner/repo', got: {git_repository}")
    owner, repo = git_repository.split("/", 1)

    async with AsyncSessionLocal() as db:
        # Get PR info to resolve feature branch name
        pr_info = await GitOpsHandler.get_pull_request(
            tenant=tenant_code, owner=owner, repo=repo, pr_number=pr_number, db=db,
        )
        if pr_info.get("status") == "error":
            raise RuntimeError(f"Failed to get PR #{pr_number}: {pr_info.get('error')}")

        feature_branch = pr_info.get("head_branch")
        if not feature_branch:
            raise RuntimeError(f"PR #{pr_number} missing feature branch: {pr_info}")

        # Get queue codes linked to this PR
        queue_repo = TransactionQueueRepository(db)
        queue_codes = await queue_repo.get_queue_codes_for_pr(pr_number, git_repository)
        activity.logger.info("PR#%s feature=%s queue_codes=%s", pr_number, feature_branch, queue_codes)

        # Delegate to existing conflict_resolve:
        #   reset feature branch to base HEAD → regenerate all files → commit
        #   parent = base SHA → merge base fixed, no unintended file deletions
        service = ScriptPRWorkflowService(db)
        await service.conflict_resolve(
            queue_codes=queue_codes,
            pr_number=pr_number,
            git_repository=git_repository,
            feature_branch=feature_branch,
            user_code=user_code,
            tenant_code=tenant_code,
        )
        activity.logger.info("PR#%s conflict_resolve ✓", pr_number)


# ── Shared alert formatting ───────────────────────────────────────────────────

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def _fmt_alert(emoji: str, title: str, pr_context: str, body: str) -> str:
    """Consistent single-message format used by all alerts."""
    return (
        f"{emoji} *{title} — {pr_context}*\n\n"
        f"{body}\n\n"
        f"{_DIV}"
    )


# ── Activity 10: send_p0_alert ────────────────────────────────────────────────

@activity.defn
async def send_p0_alert(
    user_code: str,
    pr_number: int | None,
    message: str,
    title: str = "P0 Deploy Alert",
    pr_url: str | None = None,
    dm_only: bool = False,
    channel_only: bool = False,
) -> None:
    """
    Send a P0 deployment alert to:
      1. SLACK_AUTO_APPLY_ALERT_CHANNEL — deployer @mention embedded in body (skipped if dm_only=True)
      2. DM to the deploying user (skipped if channel_only=True)

    Looks up the user's email from the DB (via user_code), resolves their
    Slack user ID via users.lookupByEmail, then posts to both destinations.
    Non-fatal — errors are logged but never raised.
    """
    from app.core.config import settings
    from app.db.session import AsyncSessionLocal
    from app.db.models.user_mst_model import UserMstModel
    from app.services.slack.user_mapper import SlackUserMapper
    from slack_sdk.web.async_client import AsyncWebClient
    from sqlalchemy import select

    if not settings.deploy_alert_slack_bot_token:
        activity.logger.warning("send_p0_alert: DEPLOY_ALERT_SLACK_BOT_TOKEN not set — skipping alert")
        return

    pr_context = f"PR #{pr_number}" if pr_number else "PR (unknown)"
    pr_line = f"*PR:* <{pr_url}|PR #{pr_number}>" if pr_url and pr_number else None

    slack_client = AsyncWebClient(token=settings.deploy_alert_slack_bot_token)
    mapper = SlackUserMapper(db=None, slack_client=slack_client)

    slack_user_id: str | None = None
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserMstModel).where(
                    UserMstModel.code == user_code,
                    UserMstModel.is_deleted == False,
                )
            )
            user = result.scalars().first()
            if user:
                mapper.db = db
                slack_user_id = await mapper.get_slack_user_id_by_email(user.email_id)
    except Exception as e:
        activity.logger.warning("send_p0_alert: failed to resolve Slack user for %s: %s", user_code, e)

    mention = f"<@{slack_user_id}>" if slack_user_id else user_code

    # Channel: @devops-on-call (plain text, no notification) + Deployer mention + body
    channel_header = f"@devops-on-call\n\n*Deployer:* {mention}" + (f"\n{pr_line}" if pr_line else "")
    channel_msg = _fmt_alert(":rotating_light:", title, pr_context, f"{channel_header}\n\n{message}")

    # DM: clickable PR link + body (no mention — already addressed to the user)
    dm_header = pr_line or ""
    dm_msg = _fmt_alert(":rotating_light:", title, pr_context, f"{dm_header}\n\n{message}" if dm_header else message)

    if not dm_only and settings.slack_auto_apply_alert_channel:
        try:
            await slack_client.chat_postMessage(
                channel=settings.slack_auto_apply_alert_channel,
                text=channel_msg,
            )
            activity.logger.info("P0 alert sent to channel %s", settings.slack_auto_apply_alert_channel)
        except Exception as e:
            activity.logger.error("send_p0_alert: channel post failed: %s", e)

    if slack_user_id and not channel_only:
        try:
            dm = await slack_client.conversations_open(users=[slack_user_id])
            dm_channel = dm["channel"]["id"]
            await slack_client.chat_postMessage(channel=dm_channel, text=dm_msg)
            activity.logger.info("P0 alert DM sent to Slack user %s", slack_user_id)
        except Exception as e:
            activity.logger.error("send_p0_alert: DM failed: %s", e)


# ── Activity 11: mark_iac_locked / clear_iac_locked ─────────────────────────

@activity.defn
async def mark_iac_locked(queue_ids: list[int]) -> None:
    """Set iac_locked_at = NOW() on the resources associated with the given queue_ids."""
    from app.db.session import AsyncSessionLocal
    from app.db.models.transaction_queue_model import TransactionQueueModel
    from app.db.models.infrastructure_mst_model import InfrastructureMstModel
    from app.db.models.service_config_model import ServiceConfigModel
    from sqlalchemy import select, update
    from datetime import datetime, timezone

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                TransactionQueueModel.transaction_code,
                TransactionQueueModel.table_name,
                TransactionQueueModel.case_ref_code,
            )
            .where(TransactionQueueModel.id.in_(queue_ids))
        )).all()

        now = datetime.now(timezone.utc)
        for transaction_code, table_name, case_ref_code in rows:
            # Gateway (add_route) rows never locked their resource when they
            # carried KONG_ROUTE; moving them onto SERVICE_CONFIG must not
            # start locking the service they point at.
            if case_ref_code == "add_route":
                continue
            if table_name == "INFRASTRUCTURE":
                await db.execute(
                    update(InfrastructureMstModel)
                    .where(InfrastructureMstModel.code == transaction_code)
                    .values(iac_locked_at=now)
                )
            elif table_name == "SERVICE_CONFIG":
                await db.execute(
                    update(ServiceConfigModel)
                    .where(ServiceConfigModel.code == transaction_code)
                    .values(iac_locked_at=now)
                )
        await db.commit()
    activity.logger.info("mark_iac_locked: set iac_locked_at for %d queue items", len(rows))


@activity.defn
async def clear_iac_locked(queue_ids: list[int]) -> None:
    """Clear iac_locked_at on the resources associated with the given queue_ids."""
    from app.db.session import AsyncSessionLocal
    from app.db.models.transaction_queue_model import TransactionQueueModel
    from app.db.models.infrastructure_mst_model import InfrastructureMstModel
    from app.db.models.service_config_model import ServiceConfigModel
    from sqlalchemy import select, update

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                TransactionQueueModel.transaction_code,
                TransactionQueueModel.table_name,
                TransactionQueueModel.case_ref_code,
            )
            .where(TransactionQueueModel.id.in_(queue_ids))
        )).all()

        for transaction_code, table_name, case_ref_code in rows:
            # add_route rows never set the lock — see mark_iac_locked.
            if case_ref_code == "add_route":
                continue
            if table_name == "INFRASTRUCTURE":
                await db.execute(
                    update(InfrastructureMstModel)
                    .where(InfrastructureMstModel.code == transaction_code)
                    .values(iac_locked_at=None)
                )
            elif table_name == "SERVICE_CONFIG":
                await db.execute(
                    update(ServiceConfigModel)
                    .where(ServiceConfigModel.code == transaction_code)
                    .values(iac_locked_at=None)
                )
        await db.commit()
    activity.logger.info("clear_iac_locked: cleared iac_locked_at for %d queue items", len(rows))


# ── Activity 12: send_atlantis_lock_alert ────────────────────────────────────

async def _resolve_slack_id_by_user_code(user_code: str, db, mapper) -> tuple[str | None, str | None]:
    """Return (email, slack_user_id) for a DevLift user_code. Both None if not found."""
    from app.db.models.user_mst_model import UserMstModel
    from sqlalchemy import select
    result = await db.execute(
        select(UserMstModel).where(UserMstModel.code == user_code, UserMstModel.is_deleted == False)
    )
    user = result.scalars().first()
    if not user:
        return None, None
    slack_id = await mapper.get_slack_user_id_by_email(user.email_id)
    return user.email_id, slack_id


async def _resolve_locking_pr_user(
    locking_pr: int,
    repo_full_name: str,
    db,
    mapper,
    github_token: str,
    github_base_url: str,
) -> dict:
    """
    Resolve who raised the locking PR.
    Returns {"display": str, "slack_id": str|None, "pr_url": str|None}

    Priority:
      1. gitops_workflow_detail JOIN user_mst (DevLift PR)
      2. GitHub user profile public email → user_mst lookup
      3. Last commit email — only if last committer login == PR author login → user_mst
      4. Fallback: GitHub handle only, no DM
    """
    import httpx
    from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
    from app.db.models.user_mst_model import UserMstModel
    from sqlalchemy import select

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "RegObs-App",
    }
    owner, repo = repo_full_name.split("/", 1) if "/" in repo_full_name else (repo_full_name, repo_full_name)

    # Path 1: DevLift PR — direct DB lookup
    try:
        stmt = (
            select(GitopsWorkflowDetailModel, UserMstModel)
            .join(UserMstModel, GitopsWorkflowDetailModel.user_mst_code == UserMstModel.code)
            .where(
                GitopsWorkflowDetailModel.pr_number == locking_pr,
                GitopsWorkflowDetailModel.git_repository == repo_full_name,
                UserMstModel.is_deleted == False,
            )
        )
        row = (await db.execute(stmt)).first()
        if row:
            wf, user = row
            _, slack_id = await _resolve_slack_id_by_user_code(user.code, db, mapper)
            return {"display": f"{user.first_name} {user.last_name}", "slack_id": slack_id, "pr_url": wf.pr_url}
    except Exception as e:
        activity.logger.warning("_resolve_locking_pr_user: DB lookup failed: %s", e)

    # Path 2 & 3: GitHub API
    pr_author_login = None
    pr_url = None
    resolved_email = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            pr_resp = await client.get(f"{github_base_url}/repos/{owner}/{repo}/pulls/{locking_pr}", headers=headers)
            if pr_resp.status_code == 200:
                pr_data = pr_resp.json()
                pr_author_login = pr_data.get("user", {}).get("login", "")
                pr_url = pr_data.get("html_url")

            if pr_author_login:
                # Path 2: user profile public email
                u_resp = await client.get(f"{github_base_url}/users/{pr_author_login}", headers=headers)
                if u_resp.status_code == 200:
                    resolved_email = u_resp.json().get("email") or None

                # Path 3: last commit email — only if same author
                if not resolved_email:
                    c_resp = await client.get(
                        f"{github_base_url}/repos/{owner}/{repo}/pulls/{locking_pr}/commits", headers=headers
                    )
                    if c_resp.status_code == 200 and c_resp.json():
                        last = c_resp.json()[-1]
                        if last.get("author", {}).get("login") == pr_author_login:
                            resolved_email = last.get("commit", {}).get("author", {}).get("email") or None
    except Exception as e:
        activity.logger.warning("_resolve_locking_pr_user: GitHub API failed: %s", e)

    # Try to match resolved email against user_mst
    if resolved_email:
        try:
            row = (await db.execute(
                select(UserMstModel).where(UserMstModel.email_id == resolved_email, UserMstModel.is_deleted == False)
            )).scalars().first()
            if row:
                _, slack_id = await _resolve_slack_id_by_user_code(row.code, db, mapper)
                return {"display": f"{row.first_name} {row.last_name}", "slack_id": slack_id, "pr_url": pr_url}
        except Exception as e:
            activity.logger.warning("_resolve_locking_pr_user: email→user lookup failed: %s", e)

    # Fallback: GitHub handle only, no DM
    return {"display": pr_author_login or f"PR #{locking_pr}", "slack_id": None, "pr_url": pr_url}


@activity.defn
async def send_atlantis_lock_alert(
    user_code: str,
    pr_number: int | None,
    project_dirs: list,
    elapsed_minutes: int,
    is_final_warning: bool,
    waiting_user_codes: list | None = None,
    next_interval_minutes: int = 10,
    total_timeout_minutes: int = 60,
    locking_pr: int | None = None,
    repo_full_name: str | None = None,
    is_timeout: bool = False,
) -> None:
    """
    Slack alert for Atlantis lock conflict.
    - Channel: mentions @devops-on-call and the blocked user; shows lock holder as
      plain-text GitHub handle + clickable PR link (no notification to lock holder).
    - DM to blocked user only at T+0, or always when is_timeout=True.
    - is_timeout=True: timeout variant — past-tense message, always DMs blocked user.
    """
    from app.core.config import settings
    from app.db.session import AsyncSessionLocal
    from app.services.slack.user_mapper import SlackUserMapper
    from app.utils.github_app_token import get_token_for_org
    from slack_sdk.web.async_client import AsyncWebClient

    if not settings.deploy_alert_slack_bot_token:
        activity.logger.warning("send_atlantis_lock_alert: DEPLOY_ALERT_SLACK_BOT_TOKEN not set — skipping")
        return

    slack_client = AsyncWebClient(token=settings.deploy_alert_slack_bot_token)
    waiting_user_codes = waiting_user_codes or []
    dirs_text = "\n".join(f"  • `{d}`" for d in project_dirs)
    auto_clear_in = max(0, total_timeout_minutes - elapsed_minutes)
    blocked_pr_ctx = f"PR #{pr_number}" if pr_number else "your deployment"
    locking_pr_ctx = f"PR #{locking_pr}" if locking_pr else "an external PR"

    async with AsyncSessionLocal() as db:
        mapper = SlackUserMapper(db=db, slack_client=slack_client)

        # Resolve blocked user
        _, blocked_slack_id = await _resolve_slack_id_by_user_code(user_code, db, mapper)

        # Resolve lock holder — for display name + PR link only, no DM
        locker_info: dict | None = None
        if locking_pr and repo_full_name:
            try:
                org = repo_full_name.split("/")[0]
                github_token = await get_token_for_org(org, db)
                locker_info = await _resolve_locking_pr_user(
                    locking_pr, repo_full_name, db, mapper, github_token, settings.github_base_url
                )
            except Exception as e:
                activity.logger.warning("send_atlantis_lock_alert: locker resolution failed: %s", e)

    # ── Build text fragments ──────────────────────────────────────────────────
    # Lock holder: plain text name + clickable PR link — no <@> tag, no notification
    locker_display = locker_info["display"] if locker_info else locking_pr_ctx
    locker_pr_link = (
        f"<{locker_info['pr_url']}|{locking_pr_ctx}>" if locker_info and locker_info.get("pr_url")
        else locking_pr_ctx
    )

    blocked_mention = f"<@{blocked_slack_id}>" if blocked_slack_id else blocked_pr_ctx
    queued_count = len(waiting_user_codes)
    queued_line = f"\n*Also queued:* {queued_count} other deployment(s)" if queued_count else ""

    if is_timeout:
        title = "Atlantis Lock Timeout — Deployments Cleared"
        emoji = ":rotating_light:"
        timing_line = "⛔ All queued deployments have been *cleared*. Please redeploy once the lock is resolved."
    elif is_final_warning:
        title = "FINAL WARNING — Atlantis Lock Conflict"
        emoji = ":warning:"
        timing_line = f"⚠️ All queued deployments will be *discarded in {auto_clear_in} min* if not resolved."
    else:
        title = "Atlantis Lock Conflict"
        emoji = ":lock:"
        timing_line = f"*Next alert in:* {next_interval_minutes} min  |  *Clears in:* {auto_clear_in} min"

    # Channel message — @devops-on-call as plain text (user group is in the channel)
    channel_body = (
        f"@devops-on-call\n\n"
        f"*Blocked:* {blocked_mention} ({blocked_pr_ctx})\n"
        f"*Lock held by:* {locker_display} ({locker_pr_link})\n"
        f"\n*Directories:*\n{dirs_text}\n"
        f"\n*Lock held for:* {elapsed_minutes} min{queued_line}\n"
        f"\n{timing_line}\n"
        f"\n*Action Required:*\n"
        f"• Review and resolve {locker_pr_link} — merge or close based on its current state."
    )
    channel_msg = _fmt_alert(emoji, title, blocked_pr_ctx, channel_body)

    # DM to the blocked user only
    blocked_body = (
        f"*Blocked:* {blocked_mention} ({blocked_pr_ctx})\n"
        f"*Lock held by:* {locker_display} ({locker_pr_link})\n"
        f"\n*Directories:*\n{dirs_text}\n"
        f"\n*Lock held for:* {elapsed_minutes} min\n"
        f"\n{timing_line}\n"
        f"\n*Note:* @devops-on-call has been notified and will resolve the lock."
    )
    blocked_dm = _fmt_alert(emoji, title, blocked_pr_ctx, blocked_body)

    async def _dm(slack_id: str, text: str) -> None:
        try:
            ch = (await slack_client.conversations_open(users=[slack_id]))["channel"]["id"]
            await slack_client.chat_postMessage(channel=ch, text=text)
        except Exception as e:
            activity.logger.error("send_atlantis_lock_alert: DM to %s failed: %s", slack_id, e)

    if settings.slack_auto_apply_alert_channel:
        try:
            await slack_client.chat_postMessage(channel=settings.slack_auto_apply_alert_channel, text=channel_msg)
            activity.logger.info("Atlantis lock channel alert sent")
        except Exception as e:
            activity.logger.error("send_atlantis_lock_alert: channel post failed: %s", e)

    # DM blocked user at T+0 (first alert) or always on timeout
    if blocked_slack_id and (elapsed_minutes == 0 or is_timeout):
        await _dm(blocked_slack_id, blocked_dm)


# ── Activity 12: get_waiting_users_for_dirs ───────────────────────────────────

@activity.defn
async def get_waiting_users_for_dirs(tenant_code: str, project_dirs: List[str]) -> List[str]:
    """
    Query the TenantCoordinatorWorkflow for user_codes of all hold_queue entries
    whose required_dirs overlap with project_dirs.
    Activities can use the Temporal client freely (no determinism constraint).
    """
    from app.temporal.client import get_temporal_client
    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(f"coordinator-{tenant_code}")
        return await handle.query("get_waiting_users_for_dirs", project_dirs)
    except Exception as e:
        activity.logger.warning("get_waiting_users_for_dirs: query failed: %s", e)
        return []


# ── Activity 13: send_lock_timeout_queued_alert ───────────────────────────────

@activity.defn
async def send_lock_timeout_queued_alert(
    waiting_user_codes: List[str],
    project_dirs: list,
    locking_pr: int | None,
    total_timeout_minutes: int,
) -> None:
    """
    DM each queued user when the IaC lock timeout clears all queued deployments.
    Uses the same _fmt_alert structure as other alerts. No channel message —
    send_p0_alert already covers the channel at timeout.
    """
    from app.core.config import settings
    from app.db.session import AsyncSessionLocal
    from app.services.slack.user_mapper import SlackUserMapper
    from slack_sdk.web.async_client import AsyncWebClient

    if not settings.deploy_alert_slack_bot_token or not waiting_user_codes:
        return

    slack_client = AsyncWebClient(token=settings.deploy_alert_slack_bot_token)
    dirs_text = "\n".join(f"  • `{d}`" for d in project_dirs)
    locking_pr_ctx = f"PR #{locking_pr}" if locking_pr else "an external PR"
    timeout_hrs = round(total_timeout_minutes / 60, 1)

    title = "IaC Lock Timeout — Deployment Cleared"
    body = (
        f"*Your queued deployment for:*\n{dirs_text}\n\n"
        f"was automatically cleared because an IaC state lock held by *{locking_pr_ctx}* "
        f"was not resolved within *{timeout_hrs} hours*.\n\n"
        f"This was not caused by your PR.\n\n"
        f"*Action Required:* Please redeploy once the lock is resolved."
    )
    dm_msg = _fmt_alert(":rotating_light:", title, "IaC Lock Timeout", body)

    async with AsyncSessionLocal() as db:
        mapper = SlackUserMapper(db=db, slack_client=slack_client)
        for user_code in waiting_user_codes:
            try:
                _, slack_id = await _resolve_slack_id_by_user_code(user_code, db, mapper)
                if not slack_id:
                    continue
                ch = (await slack_client.conversations_open(users=[slack_id]))["channel"]["id"]
                await slack_client.chat_postMessage(channel=ch, text=dm_msg)
                activity.logger.info("send_lock_timeout_queued_alert: DM sent to %s", user_code)
            except Exception as e:
                activity.logger.error("send_lock_timeout_queued_alert: DM to %s failed: %s", user_code, e)


# The k8s-manifests repo gates merges behind a ruleset (required status checks
# + review). The workflow approves and merges back-to-back, so the merge often
# fires while those checks are still running — GitHub then answers 405
# "Repository rule violations found" and, with a single attempt, the PR was
# abandoned Open even though it went green seconds later. Retry the merge until
# the checks settle, bounded well under the activity's start_to_close_timeout so
# Temporal's own retry never has to take over.
MERGE_POLL_MAX_SECONDS = 150
MERGE_POLL_INTERVAL = 15


@activity.defn
async def merge_secondary_pr(pr_number: int, repo_full_name: str, pr_type: str) -> str:
    """
    Squash-merge a secondary (non-infrastructure) PR after the infra PR has been merged.

    Retries a not-yet-mergeable PR (required checks/review still settling) for up
    to MERGE_POLL_MAX_SECONDS before giving up. A merge conflict or a closed PR is
    terminal and returns immediately — only the "waiting on the ruleset" case waits.

    Returns one of:
      "merged"         — PR was successfully merged
      "already_merged" — PR was already in merged state
      "failed"         — merge attempt failed (logged, not raised)
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    if not repo_full_name or "/" not in repo_full_name:
        activity.logger.error("merge_secondary_pr: invalid repo_full_name=%s", repo_full_name)
        return "failed"

    owner, repo = repo_full_name.split("/", 1)

    activity.logger.info(
        "merge_secondary_pr: merging pr_number=%s repo=%s type=%s",
        pr_number, repo_full_name, pr_type,
    )

    deadline = asyncio.get_running_loop().time() + MERGE_POLL_MAX_SECONDS
    attempt = 0

    while True:
        attempt += 1
        activity.heartbeat(f"merge attempt {attempt} pr={pr_number}")
        try:
            async with AsyncSessionLocal() as db:
                component = GitOpsHandler.get_component("default", db)
                result = await component.merge_pull_request(
                    owner=owner,
                    repo=repo,
                    pull_number=pr_number,
                    merge_method="squash",
                )

            if result and result.get("merged"):
                activity.logger.info(
                    "merge_secondary_pr: merged pr_number=%s repo=%s attempt=%s",
                    pr_number, repo_full_name, attempt,
                )
                return "merged"

            # A dict with this message is GitHub saying "not mergeable" without
            # raising — resolve it against the PR's real state below, same as an
            # exception, rather than blindly calling it already-merged.
            transient_reason = (
                result.get("message") if isinstance(result, dict) else None
            ) or "unexpected merge result"

        except Exception as exc:
            transient_reason = str(exc)
            # A merge conflict is not a timing race and no amount of waiting fixes
            # it — fail now instead of burning the whole poll window.
            if "conflict" in transient_reason.lower():
                activity.logger.error(
                    "merge_secondary_pr: conflict pr_number=%s repo=%s: %s",
                    pr_number, repo_full_name, transient_reason,
                )
                return "failed"

        # Not merged this pass. Ask what the PR actually is before waiting.
        try:
            async with AsyncSessionLocal() as db:
                pr = await GitOpsHandler.get_pull_request(
                    tenant="default", owner=owner, repo=repo, pr_number=pr_number, db=db,
                )
            if pr.get("merged"):
                activity.logger.info(
                    "merge_secondary_pr: already merged pr_number=%s repo=%s",
                    pr_number, repo_full_name,
                )
                return "already_merged"
            if pr.get("state") == "closed":
                activity.logger.warning(
                    "merge_secondary_pr: pr_number=%s repo=%s is closed unmerged — not retrying",
                    pr_number, repo_full_name,
                )
                return "failed"
        except Exception as exc:
            # Can't read state — treat as still-open and let the deadline decide.
            activity.logger.warning(
                "merge_secondary_pr: state check failed pr_number=%s repo=%s: %s",
                pr_number, repo_full_name, exc,
            )

        if asyncio.get_running_loop().time() + MERGE_POLL_INTERVAL >= deadline:
            activity.logger.warning(
                "merge_secondary_pr: gave up after %ss (%s attempts) pr_number=%s repo=%s: %s",
                MERGE_POLL_MAX_SECONDS, attempt, pr_number, repo_full_name, transient_reason,
            )
            return "failed"

        activity.logger.info(
            "merge_secondary_pr: not mergeable yet pr_number=%s repo=%s (%s) — retrying in %ss",
            pr_number, repo_full_name, transient_reason, MERGE_POLL_INTERVAL,
        )
        await asyncio.sleep(MERGE_POLL_INTERVAL)


# ── Helpers for deployment DM notifications ───────────────────────────────────

import re as _re

def _parse_apply_outputs(comment_body: str) -> dict:
    """
    Extract key/value pairs from the Outputs: block of an Atlantis apply comment.
    Also extracts SQS queue URLs from creation log lines (SQS has no Outputs block).
    """
    outputs: dict = {}

    # Outputs: block (appears after "Apply complete! ...")
    outputs_match = _re.search(
        r'^Outputs:\s*\n(.*?)(?:\n\n|\Z)', comment_body, _re.DOTALL | _re.MULTILINE
    )
    if outputs_match:
        for line in outputs_match.group(1).splitlines():
            line = line.strip()
            if ' = ' not in line:
                continue
            key, _, val = line.partition(' = ')
            val = val.strip().strip('"')
            if val and '(known after apply)' not in val:
                outputs[key.strip()] = val

    # SQS: queue URLs appear only in creation log as [id=https://sqs...]
    sqs_urls = _re.findall(r'\[id=(https://sqs\.[^\]]+)\]', comment_body)
    if sqs_urls:
        def _is_dlq(url: str) -> bool:
            # FIFO DLQs end in "-dlq.fifo", standard DLQs in "-dlq"
            name = url.rstrip('/').rsplit('/', 1)[-1]
            return name.removesuffix('.fifo').endswith('-dlq')
        main_urls = [u for u in sqs_urls if not _is_dlq(u)]
        dlq_urls  = [u for u in sqs_urls if _is_dlq(u)]
        if main_urls:
            outputs['sqs_queue_url'] = main_urls[0]
        if dlq_urls:
            outputs['sqs_dlq_url'] = dlq_urls[0]

    # Apply summary line e.g. "Apply complete! Resources: 10 added, 0 changed, 0 destroyed."
    summary_m = _re.search(r'Apply complete!.*?Resources:.*', comment_body)
    if summary_m:
        outputs['_apply_summary'] = summary_m.group(0).strip()

    return outputs


def _format_outputs_for_dm(outputs: dict) -> str:
    """Format parsed Terraform outputs into Slack DM lines by resource type.

    Fallback formatter used when no config_snapshot is available; prefer
    _format_resource_for_dm, which renders even on no-op applies.
    """
    if not outputs:
        return ""

    lines: list[str] = []

    # EKS service
    if 'argo_application_name' in outputs and 'ecr_repository_name' in outputs:
        lines.append(f"• ECR Repo: `{outputs['ecr_repository_name']}`")
        if outputs.get('application_namespace'):
            lines.append(f"• Namespace: `{outputs['application_namespace']}`")
        lines.append(f"• ArgoCD App: `{outputs['argo_application_name']}`")
        if outputs.get('secretsmanager_secret_name'):
            lines.append(f"• Secrets: `{outputs['secretsmanager_secret_name']}`")
        if outputs.get('ssm_parameter_path_prefix'):
            lines.append(f"• Config prefix: `{outputs['ssm_parameter_path_prefix']}`")
        # eks-workload emits pod_identity_role_arn; eks-app emits irsa_role_arn
        iam_role = outputs.get('pod_identity_role_arn') or outputs.get('irsa_role_arn')
        if iam_role:
            lines.append(f"• IAM Role: `{iam_role}`")

    # ECS service
    elif 'service_arn' in outputs and 'ecr_repository_url' in outputs:
        lines.append(f"• ECR URL: `{outputs['ecr_repository_url']}`")
        if outputs.get('service_name'):
            lines.append(f"• ECS Service: `{outputs['service_name']}`")
        if outputs.get('cluster_arn'):
            lines.append(f"• Cluster: `{outputs['cluster_arn']}`")
        if outputs.get('target_group_name'):
            lines.append(f"• Target Group: `{outputs['target_group_name']}`")
        if outputs.get('task_definition_arn'):
            lines.append(f"• Task Def: `{outputs['task_definition_arn']}`")

    # S3 bucket
    elif 's3_bucket_name' in outputs:
        lines.append(f"• Bucket: `s3://{outputs['s3_bucket_name']}`")
        if outputs.get('s3_bucket_arn'):
            lines.append(f"• ARN: `{outputs['s3_bucket_arn']}`")

    # SQS queue
    elif 'sqs_queue_url' in outputs:
        lines.append(f"• Queue URL: `{outputs['sqs_queue_url']}`")
        if outputs.get('sqs_dlq_url'):
            lines.append(f"• DLQ URL: `{outputs['sqs_dlq_url']}`")

    # Aurora / RDS
    elif 'cluster_endpoint' in outputs or 'db_instance_endpoint' in outputs:
        endpoint = outputs.get('cluster_endpoint') or outputs.get('db_instance_endpoint', '')
        reader   = outputs.get('cluster_reader_endpoint', '')
        port     = outputs.get('cluster_port') or outputs.get('db_instance_port', '3306')
        if endpoint:
            lines.append(f"• Writer: `{endpoint}:{port}`")
        if reader:
            lines.append(f"• Reader: `{reader}:{port}`")
        if outputs.get('cluster_master_username') or outputs.get('db_master_username'):
            user = outputs.get('cluster_master_username') or outputs.get('db_master_username', '')
            lines.append(f"• Username: `{user}`")
        if outputs.get('cluster_database_name') or outputs.get('db_name'):
            db = outputs.get('cluster_database_name') or outputs.get('db_name', '')
            lines.append(f"• Database: `{db}`")

    # Aurora / RDS shared cluster (domain-based outputs)
    elif 'writer_domain' in outputs or 'reader_domain' in outputs:
        port = outputs.get('port', '3306')
        if outputs.get('writer_domain'):
            lines.append(f"• Writer: `{outputs['writer_domain']}`")
        if outputs.get('reader_domain'):
            lines.append(f"• Reader: `{outputs['reader_domain']}`")
        lines.append(f"• Port: `{port}`")

    # Redis / ElastiCache
    elif 'redis_endpoint' in outputs or 'primary_endpoint_address' in outputs:
        endpoint = outputs.get('redis_endpoint') or outputs.get('primary_endpoint_address', '')
        port     = outputs.get('redis_port') or outputs.get('port', '6379')
        lines.append(f"• Redis: `redis://{endpoint}:{port}`")

    # DynamoDB
    elif 'dynamodb_table_name' in outputs or 'table_name' in outputs:
        table = outputs.get('dynamodb_table_name') or outputs.get('table_name', '')
        lines.append(f"• Table: `{table}`")
        if outputs.get('table_arn'):
            lines.append(f"• ARN: `{outputs['table_arn']}`")

    # Generic fallback — show whatever keys came back
    else:
        for k, v in outputs.items():
            if k.startswith('_') or not v or '(known after apply)' in v:
                continue
            lines.append(f"• {k}: `{v}`")

    if outputs.get('_apply_summary'):
        lines.insert(0, f"_{outputs['_apply_summary']}_")

    return '\n'.join(lines)


def _collect_db_users(snap: dict) -> list[str]:
    """Usernames for a DB user-management deployment. Handles the single-user
    form (top-level `db_user_name`) and the multi-user form (nested under
    mysql_servers[].users[].db_user_name). Never reads passwords."""
    users: list[str] = []
    top = snap.get('db_user_name') or snap.get('username')
    if top:
        users.append(top)
    for key in ('mysql_servers', 'pgsql_servers'):
        for server in snap.get(key) or []:
            for u in server.get('users') or []:
                name = u.get('db_user_name') or u.get('username')
                if name:
                    users.append(name)
    # dedupe, preserve order
    return list(dict.fromkeys(users))


def _collect_db_servers(snap: dict) -> list[str]:
    """Server names for a DB deployment — top-level `db_server_name` plus any
    listed under mysql_servers/pgsql_servers."""
    servers: list[str] = []
    top = snap.get('db_server_name') or snap.get('server_name')
    if top:
        servers.append(top)
    for key in ('mysql_servers', 'pgsql_servers'):
        for server in snap.get(key) or []:
            name = server.get('db_server_name')
            if name:
                servers.append(name)
    return list(dict.fromkeys(servers))


def _format_resource_for_dm(snap: dict, outputs: dict) -> list[str]:
    """Resource-specific summary lines, driven by the deployment's config_snapshot
    (always present) and enriched with terraform outputs where available.

    Unlike the terraform Outputs block, the config_snapshot is populated even for
    no-op applies (0 added/changed), so every resource type renders something.
    """
    itype = (snap.get('infra_type') or '').lower()
    case  = (snap.get('case_ref_code') or '').lower()
    name  = snap.get('identifier') or snap.get('name') or ''
    lines: list[str] = []

    # EKS service (ArgoCD + ECR) — labels mirror the terraform outputs
    if 'eks' in itype or 'argo_application_name' in outputs:
        if outputs.get('ecr_repository_name'):
            lines.append(f"• ECR Repo: `{outputs['ecr_repository_name']}`")
        elif name:
            lines.append(f"• Service: `{name}`")
        if outputs.get('application_namespace'):
            lines.append(f"• Namespace: `{outputs['application_namespace']}`")
        if outputs.get('application_service_account_name'):
            lines.append(f"• Service Account: `{outputs['application_service_account_name']}`")
        if outputs.get('argo_application_name'):
            lines.append(f"• ArgoCD App: `{outputs['argo_application_name']}`")
        if outputs.get('secretsmanager_secret_name'):
            lines.append(f"• Secrets: `{outputs['secretsmanager_secret_name']}`")
        if outputs.get('ssm_parameter_path_prefix'):
            lines.append(f"• Config prefix: `{outputs['ssm_parameter_path_prefix']}`")
        # eks-workload emits pod_identity_role_arn; eks-app emits irsa_role_arn
        iam_role = outputs.get('pod_identity_role_arn') or outputs.get('irsa_role_arn')
        if iam_role:
            lines.append(f"• IAM Role: `{iam_role}`")

    # ECS service
    elif 'ecs' in itype or 'service_arn' in outputs:
        if outputs.get('ecr_repository_url'):
            lines.append(f"• ECR URL: `{outputs['ecr_repository_url']}`")
        service = outputs.get('service_name') or name
        if service:
            lines.append(f"• ECS Service: `{service}`")
        if outputs.get('cluster_arn'):
            lines.append(f"• Cluster: `{outputs['cluster_arn']}`")
        if outputs.get('target_group_name'):
            lines.append(f"• Target Group: `{outputs['target_group_name']}`")
        if outputs.get('task_definition_arn'):
            lines.append(f"• Task Def: `{outputs['task_definition_arn']}`")

    # S3 bucket
    elif itype == 's3' or case == 'create_bucket' or 's3_bucket_name' in outputs:
        bucket = outputs.get('s3_bucket_name') or name
        if bucket:
            lines.append(f"• Bucket: `s3://{bucket}`")
        if snap.get('versioning'):
            lines.append("• Versioning: `enabled`")
        if outputs.get('s3_bucket_arn'):
            lines.append(f"• ARN: `{outputs['s3_bucket_arn']}`")

    # SQS queue
    elif itype == 'sqs' or case == 'create_queue' or 'sqs_queue_url' in outputs:
        if name:
            lines.append(f"• Queue: `{name}`")
        if snap.get('fifo_queue'):
            lines.append("• Type: `FIFO`")
        queue_url = outputs.get('queue_url') or outputs.get('sqs_queue_url')
        dlq_url = outputs.get('dlq_url') or outputs.get('sqs_dlq_url')
        if queue_url:
            lines.append(f"• Queue URL: `{queue_url}`")
        if dlq_url:
            lines.append(f"• DLQ URL: `{dlq_url}`")
        elif snap.get('create_dlq'):
            lines.append("• DLQ: `enabled`")

    # DynamoDB table
    elif itype == 'dynamodb' or case == 'table_management' or 'dynamodb_table_name' in outputs or 'table_name' in outputs:
        table = outputs.get('dynamodb_table_name') or outputs.get('table_name') or name
        if table:
            lines.append(f"• Table: `{table}`")
        pk = snap.get('partition_key') or snap.get('hash_key')
        if pk:
            pkt = snap.get('partition_key_type') or snap.get('hash_key_type') or 'S'
            lines.append(f"• Partition key: `{pk} ({pkt})`")
        sk = snap.get('sort_key') or snap.get('range_key')
        if sk:
            skt = snap.get('sort_key_type') or snap.get('range_key_type') or 'S'
            lines.append(f"• Sort key: `{sk} ({skt})`")
        if snap.get('ttl_enabled') and snap.get('ttl_attribute_name'):
            lines.append(f"• TTL: `{snap['ttl_attribute_name']}`")
        if outputs.get('table_arn'):
            lines.append(f"• ARN: `{outputs['table_arn']}`")

    # Kong gateway route
    elif itype == 'kong_gateway' or case == 'add_route' or 'api_name' in snap:
        svc = snap.get('api_name') or snap.get('service_name')
        if svc:
            lines.append(f"• Service: `{svc}`")
        if snap.get('method'):
            lines.append(f"• Method: `{snap['method']}`")
        if snap.get('route'):
            lines.append(f"• Route: `{snap['route']}`")

    # Database creation / user management on a shared Aurora cluster
    elif (case in ('database_creation', 'user_management', 'mysql_user_management', 'postgresql_user_management')
          or snap.get('database_name') or snap.get('db_user_name') or snap.get('username')
          or snap.get('mysql_servers') or snap.get('pgsql_servers')
          or 'writer_domain' in outputs or 'reader_domain' in outputs):
        if snap.get('database_name'):
            lines.append(f"• Database: `{snap['database_name']}`")
        users = _collect_db_users(snap)
        if users:
            lines.append(f"• {'Users' if len(users) > 1 else 'User'}: `{', '.join(users)}`")
        servers = _collect_db_servers(snap)
        if servers:
            lines.append(f"• {'Servers' if len(servers) > 1 else 'Server'}: `{', '.join(servers)}`")
        if outputs.get('writer_domain'):
            lines.append(f"• Writer: `{outputs['writer_domain']}`")
        if outputs.get('reader_domain'):
            lines.append(f"• Reader: `{outputs['reader_domain']}`")
        if outputs.get('writer_domain') or outputs.get('reader_domain'):
            lines.append(f"• Port: `{outputs.get('port', '3306')}`")

    # Unknown type — show the resource name + whatever outputs came back
    else:
        if name:
            lines.append(f"• Resource: `{name}`")
        for k, v in outputs.items():
            if k.startswith('_') or not v or '(known after apply)' in str(v):
                continue
            lines.append(f"• {k}: `{v}`")

    return lines


def _canonical_locator_fields(infra_type_ref: str, outputs: dict) -> dict:
    """Map parsed apply outputs to the canonical locator keys for the given
    resource type (SQS / S3 / DynamoDB). Returns {} for other types."""
    t = (infra_type_ref or "").lower()
    fields: dict = {}

    if "sqs" in t:
        queue_url = outputs.get("queue_url") or outputs.get("sqs_queue_url")
        dlq_url = outputs.get("dlq_url") or outputs.get("sqs_dlq_url")
        if queue_url:
            fields["queue_url"] = queue_url
            fields["queue_name"] = queue_url.rstrip("/").rsplit("/", 1)[-1]
        if outputs.get("queue_arn"):
            fields["queue_arn"] = outputs["queue_arn"]
        if dlq_url:
            fields["dlq_url"] = dlq_url
            fields["dlq_name"] = dlq_url.rstrip("/").rsplit("/", 1)[-1]
        if outputs.get("dlq_arn"):
            fields["dlq_arn"] = outputs["dlq_arn"]

    elif "s3" in t:
        bucket = outputs.get("s3_bucket_name") or outputs.get("bucket_name")
        if bucket:
            fields["bucket_name"] = bucket
        arn = outputs.get("s3_bucket_arn") or outputs.get("bucket_arn")
        if arn:
            fields["bucket_arn"] = arn

    elif "dynamodb" in t:
        table = outputs.get("dynamodb_table_name") or outputs.get("table_name")
        if table:
            fields["table_name"] = table
        arn = outputs.get("dynamodb_table_arn") or outputs.get("table_arn")
        if arn:
            fields["table_arn"] = arn

    return fields


async def _save_outputs_to_locator(queue_ids: list[int], outputs: dict) -> None:
    """Persist ground-truth values from the Atlantis apply output into
    infrastructure_mst.locator for the deployed queue items.

    The locator is pre-populated at record-creation time with convention-derived
    predictions, which can diverge from what Terraform actually creates (each
    infra repo has its own naming scheme). The full output set is merged under
    locator['apply_outputs']; for SQS/S3/DynamoDB records the canonical locator
    fields (queue_url, bucket_name, table_name, ...) are overwritten with the
    real values. Shallow JSONB merge — all other locator keys are preserved.
    """
    from app.db.session import AsyncSessionLocal
    from app.repository.transaction_queue_repository import TransactionQueueRepository
    from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
    from app.core.enum import WorkflowSourceTableEnum

    apply_outputs = {k: v for k, v in outputs.items() if not k.startswith("_")}
    if not apply_outputs:
        return

    async with AsyncSessionLocal() as db:
        queue_repo = TransactionQueueRepository(db)
        infra_repo = InfrastructureMstRepository(db)
        entity_refs = await queue_repo.get_entity_refs_by_ids(queue_ids)
        infra_codes = [
            r["transaction_code"] for r in entity_refs
            if r["table_name"] == WorkflowSourceTableEnum.INFRASTRUCTURE and r["transaction_code"]
        ]
        for code in infra_codes:
            infra = await infra_repo.get_by_code(code)
            if not infra:
                continue
            fields = {"apply_outputs": apply_outputs}
            fields.update(_canonical_locator_fields(infra.infrastructuretype_ref_code, apply_outputs))
            await infra_repo.merge_locator_fields(code, fields)
            activity.logger.info(
                "_save_outputs_to_locator: locator updated for %s (%s)",
                code, ", ".join(fields.keys()),
            )
        await db.commit()


async def _send_slack_dm(user_code: str, message: str) -> None:
    """Resolve user_code → Slack user ID and send a DM. Errors are logged, not raised."""
    from app.core.config import settings
    from app.services.slack.user_mapper import SlackUserMapper
    from app.db.session import AsyncSessionLocal
    from app.db.models.user_mst_model import UserMstModel
    from sqlalchemy import select
    from slack_sdk.web.async_client import AsyncWebClient

    if not settings.deploy_alert_slack_bot_token:
        activity.logger.warning("_send_slack_dm: DEPLOY_ALERT_SLACK_BOT_TOKEN not set — skipping")
        return

    slack_client = AsyncWebClient(token=settings.deploy_alert_slack_bot_token)
    mapper = SlackUserMapper(db=None, slack_client=slack_client)
    slack_user_id: str | None = None

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(UserMstModel).where(
                    UserMstModel.code == user_code,
                    UserMstModel.is_deleted == False,
                )
            )
            user = result.scalars().first()
            if not user or not user.email_id:
                activity.logger.warning("_send_slack_dm: no user found for code=%s", user_code)
                return
            mapper.db = db
            slack_user_id = await mapper.get_slack_user_id_by_email(user.email_id)
    except Exception as exc:
        activity.logger.error("_send_slack_dm: failed to resolve Slack user for %s: %s", user_code, exc)
        return

    if not slack_user_id:
        activity.logger.warning("_send_slack_dm: no Slack ID resolved for user_code=%s", user_code)
        return

    try:
        dm = await slack_client.conversations_open(users=[slack_user_id])
        dm_channel = dm["channel"]["id"]
        await slack_client.chat_postMessage(channel=dm_channel, text=message)
        activity.logger.info("_send_slack_dm: DM sent to Slack user %s", slack_user_id)
    except Exception as exc: 
        activity.logger.error("_send_slack_dm: DM post failed for %s: %s", slack_user_id, exc)


# ── Activity 14: send_deployment_started_dm ───────────────────────────────────

@activity.defn
async def send_deployment_started_dm(
    user_code: str,
    pr_number: int | None,
    pr_url: str | None,
    project_name: str | None,
    queue_ids: List[int],
) -> None:
    """
    DM the deploying user to let them know their deployment has started.
    Looks up display_name for each queue item so the message is human-readable.
    """
    from app.db.session import AsyncSessionLocal
    from app.db.models.transaction_queue_model import TransactionQueueModel
    from sqlalchemy import select

    display_names: list[str] = []
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TransactionQueueModel.display_name, TransactionQueueModel.case_ref_code)
                .where(TransactionQueueModel.id.in_(queue_ids))
            )
            for row in result.all():
                name = row.display_name or row.case_ref_code or ""
                if name:
                    display_names.append(name)
    except Exception as exc:
        activity.logger.warning("send_deployment_started_dm: failed to fetch queue items: %s", exc)

    ctx = project_name or f"PR #{pr_number}" if pr_number else "deployment"
    pr_line = f"<{pr_url}|PR #{pr_number}>" if pr_url and pr_number else (f"PR #{pr_number}" if pr_number else "")
    items_line = "\n".join(f"  • {n}" for n in display_names) if display_names else ""

    body_parts = ["Your deployment is being planned and will apply shortly."]
    if pr_line:
        body_parts.append(f"*Infra PR:* {pr_line}")
    if items_line:
        body_parts.append(f"*Items:*\n{items_line}")

    msg = _fmt_alert(":rocket:", "Deployment Started", ctx, "\n".join(body_parts))
    await _send_slack_dm(user_code, msg)


# ── Activity 15: send_deployment_completed_dm ─────────────────────────────────

@activity.defn
async def send_deployment_completed_dm(
    user_code: str,
    pr_number: int | None,
    repo_full_name: str | None,
    pr_url: str | None,
    project_name: str | None,
    queue_ids: list[int] | None = None,
) -> None:
    """
    DM the deploying user with a deployment completion summary.
    Fetches the Atlantis apply comment from GitHub and parses the Outputs block
    to show resource-specific details (ECR URL, secret path, queue URL, etc.).
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    outputs: dict = {}
    if pr_number and repo_full_name and "/" in repo_full_name:
        try:
            owner, repo = repo_full_name.split("/", 1)
            async with AsyncSessionLocal() as db:
                comments = await GitOpsHandler.get_pr_comments(
                    tenant="default", owner=owner, repo=repo,
                    pr_number=pr_number, db=db,
                )
            for comment in reversed(comments):
                body = comment.get("body") or ""
                if "ran apply for" in body.lower() or "apply complete" in body.lower():
                    outputs = _parse_apply_outputs(body)
                    break
        except Exception as exc:
            activity.logger.warning(
                "send_deployment_completed_dm: failed to fetch apply comment: %s", exc
            )

    # Persist the real apply outputs into infrastructure_mst.locator — the
    # creation-time locator values are convention predictions and can diverge
    # from what Terraform actually created. Best-effort: never blocks the DM.
    if outputs and queue_ids:
        try:
            await _save_outputs_to_locator(queue_ids, outputs)
        except Exception as exc:
            activity.logger.warning(
                "send_deployment_completed_dm: locator update failed: %s", exc
            )

    # Load the deployment config_snapshot(s) — the reliable source for the
    # resource summary (terraform outputs are empty on no-op applies). Also
    # resolve the geo location code to its display name.
    snapshots: list[dict] = []
    geo_name: str | None = None
    if queue_ids:
        try:
            from app.repository.transaction_queue_repository import TransactionQueueRepository
            from app.repository.geo_loc_mst_repository import GeoLocMstRepository
            async with AsyncSessionLocal() as db:
                queue_repo = TransactionQueueRepository(db)
                geo_repo = GeoLocMstRepository(db)
                for qid in queue_ids:
                    item = await queue_repo.get_by_id(qid)
                    if not item:
                        continue
                    snap = dict(item.config_snapshot or {})
                    snap["case_ref_code"] = item.case_ref_code or ""
                    # infra_type lives on the queue row; not always copied into the snapshot
                    snap.setdefault("infra_type", getattr(item, "infra_type", "") or "")
                    snapshots.append(snap)
                geo_code = snapshots[0].get("geo_loc_mst_code") if snapshots else None
                if geo_code:
                    geo = await geo_repo.get_by_code(geo_code)
                    geo_name = geo.name if geo else geo_code
        except Exception as exc:
            activity.logger.warning(
                "send_deployment_completed_dm: failed to load config snapshot: %s", exc
            )

    ctx = project_name or f"PR #{pr_number}" if pr_number else "deployment"
    pr_line = f"<{pr_url}|PR #{pr_number}>" if pr_url and pr_number else (f"PR #{pr_number}" if pr_number else "")

    body_parts = [":white_check_mark: Your deployment has been applied and merged successfully!"]
    if pr_line:
        body_parts.append(f"*Infra PR:* {pr_line}")

    # Product / Environment / Region context (shared across the deployment)
    if snapshots:
        s0 = snapshots[0]
        meta_bits = []
        if s0.get("product_name"):
            meta_bits.append(f"*Product:* {s0['product_name']}")
        if s0.get("environment"):
            meta_bits.append(f"*Environment:* {s0['environment']}")
        if geo_name:
            meta_bits.append(f"*Region:* {geo_name}")
        if meta_bits:
            body_parts.append("   ".join(meta_bits))

    # Resource summary — config-driven, enriched with terraform outputs
    resource_lines: list[str] = []
    if outputs.get("_apply_summary"):
        resource_lines.append(f"_{outputs['_apply_summary']}_")
    if snapshots:
        for snap in snapshots:
            resource_lines.extend(_format_resource_for_dm(snap, outputs))
    else:
        # No config available (older call sites) — fall back to outputs only
        fallback = _format_outputs_for_dm(outputs)
        if fallback:
            resource_lines = [fallback]
    if resource_lines:
        body_parts.append("\n*Resources:*\n" + "\n".join(resource_lines))

    msg = _fmt_alert(":tada:", "Deployment Completed", ctx, "\n".join(body_parts))
    await _send_slack_dm(user_code, msg)


# ── Activity: update_pipeline_run_track_stage ────────────────────────────────

@activity.defn
async def update_pipeline_run_track_stage(
    vendor_deployment_id: str,
    stage_name: str,
    stage_status: str,
    started_at: str,
    error_message: str | None = None,
    group: str | None = None,
) -> None:
    """
    Append or update a stage entry in build_stages for all pipeline_run_track
    rows matching vendor_deployment_id (Temporal workflow_id).

    stage_status: "running" | "success" | "failed"
    started_at:   ISO 8601 string from workflow.now().isoformat(). On a
                  terminal update (success/failed) this call-time timestamp is
                  recorded on the entry as ended_at.
    error_message: optional reason recorded on the entry when a stage fails.
    group: optional label (e.g. "service" | "kong") recorded on the entry —
           tells apart which DeploymentWorkflow child produced it when several
           share ONE multi-deploy run-track row. Omitted entirely (no "group"
           key at all) when None, so standalone deploys and pre-existing rows
           are untouched — the FE groups only when the field is present and
           renders flat otherwise.
    """
    from app.db.session import AsyncSessionLocal
    from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
    from app.core.enum import PipelineRunStatusEnum
    import app.db.models  # noqa: F401

    async with AsyncSessionLocal() as db:
        repo = PipelineRunTrackRepository(db)
        rows = await repo.get_by_vendor_deployment_id(vendor_deployment_id)

        if not rows:
            activity.logger.warning(
                "update_pipeline_run_track_stage: no rows for vendor_deployment_id=%s",
                vendor_deployment_id,
            )
            return

        for row in rows:
            stages = list(row.build_stages or [])

            if stage_status == "running":
                # Skip if the last entry is this same stage still running —
                # duplicate plan-failed signals (e.g. Atlantis posting two
                # failure comments for one run) can re-enter a stage we are
                # already parked in. A genuine rerun closes the previous entry
                # first, so it still appends.
                last = stages[-1] if stages else None
                if last and last.get("name") == stage_name and last.get("status") == "running":
                    continue
                entry = {"name": stage_name, "status": "running", "started_at": started_at}
                if group:
                    entry["group"] = group
                stages.append(entry)
            else:
                # Find the LAST entry with this name and update it. The call-time
                # timestamp of a terminal update is the stage's end time.
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
                    if group:
                        stages[last_idx]["group"] = group
                else:
                    # Single-shot marker (no prior running phase) — one instant,
                    # so started_at == ended_at.
                    entry = {
                        "name": stage_name, "status": stage_status,
                        "started_at": started_at, "ended_at": started_at,
                    }
                    if error_message:
                        entry["error"] = error_message
                    if group:
                        entry["group"] = group
                    stages.append(entry)

            final_status = None
            if stage_status == "failed":
                final_status = PipelineRunStatusEnum.FAILED
            elif stage_name == "completed" and stage_status == "success":
                final_status = PipelineRunStatusEnum.COMPLETED

            await repo.update(
                code=row.code,
                build_stages=stages,
                status=final_status,
            )

        activity.logger.info(
            "update_pipeline_run_track_stage: vendor_deployment_id=%s stage=%s status=%s rows=%d",
            vendor_deployment_id, stage_name, stage_status, len(rows),
        )


@activity.defn
async def update_pipeline_run_track_deploy_result(
    vendor_deployment_id: str,
    patch: Dict[str, Any],
) -> None:
    """
    Merge `patch` into deploy_result for all pipeline_run_track rows matching
    vendor_deployment_id (Temporal workflow_id). Existing keys written by other
    producers (e.g. the Jenkins webhook's alb_url/build_url) are preserved
    unless the patch carries the same key — EXCEPT "prs", which is additive:
    a multi-deploy batch's shared row receives "prs" writes from MULTIPLE
    DeploymentWorkflow children (service, kong, ...) against the SAME row, so
    a plain overwrite would drop every PR but the last writer's. Deduped by
    (repo, number) so a retried activity re-adding its own PRs can't create
    duplicate entries.
    """
    from app.db.session import AsyncSessionLocal
    from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
    import app.db.models  # noqa: F401

    async with AsyncSessionLocal() as db:
        repo = PipelineRunTrackRepository(db)
        rows = await repo.get_by_vendor_deployment_id(vendor_deployment_id)

        if not rows:
            activity.logger.warning(
                "update_pipeline_run_track_deploy_result: no rows for vendor_deployment_id=%s",
                vendor_deployment_id,
            )
            return

        for row in rows:
            current = row.deploy_result or {}
            merged = {**current, **patch}
            if "prs" in patch:
                existing_prs = current.get("prs") or []
                seen = {(p.get("repo"), p.get("number")) for p in existing_prs}
                merged["prs"] = existing_prs + [
                    p for p in patch["prs"]
                    if (p.get("repo"), p.get("number")) not in seen
                ]
            await repo.update(code=row.code, deploy_result=merged)

        activity.logger.info(
            "update_pipeline_run_track_deploy_result: vendor_deployment_id=%s keys=%s rows=%d",
            vendor_deployment_id, list(patch.keys()), len(rows),
        )


# ── Activity: save_service_alb_url ───────────────────────────────────────────

# Shared application ALBs by (application, environment). Services deployed via
# Temporal/Atlantis ride the per-application ALB — there is no Jenkins webhook
# to report a URL, so it is taken from this map on the service's first deploy.
# A service that needs its own ALB gets `config.alb_url` set by hand instead;
# the write-once rule below keeps that value through every redeploy.
SERVICE_ALB_URL_MAP: Dict[tuple, str] = {
    ("core", "stage"): "https://vance-core-stage-mumbai-01-common-application-alb.internal.genorim.xyz",
}


@activity.defn
async def save_service_alb_url(vendor_deployment_id: str, queue_ids: List[int]) -> None:
    """
    After a successful infra deployment, persist the shared application ALB
    into service_config.config["alb_url"] for every SERVICE_CONFIG item in
    the deployment that has an ALB route — mirroring what the Jenkins final
    webhook does for Jenkins deploys. The ALB alone is stored: the service
    path and health path are joined on at read time (service_urls).

    Written once. A row that already holds an alb_url — the first deploy's,
    or one set by hand for a service on its own ALB — is left alone. Rows
    without an ALB route (workers, model servers, no_alb) and unmapped
    (application, environment) combinations are skipped.
    """
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.db.models.transaction_queue_model import TransactionQueueModel
    from app.db.models.service_config_model import ServiceConfigModel
    from app.db.models.services_mst_model import ServicesMstModel
    from app.db.models.applications_mst_model import ApplicationsMstModel
    from app.repository.service_config_repository import ServiceConfigRepository
    from app.core.enum import WorkflowSourceTableEnum
    from app.utils.service_routing import alb_base_to_write
    import app.db.models  # noqa: F401

    async with AsyncSessionLocal() as db:
        stmt = (
            select(
                ServiceConfigModel.code,
                ApplicationsMstModel.name,
                ServiceConfigModel.environment,
                ServiceConfigModel.config,
                ServicesMstModel.service_type,
                ServiceConfigModel.alb_selection,
            )
            .select_from(TransactionQueueModel)
            .join(ServiceConfigModel, ServiceConfigModel.code == TransactionQueueModel.transaction_code)
            .join(ServicesMstModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
            .join(ApplicationsMstModel, ApplicationsMstModel.code == ServicesMstModel.applications_mst_code)
            .where(
                TransactionQueueModel.id.in_(queue_ids),
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG,
            )
            .distinct()
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            activity.logger.info(
                "save_service_alb_url: no SERVICE_CONFIG queue rows for %s — skipping",
                vendor_deployment_id,
            )
            return

        repo = ServiceConfigRepository(db)
        saved = 0
        for sc_code, app_name, environment, config, service_type, alb_selection in rows:
            app_key = (app_name or "").strip().lower()
            env_key = (environment.value if hasattr(environment, "value") else str(environment or "")).strip().lower()
            base_url, reason = alb_base_to_write(
                config if isinstance(config, dict) else {},
                mapped_base=SERVICE_ALB_URL_MAP.get((app_key, env_key)),
                service_type=service_type,
                alb_selection=alb_selection,
            )
            if not base_url:
                activity.logger.info(
                    "save_service_alb_url: %s — %s (application=%s env=%s), skipping",
                    sc_code, reason, app_key, env_key,
                )
                continue

            await repo.update_alb_url_by_codes([sc_code], base_url)
            saved += 1
            activity.logger.info(
                "save_service_alb_url: %s → %s (application=%s env=%s)",
                sc_code, base_url, app_key, env_key,
            )

        if saved:
            await db.commit()


# ── Activity: verify_plan_with_ai ────────────────────────────────────────────

@activity.defn
async def verify_plan_with_ai(
    pr_number: int,
    repo_full_name: str,
    not_before: str | None = None,
) -> Dict[str, Any]:
    """
    Reads the full Atlantis plan output from the PR and asks an LLM whether the
    plan is destructive (destroys or replaces existing resources).

    Returns a JSON-serializable dict:
      {
        "plan_found": bool,        # False if no plan comment could be read
        "is_destructive": bool,
        "severity": str,
        "destroyed_resources": [str],
        "replaced_resources": [str],
        "sensitive_resources": [str],
        "summary": str,
        "rationale": str,
      }
    When no plan content is found, returns plan_found=False / is_destructive=False
    so the workflow proceeds (the plan already succeeded by the time we get here).
    """
    from app.core.config import settings
    from app.integrations.openai_integration import OpenAIIntegration
    from langchain_core.messages import SystemMessage, HumanMessage

    activity.logger.info(f"verify_plan_with_ai: reading plan for PR #{pr_number} ({repo_full_name})")

    plan_body = await _get_latest_plan_comment_body(pr_number, repo_full_name, not_before)
    if not plan_body:
        activity.logger.warning(f"verify_plan_with_ai: no plan comment found for PR #{pr_number} — skipping verification")
        return {
            "plan_found": False,
            "is_destructive": False,
            "severity": "none",
            "destroyed_resources": [],
            "replaced_resources": [],
            "sensitive_resources": [],
            "summary": "No plan output could be read; verification skipped.",
            "rationale": "The Atlantis plan comment body was not available to inspect.",
        }

    # Cap the plan size sent to the model — Atlantis plans are usually small, but
    # guard against pathological diffs blowing the context window.
    MAX_PLAN_CHARS = 60000
    if len(plan_body) > MAX_PLAN_CHARS:
        plan_body = plan_body[:MAX_PLAN_CHARS] + "\n...[plan output truncated]..."

    system_prompt = _PLAN_VERIFY_SYSTEM_PROMPT.format(
        sensitive_types=", ".join(SENSITIVE_RESOURCE_TYPES),
        sensitive_prefixes=", ".join(SENSITIVE_ADDRESS_PREFIXES),
    )

    llm = OpenAIIntegration.get_chat_client(temperature=0.0, model=settings.openai_model)
    structured_llm = llm.with_structured_output(PlanVerificationResult)
    verdict: PlanVerificationResult = await structured_llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"Here is the full atlantis plan output:\n\n{plan_body}"),
    ])

    result = verdict.model_dump()
    result["plan_found"] = True

    # Deterministic backstop: ensure obvious sensitive destroys/replaces are flagged
    # even if the model omitted them.
    all_destructive = list(result.get("destroyed_resources") or []) + list(result.get("replaced_resources") or [])
    backstop = _classify_sensitive(all_destructive)
    merged_sensitive = list(dict.fromkeys(list(result.get("sensitive_resources") or []) + backstop))
    result["sensitive_resources"] = merged_sensitive

    activity.logger.info(
        "verify_plan_with_ai: PR #%s → destructive=%s severity=%s destroy=%d replace=%d sensitive=%d",
        pr_number, result["is_destructive"], result.get("severity"),
        len(result.get("destroyed_resources") or []), len(result.get("replaced_resources") or []),
        len(merged_sensitive),
    )
    return result


# ── Activity: post_plan_verification_comment ─────────────────────────────────

@activity.defn
async def post_plan_verification_comment(
    pr_number: int,
    repo_full_name: str,
    verdict: Dict[str, Any],
) -> None:
    """
    Posts the AI plan-verification result as a comment on the PR.

    Renders a 🛑 "destructive changes detected" report when verdict.is_destructive
    is True, or a ✅ "verified — no destructive changes" report when it is False.
    """
    def _fmt_list(items: List[str]) -> str:
        return "\n".join(f"  - `{a}`" for a in items) if items else "  - _(none)_"

    severity = (verdict.get("severity") or "unknown").upper()

    if verdict.get("is_destructive"):
        lines = [
            "## 🛑 AI Plan Verification — Destructive Changes Detected",
            "",
            f"**Severity:** {severity}",
            f"**Summary:** {verdict.get('summary') or 'N/A'}",
            "",
            "**Resources to be destroyed (`-`):**",
            _fmt_list(verdict.get("destroyed_resources") or []),
            "",
            "**Resources to be replaced (`-/+`):**",
            _fmt_list(verdict.get("replaced_resources") or []),
        ]
        sensitive = verdict.get("sensitive_resources") or []
        if sensitive:
            lines += [
                "",
                "**⚠️ Stateful / sensitive resources affected:**",
                _fmt_list(sensitive),
            ]
        lines += [
            "",
            f"**Why:** {verdict.get('rationale') or 'N/A'}",
            "",
            "---",
            "This deployment is **paused before approval**. Review the changes above. "
            "If the destruction is unintended, push a fix — Atlantis will re-plan and this "
            "check will run again. The deployment will not proceed to apply while the plan is destructive.",
        ]
    else:
        lines = [
            "## ✅ AI Plan Verification — No Destructive Changes",
            "",
            f"**Summary:** {verdict.get('summary') or 'No resources will be destroyed or replaced.'}",
            "",
            "The plan only creates (`+`) and/or updates in place (`~`) — no existing resources "
            "are destroyed (`-`) or replaced (`-/+`).",
            "",
            "---",
            "Verification passed — the deployment is proceeding to approval and apply.",
        ]

    body = "\n".join(lines)

    activity.logger.info(
        f"post_plan_verification_comment: posting {'destructive' if verdict.get('is_destructive') else 'clean'} result on PR #{pr_number}"
    )
    await _post_pr_comment(pr_number, repo_full_name, "default", body)
    activity.logger.info(f"post_plan_verification_comment: posted on PR #{pr_number} ✓")
