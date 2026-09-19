"""
Default EKS Service Operations Generator Component

Generates Jenkinsfile pipeline scripts for EKS service lifecycle operations:
  - stop_service:    kubectl scale deployment --replicas=0
  - restart_service: kubectl rollout restart deployment
  - delete_service:  kubectl delete all K8s resources (deployment, service, ingress, etc.)

Template:
  templates/eks/operations/Jenkinsfile

The generated Jenkinsfile is staged with mode="jenkins" and provisioned via
provision_pipeline_job() in JenkinsProvisioningService.
"""

import logging
import os
import re
from typing import Dict

import aiofiles

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    DEFAULT_EKS_CLUSTER_NAME,
    DEFAULT_AWS_REGION,
)
from app.core.config import settings
from app.plugin.default.default_eks_jenkins_gen_component import (
    _fetch_infra_values,
    _derive_name_prefix,
)
from app.repository.transaction_queue_repository import TransactionQueueRepository

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "eks", "operations", "Jenkinsfile"
)

# Maps case_ref_code to the kubectl command(s) to execute
_OPS_COMMANDS: Dict[str, str] = {
    "stop_service": (
        '                    echo "Scaling deployment to 0 replicas..."\n'
        '                    kubectl scale deployment/${SERVICE_NAME} '
        '--replicas=0 -n ${NAMESPACE}\n'
        '                    echo "Service stopped. Waiting for pods to terminate..."\n'
        '                    kubectl rollout status deployment/${SERVICE_NAME} '
        '-n ${NAMESPACE} --timeout=120s || true'
    ),
    "restart_service": (
        '                    echo "Restarting deployment..."\n'
        '                    kubectl rollout restart deployment/${SERVICE_NAME} '
        '-n ${NAMESPACE}\n'
        '                    echo "Waiting for rollout to complete..."\n'
        '                    kubectl rollout status deployment/${SERVICE_NAME} '
        '-n ${NAMESPACE} --timeout=300s'
    ),
    "delete_service": (
        '                    echo "Deleting all resources for service..."\n'
        '                    kubectl delete deployment ${SERVICE_NAME} -n ${NAMESPACE} --ignore-not-found\n'
        '                    kubectl delete service ${SERVICE_NAME} -n ${NAMESPACE} --ignore-not-found\n'
        '                    kubectl delete ingress ${SERVICE_NAME} -n ${NAMESPACE} --ignore-not-found\n'
        '                    kubectl delete serviceaccount ${SERVICE_NAME} -n ${NAMESPACE} --ignore-not-found\n'
        '                    kubectl delete secretproviderclass ${SERVICE_NAME} -n ${NAMESPACE} --ignore-not-found\n'
        '                    echo "All resources deleted for ${SERVICE_NAME} in ${NAMESPACE}"'
    ),
}

_OPS_STAGE_NAMES: Dict[str, str] = {
    "stop_service": "Stop Service",
    "restart_service": "Restart Service",
    "delete_service": "Delete Service",
}

_OPS_LABELS: Dict[str, str] = {
    "stop_service": "stop",
    "restart_service": "restart",
    "delete_service": "delete",
}


class DefaultEksServiceOpsGenComponent:
    """
    Generates Jenkinsfile for EKS service lifecycle operations
    (stop, restart, delete).

    Loads the template from templates/eks/operations/Jenkinsfile and replaces
    placeholders with operation-specific kubectl commands and infra values.

    The operation type is determined by the queue item's case_ref_code.
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
        tenant_code = queue_dict.get("tenant_code") or tenant
        operation = queue_dict.get("case_ref_code") or file_location.script_gen_key

        # Resolve service name
        service_name = config_snapshot.get("service_name") or config_snapshot.get("name", "unknown")
        if service_name.lower().endswith("-service"):
            service_name = service_name[:-8]

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

        # Derive K8s resource name (same logic as deploy component)
        safe_name = re.sub(r"[^a-z0-9-]", "-", service_name.lower()).strip("-")
        name_prefix = infra_values.get("name_prefix", "")
        k8s_name = f"{name_prefix}-{safe_name}" if name_prefix else safe_name
        namespace = f"{tenant_code}-ns"

        cluster_name = (
            config_snapshot.get("eks_cluster_name")
            or infra_values.get("cluster_name", DEFAULT_EKS_CLUSTER_NAME)
        )
        aws_region = (
            config_snapshot.get("aws_region")
            or infra_values.get("aws_region", DEFAULT_AWS_REGION)
        )

        # Webhook for build status
        webhook_url = f"{settings.devlift_backend_url}/api/v1/webhooks/jenkins-webhook"
        webhook_secret = settings.pipeline_webhook_secret or ""

        # Load template and replace placeholders
        async with aiofiles.open(_TEMPLATE_PATH, "r") as f:
            content = await f.read()

        kubectl_commands = _OPS_COMMANDS.get(operation, "")
        stage_name = _OPS_STAGE_NAMES.get(operation, operation)

        content = content.replace("{{AWS_REGION}}", aws_region)
        content = content.replace("{{EKS_CLUSTER_NAME}}", cluster_name)
        content = content.replace("{{NAMESPACE}}", namespace)
        content = content.replace("{{SERVICE_NAME}}", k8s_name)
        content = content.replace("{{OBSTOOL_WEBHOOK_URL}}", webhook_url)
        content = content.replace("{{WEBHOOK_SECRET}}", webhook_secret)
        content = content.replace("{{STAGE_NAME}}", stage_name)
        content = content.replace("{{KUBECTL_COMMANDS}}", kubectl_commands)
        content = content.replace("{{OPERATION}}", operation)

        # Stage for Jenkins provisioning
        op_label = _OPS_LABELS.get(operation, operation)
        job_name = f"{safe_name}-{op_label}"

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
                "job_name": job_name,
            })

            if queue_dict.get("id"):
                workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                    "original_content": content,
                    "preview_content": f"EKS Service {stage_name}: {k8s_name} in {namespace}",
                }

        return content
