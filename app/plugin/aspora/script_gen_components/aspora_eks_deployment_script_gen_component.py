"""
EKS Deployment Script Generation Component

Generates Kubernetes deployment.yaml content using KustomizeGeneratorService.
Uploads to S3 and updates database with S3 keys.
"""

import json
import logging
import os
from typing import Dict, Any

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.repository.services_mst_repository import ServicesMstRepository
from app.services.kustomize_generator_service import KustomizeGeneratorService
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.timing import log_timing

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


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


class AsporaEksDeploymentScriptGenComponent:
    """
    Component for generating EKS deployment.yaml for Aspora tenant.

    Responsibilities:
    - Generate deployment.yaml using KustomizeGeneratorService
    - Generate preview content (same as deployment.yaml)
    - Upload to S3 (original and preview)
    - Update database with S3 keys
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
        Generate EKS deployment.yaml configuration.

        This function:
        1. Checks if file exists in GitHub
        2. Generates deployment.yaml using KustomizeGeneratorService
        3. Generates preview YAML (same as deployment.yaml)
        4. Uploads to S3 if requested
        5. Returns the final YAML content

        Returns:
            Generated/updated deployment.yaml content

        Raises:
            ValueError: If required parameters are missing
        """
        config_snapshot = queue_dict.get("config_snapshot") or {}
        service_name = config_snapshot.get("service_name")
        service_code = config_snapshot.get("services_mst_code") or config_snapshot.get("service_mst_code")
        repo_for_service = repository or self.repository
        if service_code and repo_for_service and getattr(repo_for_service, "session", None):
            try:
                services_repo = ServicesMstRepository(repo_for_service.session)
                service = await services_repo.get_by_code(service_code)
                if service and service.name:
                    service_name = service.name
            except Exception as exc:
                logger.warning("Failed to resolve service name from service API: %s", exc)

        if not service_name:
            raise ValueError("service_name is required for EKS deployment generation")

        if not service_name.endswith('-service'):
            service_name = f"{service_name}-service"

        identifier = config_snapshot.get("identifier") or service_name
        environment = config_snapshot.get("environment") or queue_dict.get("environment") or "dev"

        if not identifier and file_location and file_location.file_path:
            identifier = os.path.basename(os.path.dirname(file_location.file_path))

        if not identifier:
            raise ValueError("Identifier cannot be empty")

        logger.info(f"Generating EKS deployment for: {file_location.file_path}")
        logger.info(f"  Service: {service_name} (env: {environment})")

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

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
            fetch_context = f"repo={repo} branch={feature_branch} path={file_location.file_path}"
            with log_timing(logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db,
                    tenant=tenant,
                    owner=owner,
                    repo=repo,
                    file_path=file_location.file_path,
                    branch=feature_branch,
                )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        gen_mode = "create" if not existing_file["exists"] else "update"
        gen_context = f"path={file_location.file_path} mode={gen_mode}"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            config = config_snapshot
            deployment_strategy = (
                queue_dict.get("deployment_strategy")
                or config_snapshot.get("deployment_strategy")
            )

            ecr_registry = config.get("ecr_registry", "")
            ecr_repo_name = config.get("ecr_repo_name", service_name)
            alb_subnets = config.get("alb_subnets", "")
            alb_certificate_arn = config.get("alb_certificate_arn", "")
            ingress_host = config.get("ingress_host", "")
            secrets_manager_path = config.get(
                "secrets_manager_path",
                f"{service_name}/{environment}"
            )
            aws_region = config.get("aws_region", "us-west-2")
            namespace = config.get("namespace", f"{service_name}-{environment}")

            kustomize_service = KustomizeGeneratorService()
            content = kustomize_service.generate_deployment_yaml(
                service_name=service_name,
                namespace=namespace,
                environment=environment,
                config=config,
                deployment_strategy=deployment_strategy,
                aws_region=aws_region,
                ecr_registry=ecr_registry,
                ecr_repo_name=ecr_repo_name,
                image_tag="${IMAGE_TAG}",
                alb_subnets=alb_subnets,
                alb_certificate_arn=alb_certificate_arn,
                ingress_host=ingress_host,
                secrets_manager_path=secrets_manager_path
            )

            preview_yaml = content

        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            try:
                original_s3_key = f"eks/deployment/{identifier}.yaml"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=content,
                    content_type="text/plain"
                )
                logger.info(f"Uploaded deployment YAML to S3: {result['location']}")

                preview_s3_key = f"preview/eks/deployment/{identifier}.yaml"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_yaml,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": environment,
                        "identifier": identifier,
                        "service_name": service_name,
                        "generated_by": "aspora_eks_deployment_script_gen_component"
                    }
                )
                logger.info(f"Uploaded preview deployment YAML to S3: {result['location']}")

                if repository and queue_dict.get("code"):
                    artifact_s3_key_json = {
                        "original_s3_key": original_s3_key,
                        "preview": preview_s3_key
                    }
                    update_payload = json.dumps(artifact_s3_key_json)
                    await repository.update_artifact_s3_key(queue_dict.get("code"), update_payload)
                    self.logger.info(
                        "Saved artifact_s3_key to database for queue item %s: %s",
                        queue_dict.get("code"),
                        update_payload
                    )

            except Exception as e:
                logger.error(f"Failed to upload to S3: {e}", exc_info=True)
                raise

        if tenant and file_location and workflow_context:
            if not workflow_context.skip_commit:
                skip_commit = False
                existing_content = existing_file.get("content")
                if existing_file.get("exists") and existing_content:
                    skip_commit = should_skip_commit(existing_content, content)
                    if skip_commit:
                        logger.info(
                            "Deployment YAML unchanged on feature branch %s, skipping commit for %s",
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
                        content=content,
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
                    "original_content": content,
                    "preview_content": preview_yaml
                }

        return content
