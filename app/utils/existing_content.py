"""Shared read of a generated file's current content, for script-gen components.

Every patch-in-place component needs the same thing before it can splice its
changes in: the file as it will exist *after* this run's commit lands. Which
branch holds that answer is not the same in all three modes, and getting it
wrong silently drops other people's merged work — see conflict_resolve below.
"""

from typing import Any, Dict, Optional

from app.utils.timing import log_timing


async def fetch_existing_content(
    *,
    db,
    tenant: str,
    owner: Optional[str],
    repo: str,
    file_path: str,
    base_branch: str,
    feature_branch: str,
    workflow_context,
    logger,
    component_name: str,
) -> Dict[str, Any]:
    """Fetch the current content of `file_path`, from whichever branch holds it.

    Normal deploy — the feature branch was cut from base moments ago and already
    carries this run's earlier writes, so it is the only correct source.

    Preview (skip_commit) — no feature branch exists yet; read the base branch
    the deploy would cut one from.

    Conflict resolve (is_conflict_resolve) — the feature branch is whatever it
    was when the PR was raised, and base has moved on since. conflict_resolve
    rebases onto base only at commit time (force_commit_from_parent), so at read
    time the feature branch is stale: patching it and overlaying the result onto
    base deletes anything merged in the meantime. Read base instead — it is the
    exact tree the commit is built on.

    Files the PR itself creates do not exist on base, so fall back to the feature
    branch rather than reporting the file missing and re-rendering it from a
    template. Nothing merged can be lost in that case, since base has no copy.
    """
    # Imported here, not at module scope: gitops_handler reaches the script-gen
    # components, which import this module.
    from app.handlers.gitops_handler import GitOpsHandler

    skip_commit = bool(workflow_context and getattr(workflow_context, "skip_commit", False))
    is_conflict_resolve = bool(
        workflow_context and getattr(workflow_context, "is_conflict_resolve", False)
    )

    branch = base_branch if (skip_commit or is_conflict_resolve) else feature_branch

    context = f"repo={repo} branch={branch} path={file_path}"
    with log_timing(logger, f"{component_name}.fetch_content", context=context):
        result = await GitOpsHandler.get_content(
            db=db,
            tenant=tenant,
            owner=owner,
            repo=repo,
            file_path=file_path,
            branch=branch,
        )

    # Created by this PR, so absent from base — take the branch's copy.
    if (
        is_conflict_resolve
        and feature_branch
        and feature_branch != branch
        and result.get("status") != "error"
        and not result.get("exists")
    ):
        context = f"repo={repo} branch={feature_branch} path={file_path} (fallback)"
        with log_timing(logger, f"{component_name}.fetch_content", context=context):
            result = await GitOpsHandler.get_content(
                db=db,
                tenant=tenant,
                owner=owner,
                repo=repo,
                file_path=file_path,
                branch=feature_branch,
            )

    return result
