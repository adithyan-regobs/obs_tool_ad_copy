"""
Default EKS Workflow Script Generation Component

Generates a language-agnostic GitHub Actions workflow pushed to
.github/workflows/ in the service repo.

Uses templates/generic/github-actions-eks.yml — the build happens entirely
inside Docker, no language-specific steps. The deploy strategy is configurable
via {{DEPLOY_STRATEGY}} placeholder (kubectl apply or rollout restart).

Infrastructure values (cluster name, AWS region, IAM role ARN, account ID) are
fetched from infrastructure_mst.locator + infra_vendor_account.auth_config.
ECR repo is created by the pipeline if it does not yet exist (idempotent).
"""

import json
import logging
import os
from typing import Dict, Any

import aiofiles

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.timing import log_timing
from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    DEFAULT_EKS_CLUSTER_NAME,
    DEFAULT_AWS_REGION,
    AWS_ROLE_ARN as DEFAULT_AWS_ROLE_ARN,
    AWS_ACCOUNT_ID as DEFAULT_AWS_ACCOUNT_ID,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

_GENERIC_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "generic", "github-actions-eks.yml"
)

# Pre-built deploy strategy blocks injected into {{DEPLOY_STRATEGY}}
_DEPLOY_STRATEGY_KUBECTL_APPLY = """\
      - name: Update image and deploy to EKS
        env:
          IMAGE_TAG: ${{{{ github.sha }}}}
        run: |
          sed -i "s|IMAGE_TAG_PLACEHOLDER|${{IMAGE_TAG}}|g" deployment/*.yaml
          kubectl apply -f deployment/
          kubectl rollout status deployment/{service_name} --namespace={namespace} --timeout=5m"""

_DEPLOY_STRATEGY_ROLLOUT_RESTART = """\
      - name: Restart deployment to pick up new image
        run: |
          kubectl rollout restart deployment/{service_name} --namespace={namespace}
          kubectl rollout status deployment/{service_name} --namespace={namespace} --timeout=5m"""


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
    """
    Fetch EKS cluster metadata from infrastructure_mst.locator and
    infra_vendor_account.auth_config.

    Returns:
        {cluster_name, aws_region, account_id, deploy_role_arn}
    """
    if not infrastructure_mst_code or not db:
        return {}
    from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
    infra_repo = InfrastructureMstRepository(db)
    infrastructure = await infra_repo.get_by_code(infrastructure_mst_code)
    if not infrastructure:
        logger.warning("infrastructure_mst record not found: %s", infrastructure_mst_code)
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


class DefaultEksWorkflowScriptGenComponent:
    """
    Generates the GitHub Actions CI/CD workflow using the same language-specific
    templates as the MCP EKS onboarding server, adapted for deployment/ manifests.

    Key differences from the Aspora workflow component:
    - deploy step: kubectl apply -f deployment/ (with sed image replace)
    - ECR repo creation: idempotent aws ecr create-repository step
    - Cluster name / role ARN: fetched from infrastructure_mst DB record
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
        config_snapshot = queue_dict.get("config_snapshot") or {}

        service_name = config_snapshot.get("service_name", "")
        if service_name.lower().endswith("-service"):
            service_name = service_name[:-8]
        if not service_name:
            raise ValueError("service_name is required for default EKS workflow generation")

        environment = config_snapshot.get("environment") or queue_dict.get("environment") or "stage"
        environment_name = config_snapshot.get("environment_name") or environment.capitalize()
        identifier = config_snapshot.get("identifier") or service_name
        dockerfile_path = config_snapshot.get("dockerfile_path") or "Dockerfile"

        # ── Fetch infrastructure values from DB ─────────────────────────────
        infrastructure_mst_code = config_snapshot.get("infrastructure_mst_code", "")
        infra_values = {}
        if db and infrastructure_mst_code:
            try:
                infra_values = await _fetch_infra_values(db, infrastructure_mst_code)
            except Exception as exc:
                self.logger.warning("Failed to fetch infra values from DB: %s", exc)

        cluster_name = (
            config_snapshot.get("eks_cluster_name")
            or infra_values.get("cluster_name", DEFAULT_EKS_CLUSTER_NAME)
        )
        aws_region = config_snapshot.get("aws_region") or infra_values.get("aws_region", DEFAULT_AWS_REGION)
        account_id = config_snapshot.get("aws_account_id") or infra_values.get("account_id", DEFAULT_AWS_ACCOUNT_ID)
        deploy_role_arn = (
            config_snapshot.get("aws_role_arn")
            or infra_values.get("deploy_role_arn", DEFAULT_AWS_ROLE_ARN)
        )
        ecr_registry = config_snapshot.get("ecr_registry") or f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com"
        namespace = config_snapshot.get("namespace") or service_name

        trigger_branch = file_location.base_branch or file_location.target_branch or "main"

        # ── Build args ────────────────────────────────────────────────────────
        build_args = config_snapshot.get("build_args") or []
        custom_build_args = ""
        for arg in build_args:
            name = (arg.get("name") or arg.get("key") or "").strip()
            if name:
                custom_build_args += f"            --build-arg {name}=${{{name}}} \\\n"

        dockerfile_flag = f"-f {dockerfile_path} " if dockerfile_path != "Dockerfile" else ""

        # ── Folder path filter (monorepo support) ─────────────────────────────
        folder_path = config_snapshot.get("folder_path") or ""
        folder_path_filter = ""
        if folder_path:
            folder_path_filter = f"    paths:\n      - '{folder_path.strip('/')}/**'"

        # ── GitHub environment ────────────────────────────────────────────────
        github_environment = config_snapshot.get("github_environment") or ""
        github_env_block = ""
        if github_environment:
            github_env_block = f"    environment: {github_environment}"

        # ── Deploy strategy ───────────────────────────────────────────────────
        deploy_strategy = config_snapshot.get("deploy_strategy", "kubectl_apply")
        if deploy_strategy == "rollout_restart":
            deploy_block = _DEPLOY_STRATEGY_ROLLOUT_RESTART.format(
                service_name=service_name, namespace=namespace,
            )
        else:
            deploy_block = _DEPLOY_STRATEGY_KUBECTL_APPLY.format(
                service_name=service_name, namespace=namespace,
            )

        # ── Repo / branch info ───────────────────────────────────────────────
        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # ── Check staged cache ───────────────────────────────────────────────
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context, file_location.repo, base_branch, file_location.file_path
            )
        if cached_entry:
            existing_file = {"exists": True, "content": cached_entry.get("content")}
        else:
            fetch_context = f"repo={repo} branch={feature_branch} path={file_location.file_path}"
            with log_timing(logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db, tenant=tenant, owner=owner, repo=repo,
                    file_path=file_location.file_path, branch=feature_branch,
                )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        # ── Load and render generic EKS workflow template ─────────────────────
        actual_ecr = f"{ecr_registry}/{service_name}"
        gen_context = f"path={file_location.file_path} service={service_name}"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            async with aiofiles.open(_GENERIC_TEMPLATE_PATH, "r") as f:
                workflow_yaml = await f.read()

            workflow_yaml = workflow_yaml.replace("{{SERVICE_NAME}}", service_name)
            workflow_yaml = workflow_yaml.replace("{{ENVIRONMENT_NAME}}", environment_name)
            workflow_yaml = workflow_yaml.replace("{{ENVIRONMENT}}", environment)
            workflow_yaml = workflow_yaml.replace("{{BRANCH}}", trigger_branch)
            workflow_yaml = workflow_yaml.replace("{{AWS_REGION}}", aws_region)
            workflow_yaml = workflow_yaml.replace("{{ECR_REPOSITORY}}", actual_ecr)
            workflow_yaml = workflow_yaml.replace("{{AWS_ROLE_ARN}}", deploy_role_arn)
            workflow_yaml = workflow_yaml.replace("{{EKS_CLUSTER_NAME}}", cluster_name)
            workflow_yaml = workflow_yaml.replace("{{NAMESPACE}}", namespace)
            workflow_yaml = workflow_yaml.replace("{{DOCKERFILE_FLAG}}", dockerfile_flag)
            workflow_yaml = workflow_yaml.replace("{{CUSTOM_BUILD_ARGS}}", custom_build_args)
            workflow_yaml = workflow_yaml.replace("{{FOLDER_PATH_FILTER}}", folder_path_filter)
            workflow_yaml = workflow_yaml.replace("{{GITHUB_ENVIRONMENT}}", github_env_block)
            workflow_yaml = workflow_yaml.replace("{{DEPLOY_STRATEGY}}", deploy_block)

        preview_yaml = (
            f"# EKS Workflow Preview\n"
            f"# Service     : {service_name}\n"
            f"# Branch      : {trigger_branch}\n"
            f"# Environment : {environment}\n"
            f"# Cluster     : {cluster_name}\n"
            f"# Region      : {aws_region}\n"
            f"# Role ARN    : {deploy_role_arn}\n"
            f"# ECR         : {actual_ecr}\n"
        )

        # ── Upload to S3 ─────────────────────────────────────────────────────
        if upload_to_s3:
            try:
                original_s3_key = f"default/eks/workflow/{identifier}.yml"
                await FileManagerHandler.upload_file(
                    key=original_s3_key, content=workflow_yaml, content_type="text/plain",
                )
                preview_s3_key = f"preview/default/eks/workflow/{identifier}.yml"
                await FileManagerHandler.upload_file(
                    key=preview_s3_key, content=preview_yaml, content_type="text/plain",
                    metadata={
                        "type": "preview", "service_name": service_name,
                        "environment": environment,
                        "generated_by": "default_eks_workflow_script_gen_component",
                    },
                )
                repo_for_db = repository or self.repository
                if repo_for_db and queue_dict.get("code"):
                    await repo_for_db.update_artifact_s3_key(
                        queue_dict["code"],
                        json.dumps({"original_s3_key": original_s3_key, "preview": preview_s3_key}),
                    )
            except Exception as exc:
                logger.error("Failed to upload workflow to S3: %s", exc, exc_info=True)
                raise

        # ── Stage for git commit ─────────────────────────────────────────────
        if tenant and file_location and workflow_context:
            if not workflow_context.skip_commit:
                skip_commit = False
                existing_content = existing_file.get("content")
                if existing_file.get("exists") and existing_content:
                    skip_commit = should_skip_commit(existing_content, workflow_yaml)
                    if skip_commit:
                        logger.info(
                            "Workflow unchanged on %s, skipping commit for %s",
                            feature_branch, file_location.file_path,
                        )
                if not skip_commit:
                    _upsert_staged_entry(
                        workflow_context=workflow_context,
                        repo=file_location.repo,
                        base_branch=base_branch,
                        feature_branch=feature_branch,
                        file_path=file_location.file_path,
                        content=workflow_yaml,
                        queue_id=queue_dict.get("id"),
                        script_gen_key=file_location.script_gen_key,
                    )
                    queue_label = queue_dict.get("code") or queue_dict.get("id")
                    commit_line = (
                        f"{queue_label}: {file_location.script_gen_key} -> {file_location.file_path}"
                        if queue_label
                        else f"{file_location.script_gen_key} -> {file_location.file_path}"
                    )
                    _append_commit_message(workflow_context, file_location.repo, base_branch, commit_line)

            if queue_dict.get("id"):
                workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                    "original_content": workflow_yaml,
                    "preview_content": preview_yaml,
                }

        return workflow_yaml
