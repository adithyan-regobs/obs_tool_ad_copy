"""
Aspora K8s-Manifests Script Generation Component

Generates the chart + env files committed to the `k8s-manifests` repo for
each Aspora/Vance EKS service deploy.

Dispatched by four `script_gen_key` values, all mapped to this single class:

| script_gen_key       | File path (in k8s-manifests)                                                                  | Behavior                                                       |
| -------------------- | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| k8s_chart_meta       | charts/services/{svc}/Chart.yaml                                                              | Skip-if-exists (preserves manual edits after first deploy)    |
| k8s_chart_template   | charts/services/{svc}/templates/{file}                                                        | Skip-if-exists                                                 |
| k8s_chart_values     | charts/services/{svc}/values.yaml                                                             | Render when new; patch managed fields in place when existing  |
| k8s_env_values       | environments/{product}/{env}/{aws_region}/{svc}/values.yaml                                   | Render when new; patch managed fields in place when existing  |

`Skip-if-exists` means: if the file already lives on the feature branch we
stage the existing content as-is so the orchestrator's no-change detection
collapses it into nothing. The two `values.yaml` files render from template
only for a NEW service; on redeploys the existing file is the base and only
the managed nested fields are patched (see _patch_existing_values), so
hand-added keys and comments survive. image.tag is owned by the
deploy/rollback pipeline and is never touched on updates.
"""

import logging
import os
from typing import Optional

import aiofiles

from app.handlers.gitops_handler import GitOpsHandler
from app.repository.service_config_repository import ServiceConfigRepository
from app.utils.existing_content import fetch_existing_content
from app.utils.service_routing import (
    normalize_service_name,
    resolve_health_path,
    resolve_service_path,
)
from app.utils.timing import log_timing
from app.utils.yaml_patch import replace_nested_yaml_value


# Repo-relative path inside obs_tool where the templates live.
_TEMPLATE_ROOT = os.path.join(
    os.path.dirname(__file__),
    "../../../../templates/k8s-manifests",
)

# Map script_gen_key → relative template path under _TEMPLATE_ROOT.
# k8s_chart_template uses the file_path basename to pick the template.
_TEMPLATE_PATH_BY_KEY = {
    "k8s_chart_meta": "charts/service/Chart.yaml",
    "k8s_chart_values": "charts/service/values.yaml",
    "k8s_env_values": "environments/service/values.yaml",
}

_WORKER_TEMPLATE_PATH_BY_KEY = {
    "k8s_chart_meta": "charts/worker/Chart.yaml",
    "k8s_chart_values": "charts/worker/values.yaml",
    "k8s_env_values": "environments/worker/values.yaml",
}

_SKIP_IF_EXISTS_KEYS = {"k8s_chart_meta", "k8s_chart_template"}
_RE_RENDER_KEYS = {"k8s_chart_values", "k8s_env_values"}


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


def _resolve_namespace(config_snapshot: dict, service_name: str) -> str:
    ns = config_snapshot.get("namespace")
    if ns and ns.strip() and ns.strip() != "default":
        return ns.strip()
    return service_name  # already includes -service suffix


# The path rules live in app.utils.service_routing so the save, this render
# and the displayed URL settle an empty value the same way.
def _resolve_health_path(config_snapshot: dict, service_name: str) -> str:
    return resolve_health_path(config_snapshot, service_name, config_snapshot.get("language_name"))


def _resolve_service_path(config_snapshot: dict, service_name: str) -> str:
    return resolve_service_path(config_snapshot, service_name)


def _normalize_service_name(config_snapshot: dict) -> str:
    return normalize_service_name(config_snapshot.get("service_name"))


def _to_millicore(val) -> str:
    s = str(val)
    if s.endswith("m"):
        return s
    return f"{int(float(s) * 1000)}m"


def _to_mebibyte(val) -> str:
    s = str(val)
    if s.endswith(("Mi", "Gi", "MB", "GB")):
        return s
    return f"{int(float(s) * 1024)}Mi"


class AsporaK8sManifestsScriptGenComponent:
    """
    Single component that handles all five k8s-manifests file types.
    Behaviour is selected by `file_location.script_gen_key`.
    """

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
        config_snapshot = dict(queue_dict.get("config_snapshot") or {})
        key = file_location.script_gen_key

        # Enrich config_snapshot with ingress_group_order from the DB for
        # re-render keys — the queue snapshot may not carry this field.
        if key in _RE_RENDER_KEYS and db and not config_snapshot.get("ingress_group_order"):
            sc_code = queue_dict.get("transaction_code")
            if sc_code:
                sc_repo = ServiceConfigRepository(db)
                sc = await sc_repo.get_by_code_and_tenant(sc_code, tenant)
                if sc and sc.config and sc.config.get("ingress_group_order"):
                    config_snapshot["ingress_group_order"] = sc.config["ingress_group_order"]

        # Record the ingress path this render actually used. save_service_alb_url
        # runs in another process long after this and used to re-derive the path
        # from the same snapshot with a different empty-value default, so the
        # stored service URL could point somewhere the Ingress does not route.
        if db and queue_dict.get("transaction_code"):
            try:
                await ServiceConfigRepository(db).update_resolved_service_path(
                    queue_dict["transaction_code"],
                    _resolve_service_path(config_snapshot, _normalize_service_name(config_snapshot)),
                )
            except Exception as e:
                self.logger.warning("Failed to record resolved service path: %s", e)

        if key not in _SKIP_IF_EXISTS_KEYS and key not in _RE_RENDER_KEYS:
            raise ValueError(
                f"AsporaK8sManifestsScriptGenComponent received unsupported "
                f"script_gen_key={key!r}"
            )

        repo_parts = file_location.repo.split("/")
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # Look up existing content (cached entry first, then GitHub).
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
            # Dry runs (skip_commit) have no feature branch — it is only cut at
            # deploy time — so "does this file exist" must be asked of the base
            # branch, exactly what the feature branch will be cut from. Fetching
            # the nonexistent feature branch made skip-if-exists files look
            # freshly rendered in the PR preview when the real PR keeps them.
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

        if key in _SKIP_IF_EXISTS_KEYS and existing_file.get("exists"):
            # File already present — keep current content. Stage as-is so the
            # orchestrator's no-change detector collapses this into a no-op.
            content = existing_file.get("content") or ""
            self.logger.info(
                "k8s-manifests: keeping existing %s (script_gen_key=%s)",
                file_location.file_path, key,
            )
        elif existing_file.get("exists") and existing_file.get("content"):
            # values.yaml already exists — patch only the managed fields so
            # hand-added keys and comments survive. image.tag is never touched
            # here: the deploy/rollback pipeline owns it.
            content = self._patch_existing_values(
                key, existing_file["content"], config_snapshot
            )
            self.logger.info(
                "k8s-manifests: %s exists — patched managed fields (script_gen_key=%s)",
                file_location.file_path, key,
            )
        else:
            platform_base_version = "0.7.0"
            if key == "k8s_chart_meta":
                platform_base_version = await self._fetch_platform_base_version(
                    db=db, tenant=tenant, owner=owner, repo=repo, branch=base_branch,
                )
            content = await self._render_template(
                key=key,
                file_location=file_location,
                config_snapshot=config_snapshot,
                queue_dict=queue_dict,
                platform_base_version=platform_base_version,
            )

        if workflow_context and not workflow_context.skip_commit:
            _upsert_staged_entry(
                workflow_context=workflow_context,
                repo=file_location.repo,
                base_branch=base_branch,
                feature_branch=file_location.feature_branch,
                file_path=file_location.file_path,
                content=content,
                queue_id=queue_dict.get("id"),
                script_gen_key=key,
            )
            queue_label = queue_dict.get("code") or queue_dict.get("id")
            commit_line = (
                f"{queue_label}: {key} -> {file_location.file_path}"
                if queue_label else f"{key} -> {file_location.file_path}"
            )
            _append_commit_message(
                workflow_context,
                file_location.repo,
                base_branch,
                commit_line,
            )

        if queue_dict.get("id") and workflow_context:
            payload = {
                "original_content": content,
                "preview_content": content,
            }
            if workflow_context.skip_commit:
                # k8s_chart_template covers SEVERAL files under one key
                # (configmap, deployment, serviceaccount, ...). A flat write
                # keeps only the last file's content, which the preview then
                # pins to the first file's path. Dry runs nest per file path so
                # the preview endpoint can pair every content with its file.
                # Real deploys keep the flat shape other consumers read.
                bucket = workflow_context.script_gen_responses[queue_dict["id"]].setdefault(key, {})
                bucket[file_location.file_path] = payload
            else:
                workflow_context.script_gen_responses[queue_dict["id"]][key] = payload

        return content

    def _patch_existing_values(
        self, key: str, content: str, config_snapshot: dict
    ) -> str:
        """Patch only the managed nested fields of an existing values.yaml.

        A field absent from the config leaves its line untouched; a path the
        file does not carry (worker files without ingress, qa files with keda
        disabled) is skipped, never inserted. image.tag is deliberately not a
        managed field — the deploy/rollback pipeline owns it.
        """
        cs = config_snapshot
        updates = []

        if key == "k8s_chart_values":
            if cs.get("kind"):
                updates.append((("kind",), str(cs["kind"])))
            if cs.get("port"):
                updates.append((("containerPort",), str(cs["port"])))
            if (cs.get("health") or "").strip():
                updates.append(
                    (("healthCheckPath",), _resolve_health_path(cs, _normalize_service_name(cs)))
                )
            service_path = (cs.get("service_path") or "").strip()
            if service_path and service_path != "/*":
                updates.append(
                    (("ingress", "path"), _resolve_service_path(cs, _normalize_service_name(cs)))
                )
            namespace = (cs.get("namespace") or "").strip()
            if namespace and namespace != "default":
                updates.append((("namespace",), namespace))
        else:  # k8s_env_values
            if cs.get("cpu_requested"):
                updates.append((("resources", "requests", "cpu"), _to_millicore(cs["cpu_requested"])))
            if cs.get("memory_requested"):
                updates.append((("resources", "requests", "memory"), _to_mebibyte(cs["memory_requested"])))
            if cs.get("cpu_limit"):
                updates.append((("resources", "limits", "cpu"), _to_millicore(cs["cpu_limit"])))
            if cs.get("memory_limit"):
                updates.append((("resources", "limits", "memory"), _to_mebibyte(cs["memory_limit"])))
            if cs.get("ingress_group_order"):
                updates.append((("ingress", "groupOrder"), f'"{cs["ingress_group_order"]}"'))
            hpa = cs.get("hpa") or {}
            replica_count = cs.get("replica_count")
            if replica_count and not hpa.get("enabled"):
                # Fixed count (autoscaling off): min == max == replica_count —
                # the chart's only replica mechanism is KEDA.
                updates.append((("keda", "minReplicas"), str(replica_count)))
                updates.append((("keda", "maxReplicas"), str(replica_count)))
            else:
                if hpa.get("min_replicas"):
                    updates.append((("keda", "minReplicas"), str(hpa["min_replicas"])))
                if hpa.get("max_replicas"):
                    updates.append((("keda", "maxReplicas"), str(hpa["max_replicas"])))

        for path, value in updates:
            content, _ = replace_nested_yaml_value(content, path, value)

        if key == "k8s_env_values":
            # hpa thresholds are deliberately NOT written (no keda.cpu/.memory
            # utilization overrides): real service files carry only
            # minReplicas/maxReplicas and rely on the chart defaults, and the
            # UI sends threshold values users never consciously set.
            if cs.get("compute"):
                content = self._patch_scheduling_capacity(content, str(cs["compute"]))
        return content

    @staticmethod
    def _patch_scheduling_capacity(content: str, capacity_type: str) -> str:
        """Patch scheduling.capacityType, creating the block when the file
        has none — most pre-devlift env values files never carried it, which
        made the compute toggle a silent no-op on them. A top-level block is
        valid YAML anywhere, so it is appended at the end of the file.

        Not for `on-demand`, though: the chart already reads an absent key as
        on-demand (platform-base/_scheduling.tpl), so writing it into a file
        that has none changes nothing in the rendered manifest. It only made a
        PR out of every update whose snapshot carried the schema default.
        """
        content, replaced = replace_nested_yaml_value(
            content, ("scheduling", "capacityType"), capacity_type
        )
        if replaced or capacity_type == "on-demand":
            return content
        block = f"scheduling:\n  capacityType: {capacity_type}\n"
        if content.endswith("\n\n"):
            return content + block
        if content.endswith("\n"):
            return content + "\n" + block
        return content + "\n\n" + block

    async def _render_template(
        self,
        *,
        key: str,
        file_location,
        config_snapshot: dict,
        queue_dict: dict,
        platform_base_version: str = "0.7.0",
    ) -> str:
        svc_type = (file_location.config.get("service_type") or "API").upper()
        is_worker = svc_type == "BACKGROUND_SERVICE"

        if key == "k8s_chart_template":
            # Worker templates live under charts/worker/templates/ but share the same
            # single-line {{ include "platform-base.X" . }} content as API templates.
            # We reuse charts/service/templates/ since the content is identical.
            template_rel = f"charts/service/templates/{os.path.basename(file_location.file_path)}"
        else:
            path_map = _WORKER_TEMPLATE_PATH_BY_KEY if is_worker else _TEMPLATE_PATH_BY_KEY
            template_rel = path_map.get(key)
            if template_rel is None:
                raise ValueError(f"No template registered for script_gen_key={key!r}")

        template_path = os.path.join(_TEMPLATE_ROOT, template_rel)
        async with aiofiles.open(template_path, "r") as f:
            content = await f.read()

        replacements = self._build_replacements(key, config_snapshot, queue_dict, platform_base_version)
        for placeholder, value in replacements.items():
            content = content.replace(placeholder, value)
        return content

    async def _fetch_platform_base_version(
        self,
        *,
        db,
        tenant: str,
        owner: str,
        repo: str,
        branch: str,
    ) -> str:
        try:
            result = await GitOpsHandler.get_content(
                db=db,
                tenant=tenant,
                owner=owner,
                repo=repo,
                file_path="library/platform-base/Chart.yaml",
                branch=branch,
            )
            if result.get("exists") and result.get("content"):
                for line in result["content"].splitlines():
                    if line.startswith("version:"):
                        version = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if version:
                            return version
        except Exception as exc:
            self.logger.warning(
                "Failed to fetch platform-base version from %s/%s@%s: %s; using 0.7.0 fallback",
                owner, repo, branch, exc,
            )
        return "0.7.0"

    def _build_replacements(
        self,
        key: str,
        config_snapshot: dict,
        queue_dict: dict,
        platform_base_version: str = "0.7.0",
    ) -> dict:
        service_name = _normalize_service_name(config_snapshot)
        if not service_name:
            raise ValueError("k8s-manifests render: service_name is required")

        environment = (
            config_snapshot.get("environment")
            or queue_dict.get("environment")
            or "stage"
        )
        # k8s-manifests env folders use 'stage' for staging (not 'staging').
        if environment.lower() == "staging":
            environment = "stage"

        region = (
            config_snapshot.get("aws_region")
            or config_snapshot.get("region")
            or ""
        )

        env_display = environment.capitalize()

        # Description uses bare name with spaces, sentence-cased: "wealth-account-service" → "Wealth account"
        # so description reads "Wealth account service chart" (not "Wealth-Account-Service service chart")
        bare_name = service_name[:-len("-service")] if service_name.endswith("-service") else service_name
        service_name_title = bare_name.replace("-", " ").capitalize()

        hpa_cfg = config_snapshot.get("hpa") or {}
        keda_min = str(hpa_cfg.get("min_replicas") or "2")
        keda_max = str(hpa_cfg.get("max_replicas") or "4")
        # platform-base has no Deployment replicas field — KEDA is the only
        # replica mechanism. A fixed replica count (autoscaling off) maps to
        # minReplicas == maxReplicas == replica_count.
        replica_count = config_snapshot.get("replica_count")
        if replica_count and not hpa_cfg.get("enabled"):
            keda_min = keda_max = str(replica_count)
        if environment.lower() == "qa":
            # The qa cluster has no KEDA installed, so an enabled ScaledObject
            # fails the Argo sync with a missing-CRD error. platform-base
            # defaults keda.enabled to true, so qa must disable it explicitly.
            # min/maxReplicas are omitted — the library defaults are 1/1, so
            # the PDB gate stays closed and the Deployment runs a single pod.
            keda_block = (
                "# QA cluster runs without KEDA — keep the ScaledObject off.\n"
                "keda:\n"
                "  enabled: false"
            )
        else:
            keda_block = (
                "# HA: keep at least 2 replicas so PodDisruptionBudget renders (skipped at 1)\n"
                "# and Karpenter consolidation can drain a node without dropping traffic.\n"
                "keda:\n"
                f"  minReplicas: {keda_min}\n"
                f"  maxReplicas: {keda_max}"
            )
            # No utilization overrides on purpose: real service files carry
            # only minReplicas/maxReplicas and rely on the chart's default
            # cpu/memory utilization (80/80). The UI also sends threshold
            # values users never consciously set (hidden fields), so writing
            # them created house-style drift the sync tool flagged.

        replacements = {
            "{{PLATFORM_BASE_VERSION}}": platform_base_version,
            "{{SERVICE_NAME}}": service_name,
            "{{SERVICE_NAME_UPPER}}": service_name.upper(),
            "{{SERVICE_NAME_TITLE}}": service_name_title,
            "{{KIND}}": str(config_snapshot.get("kind") or "api"),
            "{{NAMESPACE}}": _resolve_namespace(config_snapshot, service_name),
            "{{CONTAINER_PORT}}": str(config_snapshot.get("port") or "8080"),
            "{{HEALTH_CHECK_PATH}}": _resolve_health_path(config_snapshot, service_name),
            "{{SERVICE_PATH}}": _resolve_service_path(config_snapshot, service_name),
            "{{ENVIRONMENT}}": environment,
            "{{ENVIRONMENT_DISPLAY}}": env_display,
            "{{REGION}}": region,
            "{{IMAGE_TAG}}": str(config_snapshot.get("image_tag") or "latest"),
            "{{CPU_REQUEST}}": _to_millicore(config_snapshot.get("cpu_requested") or "500m"),
            "{{CPU_LIMIT}}": _to_millicore(config_snapshot.get("cpu_limit") or "500m"),
            "{{MEMORY_REQUEST}}": _to_mebibyte(config_snapshot.get("memory_requested") or "512Mi"),
            "{{MEMORY_LIMIT}}": _to_mebibyte(config_snapshot.get("memory_limit") or "512Mi"),
            "{{INGRESS_GROUP_ORDER}}": str(config_snapshot.get("ingress_group_order") or "10"),
            "{{APP_PORT}}": str(config_snapshot.get("port") or "8080"),
            "{{KEDA_BLOCK}}": keda_block,
            "{{COMPUTE}}": str(config_snapshot.get("compute") or "on-demand"),
        }
        return replacements
