"""
Aspora Atlantis Script Generation Component

Generates or updates atlantis.yaml by inserting project entries for ECS or
standalone infrastructure using the same naming and branch logic as existing
Terragrunt services.
"""

import json
import logging
import re
from collections import OrderedDict
from functools import lru_cache

from typing import Any, Dict, Optional, Tuple

import yaml

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

VANCE_ASPORA_TENANTS = {"vance", "aspora"}


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


@lru_cache(maxsize=4)
def _projects_by_dir(atlantis_content: str) -> Dict[str, Tuple[str, ...]]:
    """{normalized dir: (project name, ...)} for every entry under `projects:`.

    Both readers run and their results merge, because each covers the other's
    blind spot. YAML is indifferent to key order and indentation but dies on a
    bad edit — and, worse, silently keeps only the LAST of two top-level
    `projects:` keys, which would hide every project in the first block. The
    line scan survives both; it stops at `workflows:`, whose steps are list
    items too. A dir either reader sees counts as present: a false positive
    costs one entry not added (someone re-runs), a false negative costs a
    duplicate project in the deploy path.

    Cached because the live file is ~240KB / ~1,350 projects — about 300ms to
    parse — and every queue item in a batch re-reads the same content.
    """
    by_dir: "OrderedDict[str, list]" = OrderedDict()

    def _add(name: str, raw_dir: str) -> None:
        key = AsporaAtlantisScriptGenComponent._normalize_dir(raw_dir)
        if not key:
            return
        names = by_dir.setdefault(key, [])
        if name not in names:
            names.append(name)

    try:
        doc = yaml.safe_load(atlantis_content)
        projects = (doc or {}).get("projects") if isinstance(doc, dict) else None
        if isinstance(projects, list):
            for project in projects:
                if isinstance(project, dict) and project.get("dir"):
                    _add(str(project.get("name") or ""), str(project["dir"]))
    except Exception:  # noqa: BLE001 — a malformed file is read by the scan alone
        logger.info("atlantis.yaml could not be parsed as YAML — using the line scan")

    current: Dict[str, str] = {}
    for raw in atlantis_content.splitlines():
        stripped = raw.strip()
        if stripped.startswith("workflows:"):
            break
        if stripped.startswith("- "):
            if current.get("dir"):
                _add(current.get("name", ""), current["dir"])
            current = {}
            stripped = stripped[2:].strip()
        # `dir: foo  # note` — YAML drops the comment, so the scan must too,
        # or the two readers would key the same project differently.
        if stripped.startswith("name:"):
            current["name"] = stripped[len("name:"):].split(" #")[0].strip()
        elif stripped.startswith("dir:"):
            current["dir"] = stripped[len("dir:"):].split(" #")[0].strip()
    if current.get("dir"):
        _add(current.get("name", ""), current["dir"])

    return {k: tuple(v) for k, v in by_dir.items()}


class AsporaAtlantisScriptGenComponent:
    """
    Component for generating atlantis.yaml project entries.

    Responsibilities:
    - Fetch existing atlantis.yaml from GitHub (feature branch)
    - Insert ECS or standalone infra project entry with correct naming
    - Upload original/preview to S3 and update DB artifact keys
    - Commit updated atlantis.yaml (unless in preview mode)
    """

    INFRA_TYPE_CONFIG = {
        "s3": {"suffix": "bucket"},
        "s3_infrastructuretype_ref": {"suffix": "bucket"},
        "sqs": {"suffix": "queue"},
        "sqs_infrastructuretype_ref": {"suffix": "queue"},
        "dynamodb": {"suffix": "table"},
        "dynamodb_infrastructuretype_ref": {"suffix": "table"},
        "gateway": {"suffix": "gateway"},
        "gateway_infrastructuretype_ref": {"suffix": "gateway"},
    }

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository

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
        Generate or update atlantis.yaml content.

        This function:
        1. Fetches atlantis.yaml from GitHub feature branch
        2. Inserts ECS or standalone infra entry (if missing)
        3. Uploads original/preview to S3 if requested
        4. Commits updated atlantis.yaml unless in preview mode
        """
        config_snapshot = queue_dict.get("config_snapshot") or {}

        environment = config_snapshot.get("environment") or queue_dict.get("environment")
        if not environment:
            raise ValueError("Environment is required to build atlantis entry")

        infra_type = (
            config_snapshot.get("infra_type")
            or config_snapshot.get("infrastructure_type")
            or queue_dict.get("infra_type")
            or getattr(file_location, "infra_type_ref", None)
            or ""
        )
        case_ref_code = queue_dict.get("case_ref_code") or config_snapshot.get("case_ref_code") or ""
        case_type_to_infra_type = {
            "create_queue": "sqs",
            "create_bucket":"s3",
            "table_management":"dynamodb",
            # Gateway items arrive as 'add_route' with no infrastructuretype_ref_code,
            # so infra_type would stay 'add_route' and miss INFRA_TYPE_CONFIG entirely.
            "add_route": "gateway",
        }
        if case_ref_code in case_type_to_infra_type:
            if not infra_type or infra_type == case_ref_code:
                infra_type = case_type_to_infra_type[case_ref_code]

        # queue_dict is the last fallback for routing: a group-keyed gateway row
        # stores only its change set, so product/region live nowhere on the snapshot.
        # ScriptPRWorkflowService resolves them from the route group at deploy time
        # and puts them on queue_dict — deliberately not persisted, so renaming the
        # application can't leave a row pointing at a folder that no longer exists.
        product_name = (
            config_snapshot.get("product_name")
            or config_snapshot.get("product")
            or config_snapshot.get("applications_mst_code")
            or queue_dict.get("product_name")
            or ""
        )
        service_name = (
            config_snapshot.get("service_name")
            or config_snapshot.get("identifier")
            or config_snapshot.get("name")
            or queue_dict.get("identifier")
            or ""
        )
        geo_loc = (
            config_snapshot.get("geo_loc_mst_code")
            or config_snapshot.get("region")
            or queue_dict.get("geo_loc_mst_code")
            or queue_dict.get("region")
            or ""
        )

        hcl_file_path = self._resolve_hcl_file_path(file_location, config_snapshot, queue_dict)
        if not hcl_file_path:
            raise ValueError("Terragrunt file path is required to build atlantis entry")

        repo_parts = file_location.repo.split("/")
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        self.logger.info(f"Fetching atlantis.yaml from {feature_branch}")
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
                logger=self.logger,
                component_name=component_name,
            )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        if existing_file.get("exists"):
            atlantis_content = existing_file.get("content", "") or ""
            if not atlantis_content.strip():
                # GitHub's contents API returns an EMPTY body for files over
                # 1MB (encoding "none") — the file is there, the content is
                # not. Left unguarded, the splice below would stage that empty
                # string as the new atlantis.yaml and the commit would erase
                # every project in it. atlantis.yaml is ~230KB today, so this
                # is a ceiling to fail against, not to silently cross.
                raise ValueError(
                    f"atlantis.yaml at {file_location.file_path} came back empty "
                    "(a file over GitHub's 1MB contents limit reads this way). "
                    "Refusing to rewrite it from nothing."
                )
        else:
            raise ValueError(
                f"atlantis.yaml not found in repository: {file_location.file_path}. "
                "Cannot update missing file."
            )

        gen_context = f"path={file_location.file_path} mode=update"
        with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
            updated_content, entry_text, entry_name = self._add_atlantis_entry(
                atlantis_content=atlantis_content,
                hcl_file_path=hcl_file_path,
                product_name=product_name,
                env=environment,
                service_name=service_name,
                infra_type=infra_type,
                tenant=tenant,
                geo_loc=geo_loc,
                config_snapshot=config_snapshot,
            )

            preview_content = self._preview_atlantis_entry(entry_text, entry_name)

        if upload_to_s3:
            identifier = service_name or entry_name or "atlantis"
            artifact_s3_key_json = await self._upload_to_s3(
                content=updated_content,
                preview=preview_content,
                identifier=identifier,
                environment=environment,
                infra_type=infra_type,
                tenant=tenant
            )

            if repository and queue_dict.get("code"):
                update_payload = json.dumps(artifact_s3_key_json)
                await repository.update_artifact_s3_key(queue_dict.get("code"), update_payload)
                self.logger.info(
                    "Saved artifact_s3_key to database for queue item %s: %s",
                    queue_dict.get("code"),
                    update_payload
                )

        if tenant and file_location and workflow_context:
            if not workflow_context.skip_commit:
                _upsert_staged_entry(
                    workflow_context=workflow_context,
                    repo=file_location.repo,
                    base_branch=base_branch,
                    feature_branch=file_location.feature_branch,
                    file_path=file_location.file_path,
                    content=updated_content,
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

            if queue_dict.get("id"):
                script_gen_key = file_location.script_gen_key
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                    "original_content": updated_content,
                    "preview_content": preview_content,
                    "atlantis_project_name": entry_name
                }

        return updated_content

    def _resolve_hcl_file_path(
        self,
        file_location,
        config_snapshot: Dict[str, Any],
        queue_dict: Dict[str, Any]
    ) -> Optional[str]:
        if file_location and getattr(file_location, "config", None):
            for key in ("hcl_file_path", "terragrunt_file_path", "target_file_path"):
                value = file_location.config.get(key)
                if value:
                    return value
        return (
            config_snapshot.get("hcl_file_path")
            or config_snapshot.get("terragrunt_file_path")
            or config_snapshot.get("target_file_path")
            or config_snapshot.get("file_path")
            or queue_dict.get("hcl_file_path")
        )

    def _add_atlantis_entry(
        self,
        atlantis_content: str,
        hcl_file_path: str,
        product_name: str,
        env: str,
        service_name: str,
        infra_type: str,
        tenant: str = "",
        geo_loc: str = "",
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, str]:
        if infra_type.lower() in ("ecs", "ecs_ec2_infrastructuretype_ref", "ecs_ec2"):
            return self._add_atlantis_ecs_entry(
                atlantis_content=atlantis_content,
                hcl_file_path=hcl_file_path,
                product_name=product_name,
                env=env,
                service_name=service_name,
                tenant=tenant,
                geo_loc=geo_loc
            )

        if infra_type.lower() in ("eks", "eks_infrastructuretype_ref"):
            return self._add_atlantis_eks_entry(
                atlantis_content=atlantis_content,
                hcl_file_path=hcl_file_path,
                product_name=product_name,
                env=env,
                service_name=service_name,
                tenant=tenant,
                config_snapshot=config_snapshot or {},
            )

        return self._add_atlantis_standalone_entry(
            atlantis_content=atlantis_content,
            hcl_file_path=hcl_file_path,
            product_name=product_name,
            env=env,
            service_name=service_name,
            infra_type=infra_type,
            tenant=tenant,
            geo_loc=geo_loc
        )

    def _add_atlantis_ecs_entry(
        self,
        atlantis_content: str,
        hcl_file_path: str,
        product_name: str,
        env: str,
        service_name: str,
        tenant: str = "",
        geo_loc: str = ""
    ) -> Tuple[str, str, str]:
        dir_path = hcl_file_path.replace("/terragrunt.hcl", "")
        product_sanitized = self._sanitize_name(product_name)
        service_sanitized = self._sanitize_name(service_name)

        service_for_name = self._get_service_name_for_env_files(service_sanitized)
        env_for_name = self._normalize_environment_for_display(env, tenant)
        branch_pattern = self._get_atlantis_branch_pattern(env, tenant)

        if product_sanitized == "core" and env_for_name == "prod" and geo_loc:
            geo_loc_normalized = self._normalize_geo_loc_for_atlantis(geo_loc)
            name = f"{product_sanitized}-{env_for_name}-{geo_loc_normalized}-{service_for_name}"
        else:
            name = f"{product_sanitized}-{env_for_name}-{service_for_name}"

        entry_text = self._build_entry_text(name, dir_path, branch_pattern)
        return self._insert_entry(atlantis_content, entry_text, name, dir_path)

    def _add_atlantis_eks_entry(
        self,
        atlantis_content: str,
        hcl_file_path: str,
        product_name: str,
        env: str,
        service_name: str,
        tenant: str = "",
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, str]:
        from app.plugin.aspora.script_gen_components.aspora_eks_terragrunt_script_gen_component import (
            CLUSTER_DEPENDENCY_PATH, DEFAULT_DEPENDENCY_PATH,
        )
        cs = config_snapshot or {}
        dep_path = (
            CLUSTER_DEPENDENCY_PATH.get(cs.get("cluster_arn", ""))
            or CLUSTER_DEPENDENCY_PATH.get(cs.get("cluster_name") or cs.get("ecs_cluster", ""))
            or CLUSTER_DEPENDENCY_PATH.get(cs.get("infrastructure_mst_code", ""))
            or DEFAULT_DEPENDENCY_PATH
        )
        cluster_id = dep_path.split("/")[-1]  # "application" or "backend"

        dir_path = hcl_file_path.replace("/terragrunt.hcl", "")
        product_sanitized = self._sanitize_name(product_name)
        service_sanitized = self._sanitize_name(service_name)
        service_for_name = self._get_service_name_for_env_files(service_sanitized)
        env_for_name = self._normalize_environment_for_display(env, tenant)
        branch_pattern = self._get_atlantis_branch_pattern(env, tenant)

        name = f"{product_sanitized}-{env_for_name}-{cluster_id}-eks-{service_for_name}"
        entry_text = self._build_entry_text(name, dir_path, branch_pattern)
        return self._insert_entry(atlantis_content, entry_text, name, dir_path)

    def _add_atlantis_standalone_entry(
        self,
        atlantis_content: str,
        hcl_file_path: str,
        product_name: str,
        env: str,
        service_name: str,
        infra_type: str,
        tenant: str = "",
        geo_loc: str = ""
    ) -> Tuple[str, str, str]:
        dir_path = hcl_file_path.replace("/terragrunt.hcl", "")
        product_sanitized = self._sanitize_name(product_name)
        service_sanitized = self._sanitize_name(service_name)
        env_for_name = self._normalize_environment_for_display(env, tenant)
        branch_pattern = self._get_atlantis_branch_pattern(env, tenant)

        infra_config = self.INFRA_TYPE_CONFIG.get(infra_type.lower(), {})
        suffix = infra_config.get("suffix", infra_type.lower() or "infra")

        # Joined from parts rather than interpolated: a gateway has no per-resource
        # name — its project is "{product}-{env}[-{region}]-gateway" — so the service
        # segment is empty and interpolation would emit a double hyphen, producing a
        # project name that matches nothing in atlantis.yaml.
        segments = [product_sanitized, env_for_name]
        if product_sanitized == "core" and env_for_name == "prod" and geo_loc:
            segments.append(self._normalize_geo_loc_for_atlantis(geo_loc))
        segments += [service_sanitized, suffix]
        name = "-".join(s for s in segments if s)

        entry_text = self._build_entry_text(name, dir_path, branch_pattern)
        return self._insert_entry(atlantis_content, entry_text, name, dir_path)

    @staticmethod
    def _normalize_dir(dir_path: str) -> str:
        """A project dir reduced to what identifies it, so the spellings one
        directory can be written in all compare equal."""
        value = (dir_path or "").strip().strip('"').strip("'")
        while value.startswith("./"):
            value = value[2:]
        return value.strip("/")

    def _find_project_by_dir(self, atlantis_content: str, dir_path: str) -> Optional[str]:
        """Name of the project atlantis.yaml already has for this directory,
        or None.

        Atlantis keys a project by its `dir`, so two entries pointing at one
        directory are two projects planning the same terragrunt — and the
        second one is ours. Matching on the NAME alone could not see that: the
        names in this file predate the formula in this component (the region
        segment in particular is present in hand-written entries and absent
        from generated ones), so a directory that already had a project got a
        second entry under a slightly different name.
        """
        target = self._normalize_dir(dir_path)
        if not target:
            return None
        names = [n for n in (_projects_by_dir(atlantis_content).get(target) or ()) if n]
        if not names:
            # Either no project for this dir, or only unnamed ones. A name is
            # what `atlantis plan -p` needs, so an unnamed project is nothing
            # to adopt — fall through and add a named entry as before.
            return None
        if len(names) > 1:
            # Already true of at least one directory in the live file.
            # Harmless to us — we add nothing either way — but Atlantis plans
            # that directory once per project, so it is worth attention.
            self.logger.warning(
                "atlantis.yaml has %d projects for %s (%s) — using the first",
                len(names), target, ", ".join(names),
            )
        return names[0]

    def _insert_entry(
        self, atlantis_content: str, entry_text: str, name: str, dir_path: str = ""
    ) -> Tuple[str, str, str]:
        # Directory first: it is what Atlantis actually keys a project by, and
        # the existing name — not the one this component would generate — is
        # what every consumer needs. `atlantis plan -p <project>` is posted
        # from this value (deploy_activities reads it out of
        # script_gen_responses), so returning the generated name for a
        # directory that already has a project under another name would plan
        # a project Atlantis has never heard of.
        existing = self._find_project_by_dir(atlantis_content, dir_path)
        if existing:
            if existing != name:
                self.logger.info(
                    "Atlantis already has '%s' for %s — reusing it instead of "
                    "adding '%s' for the same directory",
                    existing, dir_path, name,
                )
            note = (
                f"# already present as '{existing}' — atlantis.yaml unchanged\n"
                f"  - name: {existing}\n"
                f"    dir: {dir_path}\n"
            )
            return atlantis_content, note, existing

        if f"name: {name}" in atlantis_content:
            self.logger.info(f"Atlantis project entry already exists: {name}")
            return atlantis_content, entry_text, name

        if "projects:" not in atlantis_content:
            self.logger.warning("Could not find projects: section in atlantis.yaml")
        elif "\nworkflows:" in atlantis_content:
            atlantis_content = atlantis_content.replace(
                "\nworkflows:",
                f"\n{entry_text}\nworkflows:",
                1,
            )
            self.logger.info(f"Added atlantis project entry: {name}")
        else:
            atlantis_content = atlantis_content.rstrip("\n") + f"\n{entry_text}"
            self.logger.info(f"Added atlantis project entry: {name}")

        return atlantis_content, entry_text, name

    def _build_entry_text(self, name: str, dir_path: str, branch_pattern: str) -> str:
        return (
            f"  - name: {name}\n"
            f"    dir: {dir_path}\n"
            "    workflow: terragrunt\n"
            f"    branch: /{branch_pattern}/\n"
        )

    def _preview_atlantis_entry(self, entry_text: str, name: str) -> str:
        if not entry_text:
            return "Atlantis entry not generated"
        return f"Atlantis project entry:\n\n{entry_text}"

    async def _upload_to_s3(
        self,
        content: str,
        preview: str,
        identifier: str,
        environment: str,
        infra_type: str,
        tenant: str
    ) -> Dict[str, str]:
        original_s3_key = f"atlantis/{identifier}.yaml"
        result = await FileManagerHandler.upload_file(
            key=original_s3_key,
            content=content,
            content_type="text/plain"
        )
        self.logger.info(f"Uploaded atlantis.yaml to S3: {result['location']}")

        preview_s3_key = f"preview/atlantis/{identifier}.yaml"
        await FileManagerHandler.upload_file(
            key=preview_s3_key,
            content=preview,
            content_type="text/plain",
            metadata={
                "type": "preview",
                "environment": environment,
                "infra_type": infra_type,
                "tenant": tenant,
                "generated_by": "aspora_atlantis_script_gen_component"
            }
        )

        return {
            "original_s3_key": original_s3_key,
            "preview": preview_s3_key
        }

    @staticmethod
    def _sanitize_name(name: str) -> str:
        return re.sub(r"[\s_-]+", "-", name.strip()).strip("-").lower()

    @staticmethod
    def _get_service_name_for_env_files(service_name: str) -> str:
        if service_name.endswith("-service"):
            return service_name
        return f"{service_name}-service"

    @staticmethod
    def _normalize_environment_for_display(environment: str, tenant: str = "") -> str:
        env_lower = environment.lower()
        tenant_lower = tenant.lower() if tenant else ""

        if env_lower == "prod":
            return "prod"
        if env_lower == "dev":
            return "dev"
        if tenant_lower in VANCE_ASPORA_TENANTS and env_lower in ("staging", "stage"):
            return "stage"
        return env_lower

    @staticmethod
    def _normalize_geo_loc_for_atlantis(geo_loc: str) -> str:
        mapping = {
            "mumbai": "mumbai",
            "aspora-mumbai": "mumbai",
            "region-aspora-mumbai": "mumbai",
            "london": "london",
            "uk": "london",
            "aspora-london": "london",
            "aspora-uk": "london",
            "region-aspora-london": "london",
        }
        return mapping.get(geo_loc.lower(), "london")

    @staticmethod
    def _get_atlantis_branch_pattern(environment: str, tenant: str = "") -> str:
        env_lower = environment.lower()
        tenant_lower = tenant.lower() if tenant else ""

        if env_lower == "prod":
            return "main"
        if env_lower == "stage":
            return "stage"
        if tenant_lower in VANCE_ASPORA_TENANTS:
            return "stage"
        if env_lower == "staging":
            return "staging"
        return "dev"
