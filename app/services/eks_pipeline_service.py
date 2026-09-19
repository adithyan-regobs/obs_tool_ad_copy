"""
EKS Pipeline Service

Handles EKS-specific pipeline sync:
- Generates config.yaml from service_config
- Generates inline workflow YAML (customizable steps)
- Pushes both files to GitHub via PR

This is separate from ECS pipeline management because:
1. EKS uses inline workflows (all steps visible in workflow file)
2. EKS requires a config.yaml file in configs/{env}/
3. No Terragrunt sync is needed for EKS
4. Supports customizable workflow steps via pipeline_steps config
"""

import os
import re
import time
import hashlib
import logging
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enum import WorkflowSourceTableEnum, PipelineRunStatusEnum
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.repository.language_ref_repository import LanguageRefRepository
from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
from app.repository.pipeline_mst_repository import PipelineMstRepository
from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
from app.repository.pipeline_vendor_mst_repository import PipelineVendorMstRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.domain.factories.gitops_workflow_detail_factory import make_gitops_workflow_detail
from app.integrations.github_integration import GitHubIntegration
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.yaml_patch import trigger_path_pattern
from app.utils.pipeline_helpers import (
    sanitize_name,
    parse_repo_url
)
from app.services.workflow_generator import generate_workflow_yaml
from app.tasks.pipeline_polling import start_pipeline_polling

logger = logging.getLogger(__name__)

# Template paths
EKS_CONFIG_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "templates", "eks", "config", "config.yaml"
)
EKS_WORKFLOW_JAVA_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "templates", "eks", "workflow", "workflow-java.yml"
)
EKS_WORKFLOW_GO_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "templates", "eks", "workflow", "workflow-go.yml"
)

# Environment-specific defaults
EKS_ENV_DEFAULTS = {
    "dev": {
        "skip_code_quality": False,
        "skip_snyk_code": False,
        "skip_snyk_oss": False,
        "skip_snyk_container": False,
        "environment_display": "Dev"
    },
    "staging": {
        "skip_code_quality": False,
        "skip_snyk_code": False,
        "skip_snyk_oss": False,
        "skip_snyk_container": False,
        "environment_display": "Stage"
    },
    "qa": {
        "skip_code_quality": False,
        "skip_snyk_code": False,
        "skip_snyk_oss": False,
        "skip_snyk_container": False,
        "environment_display": "QA"
    },
    "prod": {
        "skip_code_quality": False,
        "skip_snyk_code": False,
        "skip_snyk_oss": False,
        "skip_snyk_container": False,
        "environment_display": "Prod"
    }
}


class EKSPipelineService:
    """Service for EKS pipeline management"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.language_repo = LanguageRefRepository(session)
        self.gitops_workflow_repository = GitopsWorkflowDetailRepository(session)
        self.pipeline_vendor_repo = PipelineVendorMstRepository(session)
        self.infrastructure_repo = InfrastructureMstRepository(session)


    async def _get_github_token(self, owner: str) -> str:
        """Get GitHub App installation token for the given org."""
        from app.utils.github_app_token import get_token_for_org
        return await get_token_for_org(owner, self.session)

    async def sync_eks_pipeline(
        self,
        service_config: ServiceConfigModel,
        service: ServicesMstModel,
        branch: str,
        environment: str,
        tenant_code: str,
        user_code: str = None,
        github_token: str = None
    ) -> Dict[str, Any]:
        """
        Sync EKS pipeline for a service config branch.

        Creates/updates two files:
        1. configs/{env}/config.yaml
        2. .github/workflows/deploy-{service}-{env}.yml

        Args:
            service_config: The service configuration model
            service: The service model (ServicesMstModel)
            branch: Branch name (e.g., "main", "stage-env")
            environment: Environment (dev/staging/prod)
            tenant_code: Tenant code for tracking
            user_code: User code for tracking
            github_token: GitHub token (optional, reuses token if provided)

        Returns:
            Dict with status, pr_url, pr_number, files_committed, etc.
        """
        try:
            logger.info(f"Syncing EKS pipeline for service={service.name}, branch={branch}, env={environment}")

            # Get repository from config
            config = service_config.config or {}
            repo_url = config.get("repository", "")
            if not repo_url:
                return {
                    "branch": branch,
                    "status": "error",
                    "error": "Repository not configured in service_config"
                }

            # Parse owner/repo
            if "/" not in repo_url:
                return {
                    "branch": branch,
                    "status": "error",
                    "error": f"Invalid repository format: {repo_url}. Expected 'owner/repo'"
                }
            owner, repo = repo_url.split("/", 1)

            # Get GitHub token
            if not github_token:
                github_token = await self._get_github_token(owner)
            if not github_token:
                return {
                    "branch": branch,
                    "status": "error",
                    "error": "GitHub token not configured"
                }

            # Get language reference (both name and version)
            language_name = "java"  # Default
            language_version = None  # Will be fetched from language_ref.version
            if service_config.language_ref_code:
                language_ref = await self.language_repo.get_by_code(service_config.language_ref_code)
                if language_ref:
                    logger.info(f"[DEBUG] language_ref_code: {service_config.language_ref_code}, language_ref.name: '{language_ref.name}', language_ref.version: '{language_ref.version}'")
                    language_name = language_ref.name.lower()
                    language_version = language_ref.version  # Get version from DB (e.g., "23")
            logger.info(f"Language for EKS pipeline: {language_name}, version: {language_version}")

            # Generate config.yaml content
            config_yaml_content = self._generate_config_yaml(
                service_config=service_config,
                service=service,
                environment=environment,
                language_name=language_name,
                language_version=language_version
            )

            # Generate workflow YAML content and get steps
            workflow_yaml_content, workflow_steps, normalized_language = await self._generate_workflow_yaml(
                service_config=service_config,
                service=service,
                branch=branch,
                environment=environment,
                language_name=language_name,
                language_ref_code=service_config.language_ref_code or "JAVA_21"  # Fallback
            )

            # Generate deployment.yaml content using KustomizeGeneratorService
            from app.services.kustomize_generator_service import KustomizeGeneratorService
            kustomize_service = KustomizeGeneratorService()

            # Get infrastructure details for deployment.yaml
            ecr_registry = config.get("ecr_registry", "")
            safe_name = service.name.lower().replace(" ", "-")
            ecr_repo_name = config.get("ecr_repo_name", safe_name)
            alb_subnets = config.get("alb_subnets", "")
            alb_certificate_arn = config.get("alb_certificate_arn", "")
            ingress_host = config.get("ingress_host", "")
            secrets_manager_path = config.get("secrets_manager_path", f"{safe_name}/{environment}")
            aws_region = config.get("aws_region", "us-west-2")
            namespace = config.get("namespace", f"{safe_name}-{environment}")

            deployment_yaml_content = kustomize_service.generate_deployment_yaml(
                service_name=service.name,
                namespace=namespace,
                environment=environment,
                config=config,
                deployment_strategy=service_config.deployment_strategy,
                aws_region=aws_region,
                ecr_registry=ecr_registry,
                ecr_repo_name=ecr_repo_name,
                image_tag="${IMAGE_TAG}",  # Placeholder - replaced by CI/CD
                alb_subnets=alb_subnets,
                alb_certificate_arn=alb_certificate_arn,
                ingress_host=ingress_host,
                secrets_manager_path=secrets_manager_path
            )

            # Determine file paths
            env_for_path = "stage" if environment == "staging" else environment
            build_path = (config.get("build_path") or "").strip().strip("/")
            if build_path:
                config_file_path = f"configs/{build_path}/{env_for_path}/config-{service.name}.yaml"
                deployment_file_path = f"kustomize/{build_path}/{env_for_path}/{service.name}/deployment.yaml"
            else:
                config_file_path = f"configs/{env_for_path}/config-{service.name}.yaml"
                deployment_file_path = f"kustomize/{env_for_path}/{service.name}/deployment.yaml"
            workflow_file_path = f".github/workflows/deploy-{service.name}-{env_for_path}.yml"

            # Check for existing content (skip if no changes)
            config_changed = True
            workflow_changed = True
            deployment_changed = True

            try:
                existing_config = await GitHubIntegration.get_file_content(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=config_file_path,
                    branch=branch
                )
                if existing_config and existing_config.get("exists"):
                    if should_skip_commit(existing_config.get("content", ""), config_yaml_content):
                        config_changed = False
                        logger.info(f"No changes in config.yaml for {service.name} ({environment})")
            except Exception as e:
                logger.warning(f"Could not fetch existing config.yaml: {e}")

            try:
                existing_workflow = await GitHubIntegration.get_file_content(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=workflow_file_path,
                    branch=branch
                )
                if existing_workflow and existing_workflow.get("exists"):
                    if should_skip_commit(existing_workflow.get("content", ""), workflow_yaml_content):
                        workflow_changed = False
                        logger.info(f"No changes in workflow for {service.name} ({environment})")
            except Exception as e:
                logger.warning(f"Could not fetch existing workflow: {e}")

            try:
                existing_deployment = await GitHubIntegration.get_file_content(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=deployment_file_path,
                    branch=branch
                )
                if existing_deployment and existing_deployment.get("exists"):
                    if should_skip_commit(existing_deployment.get("content", ""), deployment_yaml_content):
                        deployment_changed = False
                        logger.info(f"No changes in deployment.yaml for {service.name} ({environment})")
            except Exception as e:
                logger.warning(f"Could not fetch existing deployment.yaml: {e}")

            # Skip if no changes
            if not config_changed and not workflow_changed and not deployment_changed:
                logger.info(f"No changes detected for EKS pipeline {service.name} ({environment})")
                return {
                    "branch": branch,
                    "status": "success",
                    "action": "no_changes",
                    "message": "EKS pipeline files are up to date"
                }

            # Create feature branch
            service_name_sanitized = re.sub(r'[\s_-]+', '-', service.name.strip()).strip('-').lower()
            base_branch_name = f"eks/pipeline-{service_name_sanitized}-{env_for_path}"
            feature_branch = await self._generate_unique_branch_name(
                github_token=github_token,
                owner=owner,
                repo=repo,
                base_name=base_branch_name
            )

            # Create branch from base
            await GitHubIntegration.create_branch(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch_name=feature_branch,
                from_branch=branch
            )
            logger.info(f"Created feature branch: {feature_branch}")

            # Commit files
            files_committed = []
            commit_sha = None

            if config_changed:
                config_result = await GitHubIntegration.update_or_create_file(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    branch=feature_branch,
                    file_path=config_file_path,
                    content=config_yaml_content,
                    message=f"[EKS] Add/update config.yaml for {service.name} ({environment})"
                )
                files_committed.append(config_file_path)
                commit_sha = config_result.get("commit_sha")
                logger.info(f"Committed config.yaml: {config_file_path}")

            if workflow_changed:
                workflow_result = await GitHubIntegration.update_or_create_file(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    branch=feature_branch,
                    file_path=workflow_file_path,
                    content=workflow_yaml_content,
                    message=f"[EKS] Add/update deployment workflow for {service.name} ({environment})"
                )
                files_committed.append(workflow_file_path)
                commit_sha = workflow_result.get("commit_sha")
                logger.info(f"Committed workflow: {workflow_file_path}")

            if deployment_changed:
                deployment_result = await GitHubIntegration.update_or_create_file(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    branch=feature_branch,
                    file_path=deployment_file_path,
                    content=deployment_yaml_content,
                    message=f"[EKS] Add/update Kubernetes deployment.yaml for {service.name} ({environment})"
                )
                files_committed.append(deployment_file_path)
                commit_sha = deployment_result.get("commit_sha")
                logger.info(f"Committed deployment.yaml: {deployment_file_path}")

            # Check for existing PR or create new one
            existing_pr = await GitHubIntegration.find_open_pr(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                head=feature_branch,
                base=branch
            )

            if existing_pr:
                # Update existing PR - also handle pipeline tracking
                logger.info(f"Updated branch {feature_branch}, existing PR #{existing_pr.get('number')}")

                # Create/update pipeline_mst and pipeline_run_track for updates too
                pipeline_repo = PipelineMstRepository(self.session)
                pipeline_code = None
                pipeline = None
                initial_run_code = None
                gitops_workflow_id = None

                try:
                    # Auto-generate pipeline name and code (keep under 100 chars)
                    # Generate pipeline_code FIRST so we can use it for gitops_workflow_detail
                    pipeline_name = f"EKS Pipeline for {service.name} - {environment} - {branch}"
                    # Use shorter format: eks_{service_short}_{env}_{branch_short}_{hash}
                    service_short = service.code[:20] if len(service.code) > 20 else service.code
                    branch_short = sanitize_name(branch)[:15] if len(sanitize_name(branch)) > 15 else sanitize_name(branch)
                    unique_hash = hashlib.md5(f"{service.code}_{environment}_{branch}".encode()).hexdigest()[:8]
                    pipeline_code = f"eks_{service_short}_{environment}_{branch_short}_{unique_hash}"

                    # Create GitOps workflow tracking for updated PR
                    # Use PIPELINE table_name and pipeline_code as transaction_code (matching ECS flow)
                    if existing_pr.get("number"):
                        try:
                            workflow_data = make_gitops_workflow_detail(
                                git_repository=repo_url,
                                git_branch=feature_branch,
                                git_commit_sha=commit_sha or "",
                                pr_number=existing_pr.get("number"),
                                pr_url=existing_pr.get("html_url", ""),
                                tenant_mst_code=tenant_code,
                                user_mst_code=user_code,
                                workflow_name=f"EKS Pipeline Update: {service.name} ({environment})",
                                transaction_code=pipeline_code,
                                table_name=WorkflowSourceTableEnum.PIPELINE
                            )
                            workflow = await self.gitops_workflow_repository.create(**workflow_data)
                            gitops_workflow_id = workflow.id
                            await self.session.commit()
                            logger.info(f"Created GitOps workflow tracking for updated EKS pipeline, ID: {gitops_workflow_id}")
                        except Exception as e:
                            logger.error(f"Failed to create GitOps workflow tracking for update: {e}")
                            await self.session.rollback()

                    deployment_config = {
                        "github_commit_sha": commit_sha,
                        "geo_loc_mst_code": service_config.geo_loc_mst_code,
                        "config_file_path": config_file_path,
                        "workflow_file_path": workflow_file_path,
                        "deployment_file_path": deployment_file_path,
                        "infrastructure_type": "eks",
                        "steps": workflow_steps,  # Store workflow steps for customization
                        "language": normalized_language  # Store normalized language (e.g., "java", "go")
                    }

                    # Lookup pipeline vendor using service hierarchy (same as ECS flow)
                    pipeline_vendor = await self.pipeline_vendor_repo.get_by_service_hierarchy(
                        service_code=service.code,
                        resource_group_code=service.resource_group_mst_code,
                        application_code=service.applications_mst_code,
                        tenant_code=service.tenants_mst_code,
                        environment=environment
                    )
                    if not pipeline_vendor:
                        logger.error(f"No pipeline vendor found for service {service.code} in {environment}")
                        raise ValueError(f"No pipeline vendor configuration found for service '{service.code}' in environment '{environment}'")
                    pipeline_vendor_code = pipeline_vendor.code

                    existing_pipeline = await pipeline_repo.get_by_code(pipeline_code)
                    if existing_pipeline:
                        await pipeline_repo.update(
                            existing_pipeline,
                            {
                                "deployment_config": deployment_config,
                                "gitops_workflow_id": gitops_workflow_id,
                                "infrastructure_mst_code": service_config.infrastructure_mst_code
                            }
                        )
                        pipeline = existing_pipeline
                        logger.info(f"Updated existing pipeline for updated PR: {pipeline_code}")
                    else:
                        pipeline = await pipeline_repo.create(
                            code=pipeline_code,
                            name=pipeline_name,
                            transaction_code=service_config.code,
                            table_name="SERVICE_CONFIG",
                            tenant_code=service.tenants_mst_code,
                            pipeline_vendor_mst_code=pipeline_vendor_code,
                            repo_url=repo_url,
                            repo_branch=branch,
                            language_ref_code=service_config.language_ref_code,
                            authentication_config={},
                            deployment_config=deployment_config,
                            gitops_workflow_id=gitops_workflow_id,
                        )
                        logger.info(f"Created new pipeline for updated PR: {pipeline_code}")

                    await self.session.commit()

                    # Create pipeline_run_track for the update
                    if commit_sha and pipeline:
                        run_track_repo = PipelineRunTrackRepository(self.session)
                        # Use shorter run code format
                        short_ts = str(int(time.time()))[-6:]
                        initial_run_code = f"{pipeline_code}_run_{short_ts}"

                        await run_track_repo.create(
                            pipeline_mst_code=pipeline.code,
                            code=initial_run_code,
                            status=PipelineRunStatusEnum.PENDING,
                            commit_sha=commit_sha,
                            log_url=None,
                            github_run_id=None,
                            error_message=None
                        )
                        await self.session.commit()
                        logger.info(f"Pipeline run track created for update: {initial_run_code}")

                        # DISABLED: Pipeline polling causes connection pool exhaustion (holds connections for 60+ min)
                        # TODO: Fix connection leak before re-enabling
                        # owner_parsed, repo_parsed = parse_repo_url(repo_url)
                        # polling_token = self._get_github_token()
                        # start_pipeline_polling(
                        #     run_code=initial_run_code,
                        #     pipeline_code=pipeline.code,
                        #     commit_sha=commit_sha,
                        #     owner=owner_parsed,
                        #     repo=repo_parsed,
                        #     db_session=self.session,
                        #     github_token=polling_token
                        # )
                        # logger.info(f"Started background polling for updated PR: {initial_run_code}")

                except Exception as e:
                    logger.error(f"Failed to handle pipeline tracking for updated PR: {e}")
                    await self.session.rollback()

                return {
                    "branch": branch,
                    "status": "success",
                    "action": "updated",
                    "feature_branch": feature_branch,
                    "files_committed": files_committed,
                    "commit_sha": commit_sha,
                    "pr_number": existing_pr.get("number"),
                    "pr_url": existing_pr.get("html_url"),
                    "pipeline_code": pipeline_code,
                    "run_code": initial_run_code,
                    "gitops_workflow_id": gitops_workflow_id,
                    "message": f"Updated EKS pipeline files - PR #{existing_pr.get('number')}"
                }

            # Create new PR
            env_display = EKS_ENV_DEFAULTS.get(environment, {}).get("environment_display", environment.capitalize())
            pr_title = f"[EKS Pipeline] {service.name} - {env_display}"
            pr_body = self._generate_pr_body(
                service_name=service.name,
                environment=environment,
                language_name=language_name,
                config_file_path=config_file_path,
                workflow_file_path=workflow_file_path,
                deployment_file_path=deployment_file_path,
                config=config
            )

            pr_result = await GitHubIntegration.create_pull_request(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                head=feature_branch,
                base=branch,
                title=pr_title,
                body=pr_body,
                draft=False
            )

            logger.info(f"Created PR #{pr_result.get('number')}: {pr_result.get('html_url')}")

            # Auto-merge the PR
            try:
                pr_number = pr_result.get("number")
                if pr_number:
                    await GitHubIntegration.merge_pull_request(
                        token=github_token,
                        base_url=settings.github_base_url,
                        owner=owner,
                        repo=repo,
                        pull_number=pr_number,
                        merge_method="squash",
                        commit_title=pr_title,
                    )
                    logger.info(f"Auto-merged PR #{pr_number}")
            except Exception as e:
                logger.warning(f"Auto-merge failed for PR #{pr_result.get('number')} (non-blocking): {e}")

            # Step A: Create pipeline_mst record
            pipeline_repo = PipelineMstRepository(self.session)
            pipeline_code = None
            pipeline = None
            gitops_workflow_id = None

            try:
                # Auto-generate pipeline name and code (keep under 100 chars)
                # Generate pipeline_code FIRST so we can use it for gitops_workflow_detail
                pipeline_name = f"EKS Pipeline for {service.name} - {environment} - {branch}"
                # Use shorter format: eks_{service_short}_{env}_{branch_short}_{hash}
                service_short = service.code[:20] if len(service.code) > 20 else service.code
                branch_short = sanitize_name(branch)[:15] if len(sanitize_name(branch)) > 15 else sanitize_name(branch)
                unique_hash = hashlib.md5(f"{service.code}_{environment}_{branch}".encode()).hexdigest()[:8]
                pipeline_code = f"eks_{service_short}_{environment}_{branch_short}_{unique_hash}"

                # Create GitOps workflow tracking
                # Use PIPELINE table_name and pipeline_code as transaction_code (matching ECS flow)
                if pr_result.get("number"):
                    try:
                        workflow_data = make_gitops_workflow_detail(
                            git_repository=repo_url,
                            git_branch=feature_branch,
                            git_commit_sha=commit_sha or "",
                            pr_number=pr_result.get("number"),
                            pr_url=pr_result.get("html_url", ""),
                            tenant_mst_code=tenant_code,
                            user_mst_code=user_code,
                            workflow_name=f"EKS Pipeline: {service.name} ({environment})",
                            transaction_code=pipeline_code,
                            table_name=WorkflowSourceTableEnum.PIPELINE
                        )
                        workflow = await self.gitops_workflow_repository.create(**workflow_data)
                        gitops_workflow_id = workflow.id
                        await self.session.commit()
                        logger.info(f"Created GitOps workflow tracking for EKS pipeline, ID: {gitops_workflow_id}")
                    except Exception as e:
                        logger.error(f"Failed to create GitOps workflow tracking: {e}")

                deployment_config = {
                    "github_commit_sha": commit_sha,
                    "geo_loc_mst_code": service_config.geo_loc_mst_code,
                    "config_file_path": config_file_path,
                    "workflow_file_path": workflow_file_path,
                    "deployment_file_path": deployment_file_path,
                    "infrastructure_type": "eks",
                    "steps": workflow_steps,  # Store workflow steps for customization
                    "language": normalized_language  # Store normalized language (e.g., "java", "go")
                }

                # Lookup pipeline vendor using service hierarchy (same as ECS flow)
                pipeline_vendor = await self.pipeline_vendor_repo.get_by_service_hierarchy(
                    service_code=service.code,
                    resource_group_code=service.resource_group_mst_code,
                    application_code=service.applications_mst_code,
                    tenant_code=service.tenants_mst_code,
                    environment=environment
                )
                if not pipeline_vendor:
                    logger.error(f"No pipeline vendor found for service {service.code} in {environment}")
                    raise ValueError(f"No pipeline vendor configuration found for service '{service.code}' in environment '{environment}'")
                pipeline_vendor_code = pipeline_vendor.code

                # Check for existing pipeline
                existing_pipeline = await pipeline_repo.get_by_code(pipeline_code)
                if existing_pipeline:
                    # Update existing pipeline
                    await pipeline_repo.update(
                        existing_pipeline,
                        {
                            "deployment_config": deployment_config,
                            "gitops_workflow_id": gitops_workflow_id,
                            "infrastructure_mst_code": service_config.infrastructure_mst_code
                        }
                    )
                    pipeline = existing_pipeline
                    logger.info(f"Updated existing pipeline: {pipeline_code}")
                else:
                    # Create new pipeline
                    pipeline = await pipeline_repo.create(
                        code=pipeline_code,
                        name=pipeline_name,
                        transaction_code=service_config.code,
                        table_name="SERVICE_CONFIG",
                        tenant_code=service.tenants_mst_code,
                        pipeline_vendor_mst_code=pipeline_vendor_code,
                        repo_url=repo_url,
                        repo_branch=branch,
                        language_ref_code=service_config.language_ref_code,
                        authentication_config={},
                        deployment_config=deployment_config,
                        gitops_workflow_id=gitops_workflow_id,
                        infrastructure_mst_code=service_config.infrastructure_mst_code
                    )
                    logger.info(f"Created new pipeline: {pipeline_code}")

                await self.session.commit()
            except Exception as e:
                logger.error(f"Failed to create/update pipeline_mst: {e}")
                await self.session.rollback()

            # Step B: Create pipeline_run_track record
            initial_run_code = None
            if commit_sha and pipeline:
                try:
                    run_track_repo = PipelineRunTrackRepository(self.session)

                    # Use shorter run code format: {pipeline_code}_run_{short_timestamp}
                    short_ts = str(int(time.time()))[-6:]  # Last 6 digits of timestamp
                    initial_run_code = f"{pipeline_code}_run_{short_ts}"

                    await run_track_repo.create(
                        pipeline_mst_code=pipeline.code,
                        code=initial_run_code,
                        status=PipelineRunStatusEnum.PENDING,
                        commit_sha=commit_sha,
                        log_url=None,
                        github_run_id=None,
                        error_message=None
                    )
                    await self.session.commit()
                    logger.info(f"Pipeline run track created: {initial_run_code}")

                    # DISABLED: Pipeline polling causes connection pool exhaustion (holds connections for 60+ min)
                    # TODO: Fix connection leak before re-enabling
                    # owner_parsed, repo_parsed = parse_repo_url(repo_url)
                    # polling_token = self._get_github_token()
                    # start_pipeline_polling(
                    #     run_code=initial_run_code,
                    #     pipeline_code=pipeline.code,
                    #     commit_sha=commit_sha,
                    #     owner=owner_parsed,
                    #     repo=repo_parsed,
                    #     db_session=self.session,
                    #     github_token=polling_token
                    # )
                    # logger.info(f"Started background polling for EKS pipeline: {initial_run_code}")
                except Exception as e:
                    logger.error(f"Failed to create pipeline_run_track or start polling: {e}")
                    await self.session.rollback()

            # Note: For EKS, pipeline PR is linked via pipeline_mst.gitops_workflow_id
            # service_config.gitops_workflow_id is only used for Terragrunt/Dockerfile PRs (ECS flow)

            return {
                "branch": branch,
                "status": "success",
                "action": "created",
                "feature_branch": feature_branch,
                "files_committed": files_committed,
                "commit_sha": commit_sha,
                "pr_number": pr_result.get("number"),
                "pr_url": pr_result.get("html_url"),
                "pipeline_code": pipeline_code,
                "run_code": initial_run_code,
                "gitops_workflow_id": gitops_workflow_id,
                "message": f"Created EKS pipeline PR #{pr_result.get('number')}"
            }

        except Exception as e:
            logger.error(f"Error syncing EKS pipeline: {str(e)}", exc_info=True)
            return {
                "branch": branch,
                "status": "error",
                "error": str(e)
            }

    def _parse_language_name(self, language_name: str) -> tuple[str, str, str]:
        """
        Parse language name from DB to extract base language, normalized name, and version.

        Examples:
            "go 1.25" -> ("go", "golang", "1.25")
            "Go 1.24.0" -> ("go", "golang", "1.24.0")
            "golang" -> ("go", "golang", "1.24.0")
            "Java 17 LTS" -> ("java", "java", "17")
            "Java Maven 17 LTS" -> ("java-maven", "java", "17")
            "Java Maven 23" -> ("java-maven", "java", "23")
            "Java 23" -> ("java", "java", "23")
            "Node.js 20 LTS" -> ("nodejs", "nodejs", "20")

        Returns:
            Tuple of (base_language, normalized_language, version)
        """
        lang_lower = language_name.lower().strip()

        # Go language parsing
        if lang_lower.startswith("go") or lang_lower.startswith("golang"):
            # Extract version from patterns like "go 1.25", "golang 1.24.0", "go1.25"
            import re
            version_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', lang_lower)
            version = version_match.group(1) if version_match else "1.24.0"
            return ("go", "golang", version)

        # Java language parsing
        if "java" in lang_lower:
            import re
            # Check for maven or gradle variant
            if "maven" in lang_lower:
                base_lang = "java-maven"
            elif "gradle" in lang_lower:
                base_lang = "java-gradle"
            else:
                base_lang = "java"

            # Extract version (e.g., "17", "23", "11")
            version_match = re.search(r'(\d+)', lang_lower)
            version = version_match.group(1) if version_match else "17"
            return (base_lang, "java", version)

        # Node.js parsing
        if "node" in lang_lower:
            import re
            version_match = re.search(r'(\d+)', lang_lower)
            version = version_match.group(1) if version_match else "20"
            return ("nodejs", "nodejs", version)

        # Python parsing
        if "python" in lang_lower:
            import re
            version_match = re.search(r'(\d+\.\d+)', lang_lower)
            version = version_match.group(1) if version_match else "3.11"
            return ("python", "python", version)

        # Default: return as-is with no version
        return (lang_lower, lang_lower, "")

    def _get_default_build_type(self, base_language: str) -> str:
        """
        Get default build type based on language.

        Args:
            base_language: Base language identifier (go, java, java-maven, java-gradle, nodejs, python)

        Returns:
            Default build type: "docker-only" for Go/Node/Python, "maven" for Java Maven, "gradle" for Java Gradle
        """
        if base_language in ["go", "golang", "nodejs", "python"]:
            return "docker-only"
        elif base_language == "java-maven":
            return "maven"
        elif base_language in ["java", "java-gradle"]:
            return "gradle"
        else:
            return "docker-only"

    @staticmethod
    def _get_aws_region_from_geo_loc(geo_loc: str) -> str:
        """
        Map business/deployment region (geo_loc) to AWS region.

        Args:
            geo_loc: Geographic location code (e.g., 'mumbai', 'london')

        Returns:
            AWS region code (e.g., 'ap-south-1', 'eu-west-2')
        """
        mapping = {
            "mumbai": "ap-south-1",
            "london": "eu-west-2",
            "uk": "eu-west-2",
            "us": "us-east-1",
            "aspora-mumbai": "ap-south-1",
            "aspora-london": "eu-west-2",
            "aspora-uk": "eu-west-2",
            "aspora-us": "us-east-1",
            "region-aspora-mumbai": "ap-south-1",
            "region-aspora-london": "eu-west-2",
            "region-aspora-us": "us-east-1",
        }
        return mapping.get(geo_loc.lower(), "ap-south-1")

    def _get_default_eks_steps(self) -> list:
        """
        Get default EKS workflow steps from backend's canonical step definitions.

        Uses the same step structure as save_workflow_to_pipeline for consistency.
        Returns steps with full structure including: id, name, order, enabled, dependsOn,
        category, mandatory, description.

        Returns:
            List of default steps with full structure (order, category, mandatory, description)
        """
        from app.types.workflowSteps import LANGUAGE_WORKFLOWS

        # Get language-specific steps (default to Go if Java not available)
        language_steps = LANGUAGE_WORKFLOWS.get("go", {}).get("steps", [])
        java_steps = LANGUAGE_WORKFLOWS.get("java", {}).get("steps", [])

        # Combine both Go and Java steps (remove duplicates)
        all_steps = {step["id"]: step for step in language_steps + java_steps}

        # Default EKS workflow steps (matching our previous defaults)
        default_step_ids = [
            "code-checkout",
            "code-quality-check",
            "build",
            "docker-build",
            "deploy"
        ]

        # Return only the default steps with their full structure
        default_steps = [all_steps[step_id] for step_id in default_step_ids if step_id in all_steps]

        return default_steps

    def _generate_config_yaml(
        self,
        service_config: ServiceConfigModel,
        service: ServicesMstModel,
        environment: str,
        language_name: str,
        language_version: str = None
    ) -> str:
        """
        Generate config.yaml content from service_config.

        Args:
            service_config: The service configuration model
            service: The service model
            environment: Environment (dev/staging/prod)
            language_name: Language name from DB (e.g., "go 1.25", "Java Maven 17 LTS")

        Returns:
            Generated config.yaml content
        """
        config = service_config.config or {}
        eks_build_config = config.get("eks_build_config") or {}

        # Parse language name to extract base language, normalized name, and version
        base_language, normalized_language, parsed_version = self._parse_language_name(language_name)

        # Determine build type: use config value if provided, otherwise default based on language
        build_type = eks_build_config.get("type") or self._get_default_build_type(base_language)

        # Read template
        try:
            with open(EKS_CONFIG_TEMPLATE_PATH, 'r') as f:
                template = f.read()
        except FileNotFoundError:
            logger.error(f"EKS config template not found: {EKS_CONFIG_TEMPLATE_PATH}")
            # Fallback to inline template
            template = self._get_config_template_fallback()

        # Version line based on language (java_version for Java, go_version for Go)
        # Priority: language_ref.version (from DB) > JSONB config > parsed from name > default
        version_line = ""
        if normalized_language == "java":
            java_version = language_version or config.get("java_version") or parsed_version or "17"
            version_line = f'java_version: "{java_version}"'
        elif normalized_language == "golang":
            go_version = language_version or config.get("go_version") or parsed_version or "1.24"
            # Always append .0 to Go version (e.g., "21" -> "21.0", "21.5" -> "21.5.0")
            go_version = f"{go_version}.0"
            version_line = f'go_version: "{go_version}"'
        elif normalized_language == "nodejs":
            nodejs_version = language_version or config.get("nodejs_version") or parsed_version or "20"
            version_line = f'nodejs_version: "{nodejs_version}"'
        elif normalized_language == "python":
            python_version = language_version or config.get("python_version") or parsed_version or "3.11"
            version_line = f'python_version: "{python_version}"'

        # Dockerfile path
        dockerfile_path = config.get("dockerfile_path") or "Dockerfile"

        # Build config - default skip_tests and skip_checks
        # For Go in prod: skip_tests=false, skip_checks=false
        is_go = normalized_language == "golang"
        is_prod = environment.lower() == "prod"

        if is_go and is_prod:
            # Go in prod: don't skip tests/checks
            skip_tests = str(eks_build_config.get("skip_tests", False)).lower()
            skip_checks = str(eks_build_config.get("skip_checks", False)).lower()
        else:
            # Default: skip tests/checks (can be overridden by config)
            skip_tests = str(eks_build_config.get("skip_tests", True)).lower()
            skip_checks = str(eks_build_config.get("skip_checks", True)).lower()

        # Generate gradle or maven config block (only for applicable build types)
        # Pass both eks_build_config and root config so values can be found in either location
        gradle_config = ""
        maven_config = ""

        if build_type == "gradle":
            gradle_config = self._generate_gradle_config(eks_build_config, config, environment)
        elif build_type == "maven":
            maven_config = self._generate_maven_config(eks_build_config, config, environment)
        # For "docker-only", no additional config blocks needed

        # Generate resource allocation config
        resource_allocation_config = self._generate_resource_allocation_config(config)

        # Generate service configuration config
        service_configuration_config = self._generate_service_configuration_config(config)

        # Generate deployment strategy config (from separate column, not from config JSONB)
        deployment_strategy_config = self._generate_deployment_strategy_config(service_config.deployment_strategy)

        # Replace placeholders
        content = template
        content = content.replace("{{SERVICE_NAME}}", service.name)
        content = content.replace("{{LANGUAGE}}", normalized_language)
        content = content.replace("{{VERSION_LINE}}", version_line)
        content = content.replace("{{DOCKERFILE_PATH}}", dockerfile_path)
        content = content.replace("{{BUILD_TYPE}}", build_type)
        content = content.replace("{{SKIP_TESTS}}", skip_tests)
        content = content.replace("{{SKIP_CHECKS}}", skip_checks)
        content = content.replace("{{GRADLE_CONFIG}}", gradle_config)
        content = content.replace("{{MAVEN_CONFIG}}", maven_config)
        content = content.replace("{{RESOURCE_ALLOCATION}}", resource_allocation_config)
        content = content.replace("{{SERVICE_CONFIGURATION}}", service_configuration_config)
        content = content.replace("{{DEPLOYMENT_STRATEGY}}", deployment_strategy_config)

        return content

    def _generate_gradle_config(self, eks_build_config: dict, config: dict = None, environment: str = None) -> str:
        """Generate Gradle build configuration block.

        Checks values in this priority order:
        1. eks_build_config (prefixed or non-prefixed)
        2. root config (prefixed or non-prefixed)
        3. default value
        """
        config = config or {}

        # Build path prefix for jar_file
        build_path = config.get("build_path") or ""
        default_jar = f"{build_path}/build/libs/*.jar" if build_path else "build/libs/*.jar"

        jar_file = (eks_build_config.get("gradle_jar_file") or eks_build_config.get("jar_file") or
                   config.get("gradle_jar_file") or config.get("jar_file") or default_jar)
        tasks = (eks_build_config.get("gradle_tasks") or eks_build_config.get("tasks") or
                config.get("gradle_tasks") or config.get("tasks") or "assemble")
        jvm_args = (eks_build_config.get("gradle_jvm_args") or eks_build_config.get("jvm_args") or
                   config.get("gradle_jvm_args") or config.get("jvm_args") or "-Xmx4g -XX:+UseG1GC")
        workers_max = (eks_build_config.get("gradle_workers_max") or eks_build_config.get("workers_max") or
                      config.get("gradle_workers_max") or config.get("workers_max") or 4)

        # xms/xmx - only include if values are present
        xms_raw = (eks_build_config.get("gradle_xms") or eks_build_config.get("xms") or
                  config.get("gradle_xms") or config.get("xms"))
        xmx_raw = (eks_build_config.get("gradle_xmx") or eks_build_config.get("xmx") or
                  config.get("gradle_xmx") or config.get("xmx"))

        # Profile based on environment: staging/stage/qa -> stg, dev -> dev, prod -> prod
        if environment in ["staging", "stage", "qa"]:
            profile = "stg"
        elif environment == "dev":
            profile = "dev"
        else:
            profile = "prod"

        config_lines = [
            "  gradle:",
            f"    jar_file: {jar_file}",
            f"    tasks: {tasks}",
            f'    jvm_args: "{jvm_args}"',
            f"    workers_max: {workers_max}",
            f"    profile: {profile}"
        ]

        # Only include xms/xmx if values are present
        if xms_raw:
            xms = xms_raw if xms_raw.endswith('m') else f"{xms_raw}m"
            config_lines.append(f"    xms: {xms}")
        if xmx_raw:
            xmx = xmx_raw if xmx_raw.endswith('m') else f"{xmx_raw}m"
            config_lines.append(f"    xmx: {xmx}")

        return "\n".join(config_lines) + "\n"

    def _generate_maven_config(self, eks_build_config: dict, config: dict = None, environment: str = None) -> str:
        """Generate Maven build configuration block.

        Checks values in this priority order:
        1. eks_build_config (prefixed or non-prefixed)
        2. root config (prefixed or non-prefixed)
        3. default value
        """
        config = config or {}

        # Build path prefix for jar_file
        build_path = config.get("build_path") or ""
        default_jar = f"{build_path}/target/*.jar" if build_path else "target/*.jar"

        jar_file = (eks_build_config.get("maven_jar_file") or eks_build_config.get("jar_file") or
                   config.get("maven_jar_file") or config.get("jar_file") or default_jar)
        goals = (eks_build_config.get("maven_goals") or eks_build_config.get("goals") or
                config.get("maven_goals") or config.get("goals") or "clean package")

        # Hardcoded maven repo values
        repo_id = "github"
        repo_name = "java-commons"
        repo_url = "https://maven.pkg.github.com/Vance-Club/java-commons"

        # Profile based on environment: staging/stage/qa -> stg, dev -> dev, prod -> prod
        if environment in ["staging", "stage", "qa"]:
            profile = "stg"
        elif environment == "dev":
            profile = "dev"
        else:
            profile = "prod"

        # xms/xmx - only include if values are present
        xms_raw = (eks_build_config.get("maven_xms") or eks_build_config.get("xms") or
                  config.get("maven_xms") or config.get("xms"))
        xmx_raw = (eks_build_config.get("maven_xmx") or eks_build_config.get("xmx") or
                  config.get("maven_xmx") or config.get("xmx"))

        config_lines = [
            "  maven:",
            f"    jar_file: {jar_file}",
            f'    goals: "{goals}"',
            f"    repo_id: {repo_id}",
            f"    repo_name: {repo_name}",
            f"    repo_url: {repo_url}",
            f"    profile: {profile}"
        ]

        # Only include xms/xmx if values are present
        if xms_raw:
            xms = xms_raw if xms_raw.endswith('m') else f"{xms_raw}m"
            config_lines.append(f"    xms: {xms}")
        if xmx_raw:
            xmx = xmx_raw if xmx_raw.endswith('m') else f"{xmx_raw}m"
            config_lines.append(f"    xmx: {xmx}")

        return "\n".join(config_lines) + "\n"

    def _generate_resource_allocation_config(self, config: dict) -> str:
        """Generate resource allocation configuration block.

        Args:
            config: The service config dictionary

        Returns:
            YAML string for resource allocation configuration
        """
        config_lines = ["resources:"]

        # CPU configuration — normalize to millicores (append "m" only if not already present)
        cpu_requested = config.get("cpu_requested") or config.get("cpu")
        cpu_limit = config.get("cpu_limit") or config.get("cpu")
        if cpu_requested:
            cpu_req = cpu_requested if str(cpu_requested).endswith("m") else f"{cpu_requested}m"
            config_lines.append(f"  cpu_requested: \"{cpu_req}\"")
        if cpu_limit:
            cpu_lim = cpu_limit if str(cpu_limit).endswith("m") else f"{cpu_limit}m"
            config_lines.append(f"  cpu_limit: \"{cpu_lim}\"")

        # Memory configuration — normalize to Mi/Gi (append "Mi" only if no suffix present)
        memory_requested = config.get("memory_requested") or config.get("ram")
        memory_limit = config.get("memory_limit") or config.get("ram")
        if memory_requested:
            mem_req = memory_requested if str(memory_requested).endswith(('Mi', 'Gi', 'MB', 'GB')) else f"{memory_requested}Mi"
            config_lines.append(f"  memory_requested: \"{mem_req}\"")
        if memory_limit:
            mem_lim = memory_limit if str(memory_limit).endswith(('Mi', 'Gi', 'MB', 'GB')) else f"{memory_limit}Mi"
            config_lines.append(f"  memory_limit: \"{mem_lim}\"")

        # Container port
        container_port = config.get("container_port") or config.get("port")
        if container_port:
            config_lines.append(f"  container_port: {container_port}")

        # Replica count
        replica_count = config.get("replica_count")
        if replica_count:
            config_lines.append(f"  replica_count: {replica_count}")

        # HPA (Horizontal Pod Autoscaler) configuration
        hpa_config = config.get("hpa")
        if hpa_config and hpa_config.get("enabled"):
            config_lines.append("  hpa:")
            config_lines.append("    enabled: true")
            if hpa_config.get("min_replicas"):
                config_lines.append(f"    min_replicas: {hpa_config.get('min_replicas')}")
            if hpa_config.get("max_replicas"):
                config_lines.append(f"    max_replicas: {hpa_config.get('max_replicas')}")
            if hpa_config.get("cpu_threshold"):
                config_lines.append(f"    cpu_threshold: {hpa_config.get('cpu_threshold')}")
            if hpa_config.get("memory_threshold"):
                config_lines.append(f"    memory_threshold: {hpa_config.get('memory_threshold')}")
        else:
            config_lines.append("  hpa:")
            config_lines.append("    enabled: false")

        # EBS Storage configuration
        ebs_enabled = config.get("ebs_enabled") or config.get("ebs_storage_enabled")
        if ebs_enabled:
            config_lines.append("  ebs:")
            config_lines.append("    enabled: true")
            ebs_size = config.get("ebs_size") or config.get("ebs_storage_size")
            ebs_mount_path = config.get("ebs_mount_path") or config.get("ebs_storage_mount_path")
            if ebs_size:
                config_lines.append(f"    size: \"{ebs_size}\"")
            if ebs_mount_path:
                config_lines.append(f"    mount_path: \"{ebs_mount_path}\"")
        else:
            config_lines.append("  ebs:")
            config_lines.append("    enabled: false")

        return "\n".join(config_lines) + "\n"

    def _generate_service_configuration_config(self, config: dict) -> str:
        """Generate service configuration block.

        Args:
            config: The service config dictionary

        Returns:
            YAML string for service configuration
        """
        config_lines = ["service:"]

        # ALB Schema (internal or internet-facing)
        alb_schema = config.get("alb_schema")
        if alb_schema:
            config_lines.append(f"  alb_schema: \"{alb_schema}\"")

        # Health endpoint
        health_endpoint = config.get("health_endpoint") or config.get("health_check_path")
        if health_endpoint:
            config_lines.append(f"  health_endpoint: \"{health_endpoint}\"")

        # Service path
        service_path = config.get("service_path") or config.get("path_pattern")
        if service_path:
            config_lines.append(f"  service_path: \"{service_path}\"")

        # Secrets configuration
        secrets_enabled = config.get("secrets_enabled")
        if secrets_enabled:
            config_lines.append("  secrets:")
            config_lines.append("    enabled: true")
            secret_keys = config.get("secret_keys")
            if secret_keys:
                # Handle comma-separated keys or list
                if isinstance(secret_keys, str):
                    keys_list = [k.strip() for k in secret_keys.split(",") if k.strip()]
                else:
                    keys_list = secret_keys
                if keys_list:
                    config_lines.append("    keys:")
                    for key in keys_list:
                        config_lines.append(f"      - \"{key}\"")
        else:
            config_lines.append("  secrets:")
            config_lines.append("    enabled: false")

        return "\n".join(config_lines) + "\n"

    def _generate_deployment_strategy_config(self, deployment_strategy: dict = None) -> str:
        """Generate deployment strategy configuration block.

        Args:
            deployment_strategy: The deployment strategy dictionary (from separate column)

        Returns:
            YAML string for deployment strategy configuration
        """
        if not deployment_strategy:
            # Default to rolling update if no strategy specified
            return "deployment:\n  strategy: rolling\n  rolling:\n    maxSurge: \"25%\"\n    maxUnavailable: \"25%\"\n"

        strategy = deployment_strategy.get("strategy", "rolling")
        config_lines = ["deployment:", f"  strategy: {strategy}"]

        if strategy == "canary":
            canary_config = deployment_strategy.get("canary", {})
            config_lines.append("  canary:")
            config_lines.append(f"    canaryService: {canary_config.get('canaryService', 'service-canary')}")
            config_lines.append(f"    stableService: {canary_config.get('stableService', 'service-stable')}")
            config_lines.append(f"    analysisEnabled: {str(canary_config.get('analysisEnabled', False)).lower()}")

            # Add traffic shifting steps
            steps = canary_config.get("steps", [])
            if steps:
                config_lines.append("    steps:")
                for step in steps:
                    config_lines.append(f"      - weight: {step.get('weight', 0)}")
                    config_lines.append(f"        pauseDuration: {step.get('pauseDuration', 30)}")
                    config_lines.append(f"        pauseType: {step.get('pauseType', 'duration')}")

        elif strategy == "bluegreen":
            bluegreen_config = deployment_strategy.get("blueGreen", {})
            config_lines.append("  blueGreen:")
            config_lines.append(f"    activeService: {bluegreen_config.get('activeService', 'service-active')}")
            config_lines.append(f"    previewService: {bluegreen_config.get('previewService', 'service-preview')}")
            config_lines.append(f"    autoPromote: {str(bluegreen_config.get('autoPromote', False)).lower()}")
            config_lines.append(f"    scaleDownDelay: {bluegreen_config.get('scaleDownDelay', 30)}")
            config_lines.append(f"    previewReplicas: {bluegreen_config.get('previewReplicas', 1)}")

        elif strategy == "rolling":
            rolling_config = deployment_strategy.get("rolling", {})
            config_lines.append("  rolling:")
            config_lines.append(f"    maxSurge: \"{rolling_config.get('maxSurge', '25%')}\"")
            config_lines.append(f"    maxUnavailable: \"{rolling_config.get('maxUnavailable', '25%')}\"")

        elif strategy == "recreate":
            config_lines.append("  recreate:")
            config_lines.append("    # Recreate strategy: terminates all pods before creating new ones")
            config_lines.append("    terminationGracePeriodSeconds: 30")

        return "\n".join(config_lines) + "\n"

    async def _generate_workflow_yaml(
        self,
        service_config: ServiceConfigModel,
        service: ServicesMstModel,
        branch: str,
        environment: str,
        language_name: str,
        language_ref_code: str
    ) -> tuple:
        """
        Generate inline workflow YAML using workflow_generator.

        Args:
            service_config: The service configuration model
            service: The service model
            branch: Branch name
            environment: Environment (dev/staging/prod)
            language_name: Language name (java/go)
            language_ref_code: Language reference code (e.g., "JAVA-MAVEN_21", "GO_1_25")

        Returns:
            Tuple of (workflow_yaml_content, steps_array, normalized_language)
        """
        config = service_config.config or {}

        # Get language name
        _, normalized_language, _ = self._parse_language_name(language_name)

        # Normalize language for deployment_config using language_ref_code
        # Examples:
        #   "JAVA-MAVEN_21" -> "Java Maven"
        #   "JAVA_21" -> "Java Gradle" (Java without Maven uses Gradle)
        #   "GO_1_25" -> "Go"
        #   "NODEJS_20" -> "Nodejs"
        logger.info(f"[DEBUG] language_ref_code: '{language_ref_code}'")

        # Extract language name from language_ref_code
        lang_code_lower = language_ref_code.lower()

        # Check for Java Maven
        if "-maven" in lang_code_lower:
            normalized_language_for_config = "Java Maven"
        # Check for Java (without Maven suffix) - uses Gradle
        elif lang_code_lower.startswith("java_") or (lang_code_lower.startswith("java-") and "maven" not in lang_code_lower):
            normalized_language_for_config = "Java Gradle"
        # For Go, Node.js, Python, etc. - capitalize first letter
        elif "_" in lang_code_lower:
            base_lang = lang_code_lower.split("_")[0]
            normalized_language_for_config = base_lang.capitalize()
        elif "-" in lang_code_lower:
            base_lang = lang_code_lower.split("-")[0]
            normalized_language_for_config = base_lang.capitalize()
        else:
            normalized_language_for_config = lang_code_lower.capitalize()

        logger.info(f"[DEBUG] Normalized language for deployment_config: '{normalized_language_for_config}'")

        # Get customizable steps from service_config or use defaults
        # Frontend can pass pipeline_steps in service_config.config
        steps = config.get("pipeline_steps") or self._get_default_eks_steps()

        # Get build path and dockerfile path
        build_path = config.get("build_path") or None
        dockerfile_path = config.get("dockerfile_path") or None

        # Get additional trigger paths
        additional_paths = config.get("other_paths") or []

        # Get AWS region from geo_loc_mst_code
        aws_region = self._get_aws_region_from_geo_loc(service_config.geo_loc_mst_code)

        # Get EKS cluster name from infrastructure_mst
        infrastructure = await self.infrastructure_repo.get_by_code(service_config.infrastructure_mst_code)
        if infrastructure and infrastructure.locator:
            eks_cluster_name = infrastructure.locator.get("cluster", infrastructure.name)
        elif infrastructure:
            eks_cluster_name = infrastructure.name
        else:
            # Fallback to service name + environment
            eks_cluster_name = f"{service.name}-{environment}"

        # Build additional trigger paths
        additional_trigger_paths = []
        if build_path:
            additional_trigger_paths.append(build_path)
        if dockerfile_path:
            additional_trigger_paths.append(dockerfile_path)
        additional_trigger_paths.extend(additional_paths)

        # Generate inline workflow YAML
        workflow_yaml = generate_workflow_yaml(
            language=normalized_language,
            steps=steps,
            service_name=service.name,
            environment=environment,
            branches=[branch],
            build_path=build_path,
            dockerfile_path=dockerfile_path,
            additional_trigger_paths=additional_trigger_paths if additional_trigger_paths else None,
            infrastructure_type="eks",
            aws_region=aws_region,
            eks_cluster_name=eks_cluster_name
        )

        return workflow_yaml, steps, normalized_language_for_config

    def _generate_folder_path_filter(self, service: ServicesMstModel, config: dict, language_name: str = None) -> str:
        """Generate folder path filter for workflow trigger.

        Logic:
        - For Go: no paths filter at all (triggers on any push to branch)
        - If other_paths provided: add build_path (if exists), dockerfile_path (if exists), other_paths
        - If no other_paths: only add paths if BOTH build_path AND dockerfile_path exist
        - If any paths are added: include configs/**
        - If no paths: no paths filter (triggers on any push)
        """
        # Check if this is Go language - no paths filter for Go
        # Use _parse_language_name to handle all Go name variants
        if language_name:
            _, normalized_language, _ = self._parse_language_name(language_name)
            if normalized_language == "golang":
                return ""

        path_lines = []

        # Trigger patterns are matched against repo-relative paths with NO ./
        # normalization on GitHub's side: './cmd/**' never fires. So strip a
        # leading ./ (the shape build tools want and users type) along with
        # trailing slashes before any pattern is derived from these.
        def _clean(p):
            p = (p or "").strip().rstrip("/")
            while p.startswith("./"):
                p = p[2:]
            # "." (or "./") is the repo root — as a pattern that is "every
            # push", which is what NO paths filter already means. Drop it.
            return None if p in ("", ".") else p

        build_path = config.get("build_path")
        dockerfile_path = config.get("dockerfile_path")
        other_paths = config.get("other_paths") or []

        build_path_clean = _clean(build_path)
        dockerfile_path_clean = _clean(dockerfile_path)

        # Check if other_paths has valid entries
        has_other_paths = any(p and p.strip() for p in other_paths)

        # If other_paths exist: add build_path and dockerfile_path individually (if they exist)
        # If no other_paths: only add if BOTH build_path AND dockerfile_path exist
        if has_other_paths:
            # Add build_path if exists
            if build_path_clean:
                path_lines.append(f"      - '{build_path_clean}/**'")
            # Add dockerfile_path if exists
            if dockerfile_path_clean:
                path_lines.append(f"      - '{dockerfile_path_clean}'")
        else:
            # No other_paths: require both build_path and dockerfile_path
            if build_path_clean and dockerfile_path_clean:
                path_lines.append(f"      - '{build_path_clean}/**'")
                path_lines.append(f"      - '{dockerfile_path_clean}'")

        # Add other_paths if provided (always included). Folders get /**; a
        # file or a glob is written as it is — see trigger_path_pattern.
        for path in other_paths:
            pattern = trigger_path_pattern(path)
            if pattern:
                path_lines.append(f"      - '{pattern}'")

        # Add configs folder if any paths are present
        if path_lines:
            # Sanitize build_path for configs path (strip leading/trailing slashes)
            sanitized_build_path = (_clean(build_path) or "").strip("/") or None if build_path else None
            if sanitized_build_path:
                path_lines.append(f"      - 'configs/{sanitized_build_path}/**'")
            else:
                path_lines.append("      - 'configs/**'")

        if path_lines:
            return "    paths:\n" + "\n".join(path_lines)
        return ""

    def _generate_pr_body(
        self,
        service_name: str,
        environment: str,
        language_name: str,
        config_file_path: str,
        workflow_file_path: str,
        deployment_file_path: str,
        config: dict
    ) -> str:
        """Generate PR body for EKS pipeline."""
        eks_build_config = config.get("eks_build_config") or {}
        build_type = eks_build_config.get("type", "maven")
        skip_tests = eks_build_config.get("skip_tests", True)
        skip_code_quality = eks_build_config.get("skip_code_quality", True)

        # Get deployment strategy
        deployment_strategy = config.get("deployment_strategy", {})
        strategy_type = deployment_strategy.get("strategy", "rolling") if deployment_strategy else "rolling"

        return f"""## EKS Pipeline Configuration

**Service:** `{service_name}`
**Environment:** {environment}
**Language:** {language_name}

### Files Changed
- `{config_file_path}` - Build configuration
- `{workflow_file_path}` - Pipeline workflow
- `{deployment_file_path}` - Kubernetes manifests (Deployment, Service, Ingress, HPA)

### Build Configuration
- **Type:** {build_type}
- **Skip Tests:** {skip_tests}
- **Skip Code Quality:** {skip_code_quality}

### Deployment Strategy
- **Strategy:** {strategy_type}

---
*Generated by {settings.app_name}*"""

    async def _generate_unique_branch_name(
        self,
        github_token: str,
        owner: str,
        repo: str,
        base_name: str
    ) -> str:
        """Generate a unique branch name by checking existing branches."""
        branch_name = base_name
        counter = 1

        while True:
            try:
                # Check if branch exists
                exists = await GitHubIntegration.branch_exists(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    branch_name=branch_name
                )
                if not exists:
                    return branch_name

                # Branch exists, try with counter
                counter += 1
                branch_name = f"{base_name}-{counter}"

                if counter > 100:  # Safety limit
                    import time
                    return f"{base_name}-{int(time.time())}"

            except Exception as e:
                logger.warning(f"Error checking branch existence: {e}")
                return base_name

    def _get_config_template_fallback(self) -> str:
        """Fallback config.yaml template if file not found."""
        return """# Service name (identifier) - used to derive ECR repo, EKS deployment names, etc.
service_name: {{SERVICE_NAME}}

language: {{LANGUAGE}}
{{VERSION_LINE}}
# aws_region: [ ap-south-1, eu-west-2, us-east-1 ]

# Path to Dockerfile (relative to repo root)
dockerfile_path: {{DOCKERFILE_PATH}}

# Build configuration
build:
  type: {{BUILD_TYPE}}
  skip_tests: {{SKIP_TESTS}}
  skip_checks: {{SKIP_CHECKS}}
{{GRADLE_CONFIG}}{{MAVEN_CONFIG}}
# Resource Allocation
{{RESOURCE_ALLOCATION}}
# Service Configuration
{{SERVICE_CONFIGURATION}}
# Deployment Strategy Configuration
{{DEPLOYMENT_STRATEGY}}
"""

    def _get_workflow_template_fallback(self, language_name: str) -> str:
        """Fallback workflow template if file not found."""
        lang_display = "Java Application" if language_name.lower() != "go" else "Go Application"
        return f"""name: {{{{ENVIRONMENT_DISPLAY}}}} Pipeline - {lang_display}

on:
  push:
    branches:
      - {{{{BRANCH}}}}
{{{{FOLDER_PATH_FILTER}}}}
  workflow_dispatch:

jobs:
  load-config:
    name: Load Configuration
    uses: Vance-Club/shared-lib/.github/workflows/load-config.yaml@main
    with:
      config_path: {{{{CONFIG_PATH}}}}

  code-quality:
    name: Code Quality Check
    needs: [load-config]
    uses: Vance-Club/shared-lib/.github/workflows/code-quality.yaml@main
    with:
      config: ${{{{ needs.load-config.outputs.config }}}}
      runner: ${{{{ needs.load-config.outputs.runner }}}}
      skip_code_quality: {{{{SKIP_CODE_QUALITY}}}}

  build:
    name: Build Image
    needs: [load-config, code-quality]
    uses: Vance-Club/shared-lib/.github/workflows/build.yaml@main
    with:
      config: ${{{{ needs.load-config.outputs.config }}}}
      runner: ${{{{ needs.load-config.outputs.runner }}}}
      skip_snyk_code: {{{{SKIP_SNYK_CODE}}}}
      skip_snyk_oss: {{{{SKIP_SNYK_OSS}}}}
      skip_snyk_container: {{{{SKIP_SNYK_CONTAINER}}}}
    secrets: inherit

  slack-notification:
    name: Send Slack Notification
    needs: [load-config, code-quality, build]
    if: always()
    uses: Vance-Club/shared-lib/.github/workflows/slack-alerts.yaml@main
    with:
      job_status: >-
        ${{{{
          (needs.code-quality.result == 'failure' || needs.build.result == 'failure') && 'failure' ||
          (needs.code-quality.result == 'success' && needs.build.result == 'success') && 'success' ||
          'cancelled'
        }}}}
      config: ${{{{ needs.load-config.outputs.config }}}}
      runner: ${{{{ needs.load-config.outputs.runner }}}}
      vulnerability_summary: ${{{{ needs.build.outputs.vulnerability_summary }}}}
      artifact_url: ${{{{ needs.build.outputs.artifact_url }}}}
    secrets:
      ARC_SLACK_BOT_TOKEN: ${{{{ secrets.ARC_SLACK_BOT_TOKEN }}}}
"""

    async def preview_eks_yaml(
        self,
        service_config: ServiceConfigModel,
        service: ServicesMstModel,
        environment: str
    ) -> Dict[str, Any]:
        """
        Preview generated EKS YAML files without pushing to GitHub.

        Args:
            service_config: The service configuration model
            service: The service model
            environment: Environment (dev/staging/prod)

        Returns:
            Dict with config_yaml, workflow_yaml, deployment_yaml, and file paths
        """
        try:
            config = service_config.config or {}

            # Get language reference
            language_name = "java"
            language_version = None
            if service_config.language_ref_code:
                language_ref = await self.language_repo.get_by_code(service_config.language_ref_code)
                if language_ref:
                    logger.info(f"[DEBUG UPDATE] language_ref_code: {service_config.language_ref_code}, language_ref.name: '{language_ref.name}', language_ref.version: '{language_ref.version}'")
                    language_name = language_ref.name.lower()
                    language_version = language_ref.version

            # Parse language for normalized name
            _, normalized_language, _ = self._parse_language_name(language_name)

            # Generate config.yaml
            config_yaml = self._generate_config_yaml(
                service_config=service_config,
                service=service,
                environment=environment,
                language_name=language_name,
                language_version=language_version
            )

            # Determine branch based on environment
            branch = "main" if environment == "prod" else "stage-env"

            # Generate workflow.yaml and get steps
            workflow_yaml, workflow_steps, normalized_language = await self._generate_workflow_yaml(
                service_config=service_config,
                service=service,
                branch=branch,
                environment=environment,
                language_name=language_name,
                language_ref_code=service_config.language_ref_code or "JAVA_21"  # Fallback
            )

            # Generate deployment.yaml using KustomizeGeneratorService
            from app.services.kustomize_generator_service import KustomizeGeneratorService
            kustomize_service = KustomizeGeneratorService()

            # Get infrastructure details for deployment.yaml
            safe_name = service.name.lower().replace(" ", "-")
            ecr_registry = config.get("ecr_registry", "")
            ecr_repo_name = config.get("ecr_repo_name", safe_name)
            alb_subnets = config.get("alb_subnets", "")
            alb_certificate_arn = config.get("alb_certificate_arn", "")
            ingress_host = config.get("ingress_host", "")
            secrets_manager_path = config.get("secrets_manager_path", f"{safe_name}/{environment}")
            aws_region = config.get("aws_region", "us-west-2")

            # Get namespace from config or derive from environment
            namespace = config.get("namespace", f"{safe_name}-{environment}")

            deployment_yaml = kustomize_service.generate_deployment_yaml(
                service_name=service.name,
                namespace=namespace,
                environment=environment,
                config=config,
                deployment_strategy=service_config.deployment_strategy,
                aws_region=aws_region,
                ecr_registry=ecr_registry,
                ecr_repo_name=ecr_repo_name,
                image_tag="${IMAGE_TAG}",  # Placeholder for preview
                alb_subnets=alb_subnets,
                alb_certificate_arn=alb_certificate_arn,
                ingress_host=ingress_host,
                secrets_manager_path=secrets_manager_path
            )

            # Calculate file paths
            env_for_path = "stage" if environment == "staging" else environment
            build_path = (config.get("build_path") or "").strip().strip("/")

            if build_path:
                config_file_path = f"configs/{build_path}/{env_for_path}/config-{service.name}.yaml"
                deployment_file_path = f"kustomize/{build_path}/{env_for_path}/{service.name}/deployment.yaml"
            else:
                config_file_path = f"configs/{env_for_path}/config-{service.name}.yaml"
                deployment_file_path = f"kustomize/{env_for_path}/{service.name}/deployment.yaml"

            workflow_file_path = f".github/workflows/deploy-{service.name}-{env_for_path}.yml"

            return {
                "service_name": service.name,
                "environment": environment,
                "language": normalized_language,
                "config_file_path": config_file_path,
                "workflow_file_path": workflow_file_path,
                "deployment_file_path": deployment_file_path,
                "config_yaml": config_yaml,
                "workflow_yaml": workflow_yaml,
                "deployment_yaml": deployment_yaml
            }

        except Exception as e:
            logger.error(f"Error generating EKS YAML preview: {str(e)}", exc_info=True)
            raise
