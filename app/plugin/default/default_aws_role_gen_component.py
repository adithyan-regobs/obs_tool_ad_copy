"""Default AWS role gen component.

Maintains the ``custom_policy_json`` value inside a tenant's
``default-role/terragrunt.hcl`` as infrastructure resources come and go. The
role itself is created once at signup by the Terraform ``application`` layer
(invoked with ``identifier = "default"``) — this component only edits the
policy payload passed to that layer.

Follows the same flow as other default script-gen components:

1. File locator emits a ``FileLocationItem`` for ``default-role/terragrunt.hcl``
   alongside the resource's own files (shared feature branch, single PR).
2. ``ScriptGenHandler`` dispatches to ``generate()`` here.
3. We fetch the current file (via staged cache first, then GitHub), splice in
   the new statement, and stage the result via ``workflow_context.staged_files``
   so the PR-workflow commits it together with the resource file.

Policy shape: one statement per supported kind, identified by a stable
``Sid``. Adding a resource appends its ARN to the ``Resource`` list (dedup).
Removing drops it. When a statement's ``Resource`` empties it is removed;
when no statements remain, ``custom_policy_json`` resets to ``""``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import aiofiles

from app.core.config import settings
from app.domain.policies import SUPPORTED_KINDS, PolicyKind, build_statement

logger = logging.getLogger(__name__)


_HEREDOC_DELIM = "EOT"
_HEREDOC_RE = re.compile(
    r'custom_policy_json\s*=\s*<<-?(\w+)\n(.*?)\n\1',
    re.DOTALL,
)
_EMPTY_RE = re.compile(r'custom_policy_json\s*=\s*""')

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..",
    "templates", "terragrunt", "org-onboarding",
    "default-role", "terragrunt.hcl",
)


def _find_staged_entry(workflow_context, repo: str, base_branch: str, file_path: str):
    if not workflow_context:
        return None
    for entry in workflow_context.staged_files:
        if (
            entry.get("repo") == repo
            and entry.get("base_branch") == base_branch
            and entry.get("file_path") == file_path
        ):
            return entry
    return None


def _upsert_staged_entry(
    workflow_context,
    *,
    repo: str,
    base_branch: str,
    feature_branch: str,
    file_path: str,
    content: str,
    queue_id,
    script_gen_key,
) -> None:
    if not workflow_context:
        return
    entry = _find_staged_entry(workflow_context, repo, base_branch, file_path)
    if entry:
        entry["content"] = content
        entry["feature_branch"] = feature_branch or entry.get("feature_branch")
        entry["queue_id"] = queue_id
        entry["script_gen_key"] = script_gen_key
        return
    workflow_context.staged_files.append({
        "repo": repo,
        "base_branch": base_branch,
        "feature_branch": feature_branch,
        "file_path": file_path,
        "content": content,
        "queue_id": queue_id,
        "script_gen_key": script_gen_key,
    })


def _append_commit_message(workflow_context, repo: str, base_branch: str, message: str) -> None:
    if not workflow_context or not message:
        return
    key = f"{repo}|||{base_branch}"
    existing = workflow_context.commit_messages.get(key, "")
    if existing:
        workflow_context.commit_messages[key] = f"{existing}\n{message}"
    else:
        workflow_context.commit_messages[key] = message


class DefaultAwsRoleGenComponent:
    """Generates the tenant's default-role ``custom_policy_json`` HCL.

    Reads the current file (staged cache → GitHub), applies the mutation, and
    stages the new content. Idempotent: re-adding the same ARN or removing one
    that isn't present returns the existing content unchanged.
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.repository = repository

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: Dict,
        workflow_context,
        upload_to_s3: bool = False,
        db=None,
    ) -> str:
        """Generate (or update) the default-role terragrunt file for a tenant.

        Expected ``file_location.config`` keys:
          - ``policy_kind``: one of ``SUPPORTED_KINDS`` (e.g. ``"secrets-read"``).
          - ``resource_arns``: list of ARNs to add or remove (single-arn callers
            may pass a string — normalized to a one-element list). Kinds like
            ``s3-rw`` pass both bucket and object ARNs in one call.
          - ``resource_arn``: legacy alias for a single-ARN call (still accepted).
          - ``policy_op``: ``"add"`` (default) or ``"remove"``.
        """
        config = file_location.config or {}
        kind = _require_kind(config.get("policy_kind"))
        arns = config.get("resource_arns")
        if arns is None:
            single = config.get("resource_arn")
            arns = [single] if single else []
        elif isinstance(arns, str):
            arns = [arns]
        if not arns:
            raise ValueError(
                "default_aws_role: config.resource_arns (or resource_arn) is required"
            )
        op = (config.get("policy_op") or "add").lower()
        if op not in ("add", "remove"):
            raise ValueError(
                f"default_aws_role: policy_op must be 'add' or 'remove', got {op!r}"
            )

        repo_parts = file_location.repo.split("/")
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        base_branch = file_location.base_branch or file_location.target_branch or ""
        feature_branch = file_location.feature_branch

        from app.handlers.gitops_handler import GitOpsHandler

        current_hcl = await self._load_current_hcl(
            GitOpsHandler=GitOpsHandler,
            tenant=tenant,
            db=db,
            workflow_context=workflow_context,
            repo_full=file_location.repo,
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            feature_branch=feature_branch,
            file_path=file_location.file_path,
        )

        current_doc = _extract_policy_doc(current_hcl)
        new_doc = current_doc
        for arn in arns:
            new_doc = _apply_mutation(new_doc, kind, arn, remove=(op == "remove"))

        if new_doc == current_doc:
            self.logger.info(
                "[DEFAULT_ROLE] No-op for tenant=%s kind=%s arns=%s op=%s",
                tenant, kind.name, arns, op,
            )
            new_hcl = current_hcl
        else:
            new_hcl = _splice_policy_doc(current_hcl, new_doc)

        if workflow_context and file_location.repo and file_location.file_path:
            _upsert_staged_entry(
                workflow_context,
                repo=file_location.repo,
                base_branch=base_branch,
                feature_branch=feature_branch or "",
                file_path=file_location.file_path,
                content=new_hcl,
                queue_id=queue_dict.get("id") if queue_dict else None,
                script_gen_key=file_location.script_gen_key,
            )
            if new_doc != current_doc:
                verb = "grant" if op == "add" else "revoke"
                arns_str = arns[0] if len(arns) == 1 else f"[{', '.join(arns)}]"
                msg = f"iam: {verb} {kind.name} access to {arns_str}"
                _append_commit_message(workflow_context, file_location.repo, base_branch, msg)

        if queue_dict and queue_dict.get("id") and workflow_context:
            workflow_context.script_gen_responses.setdefault(queue_dict["id"], {})
            workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                "original_content": new_hcl,
                "preview_content": new_hcl,
            }

        return new_hcl

    async def _load_current_hcl(
        self,
        *,
        GitOpsHandler,
        tenant: str,
        db,
        workflow_context,
        repo_full: str,
        owner: Optional[str],
        repo: str,
        base_branch: str,
        feature_branch: Optional[str],
        file_path: str,
    ) -> str:
        cached = None
        if workflow_context and not getattr(workflow_context, "skip_commit", False):
            cached = _find_staged_entry(workflow_context, repo_full, base_branch, file_path)
        if cached and cached.get("content") is not None:
            return cached["content"]

        existing = await GitOpsHandler.get_content(
            db=db,
            tenant=tenant,
            owner=owner,
            repo=repo,
            file_path=file_path,
            branch=feature_branch or base_branch,
        )
        if existing.get("status") == "error":
            raise ValueError(
                f"GitOps get_content failed for {file_path}: {existing.get('error')}"
            )
        if existing.get("exists"):
            return existing["content"]

        return await _render_template_for_tenant(tenant)


# ----------------------------------------------------------------------
# Pure helpers — unit-testable
# ----------------------------------------------------------------------

def _require_kind(kind_name: Optional[str]) -> PolicyKind:
    kind = SUPPORTED_KINDS.get(kind_name or "")
    if kind is None:
        raise ValueError(
            f"default_aws_role: unsupported policy_kind {kind_name!r}. "
            f"Supported: {sorted(SUPPORTED_KINDS)}"
        )
    return kind


def _extract_policy_doc(hcl: str) -> Dict[str, Any]:
    match = _HEREDOC_RE.search(hcl)
    if match:
        try:
            doc = json.loads(match.group(2))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"custom_policy_json heredoc contains invalid JSON: {exc}"
            ) from exc
        return {
            "Version": doc.get("Version", "2012-10-17"),
            "Statement": list(doc.get("Statement", [])),
        }
    if _EMPTY_RE.search(hcl):
        return {"Version": "2012-10-17", "Statement": []}
    raise ValueError(
        "custom_policy_json assignment not found in default-role terragrunt.hcl"
    )


def _splice_policy_doc(hcl: str, doc: Dict[str, Any]) -> str:
    if not doc.get("Statement"):
        replacement = 'custom_policy_json       = ""'
    else:
        body = json.dumps(doc, indent=2)
        replacement = (
            f"custom_policy_json       = <<{_HEREDOC_DELIM}\n"
            f"{body}\n{_HEREDOC_DELIM}"
        )
    if _HEREDOC_RE.search(hcl):
        return _HEREDOC_RE.sub(lambda _m: replacement, hcl, count=1)
    if _EMPTY_RE.search(hcl):
        return _EMPTY_RE.sub(lambda _m: replacement, hcl, count=1)
    raise ValueError(
        "custom_policy_json assignment not found in default-role terragrunt.hcl"
    )


def _apply_mutation(
    doc: Dict[str, Any],
    kind: PolicyKind,
    arn: str,
    *,
    remove: bool,
) -> Dict[str, Any]:
    statements: List[Dict[str, Any]] = [dict(s) for s in doc.get("Statement", [])]
    target_idx = next(
        (i for i, s in enumerate(statements) if s.get("Sid") == kind.sid),
        None,
    )

    if remove:
        if target_idx is None:
            return doc
        stmt = dict(statements[target_idx])
        resources = [r for r in stmt.get("Resource", []) if r != arn]
        if not resources:
            statements.pop(target_idx)
        else:
            stmt["Resource"] = resources
            statements[target_idx] = stmt
    else:
        if target_idx is None:
            statements.append(build_statement(kind, [arn]))
        else:
            stmt = dict(statements[target_idx])
            resources = list(stmt.get("Resource", []))
            if arn in resources:
                return doc
            resources.append(arn)
            stmt["Resource"] = resources
            statements[target_idx] = stmt

    return {"Version": "2012-10-17", "Statement": statements}


async def apply_policy_direct(
    *,
    tenant_code: str,
    environment: str,
    kind_name: str,
    resource_arn: str,
    operation: str = "add",
    db=None,
) -> Dict[str, Any]:
    """Direct-commit entry point for non-queue callers (e.g. the secrets API).

    Fetches the tenant's infra repo coordinates from ``tenants_mst.config``,
    reads ``default-role/terragrunt.hcl`` from GitHub (renders a fresh copy if
    it doesn't exist yet), applies the mutation, and commits the result
    directly to the tenant's infra branch. On a real change, also fires the
    tenant's ``iam/IAM_<tenant>_<env>`` Jenkins infra-apply build so the live
    AWS role picks up the commit — before the Apr 21 Jenkins migration GitHub
    Actions auto-applied every push to main, Jenkins needs an explicit
    trigger per action.

    Returns ``{"changed": bool, "commit_sha": str | None}``. Idempotent: if the
    ARN is already present (add) or absent (remove) no commit is made and no
    Jenkins build is triggered.
    """
    from app.integrations.github_integration import GitHubIntegration
    from app.repository.tenants_mst_repository import TenantsMstRepository
    from app.utils.github_app_token import get_token
    from app.db.session import AsyncSessionLocal

    if operation not in ("add", "remove"):
        raise ValueError(
            f"apply_policy_direct: operation must be 'add' or 'remove', got {operation!r}"
        )
    kind = _require_kind(kind_name)

    async def _run(session) -> Dict[str, Any]:
        tenants_repo = TenantsMstRepository(session)
        tenant = await tenants_repo.get_by_code(tenant_code)
        if tenant is None:
            raise ValueError(f"Tenant not found: {tenant_code}")

        github_cfg = (tenant.config or {}).get("github") or {}
        infra_repo = github_cfg.get("infra_repository")
        infra_branch = github_cfg.get("infra_branch")
        if not infra_repo or "/" not in infra_repo or not infra_branch:
            raise ValueError(
                f"Tenant '{tenant_code}' has no infra_repository/infra_branch in config.github"
            )
        owner, repo = infra_repo.split("/", 1)
        terragrunt_dir = (
            f"environment/{tenant_code}/{settings.onboarding_default_index}"
            f"/aws/{settings.onboarding_default_region}"
            f"/application/default"
        )
        file_path = f"{terragrunt_dir}/terragrunt.hcl"

        token = await get_token(settings.github_app_platform_installation_id)
        base_url = settings.github_base_url.rstrip("/")

        existing = await GitHubIntegration.get_file_content(
            token=token, base_url=base_url, owner=owner, repo=repo,
            file_path=file_path, branch=infra_branch,
        )
        if existing and existing.get("exists"):
            current_hcl = existing["content"]
        else:
            current_hcl = await _render_template_for_tenant(tenant_code)

        current_doc = _extract_policy_doc(current_hcl)
        new_doc = _apply_mutation(current_doc, kind, resource_arn, remove=(operation == "remove"))
        if new_doc == current_doc and existing and existing.get("exists"):
            return {"changed": False, "commit_sha": None}

        new_hcl = _splice_policy_doc(current_hcl, new_doc)
        verb = "grant" if operation == "add" else "revoke"
        message = f"iam: {verb} {kind.name} access to {resource_arn}"
        result = await GitHubIntegration.update_or_create_file(
            token=token, base_url=base_url, owner=owner, repo=repo,
            branch=infra_branch, file_path=file_path, content=new_hcl,
            message=message,
        )
        commit_sha = result.get("commit_sha")

        # Fire iam/IAM_<tenant>_<env> Jenkins infra-apply so AWS picks up the
        # commit. Non-blocking: if Jenkins is down the commit still stands and
        # the next bucket/table create will sweep it up via its iam companion.
        try:
            from app.services.jenkins_provisioning_service import JenkinsProvisioningService
            jenkins_svc = JenkinsProvisioningService(session)
            await jenkins_svc.trigger_infra_apply_build(
                tenant_code=tenant_code,
                environment=environment,
                resource_type="iam",
                resource_code=f"IAM_{tenant_code}_{environment}",
                terragrunt_path=terragrunt_dir,
                infra_repo=infra_repo,
                infra_branch=infra_branch,
                queue_codes=[],
            )
        except Exception as exc:
            logger.warning(
                "apply_policy_direct: commit %s succeeded but iam Jenkins trigger "
                "failed (non-blocking): %s", commit_sha, exc,
            )

        return {"changed": True, "commit_sha": commit_sha}

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as session:
        return await _run(session)


async def _render_template_for_tenant(tenant: str) -> str:
    async with aiofiles.open(_TEMPLATE_PATH, "r") as f:
        template = await f.read()
    tenant_namespace = f"{tenant}-ns"
    eks_cluster_name = (
        f"devlift-{settings.onboarding_default_env}"
        f"-{settings.onboarding_default_region_code}"
        f"-{settings.onboarding_default_index}"
        f"-{settings.onboarding_default_vm_name}-cluster"
    )
    return (
        template
        .replace("${tenant_namespace}", tenant_namespace)
        .replace("${eks_cluster_name}", eks_cluster_name)
    )
