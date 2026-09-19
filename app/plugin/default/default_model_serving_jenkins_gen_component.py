"""
Model Serving Jenkins Pipeline Generator Component

Generates a Jenkinsfile for deploying vLLM model-serving containers on EKS.
Unlike the standard EKS Jenkins generator, there is NO build stage — the
official vLLM Docker image is used directly. The pipeline is:
Initialisation (K8s infra) → Deploy (kubectl apply) → Verify (rollout + ALB).

The HuggingFace token is passed via a K8s Secret and the model name is
injected as container args.
"""
import re

import logging
import os
from typing import Dict, Any

import aiofiles

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    DEFAULT_EKS_CLUSTER_NAME,
    DEFAULT_AWS_REGION,
)
from app.core.config import settings
from app.plugin.default.default_eks_jenkins_gen_component import (
    _derive_name_prefix,
    _fetch_infra_values,
)

logger = logging.getLogger(__name__)

_MODEL_SERVING_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "generic", "Jenkinsfile-eks-modelserving"
)


class DefaultModelServingJenkinsGenComponent:
    """Generates Jenkinsfile content for vLLM model-serving deployments."""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def generate_jenkinsfile(
        self,
        config_snapshot: Dict[str, Any],
        db=None,
        tenant_code: str = "",
    ) -> str:
        """Generate a Jenkinsfile for model-serving deployment.

        Args:
            config_snapshot: Service configuration dict containing:
                - service_name, model_name, gpu_size
                - environment, infrastructure_mst_code
                - hf_token_secret_arn (optional, for reference)
            db: Async database session for fetching infrastructure values.
            tenant_code: Tenant code for naming.

        Returns:
            Generated Jenkinsfile content string (Groovy pipeline script).
        """
        service_name = config_snapshot.get("service_name", "")
        if service_name.lower().endswith("-service"):
            service_name = service_name[:-8]

        environment = config_snapshot.get("environment") or "stage"

        # Fetch infrastructure values from DB
        infrastructure_mst_code = config_snapshot.get("infrastructure_mst_code", "")
        geo_loc_mst_code = config_snapshot.get("geo_loc_mst_code", "")
        infra_values = {}
        if db and infrastructure_mst_code:
            try:
                infra_values = await _fetch_infra_values(db, infrastructure_mst_code, tenant_code, geo_loc_mst_code)
            except Exception as exc:
                self.logger.warning("Failed to fetch infra values: %s", exc)
        if not infra_values.get("name_prefix"):
            infra_values["name_prefix"] = _derive_name_prefix(tenant_code, geo_loc_mst_code)

        # Derive K8s resource name — short format for KServe URL:
        # {tenant}-{service}-predictor.models.devlift.ai
        safe_name = re.sub(r"[^a-z0-9-]", "-", service_name.lower()).strip("-")
        k8s_name = f"{tenant_code}-{safe_name}"
        namespace = f"{tenant_code}-ns"

        cluster_name = (
            config_snapshot.get("eks_cluster_name")
            or infra_values.get("cluster_name", DEFAULT_EKS_CLUSTER_NAME)
        )
        aws_region = config_snapshot.get("aws_region") or infra_values.get("aws_region", DEFAULT_AWS_REGION)

        # Load model-serving Jenkinsfile template
        async with aiofiles.open(_MODEL_SERVING_TEMPLATE_PATH, "r") as f:
            content = await f.read()

        # ── Resolve infra repo for manifest fetching at Jenkins runtime ──
        # Fresh session: `db` param may be None (this method is callable from
        # contexts that don't pass one) and we don't want a read-only tenant
        # lookup entangled with the caller's transaction.
        from app.utils.tenant_config import get_tenant_config
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as _db:
            tenant_cfg = await get_tenant_config(tenant_code, _db)
        infra_repo = tenant_cfg.github_infra_repository
        infra_branch = tenant_cfg.github_infra_branch or "main"
        manifests_path = f"eks-services/{safe_name}/deployment"

        content = content.replace("{{INFRA_REPO}}", infra_repo)
        content = content.replace("{{INFRA_BRANCH}}", infra_branch)
        content = content.replace("{{MANIFESTS_PATH}}", manifests_path)

        # Replace template placeholders
        content = content.replace("{{SERVICE_NAME}}", k8s_name)
        content = content.replace("{{ENVIRONMENT}}", environment)
        content = content.replace("{{AWS_REGION}}", aws_region)
        content = content.replace("{{EKS_CLUSTER_NAME}}", cluster_name)
        content = content.replace("{{NAMESPACE}}", namespace)

        # Webhook callback
        webhook_url = f"{settings.devlift_backend_url}/api/v1/webhooks/jenkins-webhook"
        content = content.replace("{{OBSTOOL_WEBHOOK_URL}}", webhook_url)
        content = content.replace("{{WEBHOOK_SECRET}}", settings.pipeline_webhook_secret or "")

        # ── EFS Model Storage values ──
        model_id = config_snapshot.get("model_name", "")
        revision = config_snapshot.get("model_revision", "main")
        efs_path = config_snapshot.get("efs_path", "")
        job_nm = config_snapshot.get("download_job_name", "")
        # Fallback: derive job name if not pre-populated (old config_snapshot)
        if not job_nm and model_id:
            _slug = re.sub(r"[^a-z0-9-]", "-", model_id.lower().replace("/", "--")).strip("-")
            _short = revision[:12] if revision else "main"
            job_nm = f"dl-{_slug}-{_short}"
            if len(job_nm) > 63:
                import hashlib
                _h = hashlib.md5(job_nm.encode()).hexdigest()[:6]
                job_nm = f"dl-{_slug[:40].rstrip('-')}-{_short}-{_h}"
        base_url = settings.devlift_backend_url.rstrip("/")

        content = content.replace("{{MODEL_ID}}", model_id)
        content = content.replace("{{REVISION}}", revision)
        content = content.replace("{{EFS_PATH}}", efs_path)
        content = content.replace("{{DOWNLOAD_JOB_NAME}}", job_nm)
        content = content.replace("{{OBS_TOOL_BASE_URL}}", base_url)

        return content

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = False,
        db=None,
    ) -> str:
        """Script gen interface wrapper — generates model-serving Jenkinsfile.

        Called by ScriptGenHandler in the main workflow loop.
        """
        config_snapshot = queue_dict.get("config_snapshot") or {}
        tenant_code = queue_dict.get("tenant_code") or tenant

        content = await self.generate_jenkinsfile(
            config_snapshot=config_snapshot,
            db=db,
            tenant_code=tenant_code,
        )

        service_name = config_snapshot.get("service_name") or config_snapshot.get("name", "unknown")
        if service_name.lower().endswith("-service"):
            service_name = service_name[:-8]

        # Stage for inline Jenkins deploy (mode="jenkins")
        if workflow_context:
            workflow_context.staged_files.append({
                "repo": file_location.repo or "",
                "base_branch": file_location.base_branch or "",
                "feature_branch": file_location.feature_branch or "",
                "file_path": file_location.file_path or "",
                "content": content,
                "queue_id": queue_dict.get("id"),
                "script_gen_key": file_location.script_gen_key,
                "mode": "jenkins",
                "config_snapshot": config_snapshot,
                "job_name": service_name,
            })

            if file_location.repo and file_location.file_path:
                key = f"{file_location.repo}|||{file_location.base_branch or ''}"
                queue_label = queue_dict.get("code") or queue_dict.get("id")
                msg = f"{queue_label}: add model-serving Jenkinsfile for {service_name}"
                existing = workflow_context.commit_messages.get(key, "")
                workflow_context.commit_messages[key] = f"{existing}\n{msg}" if existing else msg
            if queue_dict.get("id"):
                workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                    "original_content": content,
                    "preview_content": content,
                }

        return content
