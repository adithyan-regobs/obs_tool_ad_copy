"""
Generic Kubernetes Helm Script Generation Component

Generates a complete Jenkinsfile for any Helm chart deployment (PostgreSQL, MySQL,
MongoDB, Kafka, ClickHouse, Monitoring stacks, etc.).

The Jenkinsfile:
  1. Authenticates to EKS via aws eks update-kubeconfig
  2. Adds the appropriate Helm chart repo
  3. Embeds the rendered values.yaml inline as a shell heredoc
  4. Runs helm upgrade --install (idempotent — installs or upgrades)
  5. Verifies the deployment

Template:
  templates/helms/Jenkinsfile-helm

values.yaml templates (per chart type):
  templates/helms/{helm_chart_type}/values.yaml

Logic:
  - Load values.yaml template for the given helm_chart_type
  - Recursively apply matching config_snapshot keys to the template
  - Embed the rendered values.yaml into the Jenkinsfile as a heredoc
  - Replace Jenkinsfile placeholders with config_snapshot / infra values

Generated content is staged in workflow_context with mode="jenkins".
Jenkins provisioning reads it and creates/updates the pipeline job.
"""

import os
import logging
import uuid
from copy import deepcopy
from typing import Optional, Any, Dict, Tuple

import aiofiles
import yaml

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    DEFAULT_EKS_CLUSTER_NAME,
    DEFAULT_AWS_REGION,
    AWS_ACCOUNT_ID as DEFAULT_AWS_ACCOUNT_ID,
)
from app.repository.transaction_queue_repository import TransactionQueueRepository
from app.utils.timing import log_timing

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

_TEMPLATES_BASE = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "helms"
)
_JENKINSFILE_TEMPLATE = os.path.join(_TEMPLATES_BASE, "Jenkinsfile-helm")

# Metadata keys that should not be applied to values.yaml
_SKIP_KEYS = {
    "identifier", "server_name", "environment", "repository",
    "infra_repository", "target_branch", "code", "case_type",
    "tenant", "helm_chart_type", "infrastructuretype_ref_code",
    "cloudRegion", "cloudRegionId", "port", "infrastructure_mst_code",
    "eks_cluster_name", "aws_region", "aws_account_id",
}

# Chart type → (repo_name, repo_url, chart_ref)
_HELM_CHART_MAP: Dict[str, Tuple[str, str, str]] = {
    "postgres":      ("bitnami", "https://charts.bitnami.com/bitnami", "bitnami/postgresql"),
    "postgresql":    ("bitnami", "https://charts.bitnami.com/bitnami", "bitnami/postgresql"),
    "mysql":         ("bitnami", "https://charts.bitnami.com/bitnami", "bitnami/mysql"),
    "mongodb":       ("bitnami", "https://charts.bitnami.com/bitnami", "bitnami/mongodb"),
    "redis":         ("bitnami", "https://charts.bitnami.com/bitnami", "bitnami/redis"),
    "kafka":         ("bitnami", "https://charts.bitnami.com/bitnami", "bitnami/kafka"),
    "rabbitmq":      ("bitnami", "https://charts.bitnami.com/bitnami", "bitnami/rabbitmq"),
    "elasticsearch": ("bitnami", "https://charts.bitnami.com/bitnami", "bitnami/elasticsearch"),
    "clickhouse":    ("clickhouse", "https://charts.clickhouse.com", "clickhouse/clickhouse"),
    "prometheus":    ("prometheus-community", "https://prometheus-community.github.io/helm-charts", "prometheus-community/kube-prometheus-stack"),
    "grafana":       ("grafana", "https://grafana.github.io/helm-charts", "grafana/grafana"),
    "loki":          ("grafana", "https://grafana.github.io/helm-charts", "grafana/loki"),
}


async def _fetch_infra_values(
    db,
    infrastructure_mst_code: str,
    tenant_code: Optional[str] = None,
    application_code: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch EKS cluster metadata from infrastructure_mst.

    The `infrastructure_mst_code` may identify any resource (e.g. a k8s-postgres
    server) — it is NOT guaranteed to be the EKS cluster itself. Resolve
    `cluster_name` in priority order:

      A. The passed row's `locator.cluster_name` / `locator.cluster` — true
         when the row IS the EKS cluster (enterprise / direct-cluster flow).
      B. The tenant's registered EKS infrastructure_mst row — app-scoped
         first, then tenant-level (shared PaaS cluster).
      C. Compose the default PaaS name from settings, matching the
         convention in `infrastructure_mst_service.create_workload_infrastructure`.
    """
    if not infrastructure_mst_code or not db:
        return {}
    from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
    infra_repo = InfrastructureMstRepository(db)
    infrastructure = await infra_repo.get_by_code(infrastructure_mst_code)
    if not infrastructure:
        return {}

    locator = infrastructure.locator or {}
    vendor_auth = (infrastructure.infra_vendor_account.auth_config or {}) if infrastructure.infra_vendor_account else {}

    # A: the passed row IS the EKS cluster (enterprise / direct-cluster).
    cluster_name = locator.get("cluster_name") or locator.get("cluster")

    # B: lookup the tenant's registered EKS cluster row (PaaS shared cluster
    # or enterprise app-scoped cluster registered separately from the
    # workload being deployed onto it).
    if not cluster_name and tenant_code:
        try:
            from app.core.enum import EnvironmentEnum
            env_enum: Optional[EnvironmentEnum] = None
            if environment:
                try:
                    env_enum = EnvironmentEnum(environment)
                except ValueError:
                    env_enum = None

            cluster_rows: list = []
            if env_enum:
                if application_code:
                    cluster_rows = await infra_repo.list_by_filters(
                        tenant_code=tenant_code,
                        infrastructuretype_ref_code="eks_infrastructuretype_ref",
                        environment=env_enum,
                        applications_mst_code=application_code,
                    )
                if not cluster_rows:
                    cluster_rows = await infra_repo.list_tenant_level_clusters(
                        tenant_code=tenant_code,
                        infrastructuretype_ref_code="eks_infrastructuretype_ref",
                        environment=env_enum,
                    )
            if cluster_rows:
                cluster_locator = cluster_rows[0].locator or {}
                cluster_name = (
                    cluster_locator.get("cluster_name")
                    or cluster_locator.get("cluster")
                )
        except Exception as exc:
            logger.warning("Failed to lookup tenant EKS cluster: %s", exc)

    # C: compose the PaaS default name from settings.
    if not cluster_name:
        try:
            from app.core.config import settings
            cluster_name = (
                f"devlift-{settings.onboarding_default_env}"
                f"-{settings.onboarding_default_region_code}"
                f"-{settings.onboarding_default_index}"
                f"-{settings.onboarding_default_vm_name}-cluster"
            )
        except Exception as exc:
            logger.warning("Failed to compose default cluster name: %s", exc)

    return {
        "cluster_name": cluster_name,
        "aws_region": locator.get("region") or vendor_auth.get("region", DEFAULT_AWS_REGION),
        "account_id": vendor_auth.get("account_id", DEFAULT_AWS_ACCOUNT_ID),
    }


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
    queue_id: Optional[int],
    script_gen_key: Optional[str],
    mode: Optional[str] = None,
    config_snapshot: Optional[dict] = None,
    job_name: Optional[str] = None,
):
    if not workflow_context:
        return
    entry = _find_staged_entry(workflow_context, repo, base_branch, file_path)
    if entry:
        entry["content"] = content
        entry["feature_branch"] = feature_branch or entry.get("feature_branch")
        entry["queue_id"] = queue_id
        entry["script_gen_key"] = script_gen_key
        entry["mode"] = mode
        entry["config_snapshot"] = config_snapshot
        entry["job_name"] = job_name
        return
    workflow_context.staged_files.append({
        "repo": repo,
        "base_branch": base_branch,
        "feature_branch": feature_branch,
        "file_path": file_path,
        "content": content,
        "queue_id": queue_id,
        "script_gen_key": script_gen_key,
        "mode": mode,
        "config_snapshot": config_snapshot,
        "job_name": job_name,
    })


def _apply_config_to_values(values: dict, config: dict) -> dict:
    """
    Recursively apply config_snapshot values to the values.yaml dict.

    For each key in config:
      - If the key exists at any level in values, update it
      - If not, skip it

    Supports nested keys via dot notation (e.g. "auth.database")
    and flat keys that are searched recursively.
    """
    values = deepcopy(values)

    for key, value in config.items():
        if key in _SKIP_KEYS:
            continue

        if "." in key:
            parts = key.split(".")
            _set_nested(values, parts, value)
        else:
            _set_recursive(values, key, value)

    return values


def _set_nested(d: dict, keys: list, value: Any) -> bool:
    """Set a value at a nested path (dot notation)."""
    for key in keys[:-1]:
        if key in d and isinstance(d[key], dict):
            d = d[key]
        else:
            return False
    if keys[-1] in d:
        d[keys[-1]] = value
        return True
    return False


def _set_recursive(d: dict, key: str, value: Any) -> bool:
    """Recursively find and set a key in a nested dict.

    Updates ALL occurrences — not just the first. The bitnami postgres chart
    has `postgresPassword` in both `global.postgresql.auth` AND top-level `auth`
    (same admin password field, two override locations). Setting only the first
    leaves the template default (with YAML-unsafe chars like `Dvl!ftPg#S3cur3@2025`)
    in the rendered file, which helm's YAML parser chokes on.
    """
    found = False
    if key in d:
        d[key] = value
        found = True
    for k, v in d.items():
        if isinstance(v, dict):
            if _set_recursive(v, key, value):
                found = True
    return found


class K8sHelmScriptGenComponent:
    """
    Generic Helm chart Jenkinsfile generator.

    Works for any Helm chart type (postgres, mysql, mongodb, redis, kafka, etc.)
    by:
      1. Loading the values.yaml template for the chart type
      2. Applying config_snapshot keys recursively to the template
      3. Embedding the rendered values.yaml into a Jenkinsfile as a heredoc
      4. Filling Jenkinsfile placeholders with infra/config values

    The helm_chart_type in config_snapshot determines:
      - Which values.yaml template to use: templates/helms/{helm_chart_type}/values.yaml
      - Which Helm repo/chart to install: see _HELM_CHART_MAP
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)

    async def generate(
        self,
        tenant: str,
        repository: TransactionQueueRepository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = False,
        db=None,
    ) -> str:
        config_snapshot = queue_dict.get("config_snapshot") or {}
        helm_chart_type = config_snapshot.get("helm_chart_type", "postgres")
        component_name = self.__class__.__name__

        # ── Load and render values.yaml ──────────────────────────────────────
        values_template_path = (
            file_location.template_path
            or os.path.join(_TEMPLATES_BASE, helm_chart_type, "values.yaml")
        )
        if not os.path.exists(values_template_path):
            raise ValueError(f"Helm values template not found: {values_template_path}")

        gen_context = f"helm_chart_type={helm_chart_type}"
        chart_defaults = await _construct_chart_values(
            helm_chart_type, config_snapshot,
            repository=repository, queue_id=queue_dict.get("id"),
        )
        config_snapshot.update(chart_defaults)
        with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
            async with aiofiles.open(values_template_path, "r") as f:
                template_content = await f.read()
            values = yaml.safe_load(template_content)
            values = _apply_config_to_values(values, config_snapshot)
            values_content = yaml.dump(values, default_flow_style=False, sort_keys=False)

        # ── Fetch infra values (cluster, region) ─────────────────────────────
        infrastructure_mst_code = config_snapshot.get("infrastructure_mst_code", "")
        infra_values: Dict[str, Any] = {}
        if db and infrastructure_mst_code:
            try:
                infra_values = await _fetch_infra_values(
                    db,
                    infrastructure_mst_code,
                    tenant_code=config_snapshot.get("tenant_code") or tenant,
                    application_code=config_snapshot.get("applications_mst_code"),
                    environment=config_snapshot.get("environment"),
                )
            except Exception as exc:
                self.logger.warning("Failed to fetch infra values: %s", exc)

        cluster_name = (
            config_snapshot.get("eks_cluster_name")
            or config_snapshot.get("cluster_name")
            or infra_values.get("cluster_name", DEFAULT_EKS_CLUSTER_NAME)
        )
        aws_region = (
            config_snapshot.get("aws_region")
            or config_snapshot.get("cloudRegion")
            or infra_values.get("aws_region", DEFAULT_AWS_REGION)
        )
        environment = config_snapshot.get("environment") or "stage"

        # ── Derive release name and namespace ────────────────────────────────
        identifier = (
            config_snapshot.get("identifier")
            or config_snapshot.get("server_name", "")
        )
        release_name = identifier.lower().replace(" ", "-") if identifier else helm_chart_type
        namespace = f"{tenant}-ns"

        # ── Resolve Helm repo details ────────────────────────────────────────
        chart_key = helm_chart_type.lower()

        # ── Build a unique Jenkins job name for infra pipelines ──────────────
        # Prefix with k8s-{chart_key} + short UUID to avoid collisions with
        # service pipelines that may share the same identifier/server_name.
        _short_id = uuid.uuid4().hex[:8]
        jenkins_job_name = f"k8s-{chart_key}-{release_name}-{_short_id}"
        repo_name, repo_url, helm_chart = _HELM_CHART_MAP.get(
            chart_key,
            ("bitnami", "https://charts.bitnami.com/bitnami", f"bitnami/{chart_key}"),
        )

        # ── Determine content based on file_path ──────────────────────────────
        file_path = file_location.file_path or ""

        if file_path.endswith("values.yaml"):
            # values.yaml — git commit only, no inline deploy
            content = values_content
        else:
            # Jenkinsfile — render template + inline deploy via Jenkins
            content = await self._render_jenkinsfile(
                values_content, cluster_name, aws_region, environment,
                release_name, namespace, repo_name, repo_url, helm_chart,
            )

        # ── Stage for inline Jenkins deploy + git commit ──────────────────────
        _upsert_staged_entry(
            workflow_context=workflow_context,
            repo=file_location.repo or "",
            base_branch=file_location.base_branch or "",
            feature_branch=file_location.feature_branch or "",
            file_path=file_path,
            content=content,
            queue_id=queue_dict.get("id"),
            script_gen_key=file_location.script_gen_key,
            mode=getattr(file_location, "mode", None),
            config_snapshot=config_snapshot if getattr(file_location, "mode", None) == "jenkins" else None,
            job_name=jenkins_job_name if getattr(file_location, "mode", None) == "jenkins" else None,
        )

        # ── Append commit message for infra repo ──────────────────────────────
        if file_location.repo and file_path:
            key = f"{file_location.repo}|||{file_location.base_branch or ''}"
            queue_label = queue_dict.get("code") or queue_dict.get("id")
            msg = f"{queue_label}: {file_location.script_gen_key} -> {file_path}"
            existing_msg = workflow_context.commit_messages.get(key, "")
            workflow_context.commit_messages[key] = f"{existing_msg}\n{msg}" if existing_msg else msg

        # ── Store preview in workflow context ────────────────────────────────
        preview_content = self._build_preview(helm_chart_type, identifier, config_snapshot)
        if queue_dict.get("id"):
            response_key = f"{file_location.script_gen_key}:{file_path or 'inline'}"
            workflow_context.script_gen_responses[queue_dict["id"]][response_key] = {
                "original_content": content,
                "preview_content": preview_content,
            }

        return content

    async def _render_jenkinsfile(
        self,
        values_content: str,
        cluster_name: str,
        aws_region: str,
        environment: str,
        release_name: str,
        namespace: str,
        repo_name: str,
        repo_url: str,
        helm_chart: str,
    ) -> str:
        """Load Jenkinsfile-helm template and fill all placeholders."""
        async with aiofiles.open(_JENKINSFILE_TEMPLATE, "r") as f:
            jenkinsfile = await f.read()

        jenkinsfile = jenkinsfile.replace("{{AWS_REGION}}", aws_region)
        jenkinsfile = jenkinsfile.replace("{{EKS_CLUSTER_NAME}}", cluster_name)
        jenkinsfile = jenkinsfile.replace("{{NAMESPACE}}", namespace)
        jenkinsfile = jenkinsfile.replace("{{RELEASE_NAME}}", release_name)
        jenkinsfile = jenkinsfile.replace("{{HELM_REPO_NAME}}", repo_name)
        jenkinsfile = jenkinsfile.replace("{{HELM_REPO_URL}}", repo_url)
        jenkinsfile = jenkinsfile.replace("{{HELM_CHART}}", helm_chart)
        jenkinsfile = jenkinsfile.replace("{{ENVIRONMENT}}", environment)
        jenkinsfile = jenkinsfile.replace("{{VALUES_YAML_BLOCK}}", values_content.rstrip("\n"))

        from app.core.config import settings
        webhook_url = f"{settings.devlift_backend_url}/api/v1/webhooks/jenkins-webhook"
        jenkinsfile = jenkinsfile.replace("{{OBSTOOL_WEBHOOK_URL}}", webhook_url)
        jenkinsfile = jenkinsfile.replace("{{WEBHOOK_SECRET}}", settings.pipeline_webhook_secret or "")

        return jenkinsfile

    def _build_preview(
        self,
        helm_chart_type: str,
        identifier: str,
        config_snapshot: dict,
    ) -> str:
        """Build a human-readable preview of the applied config."""
        lines = [f"{helm_chart_type.title()} Helm Deployment Configuration:", ""]

        for key, value in config_snapshot.items():
            if key in _SKIP_KEYS:
                continue
            if any(s in key.lower() for s in ("password", "secret", "token")):
                lines.append(f"  {key}: ********")
            else:
                lines.append(f"  {key}: {value}")

        lines.append("")
        lines.append(f"# Identifier: {identifier}")
        lines.append(f"# Chart type: {helm_chart_type}")
        lines.append("# Deployed via: Helm + Jenkins")

        return "\n".join(lines)


async def _construct_chart_values(
    helm_chart_type: str,
    config_snapshot: dict,
    repository=None,
    queue_id=None,
) -> dict:
    """
    Return computed default values for a given chart type.

    Values from config_snapshot take priority — defaults are only used
    when the key is absent or empty in config_snapshot.

    For postgres: auto-generates postgresPassword and database if not present,
    then persists them back to the queue_item's config_snapshot so re-runs
    reuse the same password (prevents mismatch with the PVC's initdb password).
    """
    match helm_chart_type.lower():
        case "postgres" | "postgresql":
            pg_password = config_snapshot.get("postgresPassword") or f"PG-{uuid.uuid4().hex[:16]}"
            pg_database = (
                config_snapshot.get("database")
                or f"{config_snapshot.get('server_name', 'postgres').replace('-', '_')}_db"
            )
            result = {
                "postgresPassword": pg_password,
                "database": pg_database,
                "replicationUsername": config_snapshot.get("replicationUsername") or "repl_user",
                "primary.persistence.size": config_snapshot.get("primary.persistence.size") or "5Gi",
                "primary.persistence.storageClass": config_snapshot.get("primary.persistence.storageClass") or "gp3",
                "primary.resourcesPreset": config_snapshot.get("primary.resourcesPreset") or "nano",
            }

            # Persist generated password + database back to the queue so retries
            # don't generate a new password that mismatches the PVC's initdb.
            if repository and queue_id:
                try:
                    queue_item = await repository.get_by_id(queue_id)
                    if queue_item and queue_item.config_snapshot is not None:
                        changed = False
                        if queue_item.config_snapshot.get("postgresPassword") != pg_password:
                            queue_item.config_snapshot["postgresPassword"] = pg_password
                            changed = True
                        if queue_item.config_snapshot.get("database") != pg_database:
                            queue_item.config_snapshot["database"] = pg_database
                            changed = True
                        if changed:
                            from sqlalchemy.orm.attributes import flag_modified
                            flag_modified(queue_item, "config_snapshot")
                            await repository.session.flush()
                except Exception:
                    logger.warning("Failed to persist postgres secrets to queue %s", queue_id)

            return result
        case _:
            return {}
