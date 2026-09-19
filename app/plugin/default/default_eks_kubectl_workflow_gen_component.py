"""
EKS kubectl-apply GitHub Actions Workflow Generator

Generates a GitHub Actions workflow for deploying K8s manifests from the infra repo
via kubectl apply. Triggered on push to eks-services/{service}/deployment/**.

Sends webhook callbacks in the same format as Jenkins (JenkinsBuildPayload)
so the existing webhook endpoint can be reused.
"""

import logging
import os
import re
from typing import Dict, Any

import aiofiles

from app.core.config import settings
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.github_sync_helpers import should_skip_commit
from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    DEFAULT_EKS_CLUSTER_NAME,
    DEFAULT_AWS_REGION,
    AWS_ROLE_ARN as DEFAULT_AWS_ROLE_ARN,
    AWS_ACCOUNT_ID as DEFAULT_AWS_ACCOUNT_ID,
)

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "generic", "github-actions-eks-kubectl.yml"
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


def _upsert_staged_entry(workflow_context, repo, base_branch, feature_branch, file_path, content, queue_id, script_gen_key):
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
    workflow_context.commit_messages[key] = f"{existing}\n{message}" if existing else message


async def _fetch_infra_values(db, infrastructure_mst_code: str) -> Dict[str, Any]:
    if not infrastructure_mst_code or not db:
        return {}
    from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
    infra_repo = InfrastructureMstRepository(db)
    infrastructure = await infra_repo.get_by_code(infrastructure_mst_code)
    if not infrastructure:
        return {}
    locator = infrastructure.locator or {}
    vendor_auth = (infrastructure.infra_vendor_account.auth_config or {}) if infrastructure.infra_vendor_account else {}
    return {
        "cluster_name": locator.get("cluster_name") or locator.get("cluster") or infrastructure.name,
        "aws_region": locator.get("region") or vendor_auth.get("region", DEFAULT_AWS_REGION),
        "account_id": vendor_auth.get("account_id", DEFAULT_AWS_ACCOUNT_ID),
        "deploy_role_arn": (
            vendor_auth.get("deploy_role_arn")
            or vendor_auth.get("github_role_arn")
            or vendor_auth.get("assume_role_arn")
            or DEFAULT_AWS_ROLE_ARN
        ),
    }


class DefaultEksKubectlWorkflowGenComponent:
    """Generates a GHA kubectl-apply workflow for deploying K8s manifests from the infra repo."""

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
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
        config_snapshot = queue_dict.get("config_snapshot") or {}

        service_name = config_snapshot.get("service_name", "")
        if service_name.lower().endswith("-service"):
            service_name = service_name[:-8]
        if not service_name:
            raise ValueError("service_name is required for kubectl workflow generation")

        service_name_sanitized = re.sub(r"[^a-z0-9-]", "-", service_name.lower()).strip("-")
        environment = config_snapshot.get("environment") or "stage"
        environment_name = config_snapshot.get("environment_name") or environment.capitalize()
        namespace = f"{tenant}-ns"

        # Infra values
        infrastructure_mst_code = config_snapshot.get("infrastructure_mst_code", "")
        infra_values = {}
        if db and infrastructure_mst_code:
            try:
                infra_values = await _fetch_infra_values(db, infrastructure_mst_code)
            except Exception as exc:
                self.logger.warning("Failed to fetch infra values: %s", exc)

        cluster_name = (
            config_snapshot.get("eks_cluster_name")
            or infra_values.get("cluster_name", DEFAULT_EKS_CLUSTER_NAME)
        )
        aws_region = config_snapshot.get("aws_region") or infra_values.get("aws_region", DEFAULT_AWS_REGION)
        deploy_role_arn = (
            config_snapshot.get("aws_role_arn")
            or infra_values.get("deploy_role_arn", DEFAULT_AWS_ROLE_ARN)
        )

        trigger_branch = file_location.base_branch or file_location.target_branch or "main"

        # Determine verify timeout based on service type
        is_model_serving = config_snapshot.get("service_type") == "MODEL_SERVING"
        verify_timeout = "30m" if is_model_serving else "15m"

        # Webhook config
        webhook_url = f"{settings.devlift_backend_url}/api/v1/webhooks/jenkins-webhook"
        webhook_secret = settings.pipeline_webhook_secret or ""

        # GitHub environment block (optional)
        github_env_block = ""
        if environment in ("prod", "production"):
            github_env_block = f"    environment: production"

        # Load and render template
        async with aiofiles.open(_TEMPLATE_PATH, "r") as f:
            content = await f.read()

        content = content.replace("{{SERVICE_NAME}}", service_name_sanitized)
        content = content.replace("{{SERVICE_NAME_SANITIZED}}", service_name_sanitized)
        content = content.replace("{{ENVIRONMENT}}", environment)
        content = content.replace("{{ENVIRONMENT_NAME}}", environment_name)
        content = content.replace("{{NAMESPACE}}", namespace)
        content = content.replace("{{AWS_REGION}}", aws_region)
        content = content.replace("{{EKS_CLUSTER_NAME}}", cluster_name)
        content = content.replace("{{AWS_ROLE_ARN}}", deploy_role_arn)
        content = content.replace("{{BRANCH}}", trigger_branch)
        content = content.replace("{{VERIFY_TIMEOUT}}", verify_timeout)
        content = content.replace("{{OBSTOOL_WEBHOOK_URL}}", webhook_url)
        content = content.replace("{{WEBHOOK_SECRET}}", webhook_secret)
        content = content.replace("{{GITHUB_ENVIRONMENT}}", github_env_block)

        # Git context
        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""

        # Check existing content
        cached_entry = _find_staged_entry(workflow_context, file_location.repo, base_branch, file_location.file_path)
        if cached_entry:
            existing_file = {"exists": True, "content": cached_entry.get("content")}
        else:
            existing_file = await GitOpsHandler.get_content(
                db=db, tenant=tenant, owner=owner, repo=repo,
                file_path=file_location.file_path, branch=feature_branch,
            )

        # Upload to S3
        if upload_to_s3:
            try:
                s3_key = f"default/eks/kubectl-workflow/{service_name_sanitized}-{environment}.yml"
                await FileManagerHandler.upload_file(
                    key=s3_key, content=content, content_type="text/plain",
                )
            except Exception as exc:
                logger.error("Failed to upload kubectl workflow to S3: %s", exc, exc_info=True)

        # Stage for git commit
        if tenant and file_location and workflow_context:
            if not workflow_context.skip_commit:
                skip_commit = False
                existing_content = existing_file.get("content")
                if existing_file.get("exists") and existing_content:
                    skip_commit = should_skip_commit(existing_content, content)
                if not skip_commit:
                    _upsert_staged_entry(
                        workflow_context=workflow_context,
                        repo=file_location.repo,
                        base_branch=base_branch,
                        feature_branch=feature_branch,
                        file_path=file_location.file_path,
                        content=content,
                        queue_id=queue_dict.get("id"),
                        script_gen_key=file_location.script_gen_key,
                    )
                    queue_label = queue_dict.get("code") or queue_dict.get("id")
                    commit_line = f"{queue_label}: kubectl workflow -> {file_location.file_path}"
                    _append_commit_message(workflow_context, file_location.repo, base_branch, commit_line)

            if queue_dict.get("id"):
                workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                    "original_content": content,
                    "preview_content": content,
                }

        return content
