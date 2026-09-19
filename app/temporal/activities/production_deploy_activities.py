"""
Activities used only by ProductionDeploymentWorkflow (prod promotion flow).

Prod ships through ONE shared stage→main promotion PR per repo. These
activities cover what the stage/qa activity set doesn't have:

  - ensure_promotion_pr        find-or-create the stage→main PR (reuse-safe)
  - merge_pr_direct            phase-1 merge of the feature PR to stage, no
                               plan/apply (workflow calls approve_pr first —
                               branch protection blocks unapproved merges)
  - production_track_*         the production_deployment_track lifecycle (the
                               merge gate's source of truth — see the model)
  - evaluate_merge_gate        classify the promotion PR's PRODUCTION paths
                               dir-by-dir (non-prod paths are inert on main
                               and never block)
  - merge_promotion_pr         SHA-conditional merge into main
  - check_manual_commit_on_dirs  did anything land since our plan that touches
                               OUR dirs? Same-dir deploys are lock-serialized,
                               so any such touch is a MANUAL commit → the
                               workflow aborts, same policy as stage.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from temporalio import activity

logger = logging.getLogger(__name__)

# DevLift's GitHub App bot identities. Commits by these are DevLift's own
# writes (conflict resolutions, another deploy's gateway regen) — never
# "manual commits". Kept here as the single source; the webhook's bot-push
# filter uses the same names.
DEVLIFT_BOT_LOGINS = ("devlift-regobs[bot]", "devlift-ai[bot]")


def _owner_repo(repo_full_name: str) -> tuple[str, str]:
    if "/" not in repo_full_name:
        raise ValueError(f"repo_full_name must be 'owner/repo', got: {repo_full_name}")
    owner, repo = repo_full_name.split("/", 1)
    return owner, repo


def _is_production_path(path: str) -> bool:
    """
    Only production content participates in the merge gate.

    The promotion PR's diff (stage vs main) also carries stage/qa env changes
    and docs — inert on main. Blocking the merge on those would freeze
    promotions for content that cannot affect production.

    THREE kinds of path can change production, and all three count:

    1. Under a prod environment family — environment/<family>/... with "prod"
       in the family name. The project dirs themselves, and the region-level
       env.hcl / terragrunt.hcl that 390 projects in core-prod-01 include.

    2. Anything under layers/. Production projects source these modules
       directly (`source = "../../../../../layers//ecs"`) — in core-prod-01 +
       falcon-prod-01 alone: 126 queues, 90 ECS services, 50 EKS workloads, 13
       databases, and more. A layer edit changes what the NEXT production
       apply produces, for any project, whoever deploys it. It is also invisible
       to the dir-scoped manual-commit check, which only watches the deploying
       dirs — so before this, layer changes reached main completely ungated and
       first surfaced as a destructive plan on an unrelated service.

    3. Shared .hcl directly under environment/ — environment/_pagerduty.hcl is
       included by 16 production projects. The .hcl test matters: it keeps
       environment/CLAUDE.md from blocking merges.

    atlantis.yaml is a DELIBERATE exception. It does affect production (it
    defines the projects and their branch filters), but DevLift appends to it
    as part of its own deployments and those edits never carry a track row —
    counting it would make every DevLift deployment block itself.

      environment/core-prod-01/eu-west-2/services/x/terragrunt.hcl  → True
      environment/core-prod-01/eu-west-2/env.hcl                    → True
      layers/ecs/main.tf                                            → True
      environment/_pagerduty.hcl                                    → True
      environment/core-stage-01/ap-south-1/buckets/y/terragrunt.hcl → False
      environment/CLAUDE.md                                         → False
      atlantis.yaml                                                 → False
      dockers/base/Dockerfile                                       → False
    """
    parts = path.split("/")
    if not parts:
        return False
    if parts[0] == "layers":
        return True
    if parts[0] == "environment":
        if len(parts) == 2:
            return parts[1].endswith(".hcl")   # shared config, not docs
        return len(parts) >= 2 and "prod" in parts[1]
    return False


# ── Promotion PR ─────────────────────────────────────────────────────────────

@activity.defn
async def ensure_promotion_pr(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Find the open stage→main promotion PR, or create it.

    params: {tenant_code, repo_full_name, source_branch, target_branch}
    Returns: {pr_number, pr_url, head_sha, created: bool}

    Reuse handling (two layers, both land here — the shared GitHub code is
    deliberately untouched):
      1. component.create_pr pre-finds an open PR and returns it — the
         normal reuse path.
      2. The creation race (two callers create simultaneously, or the find
         missed): GitHub answers 422 "A pull request already exists for
         stage:main." → surfaces as an error string here → we look the open
         PR up and reuse it.
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    tenant = params["tenant_code"]
    repo_full_name = params["repo_full_name"]
    source = params.get("source_branch") or "stage"
    target = params.get("target_branch") or "main"
    owner, repo = _owner_repo(repo_full_name)

    async with AsyncSessionLocal() as db:
        component = GitOpsHandler.get_component(tenant, db)
        result = await component.create_pr(
            owner=owner,
            repo=repo,
            base_branch=target,
            feature_branch=source,
            pr_title=f"Promote {source} to {target} (DevLift production deployment)",
            pr_body=(
                "Automated production promotion PR managed by DevLift.\n\n"
                f"Carries everything merged to `{source}` that has not yet been "
                f"promoted to `{target}`. DevLift plans and applies its own "
                "projects here with `atlantis plan/apply -p <project>` and "
                "auto-merges only when every changed production dir is "
                "DevLift-applied."
            ),
        )

        pr_number = result.get("pr_number")
        created = pr_number is not None and "found" not in (result.get("message") or "").lower()

        if not pr_number:
            error_text = (result.get("error") or result.get("message") or "").lower()
            if "already exists" not in error_text:
                raise RuntimeError(
                    f"ensure_promotion_pr: could not find or create {source}->{target} "
                    f"PR in {repo_full_name}: {result.get('error') or result}"
                )
            # Creation race — the PR exists; fetch it to reuse.
            pr_number = await GitOpsHandler.find_open_pr_by_branch(
                tenant=tenant, owner=owner, repo=repo,
                feature_branch=source, base_branch=target, db=db,
            )
            if not pr_number:
                # Exists per GitHub, yet not findable as open — closed in the
                # same instant. Let the workflow retry the whole activity.
                raise RuntimeError(
                    f"ensure_promotion_pr: {source}->{target} PR exists but is "
                    f"not open in {repo_full_name} — retry"
                )
            created = False

        head_sha = result.get("head_sha")
        pr_url = result.get("pr_url")
        if not head_sha or not pr_url:
            # Reuse paths may not carry head SHA / URL — the workflow needs
            # both (manual-commit check, gate, user-facing links).
            pr_info = await GitOpsHandler.get_pull_request(
                tenant=tenant, owner=owner, repo=repo, pr_number=pr_number, db=db,
            )
            head_sha = head_sha or pr_info.get("head_sha")
            pr_url = pr_url or pr_info.get("html_url")

    activity.logger.info(
        "ensure_promotion_pr: %s PR #%s (%s->%s) head=%s",
        "created" if created else "reusing", pr_number, source, target,
        (head_sha or "")[:8],
    )
    return {
        "pr_number": pr_number,
        "pr_url": pr_url,
        "head_sha": head_sha,
        "created": created,
    }


@activity.defn
async def merge_pr_direct(params: Dict[str, Any]) -> str:
    """
    Phase-1 merge: feature PR → stage with NO plan/apply. Atlantis's prod
    projects only watch /main/, so this PR is invisible to it — nothing to
    plan here by design.

    The workflow MUST call the existing approve_pr activity first: stage has
    required-review branch protection, and an unapproved merge is refused.

    params: {tenant_code, repo_full_name, pr_number}
    Returns: "merged" | "already_merged" | "conflict" | "failed"

    "conflict" is a value, not an exception, so the workflow can route it to
    the existing resolve_conflict_and_merge path (same as stage's phase-1
    conflicts — atlantis.yaml appends etc.).
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    tenant = params["tenant_code"]
    repo_full_name = params["repo_full_name"]
    pr_number = params["pr_number"]
    owner, repo = _owner_repo(repo_full_name)

    async with AsyncSessionLocal() as db:
        # Crash-safe: if a previous attempt merged and died before reporting,
        # the retry must not fail on "PR not mergeable".
        pr_info = await GitOpsHandler.get_pull_request(
            tenant=tenant, owner=owner, repo=repo, pr_number=pr_number, db=db,
        )
        if pr_info.get("merged"):
            activity.logger.info("merge_pr_direct: PR #%s already merged", pr_number)
            return "already_merged"

        try:
            result = await GitOpsHandler.merge_pull_request(
                tenant=tenant, owner=owner, repo=repo,
                pull_number=pr_number, db=db, merge_method="merge",
            )
        except Exception as e:
            err = str(e).lower()
            if "conflict" in err:
                activity.logger.warning("merge_pr_direct: PR #%s has conflicts", pr_number)
                return "conflict"
            activity.logger.error("merge_pr_direct: PR #%s failed: %s", pr_number, e)
            return "failed"

    return "merged" if result.get("merged") else "failed"


# ── production_deployment_track lifecycle ────────────────────────────────────

@activity.defn
async def production_track_upsert(params: Dict[str, Any]) -> None:
    """
    Claim dirs for this deployment in production_deployment_track.

    params: {dirs: [..], workflow_id, tenant_code, repo_full_name, status}

    Insert a row per dir, or take over an existing one — same-dir deploys are
    serialized by the coordinator locks, so a live row here is a previous
    deploy's leftover being superseded (its APPLIED/FAILED state is now stale:
    OUR new content is about to land on stage unapplied).

    MUST be called BEFORE the feature PR merges to stage: the row has to
    exist before the content can possibly appear in the promotion PR, so the
    merge gate's "in the diff with no row = manual content" stays reliable.
    """
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.db.models.production_deployment_track_model import ProductionDeploymentTrackModel
    from app.core.enum import ProductionDeploymentStatusEnum
    import app.db.models  # noqa: F401

    dirs: List[str] = params["dirs"]
    status = ProductionDeploymentStatusEnum(params.get("status") or "IN_PROGRESS")

    async with AsyncSessionLocal() as db:
        for d in dirs:
            row = (await db.execute(
                select(ProductionDeploymentTrackModel).where(
                    ProductionDeploymentTrackModel.dir == d
                )
            )).scalar_one_or_none()
            if row is None:
                row = ProductionDeploymentTrackModel(
                    code=f"pdt-{uuid.uuid4().hex[:12]}",
                    name=d.rsplit("/", 1)[-1][:255],
                    dir=d,
                    workflow_id=params["workflow_id"],
                    tenant_code=params["tenant_code"],
                    repo_full_name=params["repo_full_name"],
                    status=status,
                )
                db.add(row)
            else:
                row.workflow_id = params["workflow_id"]
                row.tenant_code = params["tenant_code"]
                row.repo_full_name = params["repo_full_name"]
                row.status = status
        await db.commit()

    activity.logger.info(
        "production_track_upsert: %d dir(s) -> %s (wf=%s)",
        len(dirs), status.value, params["workflow_id"],
    )


@activity.defn
async def production_track_set_status(params: Dict[str, Any]) -> None:
    """
    Flip this workflow's track rows to a new status.

    params: {dirs: [..], workflow_id, status}
      APPLIED  — after a successful apply (dir is now mergeable)
      RESUMING — parked deploy's manual plan succeeded, re-queued for apply
      FAILED   — deploy terminated with content unapplied on stage

    Scoped to workflow_id on purpose: if this deploy was superseded, the
    successor has already re-claimed the dir with its own workflow_id — our
    late write must match zero rows rather than clobber theirs.
    """
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.db.models.production_deployment_track_model import ProductionDeploymentTrackModel
    from app.core.enum import ProductionDeploymentStatusEnum
    import app.db.models  # noqa: F401

    status = ProductionDeploymentStatusEnum(params["status"])

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ProductionDeploymentTrackModel).where(
                ProductionDeploymentTrackModel.dir.in_(params["dirs"]),
                ProductionDeploymentTrackModel.workflow_id == params["workflow_id"],
            )
        )).scalars().all()
        for row in rows:
            row.status = status
        await db.commit()

    activity.logger.info(
        "production_track_set_status: %d row(s) -> %s (wf=%s)",
        len(rows), status.value, params["workflow_id"],
    )


@activity.defn
async def production_track_delete_dirs(params: Dict[str, Any]) -> int:
    """
    Drop rows for dirs that just merged into main — promoted, nothing pending.

    params: {repo_full_name, dirs: [..]}
    Returns: number of rows deleted.
    """
    from sqlalchemy import delete
    from app.db.session import AsyncSessionLocal
    from app.db.models.production_deployment_track_model import ProductionDeploymentTrackModel
    import app.db.models  # noqa: F401

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(ProductionDeploymentTrackModel).where(
                ProductionDeploymentTrackModel.repo_full_name == params["repo_full_name"],
                ProductionDeploymentTrackModel.dir.in_(params["dirs"]),
            )
        )
        await db.commit()

    activity.logger.info("production_track_delete_dirs: %d row(s) dropped", result.rowcount)
    return result.rowcount


# ── Merge gate ───────────────────────────────────────────────────────────────

@activity.defn
async def evaluate_merge_gate(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Decide whether the promotion PR may be auto-merged.

    params: {tenant_code, repo_full_name, pr_number, my_dirs: [..]}

    Rule: every changed PRODUCTION path must belong to MY dirs or to a dir
    whose track row is APPLIED. Non-production paths (stage/qa envs,
    atlantis.yaml, layers/, docs) are inert on main and never block.

    Returns:
      {
        "mergeable": bool,
        "head_sha": str,          # evaluated head — condition the merge on it
        "merged_dirs": [..],      # mine + APPLIED dirs in the diff → delete
                                  # these track rows after a successful merge
        "blockers": [ {dir, kind: "devlift"|"manual", status, workflow_id} ],
        "applied_waiting": [..],  # other deploys' APPLIED dirs riding this merge
      }

    Orphan healing: an IN_PROGRESS/RESUMING row whose workflow is no longer
    Running was force-killed (the 24h auto-terminate flips its own row — a
    killed workflow can't) — flipped to FAILED here so the blocker alert says
    the truth. Best-effort: if the Temporal lookup fails, the gate still
    blocks correctly, only the wording is less precise.
    """
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler
    from app.db.models.production_deployment_track_model import ProductionDeploymentTrackModel
    from app.core.enum import ProductionDeploymentStatusEnum
    import app.db.models  # noqa: F401

    tenant = params["tenant_code"]
    repo_full_name = params["repo_full_name"]
    pr_number = params["pr_number"]
    my_dirs: List[str] = params["my_dirs"]
    owner, repo = _owner_repo(repo_full_name)

    async with AsyncSessionLocal() as db:
        pr_info = await GitOpsHandler.get_pull_request(
            tenant=tenant, owner=owner, repo=repo, pr_number=pr_number, db=db,
        )
        head_sha = pr_info.get("head_sha")

        all_paths = await GitOpsHandler.list_pr_files(
            tenant=tenant, owner=owner, repo=repo, pull_number=pr_number, db=db,
        )
        prod_paths = [p for p in all_paths if _is_production_path(p)]

        rows = (await db.execute(
            select(ProductionDeploymentTrackModel).where(
                ProductionDeploymentTrackModel.repo_full_name == repo_full_name
            )
        )).scalars().all()
        track_by_dir: Dict[str, Dict[str, Any]] = {
            r.dir: {"status": r.status.value, "workflow_id": r.workflow_id, "_row": r}
            for r in rows
        }

        # ── Orphan healing ───────────────────────────────────────────────────
        pending = [
            (d, meta) for d, meta in track_by_dir.items()
            if meta["status"] in ("IN_PROGRESS", "RESUMING") and d not in my_dirs
        ]
        if pending:
            try:
                from app.temporal.client import get_temporal_client
                client = await get_temporal_client()
                for d, meta in pending:
                    try:
                        desc = await client.get_workflow_handle(meta["workflow_id"]).describe()
                        running = desc.status is not None and desc.status.name == "RUNNING"
                    except Exception:
                        running = False
                    if not running:
                        activity.logger.warning(
                            "evaluate_merge_gate: dir %s claimed by dead workflow %s -> FAILED",
                            d, meta["workflow_id"],
                        )
                        meta["_row"].status = ProductionDeploymentStatusEnum.FAILED
                        meta["status"] = "FAILED"
                await db.commit()
            except Exception as exc:
                activity.logger.warning(
                    "evaluate_merge_gate: liveness check skipped: %s", exc
                )

    # ── Classification (pure logic, no I/O) ──────────────────────────────────
    def classify(path: str) -> Optional[Dict[str, Any]]:
        """None = mergeable; else a blocker descriptor for the alert."""
        for d in my_dirs:
            if path.startswith(d.rstrip("/") + "/"):
                return None
        for d, meta in track_by_dir.items():
            if path.startswith(d.rstrip("/") + "/"):
                if meta["status"] == "APPLIED":
                    return None
                return {
                    "dir": d,
                    "kind": "devlift",
                    "status": meta["status"],
                    "workflow_id": meta["workflow_id"],
                }
        # Production path with no track row → not DevLift's → manual content.
        parent = path.rsplit("/", 1)[0] if "/" in path else path
        return {"dir": parent, "kind": "manual", "status": None, "workflow_id": None}

    blockers: List[Dict[str, Any]] = []
    seen: set = set()
    for path in prod_paths:
        verdict = classify(path)
        if verdict and verdict["dir"] not in seen:
            seen.add(verdict["dir"])
            blockers.append(verdict)

    applied_waiting = [
        d for d, meta in track_by_dir.items()
        if meta["status"] == "APPLIED" and d not in my_dirs
        and any(p.startswith(d.rstrip("/") + "/") for p in prod_paths)
    ]
    merged_dirs = sorted(set(my_dirs) | set(applied_waiting))

    result = {
        "mergeable": not blockers,
        "head_sha": head_sha,
        "merged_dirs": merged_dirs,
        "blockers": blockers,
        "applied_waiting": applied_waiting,
    }
    activity.logger.info(
        "evaluate_merge_gate: PR #%s mergeable=%s blockers=%s applied_waiting=%d",
        pr_number, result["mergeable"],
        [(b["dir"], b["kind"], b["status"]) for b in blockers], len(applied_waiting),
    )
    return result


@activity.defn
async def merge_promotion_pr(params: Dict[str, Any]) -> str:
    """
    SHA-conditional merge of the promotion PR into main.

    params: {tenant_code, repo_full_name, pr_number, expected_head_sha}
    Returns: "merged" | "head_moved" | "failed"

    expected_head_sha is the head the merge gate evaluated. GitHub's merge
    API refuses with "Head branch was modified" if the head moved since —
    closing the evaluate→merge race: "head_moved" sends the workflow back to
    re-run the gate against the new content.

    Always a MERGE COMMIT — squash would diverge stage/main history and make
    every following promotion PR conflict (policy pinned in the design).
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    tenant = params["tenant_code"]
    repo_full_name = params["repo_full_name"]
    pr_number = params["pr_number"]
    owner, repo = _owner_repo(repo_full_name)

    async with AsyncSessionLocal() as db:
        try:
            result = await GitOpsHandler.merge_pull_request(
                tenant=tenant, owner=owner, repo=repo,
                pull_number=pr_number, db=db, merge_method="merge",
                expected_head_sha=params["expected_head_sha"],
            )
        except Exception as e:
            activity.logger.error("merge_promotion_pr: PR #%s failed: %s", pr_number, e)
            return "failed"

    if result.get("merged"):
        activity.logger.info("merge_promotion_pr: PR #%s merged into main", pr_number)
        return "merged"
    if result.get("head_moved"):
        return "head_moved"
    return "failed"


# ── Manual-commit detection (dir-scoped) ─────────────────────────────────────

@activity.defn
async def resolve_deployment_dirs(params: Dict[str, Any]) -> List[str]:
    """
    The dirs this deployment actually owns, read from its own feature PR.

    params: {tenant_code, repo_full_name, pr_number}
    Returns: sorted dirs, e.g.
      ["environment/core-prod-01/eu-west-2/eks-workloads/application/services/x"]

    Why not the dirs computed before the deploy started: those are PREDICTED.
    prepare_multiple_deploy runs the file locator on the raw queue item to get
    lock keys, while generation runs it on an ENRICHED config — so the two can
    disagree (an EKS service's cluster segment is resolved only by enrichment:
    predicted `.../eks-workloads/backend/...`, generated
    `.../eks-workloads/application/...`). Prediction is fine for lock keys,
    which only need to agree with each other. It is NOT fine for the merge
    gate, the track rows or the manual-commit check, which compare against
    real git paths — a wrong dir there makes the gate read our own file as
    somebody else's content and refuse to merge.

    The feature PR's changed files are not a better guess, they are the
    content itself: exactly these paths merge into stage, so exactly these
    paths are what our deployment contributes to the promotion PR's diff.

    Dir = the parent directory of each changed file under environment/ (the
    same shape the gate compares with `path.startswith(dir + "/")`). Files
    outside environment/ — atlantis.yaml, layers/, docs — belong to no
    project and are skipped.
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler
    import app.db.models  # noqa: F401

    tenant = params["tenant_code"]
    repo_full_name = params["repo_full_name"]
    pr_number = params["pr_number"]
    owner, repo = _owner_repo(repo_full_name)

    async with AsyncSessionLocal() as db:
        paths = await GitOpsHandler.list_pr_files(
            tenant=tenant, owner=owner, repo=repo, pull_number=pr_number, db=db,
        )

    dirs = sorted({
        p.rsplit("/", 1)[0] for p in paths
        if p.startswith("environment/") and "/" in p
    })
    activity.logger.info(
        "resolve_deployment_dirs: PR #%s changed %d file(s) -> dirs=%s",
        pr_number, len(paths), dirs,
    )
    return dirs


@activity.defn
async def check_manual_commit_on_dirs(params: Dict[str, Any]) -> bool:
    """
    Did anything land between two SHAs that touches OUR OWN deploying dirs?

    params: {tenant_code, repo_full_name, base_sha, head_sha, dirs: [..]}
      dirs = THIS deployment's project_dirs only — changes anywhere else
      (other services, stage envs, root files) return False; those are the
      merge gate's business, not this check's.

    Returns: True → manual commit on our content → the workflow ABORTS the
             deployment (+P0), same policy as stage's manual-commit rule.
             False → head-move was unrelated → proceed.

    Why "touched = manual": same-dir DevLift deploys are serialized by the
    coordinator locks, so while THIS deployment runs, nothing from DevLift
    can write to its dirs — any change there is by definition a human edit
    of our generated files (the generator-feedback case: terminate, track,
    fix the generator, redeploy).

    Also the stale-apply guard: Atlantis does NOT discard scoped -p plans on
    new commits and will happily apply a stale one, so this check runs right
    before apply. Fails CLOSED (True → abort) on API errors — a spurious
    abort is recoverable, a wrong prod apply is not.
    """
    from app.db.session import AsyncSessionLocal
    from app.handlers.gitops_handler import GitOpsHandler

    if params["base_sha"] == params["head_sha"]:
        return False

    owner, repo = _owner_repo(params["repo_full_name"])
    try:
        async with AsyncSessionLocal() as db:
            compared = await GitOpsHandler.compare_commits(
                tenant=params["tenant_code"], owner=owner, repo=repo,
                base_sha=params["base_sha"], head_sha=params["head_sha"], db=db,
            )
    except Exception as exc:
        activity.logger.warning(
            "check_manual_commit_on_dirs: compare failed (%s) — failing closed", exc
        )
        return True

    files = compared.get("files", [])
    authors = compared.get("authors", [])

    prefixes = [d.rstrip("/") + "/" for d in params["dirs"]]
    touched = [f for f in files if any(f.startswith(p) for p in prefixes)]
    if not touched:
        return False

    # All-bot rule: if EVERY commit in the range is authored by a DevLift
    # bot, the touches are DevLift's own writes (a parked peer's gateway
    # regen, conflict resolution) — not manual. The compare API attributes
    # files to the range, not per-commit, so a mixed human+bot range with a
    # touch fails CLOSED (rare over-abort beats a missed manual edit).
    if authors and all(a in DEVLIFT_BOT_LOGINS for a in authors):
        activity.logger.info(
            "check_manual_commit_on_dirs: %d file(s) touched but all %d commit(s) "
            "are DevLift-bot-authored — not manual", len(touched), len(authors),
        )
        return False

    activity.logger.warning(
        "check_manual_commit_on_dirs: %d file(s) in our dirs changed by non-bot "
        "author(s) %s — manual commit: %s",
        len(touched), [a for a in authors if a not in DEVLIFT_BOT_LOGINS][:5], touched[:10],
    )
    return True
