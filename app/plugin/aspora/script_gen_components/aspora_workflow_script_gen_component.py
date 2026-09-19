"""
Aspora Workflow/Pipeline Script Generation Component

Generates GitHub Actions workflow YAML files for both EKS and ECS infrastructure.
- EKS: Uses Vance shared-lib reusable workflow templates from templates/eks/workflow/
- ECS: Uses template-based generation from templates/github-actions/

Uploads to S3 and updates database with S3 keys.
"""

import os
import re
import json
import logging
import aiofiles
from typing import Dict, Any, Optional, List

from app.core.config import settings
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.yaml_patch import (
    trigger_path_pattern,
    replace_custom_build_args,
    replace_trigger_branch,
    replace_yaml_value,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


# Maps cluster ARN / cluster name / infrastructure_mst_code → cluster type label.
# Extend as new clusters are onboarded (keep in sync with CLUSTER_DEPENDENCY_PATH
# in aspora_eks_terragrunt_script_gen_component).
CLUSTER_TYPE_MAP: dict[str, str] = {
    # --- cluster ARNs ---
    "arn:aws:eks:ap-south-1:878097483768:cluster/vance-core-stage-mumbai-01-backend-cluster":      "backend",
    "arn:aws:eks:ap-south-1:878097483768:cluster/vance-core-stage-mumbai-01-application-cluster":  "application",
    "arn:aws:eks:us-east-2:383313560245:cluster/vance-core-prod-ohio-01-backend-cluster":           "backend",
    "arn:aws:eks:eu-west-2:383313560245:cluster/vance-core-prod-london-01-application-cluster":     "application",
    # --- cluster names ---
    "vance-core-stage-mumbai-01-backend-cluster":      "backend",
    "vance-core-stage-mumbai-01-application-cluster":  "application",
    "vance-core-prod-ohio-01-backend-cluster":          "backend",
    "vance-core-prod-london-01-application-cluster":    "application",
    # --- infrastructure_mst codes ---
    "infra-vance-eks-staging-mumbai-01":      "backend",
    "infra-vance-eks-staging-mumbai-app-01":  "application",
    "infra-eks-prod-canada-01":               "backend",
    "infra-eks-prod-london-app-01":           "application",
    # --- qa ---
    "arn:aws:eks:ap-south-1:418272793128:cluster/vance-core-qa-mumbai-01-application-cluster": "application",
    "vance-core-qa-mumbai-01-application-cluster":                                              "application",
    "infra-vance-eks-qa-mumbai-app-01":                                                         "application",
}

DEFAULT_CLUSTER_TYPE = "backend"

# Markers identifying which language an ECS workflow file was rendered for
# (one entry per template under templates/github-actions/). Add a line here
# when a new language template is introduced.
_ECS_LANGUAGE_MARKERS = {
    "golang": ("setup-go", "go-version:"),
    "java":   ("setup-java", "java-version:"),
    "nodejs": ("setup-node", "node-version:"),
    # python's pipeline is docker-only (no setup step exists to match) —
    # newer renders carry this explicit marker comment; older files are
    # recognized by elimination in _detect_workflow_language.
    "python": ("devlift-language: python",),
}


def _normalize_eks_service_name(service_name: str) -> str:
    """Normalize a service name to the canonical EKS form: lowercase,
    hyphen-separated, ending in '-service'.

    e.g. 'banking_service' -> 'banking-service', 'Banking' -> 'banking-service',
    'banking-service' -> 'banking-service'
    """
    normalized = re.sub(r"[\s_]+", "-", service_name.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized.endswith("-service"):
        normalized = f"{normalized}-service"
    return normalized


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


def _ecs_env_display(environment: str) -> str:
    """Short env code for ECS workflow env vars and build profiles.

    stage/staging/qa → stg, production → prod; everything else passes
    through lowercased. Mirrors what the EKS branch and the file locator
    already normalize — the ECS branch historically missed 'stage' and
    'production', which are live values in this system.
    """
    env = (environment or "").lower()
    if env in ("stage", "staging", "qa"):
        return "stg"
    if env == "production":
        return "prod"
    return env


def _normalize_go_build_path(path: str) -> str:
    """`go build cmd` resolves `cmd` as an import path and fails — a relative
    directory must be ./-prefixed. Bare values get the prefix; values already
    anchored (./, ../, /) pass through unchanged."""
    p = (path or "").strip()
    if p and not p.startswith((".", "/")):
        return f"./{p}"
    return p


def _clean_trigger_path(p) -> "str | None":
    """A path as an on.push.paths pattern: trailing slashes and any leading ./
    stripped — GitHub matches repo-relative paths with NO ./ normalization, so
    './cmd/**' never fires, for any language. (The ./ that GO needs lives on
    the golang_build_path INPUT via _normalize_go_build_path, not here.)
    '.' / './' mean the repo root — same as no filter — so they yield None."""
    p = (p or "").strip().rstrip("/")
    while p.startswith("./"):
        p = p[2:]
    return None if p in ("", ".") else p


def _trigger_path_value(line: str) -> str:
    """The path inside a trigger list line — quotes and trailing comments
    stripped, so `- 'shared/**'   # note` compares equal to `- 'shared/**'`."""
    m = re.search(r"-\s*['\"]?([^'\"#]+)", line)
    return m.group(1).strip() if m else line.strip()


def _read_yaml_value(content: str, key: str) -> str:
    """First `key: value` scalar in the file, quotes and trailing comment
    stripped. Empty string when the file has no such line."""
    m = re.search(rf"(?m)^[ \t]*{re.escape(key)}:[ \t]*[\"']?([^\"'\n#]+)", content)
    return m.group(1).strip() if m else ""


_BRANCHES_LIST = re.compile(r'(?m)^([ \t]*)branches:[ \t]*(?:#[^\n]*)?\n((?:[ \t]*-[^\n]*\n)+)')


def _paths_block(content: str):
    """Where on.push.paths lives — or, when the mapping has none, where it goes.

    Returns (indent, insert_at, block): `indent` is the branches key's, which
    paths shares; `insert_at` is right after the branches list, where a new
    block is created; `block` is a Match whose group(1) is the list body
    (possibly empty), or None when the push mapping has no paths key. None
    altogether when the file has no branches list to anchor on.

    The key is looked for anywhere in the push mapping, not only on the line
    after the branches list. KairosV2's frontend workflow puts a comment
    between the two:

        branches:
          - main
        # Only when the frontend image's inputs change …
        paths:
          - "frontend/**"

    and matching at the list's end missed it, so a redeploy inserted a second
    `paths:` key above the comment — a duplicate mapping key, which GitHub
    rejects. The mapping is scanned from the `push:` line (so a paths key
    written before branches is found too) and ends at the first non-blank,
    non-comment line indented less than `branches:`.
    """
    branches = _BRANCHES_LIST.search(content)
    if not branches:
        return None
    indent = branches.group(1)
    insert_at = branches.end()
    push = None
    for m in re.finditer(r'(?m)^([ \t]*)push:[ \t]*(?:#[^\n]*)?\n', content[:branches.start()]):
        if len(m.group(1)) < len(indent):
            push = m
    body = re.compile(
        rf'{re.escape(indent)}paths:[ \t]*(?:#[^\n]*)?(?:\n|$)((?:[ \t]*(?:-|#)[^\n]*\n)*)'
    )
    pos = push.end() if push else insert_at
    while pos < len(content):
        nl = content.find("\n", pos)
        end = len(content) if nl < 0 else nl + 1
        line = content[pos:end]
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            line_indent = len(line) - len(line.lstrip(" \t"))
            if line_indent < len(indent):
                break
            if line_indent == len(indent) and stripped.split("#", 1)[0].strip() == "paths:":
                return indent, insert_at, body.match(content, pos)
        pos = end
    return indent, insert_at, None


def _existing_trigger_paths(content: str):
    """The values in on.push.paths as the file stands, or None when the push
    mapping has no paths block at all."""
    located = _paths_block(content)
    if not located or not located[2]:
        return None
    return {
        _trigger_path_value(line)
        for line in located[2].group(1).splitlines()
        if line.strip().startswith("-")
    }


def _remove_trigger_paths(content: str, values) -> str:
    """Drop entries from on.push.paths whose path is in `values`.

    The complement to _upsert_trigger_paths' append-only rule: a hand-added
    entry can't be told from a stale UI one IN GENERAL, but an entry derived
    from a managed scalar (build_path, dockerfile_path) is provably DevLift's
    own — the old value sits on the scalar line this patcher is about to
    rewrite — so those, and only those, are safe to retire when the scalar
    changes. Callers pass the old value's derived entries, nothing broader.
    """
    values = {v for v in values if v}
    if not values:
        return content
    located = _paths_block(content)
    if not located or not located[2]:
        return content
    block = located[2]
    kept = [
        line for line in block.group(1).splitlines()
        if not (line.strip().startswith("-") and _trigger_path_value(line) in values)
    ]
    body = ("\n".join(kept) + "\n") if kept else ""
    return content[:block.start(1)] + body + content[block.end(1):]


def _upsert_trigger_paths(content: str, entries, create_if_missing: bool = True) -> str:
    """Append missing entries to on.push.paths — Kong-style: entries the UI
    knows are added when absent, existing entries are NEVER removed (a
    hand-added path is indistinguishable from a stale UI one, so deletion
    stays a manual edit). `entries` are full list lines like \"      - 'x/**'\".

    When the file has no paths block at all, one is created only if
    `create_if_missing` — a file without a filter deploys on EVERY push, and
    conjuring a filter out of base entries alone (build path + Dockerfile)
    silently narrows that on a save that configured nothing. Callers pass
    create_if_missing=True only when the user actually added other_paths.
    """
    if not entries:
        return content
    located = _paths_block(content)
    if not located:
        return content
    indent, insert_at, block = located

    if block:
        existing = {
            _trigger_path_value(line)
            for line in block.group(1).splitlines()
            if line.strip().startswith("-")
        }
        missing = [e for e in entries if _trigger_path_value(e) not in existing]
        if not missing:
            return content
        head = content[:block.end()]
        # A bare `paths:` ending the file has no newline to append after.
        if not head.endswith("\n"):
            head += "\n"
        return head + "\n".join(missing) + "\n" + content[block.end():]

    if not create_if_missing:
        return content
    new_block = f"{indent}paths:\n" + "\n".join(entries) + "\n"
    return content[:insert_at] + new_block + content[insert_at:]


def _append_commit_message(workflow_context, repo: str, base_branch: str, message: str) -> None:
    if not workflow_context or not message:
        return
    key = f"{repo}|||{base_branch}"
    existing = workflow_context.commit_messages.get(key, "")
    if existing:
        workflow_context.commit_messages[key] = f"{existing}\n{message}"
    else:
        workflow_context.commit_messages[key] = message


class AsporaWorkflowScriptGenComponent:
    """
    Component for generating workflow/pipeline YAML for Aspora tenant.

    Responsibilities:
    - Generate EKS workflow YAML (inline, via workflow_generator.py)
    - Generate ECS workflow YAML (template-based)
    - Generate preview YAML
    - Upload to S3 (original and preview)
    - Create Git commits
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository  # GitopsQueueRepository for DB operations

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
        """
        Generate workflow YAML for EKS or ECS infrastructure.

        This function:
        1. Checks if file exists in GitHub on the feature branch
        2. Always generates workflow YAML from the snapshot
           - For EKS: Inline generation via workflow_generator.py
           - For ECS: Template-based generation
        3. Generates preview YAML
        4. Uploads to S3 if requested
        5. Creates Git commit if workflow_context provided
        6. Returns the workflow YAML content

        Args:
            parameters: Dictionary containing:
                # Common parameters
                - infrastructure_type: "eks" or "ecs" (required)
                - service_name: Service name (required)
                - environment: Environment (dev, staging, prod)
                - language: Programming language (java, golang, nodejs, python)
                - github_token: GitHub API token (required)
                - github_base_url: GitHub API base URL (required)
                - owner: GitHub repository owner (required)
                - repo: GitHub repository name (required)
                - base_branch: Base branch (required)
                - identifier: Unique identifier for S3 upload
                - tenant: Tenant code

                # EKS-specific parameters
                - steps: List of workflow step configurations (optional)
                - branches: List of branches for triggers
                - build_path: Build path for triggers
                - dockerfile_path: Dockerfile path for triggers
                - additional_trigger_paths: Additional trigger paths
                - aws_region: AWS region
                - eks_cluster_name: EKS cluster name

                # ECS-specific parameters
                - branch: Single branch name
                - language_ref_code: Language reference code
                - java_version/go_version/node_version/python_version: Language version
                - build_path: Build path for monorepo support
                - dockerfile_path: Custom Dockerfile path
                - other_paths: Additional trigger paths
                - wire_enabled: Enable Wire code generation (Go only)
                - wire_path: Wire path (Go only)
                - go_use_aws_secrets: Use AWS Secrets (Go only)
                - build_args: Custom Docker build arguments

        Returns:
            Generated/updated workflow YAML content

        Raises:
            ValueError: If required parameters are missing
        """
        config_snapshot = queue_dict.get("config_snapshot") or {}
        # Log config snapshot with clear markers for troubleshooting
        repo_keys = [
            k for k in config_snapshot.keys()
            if "repo" in k.lower() or "repository" in k.lower()
        ]
        repo_values = {k: config_snapshot.get(k) for k in repo_keys}
        logger.info("=== WORKFLOW CONFIG_SNAPSHOT START ===")
        logger.info("Config snapshot repository fields: %s", repo_values)
        logger.info("Config snapshot full payload: %s", json.dumps(config_snapshot, default=str))
        logger.info("=== WORKFLOW CONFIG_SNAPSHOT END ===")
        # Extract required parameters
        # Check multiple sources for infrastructure_type: config_snapshot, file_location.config, or derive from infrastructuretype_ref_code
        infrastructure_type = config_snapshot.get('infrastructure_type')
        if not infrastructure_type and file_location and hasattr(file_location, 'config') and file_location.config:
            infrastructure_type = file_location.config.get('infrastructure_type')
        if not infrastructure_type:
            # Derive from infrastructuretype_ref_code
            infra_ref_code = config_snapshot.get('infrastructuretype_ref_code', '')
            if 'eks' in infra_ref_code.lower():
                infrastructure_type = 'eks'
            else:
                infrastructure_type = 'ecs'
        infrastructure_type = infrastructure_type.lower()
        service_name = config_snapshot.get('service_name')
        language = config_snapshot.get('language', 'golang')
        environment =config_snapshot.get('environment') or  queue_dict.get("environment")

        if not service_name:
            raise ValueError("Parameter 'service_name' is required")

        if infrastructure_type == 'eks':
            service_name = _normalize_eks_service_name(service_name)

        if infrastructure_type not in ('eks', 'ecs'):
            raise ValueError(f"infrastructure_type must be 'eks' or 'ecs', got: {infrastructure_type}")

        # Extract identifier for S3
        identifier = config_snapshot.get('identifier') or service_name

        if not identifier:
            raise ValueError("Identifier cannot be empty")

        logger.info(f"Generating {infrastructure_type.upper()} workflow for: {file_location.file_path}")
        logger.info(f"  Service: {service_name} (language: {language})")

        # github_token = config_snapshot.get('github_token')
        # github_base_url = config_snapshot.get('github_base_url')

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # Check if file exists on feature branch (updates happen here)
        logger.info(f"Checking if file exists on {feature_branch}")
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context,
                file_location.repo,
                base_branch,
                file_location.file_path
            )
        if cached_entry:
            existing_file = {
                "exists": True,
                "content": cached_entry.get("content")
            }
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
                logger=logger,
                component_name=component_name,
            )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        # File may already exist (re-deploy of the same service). When it does,
        # the existing content is the base and only the managed scalar lines
        # are patched — hand-added steps, env vars, and edited commands
        # survive. Structural blocks (paths filter, wire step, build args)
        # regenerate only for new files.
        is_update = bool(existing_file.get("exists")) and bool(existing_file.get("content"))

        # A language switch is a rebuild event, not a tweak: the whole file was
        # rendered from the old language's template (different steps, different
        # inputs) and cannot be converted by patching scalars. Re-render from
        # the new language's template instead — hand edits to the old pipeline
        # are dropped deliberately, they belong to the abandoned language.
        if is_update:
            requested_language = self._requested_language(infrastructure_type, config_snapshot)
            if requested_language:
                existing_language = self._detect_workflow_language(
                    existing_file["content"], infrastructure_type
                )
                # An EKS file states its language as a free-text input, and a
                # hand-written one spells it however its author did — 'go'
                # where a rendered file says 'golang'. Compared raw, that
                # reads as a language SWITCH and re-renders the whole file
                # from the template, destroying the hand-written pipeline the
                # locator just adopted. Normalizing puts both sides in the
                # same vocabulary. ECS is left alone: its detection already
                # returns java-gradle / java-maven, a distinction this
                # normalization would collapse.
                if infrastructure_type == 'eks' and existing_language:
                    existing_language = self._parse_language_name(existing_language)[1]
                if existing_language and self._language_mismatch(existing_language, requested_language):
                    logger.info(
                        "Workflow language changed %s -> %s — re-rendering from template",
                        existing_language, requested_language,
                    )
                    is_update = False

        if is_update:
            old_other_paths = await self._live_other_paths(db, queue_dict)
            workflow_yaml = self._patch_existing_workflow(
                existing_file["content"], infrastructure_type, config_snapshot, file_location,
                old_other_paths=old_other_paths,
            )
            logger.info(f"{infrastructure_type.upper()} workflow exists — patched existing content")
        elif infrastructure_type == 'eks':
            workflow_yaml = await self._generate_eks_workflow_yaml(
                queue_dict,
                file_location,
                repository=repository
            )
            logger.info("EKS workflow generated from template")
        else:  # ecs
            workflow_yaml = await self._generate_ecs_workflow_yaml(queue_dict, file_location)
            logger.info("ECS workflow generated from template")

        # Generate preview YAML
        preview_yaml = self._generate_preview_yaml(
            infrastructure_type=infrastructure_type,
            service_name=service_name,
            parameters=queue_dict
        )
        logger.info(f"Generated {infrastructure_type.upper()} preview YAML")

        # Upload to S3 if requested
        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            try:
                # Upload original workflow YAML
                original_s3_key = f"workflows/{infrastructure_type}/{identifier}.yml"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=workflow_yaml,
                    content_type="text/plain"
                )
                self.logger.info(f"Uploaded original workflow YAML to S3: {result['location']}")
            except Exception as e:
                self.logger.error(f"Failed to upload original workflow YAML to S3: {str(e)}")
                raise

            try:
                # Upload preview YAML
                preview_s3_key = f"preview/workflows/{infrastructure_type}/{identifier}.yml"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_yaml,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "infrastructure_type": infrastructure_type,
                        "environment": environment,
                        "identifier": identifier,
                        "service_name": service_name,
                        "language": language,
                        "generated_by": "aspora_workflow_script_gen_component"
                    }
                )
                self.logger.info(f"Uploaded preview workflow YAML to S3: {result['location']}")
            except Exception as e:
                self.logger.warning(f"Failed to upload preview workflow YAML to S3: {str(e)}")

            # Update database with S3 keys
            artifact_s3_key_json = {
                "original_s3_key": original_s3_key,
                "preview": preview_s3_key
            }

            if repository and queue_dict.get("code"):
                update_payload = json.dumps(artifact_s3_key_json)
                await repository.update_artifact_s3_key(queue_dict.get("code"), update_payload)
                self.logger.info(
                    "Saved artifact_s3_key to database for queue item %s: %s",
                    queue_dict.get("code"),
                    update_payload
                )

        # Stage Git changes for batched commit via ScriptPRWorkflowService
        if tenant and file_location and workflow_context:
            # Skip commit if in preview mode
            if not workflow_context.skip_commit:
                skip_commit = False
                existing_content = existing_file.get("content")
                if existing_file.get("exists") and existing_content:
                    skip_commit = should_skip_commit(existing_content, workflow_yaml)
                    if skip_commit:
                        self.logger.info(
                            "Workflow file unchanged on feature branch %s, skipping commit for %s",
                            feature_branch,
                            file_location.file_path
                        )

                if not skip_commit:
                    _upsert_staged_entry(
                        workflow_context=workflow_context,
                        repo=file_location.repo,
                        base_branch=base_branch,
                        feature_branch=file_location.feature_branch,
                        file_path=file_location.file_path,
                        content=workflow_yaml,
                        queue_id=queue_dict.get("id"),
                        script_gen_key=file_location.script_gen_key
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
                        commit_line
                    )

            # Update workflow context with response
            if queue_dict.get("id"):
                script_gen_key = file_location.script_gen_key
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key][file_location.base_branch] = {
                    "original_content": workflow_yaml,
                    "preview_content": preview_yaml
                }

        return workflow_yaml

    def _requested_language(self, infrastructure_type: str, config_snapshot: Dict[str, Any]):
        """Normalized language the config asks for, or None when the config
        carries no language information (then patching proceeds as usual).

        For ECS java the build tool is part of the answer (java-gradle vs
        java-maven): Maven and Gradle are different pipelines (mvnw vs
        gradlew, target vs build/libs), so switching between them must
        rebuild. EKS stays coarse — its gradle/maven templates are identical.
        """
        if infrastructure_type == 'eks':
            raw = config_snapshot.get("language_name") or config_snapshot.get("language")
            return self._parse_language_name(raw)[1] if raw else None
        language_ref_code = config_snapshot.get('language_ref_code')
        if language_ref_code:
            base = self._derive_language_from_ref_code(language_ref_code)
            if base == "java":
                # mirrors _get_template_file_for_language_ref's selection
                return "java-maven" if "MAVEN" in language_ref_code.upper() else "java-gradle"
            return base
        raw = config_snapshot.get('language')
        if not raw:
            return None
        base_lang, normalized, _ = self._parse_language_name(raw)
        if normalized == "java" and base_lang in ("java-maven", "java-gradle"):
            return base_lang
        return normalized

    @staticmethod
    def _language_mismatch(existing: str, requested: str) -> bool:
        """True when the languages genuinely differ and a rebuild is needed.

        A bare 'java' on either side (build tool undeterminable) is
        compatible with both java flavors — only java-gradle vs java-maven
        (or a different base language) forces a rebuild.
        """
        if existing == requested:
            return False
        existing_base = existing.split("-", 1)[0]
        requested_base = requested.split("-", 1)[0]
        if existing_base != requested_base:
            return True
        return "-" in existing and "-" in requested

    @staticmethod
    def _detect_workflow_language(content: str, infrastructure_type: str):
        """The language an existing workflow file was rendered for, or None
        when it cannot be told.

        EKS files carry it as an explicit `language:` input. ECS files are
        recognized by template markers — extend _ECS_LANGUAGE_MARKERS when a
        new language template lands.
        """
        if infrastructure_type == 'eks':
            m = re.search(r'(?m)^\s*language:\s*(\S+)', content)
            return m.group(1).strip() if m else None
        for language, markers in _ECS_LANGUAGE_MARKERS.items():
            if any(marker in content for marker in markers):
                if language == "java":
                    # Maven and Gradle are different pipelines — refine when
                    # the file shows its build tool, stay bare otherwise.
                    if "gradle" in content:
                        return "java-gradle"
                    if "mvnw" in content or "maven" in content:
                        return "java-maven"
                return language
        # Elimination fallback for python files rendered before the marker
        # comment existed: an ECS deploy pipeline with none of the language
        # setup steps is the docker-only python template.
        if "aws ecs update-service" in content:
            return "python"
        return None

    async def _live_other_paths(self, db, queue_dict: dict):
        """other_paths as the LIVE service_configs row still holds them — the
        PRE-change values, because the snapshot is written to the live row only
        after the PR is raised (_save_settings_for_batch). Diffing them against
        the snapshot is what lets a path deleted in the UI be deleted from the
        workflow too. None when unavailable — removal is then skipped rather
        than guessed."""
        code = queue_dict.get("transaction_code")
        tenant = queue_dict.get("tenant_code")
        if not (db and code and tenant):
            return None
        try:
            from app.repository.service_config_repository import ServiceConfigRepository
            row = await ServiceConfigRepository(db).get_by_code_and_tenant(code, tenant)
            cfg = (row.config or {}) if row else {}
            paths = cfg.get("other_paths")
            return list(paths) if isinstance(paths, list) else []
        except Exception:
            logger.warning(
                "Could not read live other_paths — skipping stale trigger-path removal",
                exc_info=True,
            )
            return None

    @staticmethod
    def _extra_trigger_patterns(config: Dict[str, Any]):
        """The on.push.paths entries the configured other_paths become."""
        return {trigger_path_pattern(p) for p in (config.get("other_paths") or [])} - {None}

    @classmethod
    def _entries_for_existing_block(
        cls, entries, paths_config: Dict[str, Any], present, old_build_path, old_dockerfile_path,
    ):
        """The entries to merge into a paths block devlift did not just create.

        The extras, always. The generator's own three lines — the build-path
        folder, the Dockerfile, the configs folder — only where the block
        already carries their previous value: a build path or Dockerfile that
        changed is REPLACED (the old entry was retired a moment ago), never
        introduced. A hand-written filter keeps exactly what its author chose
        plus what a person adds in the UI. KairosV2's frontend workflow filters
        on `frontend/**` and `.dockerignore`; a save that appended `cmd/**`,
        `Dockerfile` and `configs/cmd/**` widened its trigger to paths nobody
        configured.

        A block the file does not have is a different case: creating one with
        the extras alone would narrow "every push" down to the extras and stop
        deploys on the service's own code, so a NEW block still carries the
        full set (see _upsert_trigger_paths' create_if_missing).
        """
        def variants(p):
            raw = (p or "").strip().rstrip("/")
            bare = raw
            while bare.startswith("./"):
                bare = bare[2:]
            return {v for v in (raw, bare) if v}

        bp = _clean_trigger_path(paths_config.get("build_path"))
        df = _clean_trigger_path(paths_config.get("dockerfile_path"))
        old_bp = variants(old_build_path)
        old_df = variants(old_dockerfile_path)
        keep = set()
        if bp and any(f"{v}/**" in present for v in old_bp):
            keep.add(f"{bp}/**")
        if "configs/**" in present or any(f"configs/{v.strip('/')}/**" in present for v in old_bp):
            keep.add(f"configs/{bp.strip('/')}/**" if bp else "configs/**")
        if df and any(v in present for v in old_df):
            keep.add(df)
        wanted = cls._extra_trigger_patterns(paths_config) | keep
        return [e for e in entries if _trigger_path_value(e) in wanted]

    @staticmethod
    def _removed_other_path_patterns(old_other_paths, config_snapshot: Dict[str, Any]):
        """Trigger patterns for other_paths the user DELETED: in the live row,
        absent from the snapshot. Both raw and ./-stripped shapes, matching
        however the entry was written into the file at the time. Only runs
        when the snapshot carries the other_paths key — an older snapshot
        without it says nothing about deletions."""
        if old_other_paths is None or "other_paths" not in config_snapshot:
            return []
        new_clean = {
            _clean_trigger_path(p) for p in (config_snapshot.get("other_paths") or [])
        } - {None}
        patterns = []
        for p in old_other_paths:
            clean = _clean_trigger_path(p)
            if not clean or clean in new_clean:
                continue
            raw = (p or "").strip().rstrip("/")
            # The entry as it is written now (a file or glob goes verbatim),
            # and the two /** shapes older saves wrote for everything.
            for pattern in (trigger_path_pattern(p), f"{clean}/**", f"{raw}/**"):
                if pattern and pattern not in patterns:
                    patterns.append(pattern)
        return patterns

    def _patch_existing_workflow(
        self,
        content: str,
        infrastructure_type: str,
        config_snapshot: Dict[str, Any],
        file_location,
        old_other_paths=None,
    ) -> str:
        """Patch only the managed scalar lines of an existing workflow file.

        A field absent from the config leaves its line untouched; a key the
        file does not carry is skipped (never inserted). Structural blocks —
        paths filter, wire step, build args, run commands — are never touched.
        """
        if infrastructure_type == 'eks':
            # Read BEFORE the scalar loop rewrites them: the file's current
            # build/dockerfile path is the proof of which trigger entries are
            # DevLift's own stale output (see _remove_trigger_paths).
            old_build_path = _read_yaml_value(content, "build_path")
            old_dockerfile_path = _read_yaml_value(content, "dockerfile_path")

            svc_name = config_snapshot.get('service_name')
            if svc_name:
                content, _ = replace_yaml_value(
                    content, "service_name", _normalize_eks_service_name(svc_name)
                )

            organization = (
                config_snapshot.get('org_name')
                or config_snapshot.get('organization')
                or config_snapshot.get('product_name')
            )
            if organization:
                content, _ = replace_yaml_value(content, "organization", organization)

            environment = config_snapshot.get('environment')
            if environment:
                env_lower = environment.lower()
                env_normalized = {"staging": "stage", "production": "prod"}.get(env_lower, env_lower)
                content, _ = replace_yaml_value(content, "environment", env_normalized)

            index = config_snapshot.get('index') or config_snapshot.get('version_index')
            if index:
                content, _ = replace_yaml_value(content, "index", f'"{index}"')

            aws_region = config_snapshot.get('aws_region') or config_snapshot.get('aws-region')
            if aws_region:
                content, _ = replace_yaml_value(content, "aws_region", aws_region)

            # quoted like the template render, so create and update agree.
            # language_version is the fallback real snapshots carry (the
            # per-language keys are rarely sent — the create path has the
            # same fallback). Safe: only the file's own version line exists,
            # so a java file can never receive a go version and vice versa.
            for yaml_key, snapshot_keys, quoted in (
                ("go_version", ("go_version", "language_version"), True),
                ("java_version", ("java_version", "language_version"), True),
                ("node_version", ("node_version", "language_version"), True),
                ("python_version", ("python_version", "language_version"), True),
                ("golang_binary_name", ("golang_binary_name",), False),
                # non-golang templates carry the same input as `build_path`
                ("build_path", ("build_path",), False),
                ("dockerfile_path", ("dockerfile_path",), False),
            ):
                for snapshot_key in snapshot_keys:
                    value = config_snapshot.get(snapshot_key)
                    if value:
                        content, _ = replace_yaml_value(
                            content, yaml_key, f'"{value}"' if quoted else str(value)
                        )
                        break

            # go build needs ./-anchored directories — normalize so a bare
            # "cmd" from the UI can't break the build.
            golang_build_path = (
                config_snapshot.get('golang_build_path') or config_snapshot.get('build_path')
            )
            if golang_build_path:
                content, _ = replace_yaml_value(
                    content, "golang_build_path", _normalize_go_build_path(str(golang_build_path))
                )

            branches = config_snapshot.get('branches')
            if branches:
                branch = branches[0] if isinstance(branches, list) and branches else str(branches)
                content, _ = replace_trigger_branch(content, branch)

            # Trigger paths: append-only (Kong-style) — entries the UI knows
            # are added when missing; existing entries are never removed…
            # with ONE exception. Entries derived from build_path and
            # dockerfile_path are retired when that scalar changed: the old
            # value was on the file's own managed line, so `<old>/**` and
            # `configs/<old>/**` are provably stale DevLift output, not a
            # hand edit. Other-path entries stay append-only — their old
            # values are not in the file, so nothing proves ownership.
            if any(k in config_snapshot for k in (
                "build_path", "dockerfile_path", "other_paths", "additional_trigger_paths",
            )):
                # Both shapes of each old value: raw as legacy files carry it,
                # and ./-stripped as _generate_folder_path_filter writes now.
                def _variants(p):
                    raw = p.rstrip("/")
                    bare = raw
                    while bare.startswith("./"):
                        bare = bare[2:]
                    return {raw, bare}

                # Which of the generator's own lines the file carries — read
                # NOW, before anything is retired. See
                # _entries_for_existing_block.
                present = _existing_trigger_paths(content)

                stale = []
                new_build = str(config_snapshot.get("build_path") or "")
                if old_build_path and new_build and new_build != old_build_path:
                    for v in _variants(old_build_path):
                        stale += [f"{v}/**", f"configs/{v.strip('/')}/**"]
                new_docker = str(config_snapshot.get("dockerfile_path") or "")
                if old_dockerfile_path and new_docker and new_docker != old_dockerfile_path:
                    stale += list(_variants(old_dockerfile_path))
                # Other paths deleted in the UI: live row (old) vs snapshot (new).
                stale += self._removed_other_path_patterns(old_other_paths, config_snapshot)
                content = _remove_trigger_paths(content, stale)

                paths_config = dict(config_snapshot)
                if not paths_config.get("other_paths") and paths_config.get("additional_trigger_paths"):
                    paths_config["other_paths"] = paths_config["additional_trigger_paths"]
                _, normalized_language, _ = self._parse_language_name(
                    config_snapshot.get("language_name") or config_snapshot.get("language") or "golang"
                )
                filter_block = self._generate_folder_path_filter(paths_config, normalized_language)
                entries = [ln for ln in filter_block.splitlines() if ln.lstrip().startswith("-")]
                if present is not None:
                    entries = self._entries_for_existing_block(
                        entries, paths_config, present, old_build_path, old_dockerfile_path,
                    )
                content = _upsert_trigger_paths(
                    content, entries,
                    # A file with no filter deploys on every push — keep it
                    # that way unless the user actually configured other_paths.
                    create_if_missing=any(
                        p and str(p).strip()
                        for p in (paths_config.get("other_paths") or [])
                    ),
                )

        else:  # ecs
            environment = config_snapshot.get('environment')
            if environment:
                env_display = _ecs_env_display(environment)
                content, _ = replace_yaml_value(content, "ENVIRONMENT", env_display)

            ecr_repository = config_snapshot.get('ecr_repository')
            if ecr_repository:
                content, _ = replace_yaml_value(content, "ECR_REPOSITORY", ecr_repository)

            ecs_cluster = config_snapshot.get('ecs_cluster')
            if ecs_cluster:
                content, _ = replace_yaml_value(content, "ECS_CLUSTER", ecs_cluster)

            # Only an explicit ecs_service patches SERVICE_NAME — re-deriving it
            # from service_name here would clobber a hand-set value.
            ecs_service = config_snapshot.get('ecs_service')
            if ecs_service:
                content, _ = replace_yaml_value(content, "SERVICE_NAME", ecs_service)

            aws_role_arn = config_snapshot.get('aws_role_arn')
            if aws_role_arn:
                content, _ = replace_yaml_value(content, "role-to-assume", aws_role_arn)

            aws_region = config_snapshot.get('aws_region') or config_snapshot.get('aws-region')
            if aws_region:
                content, _ = replace_yaml_value(content, "AWS_REGION", aws_region)

            # Old python renders bake cluster/service as LITERALS into the
            # `aws ecs update-service` command (no env lines exist to patch).
            # These patterns match only literal flags — env-ref lines
            # (`--cluster ${{ env.ECS_CLUSTER }} \`) contain spaces after the
            # value token and can never match, so go/java/node files are safe.
            if ecs_cluster:
                content = re.sub(
                    r'(?m)^([ \t]*--cluster[ \t]+)[^\s\\]+([ \t]*\\?)$',
                    lambda m: m.group(1) + ecs_cluster + m.group(2),
                    content,
                )
            if ecs_service:
                content = re.sub(
                    r'(?m)^([ \t]*--service[ \t]+)[^\s\\]+([ \t]*\\?)$',
                    lambda m: m.group(1) + ecs_service + m.group(2),
                    content,
                )

            # quoting mirrors each template's own style: go/node quoted,
            # java unquoted (python templates carry no version line).
            # language_version is the fallback real snapshots carry — safe:
            # only the file's own version line exists to match.
            for yaml_key, snapshot_keys, quote in (
                ("java-version", ("java_version", "language_version"), False),
                ("go-version", ("go_version", "language_version"), True),
                ("node-version", ("node_version", "language_version"), True),
                ("python-version", ("python_version", "language_version"), True),
            ):
                value = None
                for snapshot_key in snapshot_keys:
                    value = config_snapshot.get(snapshot_key)
                    if value:
                        break
                if value:
                    content, _ = replace_yaml_value(
                        content, yaml_key, f"'{value}'" if quote else str(value)
                    )

            # same source the create path uses for {{BRANCH}}
            branch = (
                file_location.base_branch
                if file_location and file_location.base_branch
                else config_snapshot.get('branch')
            )
            if branch:
                content, _ = replace_trigger_branch(content, branch)

            # Trigger paths: append-only (Kong-style) — entries the UI knows
            # are added when missing; existing entries are never removed,
            # except other_paths the user deleted (live row vs snapshot).
            if any(k in config_snapshot for k in ("build_path", "dockerfile_path", "other_paths")):
                present = _existing_trigger_paths(content)
                content = _remove_trigger_paths(
                    content,
                    self._removed_other_path_patterns(old_other_paths, config_snapshot),
                )
                entries = self._ecs_trigger_path_lines(config_snapshot, file_location)
                if present is not None:
                    # A block devlift did not create takes the extras only —
                    # same rule as _entries_for_existing_block. An ECS file
                    # carries no build-path input to track, so nothing
                    # derived is ever replaced here either.
                    extras = self._extra_trigger_patterns(config_snapshot)
                    entries = [e for e in entries if _trigger_path_value(e) in extras]
                content = _upsert_trigger_paths(
                    content, entries,
                    create_if_missing=any(
                        p and str(p).strip()
                        for p in (config_snapshot.get("other_paths") or [])
                    ),
                )

            # Docker build args. The create path renders this block from config;
            # without the same on update, editing Build Arguments on a service
            # whose workflow already exists changed service_configs and the
            # queue snapshot but left the file byte-identical — so the deploy
            # died with "No pull request was created — no diff detected" and the
            # approval was spent on a change that could never ship.
            #
            # Keyed on the key being PRESENT: absent means this change does not
            # speak to build args, an empty list means the user removed them
            # all. replace_custom_build_args rewrites only the names outside
            # TEMPLATE_OWNED_BUILD_ARGS, so JAR_FILE / PROFILE (java) and the
            # Go secrets pair are never disturbed.
            if "build_args" in config_snapshot:
                content, _changed = replace_custom_build_args(
                    content,
                    self._build_custom_build_args(config_snapshot.get("build_args")),
                )
                if _changed:
                    self.logger.info("Rewrote docker build args in the existing ECS workflow")

        return content

    async def _generate_eks_workflow_yaml(
        self,
        parameters: Dict[str, Any],
        file_location=None,
        repository=None
    ) -> str:
        config_snapshot = parameters.get("config_snapshot") or {}
        environment = config_snapshot.get('environment') or parameters.get('environment', 'dev')
        language_name = (
            config_snapshot.get("language_name")
            or config_snapshot.get("language")
            or "golang"
        )
        lang_ref = config_snapshot.get('language_ref_code', '')
        if not language_name and lang_ref:
            language_name = self._derive_language_from_ref_code(lang_ref)
        base_lang, normalized_language, parsed_version = self._parse_language_name(language_name)

        _TEMPLATE_MAP = {
            "golang":      "workflow-go.yml",
            "java-maven":  "workflow-java-maven.yml",
            "java-gradle": "workflow-java-gradle.yml",
            "java":        "workflow-java.yml",
            "nodejs":      "workflow-nodejs.yml",
            "python":      "workflow-python.yml",
        }
        template_file = _TEMPLATE_MAP.get(base_lang) or _TEMPLATE_MAP.get(normalized_language) or "workflow-java.yml"
        template_path = os.path.join(
            os.path.dirname(__file__),
            "../../../../templates/eks/workflow",
            template_file,
        )
        try:
            async with aiofiles.open(template_path, "r") as f:
                workflow_yaml = await f.read()
        except FileNotFoundError:
            raise ValueError(f"EKS workflow template not found: {template_file}")

        env_lower = environment.lower()
        env_normalized = {"staging": "stage", "production": "prod"}.get(env_lower, env_lower)
        env_display_map = {"dev": "Dev", "stage": "Stage", "staging": "Stage", "qa": "QA", "prod": "Prod"}
        environment_display = env_display_map.get(env_lower, environment.capitalize())

        shared_lib_ref = "main"

        branches = config_snapshot.get('branches', ['main'])
        branch = branches[0] if isinstance(branches, list) and branches else str(branches)

        # org_name enriched by config_enrichment; fall back to product_name
        organization = (
            config_snapshot.get('org_name')
            or config_snapshot.get('organization')
            or config_snapshot.get('product_name', '')
        )

        index = config_snapshot.get('index') or config_snapshot.get('version_index') or '01'
        aws_region = config_snapshot.get('aws_region') or config_snapshot.get('aws-region') or ''
        lang_version = config_snapshot.get('language_version') or parsed_version or ''
        go_version = config_snapshot.get('go_version') or lang_version or '1.24'
        golang_binary_name = config_snapshot.get('golang_binary_name') or 'app'
        golang_build_path = _normalize_go_build_path(
            config_snapshot.get('golang_build_path')
            or config_snapshot.get('build_path')
            or './cmd'
        )
        java_version = config_snapshot.get('java_version') or lang_version or '17'
        node_version = config_snapshot.get('node_version') or lang_version or '20'
        python_version = config_snapshot.get('python_version') or lang_version or '3.11'
        # NOTE: build_path is deliberately NOT read here — see the {{BUILD_PATH}}
        # strip below. Go's path arrives as golang_build_path above, and the
        # trigger-path filter reads build_path from the config itself.
        dockerfile_path = config_snapshot.get('dockerfile_path') or 'Dockerfile'

        workflow_yaml = workflow_yaml.replace("{{BRANCH}}", branch)

        # Trigger paths filter. The UI sends additional_trigger_paths for EKS;
        # the shared helper reads other_paths, so map it across.
        paths_config = dict(config_snapshot)
        if not paths_config.get("other_paths") and paths_config.get("additional_trigger_paths"):
            paths_config["other_paths"] = paths_config["additional_trigger_paths"]
        folder_path_filter = self._generate_folder_path_filter(paths_config, normalized_language)
        if folder_path_filter:
            workflow_yaml = workflow_yaml.replace("{{FOLDER_PATH_FILTER}}", folder_path_filter)
        else:
            # Drop the placeholder line entirely so no blank line is left behind.
            workflow_yaml = re.sub(r'[ \t]*\{\{FOLDER_PATH_FILTER\}\}\n?', '', workflow_yaml)

        cluster_arn = config_snapshot.get("cluster_arn") or ""
        cluster_name = config_snapshot.get("cluster_name") or config_snapshot.get("ecs_cluster") or ""
        infra_code = config_snapshot.get("infrastructure_mst_code") or ""
        cluster_type = (
            CLUSTER_TYPE_MAP.get(cluster_arn)
            or CLUSTER_TYPE_MAP.get(cluster_name)
            or CLUSTER_TYPE_MAP.get(infra_code)
            or DEFAULT_CLUSTER_TYPE
        )

        workflow_yaml = workflow_yaml.replace("{{CLUSTER_TYPE}}", cluster_type)
        workflow_yaml = workflow_yaml.replace("{{SHARED_LIB_REF}}", shared_lib_ref)
        workflow_yaml = workflow_yaml.replace(
            "{{SKIP_SLACK}}", "true" if settings.deploy_skip_slack_notification else "false"
        )
        svc_name = config_snapshot.get('service_name', '')
        if svc_name:
            svc_name = _normalize_eks_service_name(svc_name)
        workflow_yaml = workflow_yaml.replace("{{SERVICE_NAME}}", svc_name)
        workflow_yaml = workflow_yaml.replace("{{ORGANIZATION}}", organization)
        workflow_yaml = workflow_yaml.replace("{{ENVIRONMENT}}", env_normalized)
        workflow_yaml = workflow_yaml.replace("{{INDEX}}", str(index))
        workflow_yaml = workflow_yaml.replace("{{AWS_REGION}}", aws_region)
        workflow_yaml = workflow_yaml.replace("{{LANGUAGE}}", normalized_language)
        workflow_yaml = workflow_yaml.replace("{{GO_VERSION}}", str(go_version))
        workflow_yaml = workflow_yaml.replace("{{GOLANG_BINARY_NAME}}", golang_binary_name)
        workflow_yaml = workflow_yaml.replace("{{GOLANG_BUILD_PATH}}", golang_build_path)
        workflow_yaml = workflow_yaml.replace("{{JAVA_VERSION}}", str(java_version))
        workflow_yaml = workflow_yaml.replace("{{NODE_VERSION}}", str(node_version))
        workflow_yaml = workflow_yaml.replace("{{PYTHON_VERSION}}", str(python_version))
        # gradle_profile: only the java-gradle template carries the placeholder,
        # and only prod pins it — non-prod drops the line so shared-lib's own
        # default applies.
        if env_normalized == "prod":
            workflow_yaml = workflow_yaml.replace("{{GRADLE_PROFILE}}", "prod")
        else:
            workflow_yaml = re.sub(r'[ \t]*\S[^\n]*\{\{GRADLE_PROFILE\}\}\n?', '', workflow_yaml)
        # build_path is NEVER emitted into an EKS workflow: shared-lib's
        # build.yaml declares `golang_build_path` (default "./cmd") and no bare
        # `build_path` input at all. Every template _TEMPLATE_MAP can select
        # that carries {{BUILD_PATH}} is a NON-Go one (java / java-gradle /
        # java-maven / nodejs / python) passing it straight to that reusable
        # workflow — and GitHub refuses the whole run on an undeclared input:
        # "Invalid input, build_path is not defined in the referenced
        # workflow". Not a bad build: no checkout, no logs, nothing.
        #
        # It used to be emitted whenever the value was truthy, so any Java
        # service whose config carried one — set while the UI still offered the
        # field, inherited from a Go -> Java language switch, or copied by
        # clone-settings — shipped a workflow that could not start.
        #
        # Go is unaffected: workflow-go.yml carries {{GOLANG_BUILD_PATH}}, its
        # own placeholder, substituted above. The sub below is a no-op there.
        #
        # If Java ever genuinely needs a build path (a Gradle monorepo does),
        # the fix is to ADD the input to shared-lib's build.yaml — stripping it
        # here only makes the run start, it cannot make it build the right
        # directory.
        workflow_yaml = re.sub(r'[ \t]*\S[^\n]*\{\{BUILD_PATH\}\}\n?', '', workflow_yaml)
        workflow_yaml = workflow_yaml.replace("{{DOCKERFILE_PATH}}", dockerfile_path)

        return workflow_yaml

    def _generate_folder_path_filter(self, config: dict, language: str) -> str:
        """Generate folder path filter for workflow trigger.

        - For Go: no paths filter (triggers on any push to branch)
        - If other_paths provided: add build_path, dockerfile_path, other_paths
        - If no other_paths: only add if BOTH build_path AND dockerfile_path exist
        - If any paths: include configs/**
        """
        if language == "golang":
            return ""

        build_path = config.get("build_path")
        dockerfile_path = config.get("dockerfile_path")
        other_paths = config.get("other_paths") or []

        build_path_clean = _clean_trigger_path(build_path)
        dockerfile_path_clean = _clean_trigger_path(dockerfile_path)
        has_other_paths = any(p and p.strip() for p in other_paths)

        path_lines = []

        if has_other_paths:
            if build_path_clean:
                path_lines.append(f"      - '{build_path_clean}/**'")
            if dockerfile_path_clean:
                path_lines.append(f"      - '{dockerfile_path_clean}'")
        else:
            if build_path_clean and dockerfile_path_clean:
                path_lines.append(f"      - '{build_path_clean}/**'")
                path_lines.append(f"      - '{dockerfile_path_clean}'")

        # Folders get /**; a file or a glob is written as it is — see
        # trigger_path_pattern.
        for path in other_paths:
            pattern = trigger_path_pattern(path)
            if pattern:
                path_lines.append(f"      - '{pattern}'")

        if path_lines:
            sanitized = (_clean_trigger_path(build_path) or "").strip("/") or None
            if sanitized:
                path_lines.append(f"      - 'configs/{sanitized}/**'")
            else:
                path_lines.append("      - 'configs/**'")

        if path_lines:
            return "    paths:\n" + "\n".join(path_lines)
        return ""

    async def _generate_ecs_workflow_yaml(self, parameters: Dict[str, Any], file_location) -> str:
        """
        Generate ECS workflow YAML using template-based approach.

        Args:
            parameters: Parameter dictionary
            file_location: FileLocationItem containing base_branch information

        Returns:
            Generated workflow YAML string
        """
        # Extract ECS-specific parameters
        config_snapshot=parameters.get('config_snapshot')
        language_ref_code = config_snapshot.get('language_ref_code', '')

        # Get template file for language using language_ref_code
        template_file = self._get_template_file_for_language_ref(language_ref_code)

        # Load template
        try:
            async with aiofiles.open(template_file, 'r') as f:
                template_content = await f.read()
            self.logger.info(f"Loaded ECS workflow template from: {template_file}")
        except FileNotFoundError:
            self.logger.error(f"Template not found: {template_file}")
            raise ValueError(f"ECS workflow template not found for language: {language_ref_code}")

        # Replace placeholders
        workflow_yaml = self._replace_ecs_placeholders(
            template_content=template_content,
            parameters=parameters,
            file_location=file_location
        )

        return workflow_yaml

    def _replace_ecs_placeholders(
        self,
        template_content: str,
        parameters: Dict[str, Any],
        file_location
    ) -> str:
        """
        Replace ECS template placeholders with actual values.

        Args:
            template_content: Template content with placeholders
            parameters: Parameter dictionary
            file_location: FileLocationItem containing base_branch information

        Returns:
            Template with placeholders replaced
        """
        config_snapshot = parameters.get("config_snapshot") or {}
        service_name = config_snapshot.get('service_name', 'my-service')
        environment = config_snapshot.get('environment') or parameters.get('environment', 'dev')
        branch = file_location.base_branch if file_location else config_snapshot.get('branch', 'main')
        language_ref_code = config_snapshot.get('language_ref_code', '')
        # Derive language from language_ref_code (more reliable than 'language' field)
        language = self._derive_language_from_ref_code(language_ref_code) if language_ref_code else config_snapshot.get('language', 'golang')

        # Sanitize service name for ECS_SERVICE
        sanitized_service_name = service_name.replace(" ", "_").replace("-", "_").lower()

        # Map environment for display (stage/staging/qa -> stg, production ->
        # prod). Use original environment name for workflow name/run-name.
        env_display = _ecs_env_display(environment)
        env_for_name = environment

        # Extract ECS parameters (now enriched by script_pr_workflow_service with infrastructure values)
        ecr_repository = config_snapshot.get('ecr_repository') or parameters.get('ecr_repository', '')
        if not ecr_repository:
            self.logger.warning("ECR repository not found in config_snapshot - workflow may be incomplete")

        ecs_cluster = config_snapshot.get('ecs_cluster') or parameters.get('ecs_cluster', '')
        aws_role_arn = config_snapshot.get('aws_role_arn') or parameters.get('aws_role_arn', '')
        aws_region = config_snapshot.get('aws_region') or config_snapshot.get('aws-region') or parameters.get('aws_region', 'us-east-1')

        # Get language version
        java_version = config_snapshot.get('java_version') or parameters.get('java_version') or config_snapshot.get('language_version', '')
        go_version = config_snapshot.get('go_version') or parameters.get('go_version') or config_snapshot.get('language_version', '')
        node_version = config_snapshot.get('node_version') or parameters.get('node_version') or config_snapshot.get('language_version', '')
        python_version = config_snapshot.get('python_version') or parameters.get('python_version') or config_snapshot.get('language_version', '')

        # Get build/Dockerfile parameters
        build_path = config_snapshot.get('build_path') or parameters.get('build_path', '')
        dockerfile_path = config_snapshot.get('dockerfile_path') or parameters.get('dockerfile_path', '')
        other_paths = config_snapshot.get('other_paths') or parameters.get('other_paths', [])

        # Go-specific parameters
        wire_enabled = config_snapshot.get('wire_enabled') or parameters.get('wire_enabled', False)
        wire_path = config_snapshot.get('wire_path') or parameters.get('wire_path', '')
        go_use_aws_secrets = config_snapshot.get('go_use_aws_secrets') or parameters.get('go_use_aws_secrets', False)
        build_args = config_snapshot.get('build_args') or parameters.get('build_args', [])

        # Start replacing placeholders
        yaml_content = template_content

        # Basic placeholders
        yaml_content = yaml_content.replace("{{SERVICE_NAME}}", service_name)
        yaml_content = yaml_content.replace("{{SERVICE_CODE}}", sanitized_service_name)
        yaml_content = yaml_content.replace("{{ENVIRONMENT_NAME}}", env_for_name)  # For workflow name/run-name
        yaml_content = yaml_content.replace("{{ENVIRONMENT}}", env_display)  # For env vars and other places
        yaml_content = yaml_content.replace("{{BRANCH}}", branch)
        yaml_content = yaml_content.replace("{{ECR_REPOSITORY}}", ecr_repository)
        yaml_content = yaml_content.replace("{{ECS_CLUSTER}}", ecs_cluster)
        yaml_content = yaml_content.replace("{{AWS_ROLE_ARN}}", aws_role_arn)
        yaml_content = yaml_content.replace("{{AWS_REGION}}", aws_region)

        # Language version placeholders
        yaml_content = yaml_content.replace("{{JAVA_VERSION}}", java_version)
        yaml_content = yaml_content.replace("{{GO_VERSION}}", go_version)
        yaml_content = yaml_content.replace("{{NODE_VERSION}}", node_version)
        yaml_content = yaml_content.replace("{{PYTHON_VERSION}}", python_version)

        # ECS_SERVICE - needs to be dynamically generated
        ecs_service = config_snapshot.get('ecs_service') or parameters.get('ecs_service', f"{sanitized_service_name}-{env_display}")
        yaml_content = yaml_content.replace("{{ECS_SERVICE}}", ecs_service)

        # GitHub environment approval gate (prod deployments only)
        if (environment or "").lower() in ("prod", "production"):
            yaml_content = yaml_content.replace("{{GITHUB_ENVIRONMENT}}", "    environment: prod\n")
        else:
            yaml_content = yaml_content.replace("{{GITHUB_ENVIRONMENT}}", "")

        # Dockerfile flag
        if dockerfile_path:
            yaml_content = yaml_content.replace("{{DOCKERFILE_FLAG}}", f"--file {dockerfile_path} \\\n            ")
        else:
            yaml_content = yaml_content.replace("{{DOCKERFILE_FLAG}}", "")

        # Path filters — computed by a shared method so the create render and
        # the update append use identical rules.
        is_go_language = language.lower() in ('golang', 'go') or language_ref_code.upper().startswith("GO")
        build_path_clean = build_path.rstrip('/') if build_path else None
        dockerfile_path_clean = dockerfile_path.rstrip('/') if dockerfile_path else None
        path_lines = self._ecs_trigger_path_lines(config_snapshot, file_location)

        if path_lines:
            yaml_content = yaml_content.replace("{{FOLDER_PATH_FILTER}}", f"    paths:\n" + "\n".join(path_lines))
        else:
            yaml_content = yaml_content.replace("{{FOLDER_PATH_FILTER}}", "")

        # Go-specific placeholders
        if is_go_language:
            # BUILD_PATH for Go — ./-anchored so `go build` treats it as a
            # directory, not an import path.
            yaml_content = yaml_content.replace(
                "{{BUILD_PATH}}",
                _normalize_go_build_path(build_path_clean) if build_path_clean else ".",
            )

            # Wire step
            if wire_enabled and wire_path:
                wire_step = f"""
      - name: Regenerate Wire
        run: |
          go install github.com/google/wire/cmd/wire@latest
          wire {wire_path}
"""
                yaml_content = yaml_content.replace("{{WIRE_STEP}}", wire_step)
            else:
                yaml_content = yaml_content.replace("{{WIRE_STEP}}", "")

            # Go Docker build args (AWS Secrets)
            if go_use_aws_secrets:
                config_env = _ecs_env_display(environment)
                go_docker_args = f"""            --build-arg AWS_SECRETS_MANAGER_NAME=${{{{ secrets.AWS_SECRETS_MANAGER_NAME }}}} \\
            --build-arg CONFIG_ENV={config_env} \\
"""
                yaml_content = yaml_content.replace("{{GO_DOCKER_BUILD_ARGS}}", go_docker_args)
            else:
                yaml_content = yaml_content.replace("{{GO_DOCKER_BUILD_ARGS}}", "")
        else:
            # Clear Go-specific placeholders for non-Go languages
            yaml_content = yaml_content.replace("{{BUILD_PATH}}", build_path_clean if build_path_clean else ".")
            yaml_content = yaml_content.replace("{{WIRE_STEP}}", "")
            yaml_content = yaml_content.replace("{{GO_DOCKER_BUILD_ARGS}}", "")

        # Custom build args (all languages)
        custom_build_args = self._build_custom_build_args(build_args)
        yaml_content = yaml_content.replace("{{CUSTOM_BUILD_ARGS}}", custom_build_args)

        # JAR path for Java builds
        is_maven = "MAVEN" in language_ref_code.upper()
        jar_dir = "target" if is_maven else "build/libs"
        if build_path_clean:
            yaml_content = yaml_content.replace("{{JAR_PATH}}", f"{build_path_clean}/{jar_dir}/*.jar")
        else:
            yaml_content = yaml_content.replace("{{JAR_PATH}}", f"{jar_dir}/*.jar")

        return yaml_content

    def _ecs_trigger_path_lines(self, config_snapshot: Dict[str, Any], file_location) -> List[str]:
        """The on.push.paths entries an ECS workflow should carry, from the
        config. Same rules the template render always used:
        - Go: no build/dockerfile path entries (triggers on any push)
        - other_paths present (monorepo): build_path/dockerfile added independently
        - no other_paths: build_path/dockerfile only when BOTH are set
        - any service path present: the workflow file itself is included
        """
        language_ref_code = config_snapshot.get('language_ref_code', '')
        language = (
            self._derive_language_from_ref_code(language_ref_code)
            if language_ref_code else config_snapshot.get('language', 'golang')
        )
        is_go_language = language.lower() in ('golang', 'go') or language_ref_code.upper().startswith("GO")

        build_path = config_snapshot.get('build_path') or ''
        dockerfile_path = config_snapshot.get('dockerfile_path') or ''
        other_paths = config_snapshot.get('other_paths') or []

        build_path_clean = _clean_trigger_path(build_path)
        dockerfile_path_clean = _clean_trigger_path(dockerfile_path)
        has_other_paths = bool(other_paths)

        if has_other_paths:
            should_add_build_path = build_path_clean and not is_go_language
            should_add_dockerfile_path = dockerfile_path_clean and not is_go_language
        else:
            should_add_build_path = build_path_clean and dockerfile_path_clean and not is_go_language
            should_add_dockerfile_path = dockerfile_path_clean and build_path_clean and not is_go_language

        has_service_paths = should_add_build_path or has_other_paths

        path_lines = []
        if should_add_build_path:
            path_lines.append(f"      - '{build_path_clean}/**'")
        for other_path in other_paths:
            pattern = trigger_path_pattern(other_path)
            if pattern:
                path_lines.append(f"      - '{pattern}'")
        if has_service_paths or should_add_dockerfile_path:
            workflow_file_path = file_location.file_path if file_location else None
            if workflow_file_path:
                path_lines.append(f"      - '{workflow_file_path}'")
            if should_add_dockerfile_path:
                path_lines.append(f"      - '{dockerfile_path_clean}'")
        return path_lines

    def _build_custom_build_args(self, build_args: Optional[List[Dict[str, Any]]]) -> str:
        if not build_args:
            return ""

        valid_args = []
        for arg in build_args:
            name = (arg.get("name") or arg.get("key") or "").strip()
            if not name:
                continue
            value = (arg.get("value") or "").strip()
            if value:
                valid_args.append(f'            --build-arg {name}="{value}" \\')
            else:
                valid_args.append(f'            --build-arg {name}="${{{{ secrets.{name} }}}}" \\')

        if not valid_args:
            return ""

        return "\n".join(valid_args) + "\n"

    def _generate_preview_yaml(
        self,
        infrastructure_type: str,
        service_name: str,
        parameters: Dict[str, Any]
    ) -> str:
        """
        Generate preview YAML for display.

        Args:
            infrastructure_type: "eks" or "ecs"
            service_name: Service name
            parameters: Parameter dictionary

        Returns:
            Preview YAML string
        """
        config_snapshot=parameters.get('config_snapshot')
        environment = parameters.get('environment', 'dev')
        language = config_snapshot.get('language', 'golang')

        preview_lines = []
        preview_lines.append("#" * 60)
        preview_lines.append(f"# {infrastructure_type.upper()} Workflow Configuration Preview")
        preview_lines.append("#" * 60)
        preview_lines.append("")
        preview_lines.append(f"Service: {service_name}")
        preview_lines.append(f"Infrastructure: {infrastructure_type.upper()}")
        preview_lines.append(f"Environment: {environment}")
        preview_lines.append(f"Language: {language}")
        preview_lines.append("")

        if infrastructure_type == 'eks':
            preview_lines.append("Workflow includes:")
            steps = parameters.get('steps')
            if steps:
                enabled_steps = [s for s in steps if s.get('enabled', False)]
                for step in enabled_steps:
                    step_name = step.get('name', step.get('id', 'unknown'))
                    preview_lines.append(f"  - {step_name}")
            else:
                preview_lines.append("  - Default EKS workflow steps (code checkout, build, deploy)")
            preview_lines.append("")
            preview_lines.append("AWS Resources:")
            preview_lines.append(f"  - EKS Cluster: {parameters.get('eks_cluster_name', 'N/A')}")
            preview_lines.append(f"  - Region: {parameters.get('aws_region', 'N/A')}")
        else:  # ecs
            preview_lines.append("Workflow includes:")
            preview_lines.append("  - Build application (language-specific)")
            preview_lines.append("  - Build & push Docker image to ECR")
            preview_lines.append("  - Force ECS deployment")
            preview_lines.append("")
            preview_lines.append("AWS Resources:")
            preview_lines.append(f"  - ECS Cluster: {parameters.get('ecs_cluster', 'N/A')}")
            preview_lines.append(f"  - ECR Repository: {parameters.get('ecr_repository', 'N/A')}")
            preview_lines.append(f"  - Region: {parameters.get('aws_region', 'N/A')}")
            preview_lines.append(f"  - IAM Role: {parameters.get('aws_role_arn', 'N/A')}")

        preview_lines.append("")
        preview_lines.append("#" * 60)
        preview_lines.append("# End of Preview")
        preview_lines.append("#" * 60)

        return "\n".join(preview_lines)

    def _get_template_file_for_language(self, language: str) -> str:
        """
        Get template file path for a given language.

        Args:
            language: Programming language

        Returns:
            Absolute path to template file
        """
        # Normalize language
        language_lower = language.lower()

        # Map language to template file
        template_map = {
            'golang': 'go.yml',
            'go': 'go.yml',
            'java': 'java-maven.yml',  # Default to Maven
            'nodejs': 'nodejs.yml',
            'node.js': 'nodejs.yml',
            'python': 'python.yml',
        }

        # Check language_ref_code for build type
        language_ref_code = self._get_from_params('language_ref_code', '')
        if language_ref_code:
            if 'gradle' in language_ref_code.lower():
                template_map['java'] = 'java-gradle.yml'

        template_file = template_map.get(language_lower, 'go.yml')

        # Build absolute path
        template_path = os.path.join(
            os.path.dirname(__file__),
            "../../../../templates/github-actions",
            template_file
        )

        return template_path

    def _get_template_file_for_language_ref(self, language_ref_code: str) -> str:
        """
        Get template file path based on language_ref_code from database.

        This method properly maps language_ref_code (like JAVA_21, GO_1_23, etc.)
        to the correct template file.

        Args:
            language_ref_code: Language reference code (e.g., JAVA_21, JAVA-MAVEN_21, GO_1_23)

        Returns:
            Absolute path to template file
        """
        lang_ref_upper = language_ref_code.upper()

        # Determine template filename based on language_ref_code pattern
        if 'JAVA' in lang_ref_upper:
            # Check if it's Maven or Gradle
            if 'MAVEN' in lang_ref_upper:
                template_file = 'java-maven.yml'
            else:
                # Default Java projects (JAVA_21, JAVA_17, etc.) use Gradle
                template_file = 'java-gradle.yml'
        elif 'GO' in lang_ref_upper or 'GOLANG' in lang_ref_upper:
            template_file = 'go.yml'
        elif 'NODE' in lang_ref_upper or 'JS' in lang_ref_upper:
            template_file = 'nodejs.yml'
        elif 'PYTHON' in lang_ref_upper:
            template_file = 'python.yml'
        else:
            # Default fallback
            self.logger.warning(f"Unknown language_ref_code: {language_ref_code}, defaulting to go.yml")
            template_file = 'go.yml'

        # Build absolute path
        template_path = os.path.join(
            os.path.dirname(__file__),
            "../../../../templates/github-actions",
            template_file
        )

        self.logger.info(f"Selected template for {language_ref_code}: {template_file}")
        return template_path

    def _derive_language_from_ref_code(self, language_ref_code: str) -> str:
        """
        Derive normalized language name from language_ref_code.

        Args:
            language_ref_code: Language reference code (e.g., JAVA_21, GO_1_23, NODEJS_20)

        Returns:
            Normalized language name (java, golang, nodejs, python)
        """
        lang_ref_upper = language_ref_code.upper()

        if 'JAVA' in lang_ref_upper:
            return 'java'
        elif 'GO' in lang_ref_upper or 'GOLANG' in lang_ref_upper:
            return 'golang'
        elif 'NODE' in lang_ref_upper or 'JS' in lang_ref_upper:
            return 'nodejs'
        elif 'PYTHON' in lang_ref_upper:
            return 'python'
        else:
            self.logger.warning(f"Unknown language_ref_code: {language_ref_code}, defaulting to golang")
            return 'golang'  # Default

    def _normalize_language_for_eks(self, language: str) -> str:
        """
        Normalize language name for EKS workflow generator.

        Args:
            language: Language name

        Returns:
            Normalized language name (java, golang, nodejs, python)
        """
        language_lower = language.lower()

        if language_lower in ('java', 'java-maven', 'java-gradle'):
            return 'java'
        elif language_lower in ('golang', 'go'):
            return 'golang'
        elif language_lower in ('nodejs', 'node.js', 'node'):
            return 'nodejs'
        elif language_lower == 'python':
            return 'python'
        else:
            return 'golang'  # Default

    def _parse_language_name(self, language_name: str) -> tuple[str, str, str]:
        """
        Parse language name to extract base language, normalized name, and version.
        """
        lang_lower = (language_name or "").lower().strip()

        if lang_lower.startswith("go") or lang_lower.startswith("golang"):
            import re
            version_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', lang_lower)
            version = version_match.group(1) if version_match else "1.24.0"
            return ("go", "golang", version)

        if "java" in lang_lower:
            import re
            if "maven" in lang_lower:
                base_lang = "java-maven"
            elif "gradle" in lang_lower:
                base_lang = "java-gradle"
            else:
                base_lang = "java"

            version_match = re.search(r'(\d+)', lang_lower)
            version = version_match.group(1) if version_match else "17"
            return (base_lang, "java", version)

        if "node" in lang_lower:
            import re
            version_match = re.search(r'(\d+)', lang_lower)
            version = version_match.group(1) if version_match else "20"
            return ("nodejs", "nodejs", version)

        if "python" in lang_lower:
            import re
            version_match = re.search(r'(\d+\.\d+)', lang_lower)
            version = version_match.group(1) if version_match else "3.11"
            return ("python", "python", version)

        return (lang_lower, lang_lower, "")

    def _get_default_eks_steps(self) -> List[Dict[str, Any]]:
        """
        Get default EKS workflow steps.

        Returns:
            List of default workflow step configurations
        """
        return [
            {
                "id": "code-checkout",
                "name": "Checkout Code",
                "enabled": True,
                "order": 1,
                "dependsOn": [],
                "category": "setup",
                "mandatory": True
            },
            {
                "id": "code-quality-check",
                "name": "Code Quality Check",
                "enabled": True,
                "order": 2,
                "dependsOn": ["code-checkout"],
                "category": "quality",
                "mandatory": False
            },
            {
                "id": "build",
                "name": "Build Application",
                "enabled": True,
                "order": 3,
                "dependsOn": ["code-quality-check"],
                "category": "build",
                "mandatory": True
            },
            {
                "id": "docker-build",
                "name": "Build Docker Image",
                "enabled": True,
                "order": 4,
                "dependsOn": ["build"],
                "category": "build",
                "mandatory": True
            },
            {
                "id": "deploy",
                "name": "Deploy to EKS",
                "enabled": True,
                "order": 5,
                "dependsOn": ["docker-build"],
                "category": "deploy",
                "mandatory": True
            }
        ]

    def _get_from_params(self, key: str, default=None):
        """Helper to get value from parameters (for use in template mapping)."""
        # This is a workaround for accessing parameters in _get_template_file_for_language
        # In practice, parameters should be passed explicitly
        return default
