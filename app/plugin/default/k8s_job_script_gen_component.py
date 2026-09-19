"""
K8s Job Script Generation Component

Generates a Kubernetes Job manifest for one-time operational tasks
on existing infrastructure (CREATE DATABASE, CREATE USER, etc.).

Unlike K8sHelmScriptGenComponent (which installs Helm charts), this component
targets already-running servers and runs a single command to completion.

Template resolution:
  Supplied by the file locator via file_location.template_path
  (e.g. templates/helm-jobs/k8s_postgres_create_database/job.yaml)

The generated Job YAML is staged with mode="kubectl" and applied directly
to the K8s cluster by _apply_k8s_jobs_from_queue() in ScriptPRWorkflowService.

How placeholders work:
  - Every {{KEY}} token in job.yaml is replaced with config_snapshot[key.lower()]
  - Standard tokens filled automatically: JOB_NAME, NAMESPACE, ENVIRONMENT
  - All other tokens must be present in config_snapshot (set by frontend / locator)

Adding a new operation:
  1. Create templates/helm-jobs/{operation}/job.yaml with {{PLACEHOLDER}} tokens
  2. Register the script_gen_key in ScriptGenHandler default map
  3. Add a case in DefaultFileLocator with mode="kubectl" and template_path set
  No changes to this component needed.
"""

import logging
import os
import re
import uuid

import aiofiles

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    DEFAULT_EKS_CLUSTER_NAME,
    DEFAULT_AWS_REGION,
)
from app.repository.transaction_queue_repository import TransactionQueueRepository

logger = logging.getLogger(__name__)


class K8sJobScriptGenComponent:
    """
    Generic K8s Job generator for one-time operational tasks on existing infra.

    Works for any operation whose template_path is set in file_location:
      1. Loads the job.yaml from file_location.template_path
      2. Replaces {{PLACEHOLDER}} tokens with values from config_snapshot
      3. Stages the rendered YAML with mode="kubectl" for direct cluster apply

    Adding a new operation only requires a new template and locator entry — no
    changes to this component.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

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
        operation = file_location.script_gen_key

        # ── Load job.yaml from locator-supplied template_path ─────────────────
        template_path = file_location.template_path
        if not template_path or not os.path.exists(template_path):
            raise ValueError(
                f"K8s Job template not found: '{template_path}'. "
                f"Set template_path in the FileLocationItem for operation='{operation}'."
            )

        async with aiofiles.open(template_path, "r") as f:
            job_yaml = await f.read()

        # ── Standard tokens always available ─────────────────────────────────
        namespace = f"{tenant}-ns"
        environment = config_snapshot.get("environment") or "stage"

        # Use case_ref_code for the job name slug (business operation name)
        case_ref_code = (
            queue_dict.get("case_ref_code")
            or config_snapshot.get("case_ref_code")
            or operation
        )
        op_slug = case_ref_code.replace("k8s_", "").replace("_", "-")[:24]

        identifier = (
            config_snapshot.get("identifier")
            or config_snapshot.get("database_name")
            or config_snapshot.get("new_username")
            or "job"
        )
        identifier_slug = identifier.lower().replace(" ", "-").replace("_", "-")[:20]
        suffix = uuid.uuid4().hex[:8]
        job_name = f"{op_slug}-{identifier_slug}-{suffix}"

        # ── Fill {{PLACEHOLDER}} tokens ───────────────────────────────────────
        # Standard tokens first
        job_yaml = job_yaml.replace("{{JOB_NAME}}", job_name)
        job_yaml = job_yaml.replace("{{NAMESPACE}}", namespace)
        job_yaml = job_yaml.replace("{{ENVIRONMENT}}", environment)

        # Remaining tokens resolved from config_snapshot (case-insensitive key match)
        for token in re.findall(r"\{\{(\w+)\}\}", job_yaml):
            value = config_snapshot.get(token.lower()) or config_snapshot.get(token) or ""
            job_yaml = job_yaml.replace(f"{{{{{token}}}}}", str(value))

        # ── Stage with mode="kubectl" ─────────────────────────────────────────
        if workflow_context:
            workflow_context.staged_files.append({
                "repo": "",
                "base_branch": "",
                "feature_branch": "",
                "file_path": "",
                "content": job_yaml,
                "queue_id": queue_dict.get("id"),
                "script_gen_key": operation,
                "mode": "kubectl",
                "config_snapshot": config_snapshot,
                "job_name": job_name,
                "namespace": namespace,
                "cluster_name": (
                    config_snapshot.get("eks_cluster_name")
                    or DEFAULT_EKS_CLUSTER_NAME
                ),
                "aws_region": (
                    config_snapshot.get("aws_region")
                    or config_snapshot.get("cloudRegion")
                    or DEFAULT_AWS_REGION
                ),
            })
            if queue_dict.get("id"):
                workflow_context.script_gen_responses[queue_dict["id"]][operation] = {
                    "original_content": job_yaml,
                    "preview_content": self._build_preview(operation, config_snapshot, job_name),
                }

        return job_yaml

    def _build_preview(self, operation: str, config_snapshot: dict, job_name: str) -> str:
        """Human-readable summary of what the job will do."""
        lines = [f"K8s Job: {operation}", ""]
        for key, value in config_snapshot.items():
            if any(s in key.lower() for s in ("password", "secret", "token")):
                lines.append(f"  {key}: ********")
            else:
                lines.append(f"  {key}: {value}")
        lines.append("")
        lines.append(f"# Job name  : {job_name}")
        lines.append("# Applied via: kubectl apply")
        return "\n".join(lines)
