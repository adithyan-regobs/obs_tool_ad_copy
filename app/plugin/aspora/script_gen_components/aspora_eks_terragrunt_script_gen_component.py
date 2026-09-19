"""
Aspora EKS Terragrunt Script Generation Component

Renders the EKS app terragrunt.hcl that lives in the infra repo at
`environment/{org}-{env}-{idx}/{region}/eks-workloads/{cluster_id}/services/{service}-service/terragrunt.hcl`.

The cluster targeting is implicit in directory placement combined with the
`dependency "eks_cluster"` config_path. This component maps the selected
cluster (cluster_arn / cluster_name from config_snapshot) to the relative
config_path that points at the sibling EKS module in the infra repo.
"""

import json
import logging
import os
import re
from typing import Optional

import aiofiles

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.hclfmt import hclfmt
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing

logger = logging.getLogger(__name__)


# Mapper: cluster_arn (preferred) → cluster_name → infrastructure_mst code → relative config_path
# for the `dependency "eks_cluster"` block. Add entries here as new
# clusters get onboarded.
CLUSTER_DEPENDENCY_PATH = {
    # --- cluster ARNs ---
    "arn:aws:eks:ap-south-1:878097483768:cluster/vance-core-stage-mumbai-01-backend-cluster": "../../../../eks/backend",
    "arn:aws:eks:ap-south-1:878097483768:cluster/vance-core-stage-mumbai-01-application-cluster": "../../../../eks/application",
    "arn:aws:eks:us-east-2:383313560245:cluster/vance-core-prod-ohio-01-backend-cluster": "../../../../eks/backend",
    "arn:aws:eks:eu-west-2:383313560245:cluster/vance-core-prod-london-01-application-cluster": "../../../../eks/application",
    "arn:aws:eks:ap-south-1:418272793128:cluster/vance-core-qa-mumbai-01-application-cluster": "../../../../eks/application",
    # --- cluster names ---
    "vance-core-stage-mumbai-01-backend-cluster": "../../../../eks/backend",
    "vance-core-stage-mumbai-01-application-cluster": "../../../../eks/application",
    "vance-core-prod-ohio-01-backend-cluster": "../../../../eks/backend",
    "vance-core-prod-london-01-application-cluster": "../../../../eks/application",
    "vance-core-qa-mumbai-01-application-cluster": "../../../../eks/application",
    # --- infrastructure_mst codes ---
    "infra-vance-eks-staging-mumbai-01": "../../../../eks/backend",
    "infra-vance-eks-staging-mumbai-app-01": "../../../../eks/application",
    "infra-eks-prod-canada-01": "../../../../eks/backend",
    "infra-eks-prod-london-app-01": "../../../../eks/application",
    "infra-vance-eks-qa-mumbai-app-01": "../../../../eks/application",
}

DEFAULT_DEPENDENCY_PATH = "../../../../eks/backend"


# Canonical (action_prefix, sid_prefix) for the region-scoped FullAccess
# statements built from `config_snapshot.custom_iam_policies`. Extend as
# new services are needed.
AWS_SERVICE_ALIASES = {
    "s3": ("s3", "S3"),
    "sqs": ("sqs", "SQS"),
    "dynamo": ("dynamodb", "DynamoDB"),
    "dynamodb": ("dynamodb", "DynamoDB"),
    "secrets": ("secretsmanager", "SecretsManager"),
    "secretsmanager": ("secretsmanager", "SecretsManager"),
    "kms": ("kms", "KMS"),
    "lambda": ("lambda", "Lambda"),
    "ses": ("ses", "SES"),
    "sns": ("sns", "SNS"),
}


def _hcl_bool(value) -> str:
    if isinstance(value, str):
        return "true" if value.strip().lower() in ("true", "1", "yes", "y") else "false"
    return "true" if bool(value) else "false"


def _resolve_service_alias(name: str):
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in AWS_SERVICE_ALIASES:
        return AWS_SERVICE_ALIASES[key]
    # Unknown service — best-effort: keep the lowercase action prefix and
    # title-case the Sid. Caller logs a warning so the alias map can grow.
    return (key, key[:1].upper() + key[1:])


# Sid prefix for each action prefix, e.g. "s3" -> "S3".
SID_BY_ACTION_PREFIX = {action: sid for action, sid in AWS_SERVICE_ALIASES.values()}


def _render_statement(action_prefix: str, sid_prefix: str) -> str:
    """The one statement shape this component ever writes."""
    return (
        "      {\n"
        f"        Sid      = \"{sid_prefix}FullAccessRegionScoped\"\n"
        "        Effect   = \"Allow\"\n"
        f"        Action   = [\"{action_prefix}:*\"]\n"
        "        Resource = \"*\"\n"
        "        Condition = {\n"
        "          StringEquals = {\n"
        "            \"aws:RequestedRegion\" = include.env.locals.region\n"
        "          }\n"
        "        }\n"
        "      }"
    )


def _prefixes_of(services) -> set:
    """Action prefixes for a `custom_iam_policies` list, e.g. ['dynamo'] -> {'dynamodb'}."""
    if not isinstance(services, (list, tuple, set)):
        return set()
    out = set()
    for raw in services:
        alias = _resolve_service_alias(raw if isinstance(raw, str) else str(raw))
        if alias:
            out.add(alias[0])
    return out


def _split_statements(content: str) -> list:
    """Source text of each `{ ... }` element of `custom_policy_json`'s Statement list.

    Sliced out verbatim, braces included, so a statement that stays is written
    back exactly as the file had it rather than re-rendered from a parse of it.
    """
    m = re.search(r'custom_policy_json[ \t]*=[ \t]*jsonencode\(', content)
    if not m:
        return []
    m2 = re.compile(r'Statement[ \t]*=[ \t]*\[').search(content, m.end())
    if not m2:
        return []

    i, n = m2.end(), len(content)
    out, depth, start, in_str, esc = [], 0, None, False, False
    while i < n:
        ch = content[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(content[start:i + 1])
                start = None
        elif ch == "]" and depth == 0:
            break
        i += 1
    return out


def _statement_action_prefixes(statement_src: str) -> set:
    """The AWS services a statement names, e.g. {"s3"} for Action = ["s3:*"]."""
    m = re.search(r'Action[ \t]*=[ \t]*(\[[^\]]*\]|"[^"]*")', statement_src, re.DOTALL)
    if not m:
        return set()
    return {
        a.split(":", 1)[0].strip().lower()
        for a in re.findall(r'"([^"]+)"', m.group(1))
        if ":" in a
    }


def _written_by_devlift(statement_src: str, action_prefix: str) -> bool:
    """True only if this statement is byte-equal (bar whitespace) to our own output.

    The test for "may we delete this". Devlift emits exactly one shape per
    service, so anything that differs at all — a missing Condition, a narrowed
    Action list, an Effect of Deny, a hand-picked Sid — was written by a person,
    and a person's statement is never ours to remove.
    """
    sid_prefix = SID_BY_ACTION_PREFIX.get(action_prefix)
    if not sid_prefix:
        return False
    squash = lambda t: re.sub(r"\s+", " ", t).strip()
    return squash(statement_src) == squash(_render_statement(action_prefix, sid_prefix))


def _build_custom_policy_hcl(content: str, before, after, logger=None):
    """New value for `custom_policy_json`, or None to leave the file alone.

    Driven by what CHANGED, not by what is selected. `after` is the proposal's
    `custom_iam_policies`; `before` is the same field on the live service_configs
    row, which still describes what is deployed because the settings saver only
    writes it once the PR has merged. The difference between the two is the only
    honest evidence of intent this component gets.

    Rebuilding from `after` alone is what broke PR #4756: the generator re-emitted
    its own S3 block over a hand-written `S3FullAccess`, renaming it and bolting a
    Condition onto it, because a selected service always produced a statement. It
    also meant an empty list wiped every statement in the file — indistinguishable
    from a config devlift simply had no record of.

    So, per service:
      in both          → untouched, whatever the file says
      in `after` only  → generated and appended, unless the file already has one
      in `before` only → deleted, but only if _written_by_devlift
      in neither       → untouched; devlift never knew about it

    `before` that cannot be read (no db, missing row) is treated as unknown: it
    deletes nothing, because "no evidence" must never read as "remove it".
    """
    placeholder = "{{CUSTOM_POLICY_JSON}}" in content
    after_prefixes = _prefixes_of(after)

    # A freshly rendered template has nothing to preserve and a placeholder that
    # must not survive into the committed file, so it is always written.
    if placeholder:
        statements = [
            _render_statement(p, SID_BY_ACTION_PREFIX.get(p, p[:1].upper() + p[1:]))
            for p in sorted(after_prefixes)
        ]
        return _wrap_statements(statements)

    before_known = isinstance(before, (list, tuple, set))
    before_prefixes = _prefixes_of(before) if before_known else set()
    added = after_prefixes - before_prefixes
    removed = (before_prefixes - after_prefixes) if before_known else set()

    if not before_known and logger:
        logger.info(
            "EKS custom_policy_json: no live custom_iam_policies to compare against — "
            "adding only, removing nothing."
        )

    if not added and not removed:
        if logger:
            logger.info(
                "EKS custom_policy_json: selection unchanged (%s) — leaving the policy as written.",
                ", ".join(sorted(after_prefixes)) or "none",
            )
        return None

    existing = _split_statements(content)
    kept, in_file = [], set()
    for stmt in existing:
        prefixes = _statement_action_prefixes(stmt)
        in_file |= prefixes
        doomed = prefixes & removed
        if doomed and all(_written_by_devlift(stmt, p) for p in doomed):
            if logger:
                logger.info(
                    "EKS custom_policy_json: removing %s — deselected, and the statement is "
                    "devlift's own.", ", ".join(sorted(doomed)),
                )
            continue
        if doomed and logger:
            logger.warning(
                "EKS custom_policy_json: %s was deselected, but its statement in the file is not "
                "the one devlift writes — keeping it. Edit the terragrunt by hand to remove it.",
                ", ".join(sorted(doomed)),
            )
        kept.append(stmt)

    statements = [_reindent_statement(s) for s in kept]
    for prefix in sorted(added):
        if prefix in in_file:
            # Selected anew, but the file already grants it by hand. Adding ours
            # beside it would grant the same service twice under two Sids.
            if logger:
                logger.info(
                    "EKS custom_policy_json: %s selected, but the file already grants it — "
                    "keeping the existing statement.", prefix,
                )
            continue
        statements.append(
            _render_statement(prefix, SID_BY_ACTION_PREFIX.get(prefix, prefix[:1].upper() + prefix[1:]))
        )

    return _wrap_statements(statements)


def _wrap_statements(statements: list) -> str:
    """`jsonencode({...})` around rendered statements, or `""` when there are none."""
    if not statements:
        return '""'
    body = ",\n".join(statements)
    return (
        "jsonencode({\n"
        "    Version = \"2012-10-17\"\n"
        "    Statement = [\n"
        f"{body}\n"
        "    ]\n"
        "  })"
    )


def _reindent_statement(statement_src: str) -> str:
    """Put a preserved statement back at the 6-space list indent.

    Only leading whitespace moves; the statement's own text is untouched.
    """
    lines = statement_src.strip().split("\n")
    if len(lines) == 1:
        return "      " + lines[0]
    body = [ln for ln in lines[1:] if ln.strip()]
    base = min((len(ln) - len(ln.lstrip()) for ln in body), default=0)
    out = ["      " + lines[0].strip()]
    for ln in lines[1:]:
        if not ln.strip():
            out.append("")
        elif len(ln) - len(ln.lstrip()) >= base:
            out.append("      " + ln[base:])
        else:
            out.append("      " + ln.lstrip())
    return "\n".join(out)


def _replace_scalar_field(content: str, field: str, value: str) -> str:
    """Set the RHS of a single-line `field = <value>` assignment.

    Matches everything from `=` to end of line, so it works whether the base is
    a fresh template (`{{PLACEHOLDER}}`) or an already-rendered file.
    """
    pattern = rf'(^[ \t]*{re.escape(field)}[ \t]*=[ \t]*).*$'
    new_content, _ = re.subn(
        pattern, lambda m: m.group(1) + value, content, count=1, flags=re.MULTILINE
    )
    return new_content


def _set_eks_dependency_path(content: str, dependency_path: str) -> str:
    """Point the eks_cluster dependency (and its dependencies.paths entry) at
    `dependency_path`. Scoped to the eks_cluster block so the sibling argocd
    config_path is left untouched.
    """
    content = re.sub(
        r'(dependency\s+"eks_cluster"\s*\{[^}]*?config_path\s*=\s*)"[^"]*"',
        lambda m: m.group(1) + f'"{dependency_path}"',
        content,
        count=1,
        flags=re.DOTALL,
    )
    content = re.sub(
        r'(dependencies\s*\{\s*paths\s*=\s*\[)[^\]]*(\])',
        lambda m: m.group(1)
        + f'"../../../../argocd-applications", "{dependency_path}"'
        + m.group(2),
        content,
        count=1,
        flags=re.DOTALL,
    )
    return content


def _replace_custom_policy_json(content: str, new_value: str) -> str:
    """Replace the whole `custom_policy_json = ...` RHS in place.

    The value may be a `{{PLACEHOLDER}}`, a quoted empty string (`""`), or a
    multi-line `jsonencode({...})` block. The extent is found by paren
    balancing so the trailing `argo = {...}` block is never consumed.
    """
    m = re.search(r'custom_policy_json[ \t]*=[ \t]*', content)
    if not m:
        return content
    start = m.end()
    rest = content[start:]
    stripped = rest.lstrip()
    lead_ws = len(rest) - len(stripped)

    if stripped.startswith('"'):
        end = stripped.index('"', 1) + 1
    elif stripped.startswith('{{'):
        end = stripped.index('}}') + 2
    elif stripped.startswith('jsonencode'):
        paren_open = stripped.index('(')
        depth = 0
        end = len(stripped)
        for i in range(paren_open, len(stripped)):
            if stripped[i] == '(':
                depth += 1
            elif stripped[i] == ')':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    else:
        end = len(stripped.split('\n', 1)[0])

    value_len = lead_ws + end
    return content[:start] + new_value + content[start + value_len:]


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
    repo: str,
    base_branch: str,
    feature_branch: str,
    file_path: str,
    content: str,
    queue_id,
    script_gen_key,
):
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


class AsporaEksTerragruntScriptGenComponent:
    """
    Generates the EKS app terragrunt.hcl in the infra repo.

    Reads the selected cluster from `config_snapshot` (cluster_arn or
    cluster_name) and resolves the `dependency "eks_cluster"` config_path
    via `CLUSTER_DEPENDENCY_PATH`. Falls back to `DEFAULT_DEPENDENCY_PATH`
    when the cluster is not registered in the mapper.
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository

    @staticmethod
    async def _live_iam_policies(db, queue_dict: dict):
        """`custom_iam_policies` as the LIVE service_configs row still holds it.

        That row describes what is deployed, not what is being proposed: the
        settings saver writes a snapshot into it only once the PR has merged.
        Read here, before this change's PR exists, it is the "before" side of the
        comparison that tells a real deselection apart from a config devlift has
        no record of.

        None means "could not be read", which deletes nothing downstream.
        """
        code = queue_dict.get("transaction_code")
        tenant = queue_dict.get("tenant_code")
        if not (db and code and tenant):
            return None
        try:
            from app.repository.service_config_repository import ServiceConfigRepository
            row = await ServiceConfigRepository(db).get_by_code_and_tenant(code, tenant)
            cfg = (row.config or {}) if row else {}
            policies = cfg.get("custom_iam_policies")
            return policies if isinstance(policies, list) else None
        except Exception:
            logger.warning(
                "Could not read live custom_iam_policies for %s — no IAM statement will be "
                "removed by this run.", code, exc_info=True,
            )
            return None

    def _resolve_dependency_path(self, config_snapshot: dict) -> str:
        cluster_arn = config_snapshot.get("cluster_arn")
        cluster_name = config_snapshot.get("cluster_name") or config_snapshot.get("ecs_cluster")
        infra_mst_code = config_snapshot.get("infrastructure_mst_code")
        if cluster_arn and cluster_arn in CLUSTER_DEPENDENCY_PATH:
            return CLUSTER_DEPENDENCY_PATH[cluster_arn]
        if cluster_name and cluster_name in CLUSTER_DEPENDENCY_PATH:
            return CLUSTER_DEPENDENCY_PATH[cluster_name]
        if infra_mst_code and infra_mst_code in CLUSTER_DEPENDENCY_PATH:
            return CLUSTER_DEPENDENCY_PATH[infra_mst_code]
        self.logger.warning(
            "EKS cluster not registered in mapper (arn=%s, name=%s, infra_code=%s); falling back to %s",
            cluster_arn, cluster_name, infra_mst_code, DEFAULT_DEPENDENCY_PATH,
        )
        return DEFAULT_DEPENDENCY_PATH

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = True,
        db=None,
    ) -> str:
        config_snapshot = queue_dict.get("config_snapshot") or {}

        repo_parts = file_location.repo.split("/")
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # Refuse to overwrite an existing HCL — same guardrail the S3 component uses.
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context,
                file_location.repo,
                base_branch,
                file_location.file_path,
            )
        if cached_entry:
            existing_file = {"exists": True, "content": cached_entry.get("content")}
        else:
            existing_file = await fetch_existing_content(
                db=db,
                tenant=tenant,
                owner=owner,
                repo=repo,
                file_path=file_location.file_path,
                base_branch=base_branch,
                feature_branch=feature_branch,
                workflow_context=workflow_context,
                logger=self.logger,
                component_name=component_name,
            )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        # File may already exist (re-deploy of the same service to the same
        # env/region). When it does, we use the existing content as the base and
        # patch only the snapshot-driven fields, so manual edits to other lines
        # survive — matching the ECS/DynamoDB components. No-change detection
        # still skips the commit if the patched output is byte-for-byte equal.
        is_update = bool(existing_file.get("exists"))
        if is_update:
            self.logger.info(
                "EKS terragrunt.hcl already exists at %s — patching existing content; "
                "no-change detection will skip the commit if unchanged.",
                file_location.file_path,
            )

        dependency_path = self._resolve_dependency_path(config_snapshot)

        custom_iam_policies = config_snapshot.get("custom_iam_policies")
        live_iam_policies = await self._live_iam_policies(db, queue_dict)

        create_ecr = config_snapshot.get("create_ecr", True)
        create_secrets = config_snapshot.get("create_secrets", True)
        create_ssm = config_snapshot.get("create_ssm", True)
        create_argo = config_snapshot.get("create_argo", True)

        # Terraform owns creating the SM/SSM containers. If the snapshot has
        # create_secrets / create_ssm switched OFF but the service actually HAS
        # secrets/parameters (variable_mst rows exist from save time, staged ones
        # included), render the container ON so the variables stage right after
        # has somewhere to write. Flags whose evidence is absent stay off — no
        # empty containers forced into existence. The summary spans ALL
        # environments.
        #
        # Computed HERE rather than written back onto config_snapshot. The
        # snapshot is fingerprinted by approved_snapshot_hash at approval and
        # re-verified on every deploy, so flipping a flag on it mid-deploy broke
        # the seal: the deploy itself passed (nothing re-checks once Temporal has
        # the batch), but a FAILED deploy returns the row to APPROVED with the
        # original seal, and the retry was then refused with "changed after it
        # was approved" — plus a false `seal-broken` event written to the row's
        # history blaming the user for an edit the system had made. The approved
        # content stays immutable; only what we render differs from it.
        if not create_secrets or not create_ssm:
            from app.core.enum import WorkflowSourceTableEnum
            from app.integrations.secret_config_client import SecretConfigClient

            summary = await SecretConfigClient().get_variables_summary(
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                transaction_code=queue_dict.get("transaction_code"),
            )
            if not create_secrets and summary.get("has_secrets"):
                create_secrets = True
                self.logger.info(
                    "create_secrets is OFF on the approved snapshot but %s has "
                    "secrets — rendering the container ON (snapshot untouched)",
                    queue_dict.get("transaction_code"),
                )
            if not create_ssm and summary.get("has_variables"):
                create_ssm = True
                self.logger.info(
                    "create_ssm is OFF on the approved snapshot but %s has "
                    "variables — rendering the container ON (snapshot untouched)",
                    queue_dict.get("transaction_code"),
                )
        auth_mode = str(config_snapshot.get("auth_mode") or "pod_identity").strip().lower()
        if auth_mode not in ("irsa", "pod_identity", "none"):
            self.logger.warning("Unexpected auth_mode=%r; falling back to pod_identity", auth_mode)
            auth_mode = "pod_identity"

        gen_mode = "update" if is_update else "create"
        gen_context = (
            f"path={file_location.file_path} mode={gen_mode} dep={dependency_path} "
            f"iam_policies={custom_iam_policies or []} auth_mode={auth_mode} "
            f"create_ecr={create_ecr} create_secrets={create_secrets} "
            f"create_ssm={create_ssm} create_argo={create_argo}"
        )
        with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
            terragrunt_content = existing_file.get("content") if is_update else None
            if not terragrunt_content:
                # New file (or existing somehow empty) -> render from template.
                template_path = os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/terragrunt/eks-apps/terragrunt.hcl",
                )
                self.logger.info("Loading EKS app terragrunt template from: %s", template_path)
                async with aiofiles.open(template_path, "r") as f:
                    terragrunt_content = await f.read()

            # Patch the snapshot-driven fields on whichever base. On a fresh
            # template these fill the {{PLACEHOLDER}} tokens; on an existing file
            # they overwrite the rendered values while leaving other lines intact.
            terragrunt_content = _set_eks_dependency_path(terragrunt_content, dependency_path)
            terragrunt_content = _replace_scalar_field(
                terragrunt_content, "create_ecr", _hcl_bool(create_ecr)
            )
            terragrunt_content = _replace_scalar_field(
                terragrunt_content, "create_secrets", _hcl_bool(create_secrets)
            )
            terragrunt_content = _replace_scalar_field(
                terragrunt_content, "create_ssm", _hcl_bool(create_ssm)
            )
            terragrunt_content = _replace_scalar_field(
                terragrunt_content, "create_argo", _hcl_bool(create_argo)
            )
            terragrunt_content = _replace_scalar_field(
                terragrunt_content, "auth_mode", f'"{auth_mode}"'
            )
            # Diffed against the live row and the file, so an unchanged selection
            # leaves the policy exactly as written and a deselection only removes
            # a statement devlift itself wrote. None means "do not touch it".
            custom_policy_hcl = _build_custom_policy_hcl(
                terragrunt_content, live_iam_policies, custom_iam_policies, self.logger
            )
            if custom_policy_hcl is not None:
                terragrunt_content = _replace_custom_policy_json(
                    terragrunt_content, custom_policy_hcl
                )

            # Best-effort fmt for readability. Pass-through when no formatter
            # binary is on PATH, so functionality is unchanged.
            terragrunt_content = await hclfmt(terragrunt_content)

        identifier = (
            config_snapshot.get("identifier")
            or config_snapshot.get("service_name")
            or os.path.basename(os.path.dirname(file_location.file_path))
        )
        if identifier and not identifier.endswith('-service'):
            identifier = f"{identifier}-service"

        if upload_to_s3 and identifier:
            try:
                original_s3_key = f"eks-apps/{identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=terragrunt_content,
                    content_type="text/plain",
                )
                self.logger.info("Uploaded EKS app HCL to S3: %s", result.get("location"))

                if repository and queue_dict.get("code"):
                    update_payload = json.dumps({
                        "original_s3_key": original_s3_key,
                        "preview": original_s3_key,
                    })
                    await repository.update_artifact_s3_key(queue_dict.get("code"), update_payload)
            except Exception as e:
                self.logger.warning("Failed to upload EKS app HCL to S3: %s", e)

        if workflow_context and not workflow_context.skip_commit:
            _upsert_staged_entry(
                workflow_context=workflow_context,
                repo=file_location.repo,
                base_branch=base_branch,
                feature_branch=file_location.feature_branch,
                file_path=file_location.file_path,
                content=terragrunt_content,
                queue_id=queue_dict.get("id"),
                script_gen_key=file_location.script_gen_key,
            )
            queue_label = queue_dict.get("code") or queue_dict.get("id")
            if queue_label:
                commit_line = f"{queue_label}: {file_location.script_gen_key} -> {file_location.file_path}"
            else:
                commit_line = f"{file_location.script_gen_key} -> {file_location.file_path}"
            _append_commit_message(
                workflow_context,
                file_location.repo,
                base_branch,
                commit_line,
            )

        if queue_dict.get("id") and workflow_context:
            workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                "original_content": terragrunt_content,
                "preview_content": terragrunt_content,
                "dependency_path": dependency_path,
            }

        return terragrunt_content
