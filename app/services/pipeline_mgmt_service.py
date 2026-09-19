"""
Pipeline Management Service

Service layer for managing CI/CD pipelines including:
- GitHub Actions YAML generation from templates
- Dockerfile generation from templates
- Hierarchical pipeline vendor configuration lookup

Note: ECR repositories and IAM roles are now managed via Terraform.
This service constructs resource ARNs using naming conventions.
"""

import os
from typing import Dict, Any, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status
import logging

from app.core.enum import InfraVendorEnum, PipelineAgentEnum, PipelineRunStatusEnum, WorkflowSourceTableEnum, PRStatusEnum
from app.schemas.pipeline_schemas import (
    CreatePipelineRequest,
    CreatePipelineResponse
)
from app.repository.pipeline_mst_repository import PipelineMstRepository
from app.repository.pipeline_vendor_mst_repository import PipelineVendorMstRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.repository.language_ref_repository import LanguageRefRepository
from app.repository.infra_vendor_accounts_mst_repository import InfraVendorAccountsMstRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.service_config_repository import ServiceConfigRepository
from app.repository.geo_loc_mst_repository import GeoLocMstRepository
from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
from app.domain.validators.pipeline_rules import PipelineValidator
from app.domain.factories.gitops_workflow_detail_factory import make_gitops_workflow_detail

# Import utilities
from app.utils.pipeline_helpers import (
    sanitize_name,
    generate_pipeline_code,
    generate_workflow_filename,
    generate_commit_message,
    parse_repo_url,
    generate_run_code
)
from app.utils.github_status_mapper import map_github_status
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.pr_sync_helpers import (
    determine_pr_action,
    fetch_existing_pr_content,
    cleanup_old_pr
)

# Aspora Client Plugin - PR Strategy
from app.strategies.aspora.pr_strategy import AsporaPRStrategy
from app.strategies.aspora.branch_helpers import (
    validate_branch_for_pr,
    get_branch_info_message
)
from app.tasks.pipeline_polling import start_pipeline_polling
from app.core.config import settings
logger = logging.getLogger(__name__)


class PipelineMgmtService:
    """
    Pipeline management service for creating and managing CI/CD pipelines.
    Currently supports GitHub Actions with AWS infrastructure.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.pipeline_repo = PipelineMstRepository(db)
        self.pipeline_vendor_repo = PipelineVendorMstRepository(db)
        self.service_repo = ServicesMstRepository(db)
        self.language_repo = LanguageRefRepository(db)
        self.infra_vendor_accounts_repo = InfraVendorAccountsMstRepository(db)
        self.infrastructure_repo = InfrastructureMstRepository(db)
        self.service_config_repo = ServiceConfigRepository(db)
        self.geo_loc_mst_repo = GeoLocMstRepository(db)
        self.gitops_workflow_repository = GitopsWorkflowDetailRepository(db)

    async def _get_github_token(self, owner: str) -> str:
        """Get GitHub App installation token for the given org."""
        from app.utils.github_app_token import get_token_for_org
        return await get_token_for_org(owner, self.db)

    async def _get_infrastructure_for_service_config(
        self,
        service_config,
        service,
        geo_loc_mst_code: str,
        environment: "EnvironmentEnum"
    ):
        """
        Get infrastructure for a service config, preferring explicit selection over hierarchical lookup.

        Args:
            service_config: ServiceConfigModel with optional infrastructure_mst_code
            service: ServicesMstModel with hierarchy info
            geo_loc_mst_code: Geographic location code
            environment: Environment enum

        Returns:
            InfrastructureMstModel or None
        """
        infrastructure = None

        # First, check if infrastructure is explicitly set on the service config
        if service_config.infrastructure_mst_code:
            logger.info(f"Using explicitly configured infrastructure: {service_config.infrastructure_mst_code}")
            infrastructure = await self.infrastructure_repo.get_by_code(service_config.infrastructure_mst_code)
            if infrastructure:
                # Validate that infrastructure type matches
                if infrastructure.infrastructuretype_ref_code != service_config.infrastructuretype_ref_code:
                    logger.warning(
                        f"Infrastructure type mismatch: configured infrastructure has type "
                        f"'{infrastructure.infrastructuretype_ref_code}' but service config requires "
                        f"'{service_config.infrastructuretype_ref_code}', falling back to hierarchical lookup"
                    )
                    infrastructure = None
            else:
                logger.warning(f"Configured infrastructure '{service_config.infrastructure_mst_code}' not found, falling back to hierarchical lookup")

        # Fallback to hierarchical lookup if no explicit infrastructure set or not found
        if not infrastructure:
            logger.info(f"Looking up infrastructure via hierarchy for geo_loc={geo_loc_mst_code}, env={environment}, type={service_config.infrastructuretype_ref_code}")
            infrastructure = await self.infrastructure_repo.get_by_region_hierarchy(
                geo_loc_mst_code=geo_loc_mst_code,
                resource_group_code=service.resource_group_mst_code,
                application_code=service.applications_mst_code,
                tenant_code=service.tenants_mst_code,
                environment=environment,
                infrastructuretype_ref_code=service_config.infrastructuretype_ref_code
            )

        return infrastructure

    async def create_pipeline(
        self,
        data: CreatePipelineRequest,
        tenant_code: str = None,
        user_code: str = None
    ) -> CreatePipelineResponse:
        """
        Create a new CI/CD pipeline with YAML and Dockerfile generation.

        Note: ECR repositories and IAM roles are managed via Terraform.
        This service constructs resource ARNs using naming conventions.

        Steps:
        1. Validate input and check for duplicates
        2. Get service details and validate infrastructure
        3. Validate language reference
        4. Lookup pipeline vendor configuration (hierarchical)
        5. Get infrastructure config for naming conventions
        6. Construct ECR repo URL and IAM role ARN from naming conventions
        7. Generate Dockerfile from template
        8. Generate YAML from template
        9. Commit Dockerfile and YAML to GitHub
        10. Save pipeline configuration

        Args:
            data: CreatePipelineRequest with pipeline configuration
            tenant_code: Tenant code (from JWT) for workflow tracking
            user_code: User code (from JWT) for workflow tracking

        Returns:
            CreatePipelineResponse with pipeline details and generated YAML

        Raises:
            HTTPException: For various error conditions (400, 404, 500)
        """
        try:
            # Step 1: Validate input
            logger.info(f"Creating pipeline for service: {data.service_code}")
            PipelineValidator.validate_create_pipeline_request(data)

            # Step 2: Check for duplicate pipeline
            existing = await self.pipeline_repo.check_pipeline_exists(
                service_code=data.service_code,
                repo_url=data.github_repository,
                branch=data.branch_name,
                environment=data.environment.value
            )

            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Pipeline already exists for this configuration. "
                        f"Existing pipeline code: {existing.code}"
                    )
                )

            # Step 3: Get service details
            service = await self.service_repo.get_by_code(data.service_code)
            if not service:
                raise HTTPException(
                    status_code=404,
                    detail=f"Service not found: {data.service_code}"
                )

            # Step 4: Get service config (contains language_ref_code, port, entrypoint, etc.)
            geo_loc_mst_code = data.geo_loc_mst_code

            # Fetch geographic location to get the name for naming conventions
            geo_loc_mst = await self.geo_loc_mst_repo.get_by_code(geo_loc_mst_code)
            if not geo_loc_mst:
                raise HTTPException(
                    status_code=404,
                    detail=f"Geographic location not found: {geo_loc_mst_code}"
                )
            # Use geo_loc name in lowercase for naming conventions
            geo_loc_name_for_naming = geo_loc_mst.name.lower()
            logger.info(f"Using geo_loc name for naming: {geo_loc_name_for_naming}")

            service_config = await self.service_config_repo.get_by_service_env_and_geo_loc(
                service_code=data.service_code,
                environment=data.environment.value,
                geo_loc_mst_code=geo_loc_mst_code
            )

            if not service_config:
                raise HTTPException(
                    status_code=404,
                    detail=f"Service config not found for service '{data.service_code}' in environment '{data.environment.value}' and geo_loc '{geo_loc_mst_code}'"
                )

            # Validate infrastructure vendor from service_config
            if service_config.infra_vendor_enum != InfraVendorEnum.aws:
                raise HTTPException(
                    status_code=400,
                    detail=f"Only AWS infrastructure is currently supported. Config uses: {service_config.infra_vendor_enum.value}"
                )

            # Get language reference from service_config.language_ref_code
            if not service_config.language_ref_code:
                raise HTTPException(
                    status_code=400,
                    detail=f"Language not configured in service_config for service '{data.service_code}'. Please set language_ref_code."
                )

            language_ref = await self.language_repo.get_by_code(service_config.language_ref_code)
            if not language_ref:
                raise HTTPException(
                    status_code=404,
                    detail=f"Language reference not found: {service_config.language_ref_code}"
                )

            # Step 5: Hierarchical pipeline vendor lookup
            pipeline_vendor = await self.pipeline_vendor_repo.get_by_service_hierarchy(
                service_code=data.service_code,
                resource_group_code=service.resource_group_mst_code,
                application_code=service.applications_mst_code,
                tenant_code=service.tenants_mst_code,
                environment=data.environment.value
            )

            if not pipeline_vendor:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No pipeline vendor configuration found for service '{data.service_code}' "
                        f"in environment '{data.environment.value}'. "
                        "Please configure pipeline vendor at service, resource group, application, or tenant level."
                    )
                )

            if pipeline_vendor.pipeline_agent_enum != PipelineAgentEnum.github_actions:
                raise HTTPException(
                    status_code=400,
                    detail=f"Pipeline vendor must be GitHub Actions. Found: {pipeline_vendor.pipeline_agent_enum.value}"
                )

            # Extract GitHub PAT from pipeline vendor config (used for polling, etc.)
            # Note: For commits, we use GitHub App token via _get_github_token(owner)
            github_pat = pipeline_vendor.auth_config.get("github_pat") if pipeline_vendor.auth_config else None

            # Step 6: Get infrastructure config for naming conventions
            from app.core.enum import EnvironmentEnum
            env_enum = EnvironmentEnum(data.environment.value)

            # Get infrastructure - prefer explicit selection, fallback to hierarchical lookup
            infrastructure = await self._get_infrastructure_for_service_config(
                service_config=service_config,
                service=service,
                geo_loc_mst_code=geo_loc_mst_code,
                environment=env_enum
            )

            if not infrastructure:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Infrastructure not configured for geo_loc '{geo_loc_mst_code}' "
                        f"(type: {service_config.infrastructuretype_ref_code}, environment: '{data.environment.value}'). "
                        "Please configure infrastructure at resource group, application, or tenant level."
                    )
                )

            infrastructure_config = infrastructure.locator if infrastructure.locator else {}

            # Get AWS region from infrastructure_mst.locator for YAML generation
            aws_region = infrastructure_config.get("region")
            if not aws_region:
                raise HTTPException(
                    status_code=500,
                    detail="AWS region not found in infrastructure_config. Please update infrastructure_mst.locator"
                )

            # Step 7: Construct ECR repo URL and IAM role ARN using naming strategy
            # These resources are created via Terraform, we just construct the ARNs
            from app.utils.naming_strategies import get_naming_strategy

            account_id = infrastructure_config.get("account_id")
            if not account_id:
                raise HTTPException(
                    status_code=500,
                    detail="AWS account_id not found in infrastructure_config. Please update infrastructure_mst.locator"
                )

            index = infrastructure_config.get("index", "01")

            # Get naming strategy based on tenant
            naming_strategy = get_naming_strategy(service.tenant.code)

            # Get application name for naming
            application_name = service.application.name if service.application else service.tenant.code

            # Generate org name using naming strategy
            org_name = naming_strategy.generate_org_name(
                tenant_code=service.tenant.code,
                application_name=application_name
            )

            # Map environment for naming: staging -> stage, others stay as-is
            env_for_naming = "stage" if data.environment.value == "staging" else data.environment.value

            # Generate ECR repo name using naming strategy
            ecr_repo_name = naming_strategy.generate_ecr_repo_name(
                org_name=org_name,
                environment=env_for_naming,
                geo_loc_mst_code=geo_loc_name_for_naming,
                index=index,
                service_name=service.name
            )

            # Build full ECR URI
            ecr_uri = naming_strategy.generate_ecr_uri(
                account_id=account_id,
                aws_region=aws_region,
                ecr_repo_name=ecr_repo_name
            )
            logger.info(f"Constructed ECR repository URL: {ecr_uri}")

            # Build IAM role ARN using naming strategy
            iam_role_arn = naming_strategy.generate_iam_role_arn(account_id=account_id)
            logger.info(f"Constructed IAM role ARN: {iam_role_arn}")

            # Step 8: Extract container port and entrypoint from service_config (fetched in Step 4)
            config = service_config.config
            if not config:
                raise HTTPException(
                    status_code=400,
                    detail=f"Service config is empty for service '{data.service_code}'. Please configure port and docker_entrypoint."
                )

            container_port = config.get("port")
            if not container_port:
                raise HTTPException(
                    status_code=400,
                    detail=f"Container port not configured in service_config for service '{data.service_code}'."
                )
            container_port = int(container_port)

            dockerfile_path = config.get("dockerfile_path")
            if dockerfile_path:
                logger.info(f"Using dockerfile_path from service_config: {dockerfile_path}")
            else:
                logger.info("No dockerfile_path configured, using default 'Dockerfile'")

            # Extract build_path and other_paths from service_config
            build_path = config.get("build_path")
            other_paths = config.get("other_paths")
            if build_path:
                logger.info(f"Using build_path from service_config: {build_path}")
            if other_paths:
                logger.info(f"Using other_paths from service_config: {other_paths}")

            # Extract Wire configuration for Go projects
            wire_enabled = config.get("wire_enabled", False)
            wire_path = config.get("wire_path")
            if wire_enabled:
                logger.info(f"Wire enabled with path: {wire_path}")

            # Step 9: Generate Dockerfile from template (commented out - Dockerfile managed separately)
            # logger.info(f"Generating Dockerfile for language: {service_config.language_ref_code}")
            # dockerfile_content = await self._generate_dockerfile_from_template(
            #     language_ref=language_ref,
            #     entrypoint=docker_entrypoint,
            #     container_port=container_port,
            #     environment=data.environment.value
            # )

            # Step 10: Generate YAML from template with simplified deployment
            logger.info(f"Generating YAML from template for language: {service_config.language_ref_code}")

            # Generate workflow file path for including in path filters
            workflow_filename = generate_workflow_filename(service.name, data.environment.value)
            workflow_file_path = f".github/workflows/{workflow_filename}"

            # Extract Go AWS Secrets Manager configuration
            go_use_aws_secrets = config.get("go_use_aws_secrets", False)

            yaml_content = await self._generate_yaml_from_template_simplified(
                language_ref=language_ref,
                pipeline_agent=pipeline_vendor.pipeline_agent_enum,
                ecr_uri=ecr_uri,
                iam_role_arn=iam_role_arn,
                region=aws_region,
                service=service,
                build_path=build_path,
                other_paths=other_paths,
                branch=data.branch_name,
                environment=data.environment.value,
                infrastructure_config=infrastructure_config,
                geo_loc_mst_code=geo_loc_name_for_naming,  # Pass geo_loc name (lowercase) instead of code
                dockerfile_path=dockerfile_path,
                workflow_file_path=workflow_file_path,
                wire_enabled=wire_enabled,
                wire_path=wire_path,
                go_use_aws_secrets=go_use_aws_secrets,
                build_args=config.get("build_args")
            )

            # Step 11: Commit workflow file to GitHub (Dockerfile managed separately)
            logger.info(f"Committing workflow file to GitHub repository: {data.github_repository}")
            github_commit_result = await self._commit_yaml_to_github(
                yaml_content=yaml_content,
                github_repository=data.github_repository,
                branch_name=data.branch_name,
                service_name=service.name,
                environment=data.environment.value,
                tenant_code=tenant_code
            )

            # Check if GitHub commit was successful
            if not github_commit_result:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to commit workflow file to GitHub. Pipeline not created."
                )

            # Extract GitHub commit info
            github_commit_sha = github_commit_result.get("commit_sha")
            workflow_file_path = github_commit_result.get("file_path")
            github_commit_url = github_commit_result.get("commit_url")

            # Step 11.5: Create GitOps workflow tracking if PR was created
            gitops_workflow_id = None
            # Step 11.5: Generate pipeline_code ONCE and reuse for both workflow and pipeline
            # This ensures transaction_code matches pipeline_code exactly
            pipeline_code = generate_pipeline_code(service.code, data.environment.value)

            if github_commit_result.get("pr_number"):
                try:
                    workflow_data = make_gitops_workflow_detail(
                        git_repository=data.github_repository,
                        git_branch=github_commit_result.get("feature_branch"),
                        git_commit_sha=github_commit_sha,
                        pr_number=github_commit_result.get("pr_number"),
                        pr_url=github_commit_result.get("pr_url"),
                        tenant_mst_code=tenant_code,
                        user_mst_code=user_code,
                        workflow_name=f"Pipeline: {data.pipeline_name} ({data.environment.value})",
                        transaction_code=pipeline_code,  # Use same pipeline_code
                        table_name=WorkflowSourceTableEnum.PIPELINE
                    )
                    workflow = await self.gitops_workflow_repository.create(**workflow_data)
                    gitops_workflow_id = workflow.id
                    logger.info(f"GitOps workflow created: {workflow.code}, PR #{github_commit_result.get('pr_number')}")
                except Exception as e:
                    logger.error(f"Failed to create GitOps workflow tracking: {e}")
                    # Non-fatal - continue with pipeline creation

            # Step 12: Save pipeline to database
            logger.info(f"Saving pipeline to database")
            # pipeline_code already generated above

            # Build deployment_config with commit sha, geo location, and workflow file path
            deployment_config = {
                "github_commit_sha": github_commit_sha,
                "geo_loc_mst_code": data.geo_loc_mst_code,
                "workflow_file_path": workflow_file_path
            }

            # Note: We do NOT store github_pat in pipeline's authentication_config
            # The token is fetched dynamically from pipeline_vendor_mst via hierarchical lookup
            # This ensures single source of truth and easier token rotation
            authentication_config = {}

            pipeline = await self.pipeline_repo.create(
                code=pipeline_code,
                name=data.pipeline_name,
                transaction_code=data.transaction_code,
                table_name=data.table_name or "SERVICE_CONFIG",
                tenant_code=tenant_code,
                pipeline_vendor_mst_code=pipeline_vendor.code,
                repo_url=data.github_repository,
                repo_branch=data.branch_name,
                language_ref_code=language_ref.code,
                authentication_config=authentication_config,
                deployment_config=deployment_config,
                gitops_workflow_id=gitops_workflow_id
            )

            await self.db.commit()
            logger.info(f"Pipeline created successfully: {pipeline.code}")

            # Step 13: Create initial pipeline run track record for the automatic workflow run
            # This tracks the workflow run that GitHub Actions will automatically trigger
            # when the workflow file is committed (because of on: push trigger)
            initial_run_code = None
            if github_commit_sha:
                from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
                run_track_repo = PipelineRunTrackRepository(self.db)

                initial_run_code = generate_run_code(pipeline.code)
                logger.info(f"Creating initial pipeline run track record: {initial_run_code}")

                await run_track_repo.create(
                    pipeline_mst_code=pipeline.code,
                    code=initial_run_code,
                    status=PipelineRunStatusEnum.PENDING,
                    commit_sha=github_commit_sha,
                    log_url=None,
                    github_run_id=None,
                    error_message=None
                )

                await self.db.commit()
                logger.info(f"Initial run track record created: {initial_run_code}")

                # DISABLED: Pipeline polling causes connection pool exhaustion (holds connections for 60+ min)
                # TODO: Fix connection leak before re-enabling
                # owner, repo = parse_repo_url(data.github_repository)
                # polling_token = await self._get_github_token(owner) or github_pat
                # start_pipeline_polling(
                #     run_code=initial_run_code,
                #     pipeline_code=pipeline.code,
                #     commit_sha=github_commit_sha,
                #     owner=owner,
                #     repo=repo,
                #     db_session=self.db,
                #     github_token=polling_token
                # )
                # logger.info(f"Started background polling for initial run: {initial_run_code}")

            # Step 14: Return response
            return CreatePipelineResponse(
                status="success",
                message="Pipeline created successfully",
                pipeline_code=pipeline.code,
                ecr_repo_url=ecr_uri,
                iam_role_arn=iam_role_arn,
                yaml_content=yaml_content,
                github_commit_sha=github_commit_sha,
                workflow_file_path=workflow_file_path,
                github_commit_url=github_commit_url
            )

        except HTTPException:
            # Re-raise HTTP exceptions as-is
            raise
        except ValueError as e:
            # Business logic validation errors
            logger.error(f"Validation error creating pipeline: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            # Unexpected errors
            logger.error(f"Unexpected error creating pipeline: {str(e)}", exc_info=True)
            await self.db.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create pipeline: {str(e)}"
            )

    async def save_pipeline(
        self,
        data,  # SavePipelineRequest
        tenant_code: str = None,
        user_code: str = None
    ):
        """
        Save a pipeline configuration for existing client pipelines (onboarding).

        This method performs validation and lookups (Steps 1-9) but skips:
        - YAML generation (Step 10)
        - GitHub commit/PR (Step 11)
        - GitOps tracking (Step 12)
        - Run tracking/polling (Step 14)

        Use case: Onboarding existing client pipelines without creating new PRs.

        Args:
            data: SavePipelineRequest with pipeline configuration
            tenant_code: Tenant code (from JWT)
            user_code: User code (from JWT)

        Returns:
            SavePipelineResponse with pipeline details

        Raises:
            HTTPException: For various error conditions (400, 404, 500)
        """
        from app.schemas.pipeline_schemas import SavePipelineResponse

        try:
            # Step 1: Validate input
            logger.info(f"Saving pipeline for service: {data.service_code}")
            PipelineValidator.validate_create_pipeline_request(data)

            # Step 2: Check for duplicate pipeline (including workflow_file_path)
            existing = await self.pipeline_repo.check_pipeline_exists(
                service_code=data.service_code,
                repo_url=data.github_repository,
                branch=data.branch_name,
                environment=data.environment.value,
                workflow_file_path=data.workflow_file_path
            )

            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Pipeline already exists for this configuration. "
                        f"Existing pipeline code: {existing.code}"
                    )
                )

            # Step 3: Get service details
            service = await self.service_repo.get_by_code(data.service_code)
            if not service:
                raise HTTPException(
                    status_code=404,
                    detail=f"Service not found: {data.service_code}"
                )

            # Step 4: Get geo location
            geo_loc_mst_code = data.geo_loc_mst_code
            geo_loc_mst = await self.geo_loc_mst_repo.get_by_code(geo_loc_mst_code)
            if not geo_loc_mst:
                raise HTTPException(
                    status_code=404,
                    detail=f"Geographic location not found: {geo_loc_mst_code}"
                )
            geo_loc_name_for_naming = geo_loc_mst.name.lower()
            logger.info(f"Using geo_loc name for naming: {geo_loc_name_for_naming}")

            # Step 5: Get service config
            service_config = await self.service_config_repo.get_by_service_env_and_geo_loc(
                service_code=data.service_code,
                environment=data.environment.value,
                geo_loc_mst_code=geo_loc_mst_code
            )

            if not service_config:
                raise HTTPException(
                    status_code=404,
                    detail=f"Service config not found for service '{data.service_code}' in environment '{data.environment.value}' and geo_loc '{geo_loc_mst_code}'"
                )

            # Validate infrastructure vendor from service_config
            if service_config.infra_vendor_enum != InfraVendorEnum.aws:
                raise HTTPException(
                    status_code=400,
                    detail=f"Only AWS infrastructure is currently supported. Config uses: {service_config.infra_vendor_enum.value}"
                )

            # Step 6: Get language reference
            if not service_config.language_ref_code:
                raise HTTPException(
                    status_code=400,
                    detail=f"Language not configured in service_config for service '{data.service_code}'. Please set language_ref_code."
                )

            language_ref = await self.language_repo.get_by_code(service_config.language_ref_code)
            if not language_ref:
                raise HTTPException(
                    status_code=404,
                    detail=f"Language reference not found: {service_config.language_ref_code}"
                )

            # Step 7: Hierarchical pipeline vendor lookup
            pipeline_vendor = await self.pipeline_vendor_repo.get_by_service_hierarchy(
                service_code=data.service_code,
                resource_group_code=service.resource_group_mst_code,
                application_code=service.applications_mst_code,
                tenant_code=service.tenants_mst_code,
                environment=data.environment.value
            )

            if not pipeline_vendor:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No pipeline vendor configuration found for service '{data.service_code}' "
                        f"in environment '{data.environment.value}'. "
                        "Please configure pipeline vendor at service, resource group, application, or tenant level."
                    )
                )

            if pipeline_vendor.pipeline_agent_enum != PipelineAgentEnum.github_actions:
                raise HTTPException(
                    status_code=400,
                    detail=f"Pipeline vendor must be GitHub Actions. Found: {pipeline_vendor.pipeline_agent_enum.value}"
                )

            # Step 8: Get infrastructure config
            from app.core.enum import EnvironmentEnum
            env_enum = EnvironmentEnum(data.environment.value)

            # Get infrastructure - prefer explicit selection, fallback to hierarchical lookup
            infrastructure = await self._get_infrastructure_for_service_config(
                service_config=service_config,
                service=service,
                geo_loc_mst_code=geo_loc_mst_code,
                environment=env_enum
            )

            if not infrastructure:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Infrastructure not configured for geo_loc '{geo_loc_mst_code}' "
                        f"(type: {service_config.infrastructuretype_ref_code}, environment: '{data.environment.value}'). "
                        "Please configure infrastructure at resource group, application, or tenant level."
                    )
                )

            infrastructure_config = infrastructure.locator if infrastructure.locator else {}

            aws_region = infrastructure_config.get("region")
            if not aws_region:
                raise HTTPException(
                    status_code=500,
                    detail="AWS region not found in infrastructure_config. Please update infrastructure_mst.locator"
                )

            # Step 9: Construct ECR repo URL and IAM role ARN
            from app.utils.naming_strategies import get_naming_strategy

            account_id = infrastructure_config.get("account_id")
            if not account_id:
                raise HTTPException(
                    status_code=500,
                    detail="AWS account_id not found in infrastructure_config. Please update infrastructure_mst.locator"
                )

            index = infrastructure_config.get("index", "01")
            naming_strategy = get_naming_strategy(service.tenant.code)
            application_name = service.application.name if service.application else service.tenant.code

            org_name = naming_strategy.generate_org_name(
                tenant_code=service.tenant.code,
                application_name=application_name
            )

            env_for_naming = "stage" if data.environment.value == "staging" else data.environment.value

            ecr_repo_name = naming_strategy.generate_ecr_repo_name(
                org_name=org_name,
                environment=env_for_naming,
                geo_loc_mst_code=geo_loc_name_for_naming,
                index=index,
                service_name=service.name
            )

            ecr_uri = naming_strategy.generate_ecr_uri(
                account_id=account_id,
                aws_region=aws_region,
                ecr_repo_name=ecr_repo_name
            )
            logger.info(f"Constructed ECR repository URL: {ecr_uri}")

            iam_role_arn = naming_strategy.generate_iam_role_arn(account_id=account_id)
            logger.info(f"Constructed IAM role ARN: {iam_role_arn}")

            # SKIP Steps 10-12: No YAML generation, no GitHub commit, no GitOps tracking

            # Step 13: Save pipeline to database
            logger.info(f"Saving pipeline to database")
            pipeline_code = generate_pipeline_code(service.code, data.environment.value)

            # Build deployment_config with workflow_file_path from request (no commit sha)
            deployment_config = {
                "github_commit_sha": None,
                "geo_loc_mst_code": data.geo_loc_mst_code,
                "workflow_file_path": data.workflow_file_path
            }

            authentication_config = {}

            pipeline = await self.pipeline_repo.create(
                code=pipeline_code,
                name=data.pipeline_name,
                transaction_code=data.transaction_code,
                table_name=data.table_name or "SERVICE_CONFIG",
                tenant_code=tenant_code,
                pipeline_vendor_mst_code=pipeline_vendor.code,
                repo_url=data.github_repository,
                repo_branch=data.branch_name,
                language_ref_code=language_ref.code,
                authentication_config=authentication_config,
                deployment_config=deployment_config,
                gitops_workflow_id=None  # No GitOps tracking for onboarding
            )

            await self.db.commit()
            logger.info(f"Pipeline saved successfully: {pipeline.code}")

            # SKIP Step 14: No run tracking or polling for onboarding

            # Return response
            return SavePipelineResponse(
                status="success",
                message="Pipeline saved successfully",
                pipeline_code=pipeline.code,
                ecr_repo_url=ecr_uri,
                iam_role_arn=iam_role_arn,
                workflow_file_path=data.workflow_file_path
            )

        except HTTPException:
            raise
        except ValueError as e:
            logger.error(f"Validation error saving pipeline: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Unexpected error saving pipeline: {str(e)}", exc_info=True)
            await self.db.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save pipeline: {str(e)}"
            )

    # ==================== PRIVATE HELPER METHODS ====================

    async def _get_github_token_for_pipeline(self, pipeline_code: str) -> str:
        """
        Get GitHub token for a pipeline using hierarchical lookup.

        This method:
        1. Fetches the pipeline by code
        2. Gets the associated service
        3. Performs hierarchical pipeline_vendor lookup
        4. Extracts GitHub PAT from pipeline_vendor.auth_config

        Args:
            pipeline_code: Pipeline code

        Returns:
            GitHub Personal Access Token

        Raises:
            HTTPException: If pipeline, service, or token not found
        """
        # Fetch pipeline
        pipeline = await self.pipeline_repo.get_by_code(pipeline_code)
        if not pipeline:
            raise HTTPException(
                status_code=404,
                detail=f"Pipeline with code '{pipeline_code}' not found"
            )

        # Resolve service through service_config → services_mst
        from app.repository.service_config_repository import ServiceConfigRepository
        svc_cfg_repo = ServiceConfigRepository(self.db)
        service_config = await svc_cfg_repo.get_by_code(pipeline.transaction_code)
        if not service_config:
            raise HTTPException(
                status_code=404,
                detail=f"Service config not found for pipeline: {pipeline_code}"
            )

        service = await self.service_repo.get_by_code(service_config.services_mst_code)
        if not service:
            raise HTTPException(
                status_code=404,
                detail=f"Service not found for pipeline: {pipeline_code}"
            )

        environment = service_config.environment.value if hasattr(service_config.environment, 'value') else str(service_config.environment)

        # Hierarchical pipeline vendor lookup
        pipeline_vendor = await self.pipeline_vendor_repo.get_by_service_hierarchy(
            service_code=service.code,
            resource_group_code=service.resource_group_mst_code,
            application_code=service.applications_mst_code,
            tenant_code=service.tenants_mst_code,
            environment=environment
        )

        if not pipeline_vendor:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No pipeline vendor configuration found for pipeline '{pipeline_code}' "
                    f"(service: '{service.code}', environment: '{environment}'). "
                    "Please configure pipeline vendor at service, resource group, application, or tenant level."
                )
            )

        # Extract GitHub PAT
        github_pat = pipeline_vendor.auth_config.get("github_pat") if pipeline_vendor.auth_config else None
        if not github_pat:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"GitHub PAT not configured in pipeline vendor '{pipeline_vendor.code}'. "
                    "Please update pipeline_vendor_mst.auth_config with 'github_pat' key."
                )
            )

        logger.info(f"Retrieved GitHub token for pipeline '{pipeline_code}' from pipeline_vendor '{pipeline_vendor.code}'")
        return github_pat

    async def _generate_dockerfile_from_template(
        self,
        language_ref,
        entrypoint: str,
        container_port: int,
        environment: str
    ) -> str:
        """
        Generate Dockerfile content from template based on language.

        Args:
            language_ref: LanguageRef model instance (has name field: "Python", "Go", "Java")
            entrypoint: Docker entrypoint from service_config.config.docker_entrypoint (required)
            container_port: Container port from service_config.config.port
            environment: Environment (dev/staging/prod)

        Returns:
            Generated Dockerfile content as string

        Raises:
            HTTPException: If Dockerfile template not found or language not supported
        """
        # Get language name from language_ref.name (e.g., "Python", "Go", "Java")
        language_name = language_ref.name.lower()

        # Supported languages: Java, Go, Python only
        dockerfile_templates = {
            "python": "python.Dockerfile",
            "go": "go.Dockerfile",
            "java": "java.Dockerfile",
        }

        template_filename = dockerfile_templates.get(language_name)
        if not template_filename:
            raise HTTPException(
                status_code=400,
                detail=f"Language '{language_name}' is not supported. Supported languages: Python, Go, Java"
            )

        # Build template file path
        template_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "templates", "dockerfiles", template_filename
        )

        # Read template file
        try:
            with open(template_path, 'r') as f:
                template_content = f.read()
        except FileNotFoundError:
            raise HTTPException(
                status_code=500,
                detail=f"Dockerfile template not found: {template_path}"
            )

        # Replace placeholders
        # {{ENTRYPOINT}} - docker entrypoint from service_config (e.g., "/main", "app.jar", "main.py")
        dockerfile_content = template_content
        dockerfile_content = dockerfile_content.replace("{{ENTRYPOINT}}", entrypoint)
        dockerfile_content = dockerfile_content.replace("{{CONTAINER_PORT}}", str(container_port))
        dockerfile_content = dockerfile_content.replace("{{ENVIRONMENT}}", environment)

        logger.info(f"Generated Dockerfile from template: {template_filename}")
        return dockerfile_content

    async def _generate_yaml_from_template_simplified(
        self,
        language_ref,
        pipeline_agent: PipelineAgentEnum,
        ecr_uri: str,
        iam_role_arn: str,
        region: str,
        service,
        build_path: Optional[str],
        other_paths: Optional[list],
        branch: str,
        environment: str,
        infrastructure_config: Dict[str, Any],
        geo_loc_mst_code: str,
        dockerfile_path: Optional[str] = None,
        workflow_file_path: Optional[str] = None,
        wire_enabled: bool = False,
        wire_path: Optional[str] = None,
        go_use_aws_secrets: bool = False,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """
        Generate CI/CD YAML from template with simplified deployment.

        This version:
        - Does NOT inject secrets/parameters
        - Uses simplified ECS deployment (force-new-deployment)
        - Constructs ECR repo and IAM role from naming conventions

        Args:
            language_ref: LanguageRef model instance with yaml_templates JSONB
            pipeline_agent: Pipeline agent enum (github_actions, etc.)
            ecr_uri: ECR repository URI (constructed from naming convention)
            iam_role_arn: IAM role ARN (constructed from naming convention)
            region: AWS region
            service: Service model instance
            build_path: Build path for JAR/Go build location (from service_config)
            other_paths: Additional paths that should trigger the workflow (from service_config)
            branch: Git branch name
            environment: Environment
            workflow_file_path: Optional workflow file path to include in path filters

        Returns:
            Generated YAML content as string

        Raises:
            HTTPException: If language doesn't support the pipeline agent
        """
        # Get template URL from JSONB based on pipeline agent
        agent_key = pipeline_agent.value  # e.g., "github_actions"
        template_url = language_ref.yaml_templates.get(agent_key)

        if not template_url:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Language '{language_ref.name}' does not support pipeline agent '{pipeline_agent.value}'. "
                    f"Supported agents: {', '.join(language_ref.yaml_templates.keys())}"
                )
            )

        # Build template file path from URL
        template_file = os.path.join(
            os.path.dirname(__file__), "..", "..",
            template_url.lstrip("/")
        )

        # Read template file
        try:
            with open(template_file, 'r') as f:
                template_content = f.read()
        except FileNotFoundError:
            raise HTTPException(
                status_code=500,
                detail=f"YAML template not found: {template_file}"
            )

        # Replace standard placeholders
        sanitized_service_name = service.name.replace(" ", "_").replace("-", "_").lower()

        yaml_content = template_content
        yaml_content = yaml_content.replace("{{SERVICE_NAME}}", service.name)
        yaml_content = yaml_content.replace("{{SERVICE_CODE}}", sanitized_service_name)
        # Map environment for display: staging/qa -> stg
        env_display = "stg" if environment in ("staging", "qa") else environment
        yaml_content = yaml_content.replace("{{ENVIRONMENT_NAME}}", environment)  # For workflow name/run-name (use original env name)
        yaml_content = yaml_content.replace("{{ENVIRONMENT}}", env_display)
        yaml_content = yaml_content.replace("{{BRANCH}}", branch)
        yaml_content = yaml_content.replace("{{ECR_REPOSITORY}}", ecr_uri)
        yaml_content = yaml_content.replace("{{AWS_ROLE_ARN}}", iam_role_arn)
        yaml_content = yaml_content.replace("{{AWS_REGION}}", region)

        # Replace language version placeholders
        yaml_content = yaml_content.replace("{{JAVA_VERSION}}", language_ref.version or "")
        yaml_content = yaml_content.replace("{{GO_VERSION}}", language_ref.version or "")
        yaml_content = yaml_content.replace("{{NODE_VERSION}}", language_ref.version or "")

        # Handle GitHub environment - only add for prod
        if environment == "prod":
            yaml_content = yaml_content.replace("{{GITHUB_ENVIRONMENT}}", "    environment: prod\n")
        else:
            yaml_content = yaml_content.replace("{{GITHUB_ENVIRONMENT}}", "")

        # Handle Dockerfile path - only include --file flag if dockerfile_path is specified
        if dockerfile_path:
            yaml_content = yaml_content.replace("{{DOCKERFILE_FLAG}}", f"--file {dockerfile_path} \\\n            ")
        else:
            yaml_content = yaml_content.replace("{{DOCKERFILE_FLAG}}", "")

        # Handle path filter for GitHub Actions triggers
        # Only add paths filter if both build_path and dockerfile_path are configured
        # If no paths configured, workflow triggers on any push to the branch
        path_lines = []

        # Strip trailing slashes from paths to avoid double slashes in output
        build_path_clean = build_path.rstrip('/') if build_path else None
        dockerfile_path_clean = dockerfile_path.rstrip('/') if dockerfile_path else None

        # Check if this is Go language (codes like GO_1_23, GO_1_24, GO_1_25)
        is_go_language = language_ref.code.upper().startswith("GO")

        # For Go: don't add build_path or dockerfile_path to path filter
        # For other languages: only add build_path if dockerfile_path is also provided (and vice versa)
        should_add_build_path = build_path_clean and dockerfile_path_clean and not is_go_language
        should_add_dockerfile_path = dockerfile_path_clean and build_path_clean and not is_go_language

        has_service_paths = should_add_build_path or bool(other_paths)

        # Add build_path if conditions are met (not Go, and dockerfile_path is provided)
        if should_add_build_path:
            path_lines.append(f"      - '{build_path_clean}/**'")

        # Add other_paths if provided
        if other_paths:
            for other_path in other_paths:
                if other_path and other_path.strip():
                    clean_path = other_path.strip().rstrip('/')
                    path_lines.append(f"      - '{clean_path}/**'")

        # Only include workflow file path and dockerfile path if service paths are configured
        # This ensures the workflow also triggers when these files change in a monorepo setup
        if has_service_paths or should_add_dockerfile_path:
            if workflow_file_path:
                path_lines.append(f"      - '{workflow_file_path}'")
            # Add dockerfile_path only if build_path is also provided
            if should_add_dockerfile_path:
                path_lines.append(f"      - '{dockerfile_path_clean}'")

        if path_lines:
            yaml_content = yaml_content.replace("{{FOLDER_PATH_FILTER}}", f"    paths:\n" + "\n".join(path_lines))
        else:
            yaml_content = yaml_content.replace("{{FOLDER_PATH_FILTER}}", "")

        # Handle JAR path for Java builds (monorepo support)
        # Maven uses target/*.jar, Gradle uses build/libs/*.jar
        # Detect Maven by checking if language_ref code contains "MAVEN"
        is_maven = "MAVEN" in language_ref.code.upper()
        jar_dir = "target" if is_maven else "build/libs"

        if build_path_clean:
            # For monorepos, JAR is in {build_path}/{jar_dir}/*.jar
            yaml_content = yaml_content.replace("{{JAR_PATH}}", f"{build_path_clean}/{jar_dir}/*.jar")
        else:
            # For single-service repos, JAR is in {jar_dir}/*.jar
            yaml_content = yaml_content.replace("{{JAR_PATH}}", f"{jar_dir}/*.jar")

        # Handle BUILD_PATH for Go builds (monorepo support)
        # Fallback to "." (current directory) if not provided
        yaml_content = yaml_content.replace("{{BUILD_PATH}}", build_path_clean if build_path_clean else ".")

        # Handle Wire step for Go projects (dependency injection code generation)
        if is_go_language and wire_enabled and wire_path:
            wire_step = f"""
      - name: Regenerate Wire
        run: |
          go install github.com/google/wire/cmd/wire@latest
          wire {wire_path}
"""
            yaml_content = yaml_content.replace("{{WIRE_STEP}}", wire_step)
            logger.info(f"Added Wire step for Go project with path: {wire_path}")
        else:
            yaml_content = yaml_content.replace("{{WIRE_STEP}}", "")

        # Handle Go Docker build args (AWS Secrets Manager support)
        if is_go_language:
            if go_use_aws_secrets:
                # Map environment: dev -> dev, staging -> stg, qa -> stg, prod -> prod
                config_env_map = {"dev": "dev", "staging": "stg", "qa": "stg", "prod": "prod"}
                config_env = config_env_map.get(environment, environment)
                go_build_args = f"""            --build-arg AWS_SECRETS_MANAGER_NAME=${{{{ secrets.AWS_SECRETS_MANAGER_NAME }}}} \\
            --build-arg CONFIG_ENV={config_env} \\
"""
                yaml_content = yaml_content.replace("{{GO_DOCKER_BUILD_ARGS}}", go_build_args)
                logger.info(f"Added Go Docker build args for AWS Secrets Manager with CONFIG_ENV={config_env}")
            else:
                yaml_content = yaml_content.replace("{{GO_DOCKER_BUILD_ARGS}}", "")
        else:
            yaml_content = yaml_content.replace("{{GO_DOCKER_BUILD_ARGS}}", "")

        # Handle custom build args (user-defined build arguments for docker build)
        if build_args:
            # Filter out empty entries and generate --build-arg flags
            valid_args = [arg for arg in build_args if arg.get("name", "").strip()]
            if valid_args:
                build_args_lines = []
                for arg in valid_args:
                    name = arg["name"].strip()
                    value = arg.get("value", "").strip()
                    if value:
                        build_args_lines.append(f'            --build-arg {name}="{value}" \\')
                    else:
                        # For args without value, use GitHub secrets pattern
                        build_args_lines.append(f'            --build-arg {name}="${{{{ secrets.{name} }}}}" \\')
                custom_build_args = "\n".join(build_args_lines) + "\n"
                yaml_content = yaml_content.replace("{{CUSTOM_BUILD_ARGS}}", custom_build_args)
                logger.info(f"Added {len(valid_args)} custom Docker build args")
            else:
                yaml_content = yaml_content.replace("{{CUSTOM_BUILD_ARGS}}", "")
        else:
            yaml_content = yaml_content.replace("{{CUSTOM_BUILD_ARGS}}", "")

        # Replace ECS deployment placeholders directly
        from app.utils.naming_strategies import get_naming_strategy

        # Get naming strategy based on tenant
        naming_strategy = get_naming_strategy(service.tenant.code)

        # Get application name for naming
        application_name = service.application.name if service.application else service.tenant.code
        index = infrastructure_config.get("index", "01")

        # Map environment for naming: staging -> stage, others stay as-is
        env_for_naming = "stage" if environment == "staging" else environment

        # Generate ECS service name using naming strategy
        # Note: geo_loc_mst_code parameter now contains geo_loc name (lowercase)
        ecs_service = naming_strategy.generate_ecs_service_name(
            application_name=application_name,
            environment=env_for_naming,
            geo_loc_mst_code=geo_loc_mst_code,
            index=index,
            service_name=service.name
        )

        # Cluster name comes from infrastructure_mst.locator
        ecs_cluster = infrastructure_config.get("cluster_name")
        if not ecs_cluster:
            raise HTTPException(
                status_code=500,
                detail="ECS cluster_name not found in infrastructure_config. Please update infrastructure_mst.locator"
            )

        # Replace ECS placeholders in template
        yaml_content = yaml_content.replace("{{ECS_CLUSTER}}", ecs_cluster)
        yaml_content = yaml_content.replace("{{ECS_SERVICE}}", ecs_service)

        logger.info(f"Generated simplified YAML from template: {language_ref.code}")
        return yaml_content

    async def _get_aws_auth_config(self, service, environment: str, service_config=None) -> Dict[str, Any]:
        """
        Get AWS authentication configuration using hierarchical lookup.

        Lookup Pattern:
        Hierarchical lookup via InfrastructureMstRepository
        Priority: Resource Group → Application → Tenant
        (Service-level infrastructure is skipped)

        Args:
            service: Service model instance
            environment: Environment string
            service_config: Service config model instance (optional, will fetch if not provided)

        Returns:
            AWS auth config dict from infra_vendor_accounts_mst

        Raises:
            HTTPException: If infrastructure or vendor account not configured
        """
        from app.core.enum import EnvironmentEnum

        # Convert environment string to enum
        env_enum = EnvironmentEnum(environment)

        # Get infrastructuretype_ref_code from service_config
        if service_config is None:
            # Fetch service_config if not provided (for backwards compatibility)
            service_config = await self.service_config_repo.get_by_service_and_environment(
                service_code=service.code,
                environment=environment
            )

        infrastructuretype_ref_code = service_config.infrastructuretype_ref_code if service_config else None

        infrastructure = None

        # Hierarchical lookup only (skip service-level check)
        logger.info("Performing hierarchical infrastructure lookup")
        infrastructure = await self.infrastructure_repo.get_by_service_hierarchy(
            service_code=service.code,
            resource_group_code=service.resource_group_mst_code,
            application_code=service.applications_mst_code,
            tenant_code=service.tenants_mst_code,
            environment=env_enum,
            infrastructuretype_ref_code=infrastructuretype_ref_code
        )

        # Error if infrastructure not found at any level
        if not infrastructure:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Infrastructure not configured for service '{service.code}' "
                    f"(type: {infrastructuretype_ref_code}, environment: '{environment}'). "
                    "Please configure infrastructure at resource group, application, or tenant level."
                )
            )

        # Ensure infrastructure has vendor account
        if not infrastructure.infra_vendor_account:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Infrastructure '{infrastructure.code}' found but has no vendor account configured. "
                    "Please link infrastructure to an infra_vendor_accounts_mst entry."
                )
            )

        logger.info(f"Using AWS credentials from vendor account: {infrastructure.infra_vendor_account.code}")

        # Clone auth_config and override region with service's actual AWS region identifier
        auth_config = infrastructure.infra_vendor_account.auth_config.copy()

        # Get the actual AWS region identifier (e.g., "ap-south-1" not "aws-ap-south-1")
        if service.region_custom:
            # For on-prem or custom regions, use as-is
            auth_config["region"] = service.region_custom
            logger.info(f"Using custom region: {service.region_custom}")
        elif service.region_ref:
            # For cloud vendors, use the actual region identifier from region_ref
            auth_config["region"] = service.region_ref.region_identifier
            logger.info(f"Using region from region_ref: {service.region_ref.region_identifier}")
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Service '{service.code}' has no region configured"
            )

        return auth_config

    def _build_ecr_repo_name(
        self,
        tenant_name: str,
        region: str,
        environment: str,
        service_name: str
    ) -> str:
        """
        Build ECR repository name following the naming convention:
        {tenant_name}_{region}_{env}_{service_name}

        All components are sanitized (lowercase, alphanumeric, hyphens only).

        Args:
            tenant_name: Tenant name
            region: AWS region
            environment: Environment (dev, staging, prod)
            service_name: Service name

        Returns:
            Sanitized ECR repository name
        """
        # Sanitize each component
        tenant_sanitized = sanitize_name(tenant_name)
        region_sanitized = sanitize_name(region)
        env_sanitized = sanitize_name(environment)
        service_sanitized = sanitize_name(service_name)

        # Build repository name
        repo_name = f"{tenant_sanitized}_{region_sanitized}_{env_sanitized}_{service_sanitized}"

        # ECR repository names must be lowercase
        repo_name = repo_name.lower()

        logger.info(f"Built ECR repository name: {repo_name}")
        return repo_name

    async def _get_service_secrets_and_params(
        self,
        service_code: str,
        environment
    ) -> list:
        """
        Fetch all active secrets/parameters for service in given environment.

        Args:
            service_code: Service code
            environment: Environment enum (dev/staging/prod)

        Returns:
            List of AwsSecretsParametersMstModel instances
        """
        return await self.secrets_params_repo.get_by_service_code_and_environment(
            service_code=service_code,
            environment=environment
        )

    async def _commit_yaml_to_github(
        self,
        yaml_content: str,
        github_repository: str,
        branch_name: str,
        service_name: str,
        environment: str,
        github_token: str = None,
        existing_pipeline_pr: Optional[Dict] = None,
        create_secondary_pr: bool = False,
        tenant_code: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        Commit the generated YAML workflow file to GitHub repository using PR workflow.

        Creates a feature branch, commits the file, and creates a PR to the base branch.
        Uses centralized GitHub App token for bot commits.

        Uses Aspora PR Strategy:
        - Protected branches (main/master/pre-prod/qa/sandbox) → PR to pre-prod (fallback stage-env)
        - stage-env/stage-env-copy → PR to stage-env
        - Feature branches → PR to same branch
        - Optional secondary PR for qa/sandbox/stage-env-copy

        Uses Smart PR logic when existing_pipeline_pr is provided:
        1. No existing PR → create new
        2. Existing PR with same content → skip (return existing PR info)
        3. Existing PR with different content → replace (close old, create new)
        4. Existing PR was manually closed → create new

        Args:
            yaml_content: Generated YAML content
            github_repository: Repository in format "owner/repo"
            branch_name: Branch user selected in UI (workflow trigger branch)
            service_name: Service name
            environment: Environment
            github_token: GitHub token (optional, reuses token from upstream to avoid duplicate requests)
            existing_pipeline_pr: Optional dict with existing PR info for Smart PR logic
            create_secondary_pr: Whether to create secondary PR for eligible branches (qa/sandbox/stage-env-copy)

        Returns:
            Dict with commit info (commit_sha, file_path, commit_url, pr_url, pr_number) or None if failed
        """
        from app.integrations.github_integration import GitHubIntegration
        import re

        try:
            # Parse owner and repo from github_repository
            if "/" not in github_repository:
                logger.error(f"Invalid GitHub repository format: {github_repository}. Expected 'owner/repo'")
                return None

            owner, repo = github_repository.split("/", 1)

            # Get GitHub token (uses passed token if available, otherwise fetches new one)
            if not github_token:
                logger.info("Getting GitHub token for PR workflow...")
                github_token = await self._get_github_token(owner)
            else:
                logger.info("Using passed GitHub token for PR workflow")
            if not github_token:
                logger.error("GitHub token not configured")
                return None
            logger.info(f"Got GitHub token successfully")

            # Generate workflow filename
            filename = generate_workflow_filename(service_name, environment)
            file_path = f".github/workflows/{filename}"

            # Sanitize service name for branch
            service_name_sanitized = re.sub(r'[\s_-]+', '-', service_name.strip()).strip('-').lower()

            # === Aspora PR Strategy (only for aspora tenant) ===
            is_aspora_tenant = tenant_code and tenant_code.lower() == "aspora"

            if is_aspora_tenant:
                # Fetch available branches for strategy decision
                available_branches = await self._get_repo_branch_names(
                    github_token=github_token,
                    owner=owner,
                    repo=repo
                )

                # Apply Aspora PR strategy
                strategy = AsporaPRStrategy()
                routing_info = strategy.get_pr_routing_info(
                    selected_branch=branch_name,
                    available_branches=available_branches,
                    create_secondary_pr=create_secondary_pr
                )

                # Get the actual PR target and feature branch source from strategy
                base_branch = routing_info["pr_target"]
                feature_branch_source = routing_info["feature_branch_source"]

                logger.info(f"Aspora PR Strategy: selected={branch_name}, pr_target={base_branch}, "
                           f"feature_source={feature_branch_source}, secondary_pr={routing_info['secondary_pr_enabled']}")
            else:
                # Default behavior for non-aspora tenants: PR to same branch
                base_branch = branch_name
                feature_branch_source = branch_name
                routing_info = {
                    "pr_target": branch_name,
                    "feature_branch_source": branch_name,
                    "is_protected": False,
                    "secondary_pr_target": None,
                    "secondary_pr_enabled": False
                }
                logger.info(f"Default PR Strategy (non-aspora): branch={branch_name}")

            # Smart PR: Check if we should skip/create/replace (when existing PR info provided)
            pr_action_result = {"action": "create"}
            if existing_pipeline_pr:
                existing_pr_content = fetch_existing_pr_content(
                    GitHubIntegration=GitHubIntegration,
                    github_token=github_token,
                    github_base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=file_path,
                    branch=existing_pipeline_pr.get("git_branch")
                )
                pr_action_result = determine_pr_action(
                    existing_pr_info=existing_pipeline_pr,
                    existing_pr_content=existing_pr_content,
                    new_content=yaml_content,
                    owner=owner,
                    repo=repo
                )

                # Handle skip action - return existing PR info
                if pr_action_result.get("action") == "skip":
                    logger.info(f"Smart PR: Skipping - content unchanged for {service_name} ({environment})")
                    return {
                        "status": "no_changes",
                        "message": f"No changes detected - using existing PR #{pr_action_result.get('existing_pr_number')}",
                        "file_path": file_path,
                        "pr_url": pr_action_result.get("existing_pr_url"),
                        "pr_number": pr_action_result.get("existing_pr_number"),
                        "commit_sha": pr_action_result.get("existing_commit_sha"),
                        "feature_branch": pr_action_result.get("existing_branch"),
                        "success": True
                    }
            else:
                # No existing PR info - check base branch content for simple skip
                existing_content = None
                try:
                    existing_file = await GitHubIntegration.get_file_content(
                        token=github_token,
                        base_url=settings.github_base_url,
                        owner=owner,
                        repo=repo,
                        file_path=file_path,
                        branch=base_branch
                    )
                    if existing_file and existing_file.get("exists"):
                        existing_content = existing_file.get("content")
                        logger.info(f"Found existing workflow file in {base_branch}: {file_path}")
                    else:
                        logger.info(f"No existing workflow file found in {base_branch}, will create new")
                except Exception as e:
                    logger.warning(f"Could not fetch existing workflow file from GitHub: {e}, will create new")

                # Check if content has changed (skip PR if no changes)
                if existing_content and should_skip_commit(existing_content, yaml_content):
                    logger.info(f"No changes detected for pipeline {service_name} in {environment}, skipping PR creation")
                    return {
                        "status": "no_changes",
                        "message": f"No changes detected for pipeline {service_name} - workflow YAML is identical",
                        "file_path": file_path,
                        "success": True
                    }

            # Step 2: Create feature branch name with versioning (only if there are changes)
            base_branch_name = f"pipeline/{service_name_sanitized}-{environment}"
            feature_branch = await self._generate_unique_branch_name(
                github_token=github_token,
                owner=owner,
                repo=repo,
                base_name=base_branch_name
            )
            logger.info(f"Using feature branch: {feature_branch}")

            # Step 3: Create feature branch from the appropriate source branch (Aspora strategy)
            # For protected branches: from pre-prod or stage-env
            # For feature branches: from the selected branch itself
            branch_result = await GitHubIntegration.create_branch(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch_name=feature_branch,
                from_branch=feature_branch_source  # Use strategy-determined source, not base_branch
            )
            branch_already_exists = branch_result.get("already_exists", False)

            if branch_already_exists:
                logger.info(f"Branch {feature_branch} already exists, will update it")
            else:
                logger.info(f"Created new branch: {feature_branch}")

            # Step 4: Generate commit message
            commit_message = generate_commit_message(service_name, environment)

            logger.info(f"Committing workflow file to GitHub: {github_repository}/{file_path} on branch {feature_branch}")

            # Step 4: Commit file to feature branch
            result = await GitHubIntegration.update_or_create_file(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch=feature_branch,
                file_path=file_path,
                content=yaml_content,
                message=commit_message
            )

            logger.info(f"Successfully committed workflow file: {result.get('file_path')}")

            # Step 5: Check if there's an open PR for this branch, create one if not
            existing_pr = await GitHubIntegration.find_open_pr(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                head=feature_branch,
                base=base_branch
            )

            if existing_pr:
                # Open PR already exists - just updated the branch
                logger.info(f"Updated branch {feature_branch}, existing PR #{existing_pr.get('number')} will show changes")
                return {
                    "commit_sha": result.get("commit_sha"),
                    "file_path": file_path,
                    "commit_url": result.get("commit_url"),
                    "html_url": result.get("html_url"),
                    "feature_branch": feature_branch,
                    "base_branch": base_branch,
                    "pr_number": existing_pr.get("number"),
                    "pr_url": existing_pr.get("html_url"),
                    "pr_state": existing_pr.get("state"),
                    "success": True,
                    "message": f"Updated branch - changes added to existing PR #{existing_pr.get('number')}"
                }
            else:
                # No open PR exists - create a new one (even if branch already existed, PR may have been closed/merged)
                pr_title = f"[Pipeline] Add deployment workflow for {service_name} - {environment}"

                # Add supersede note if replacing old PR
                supersede_note = ""
                if pr_action_result.get("action") == "replace":
                    old_pr_num = pr_action_result.get("old_pr_to_close")
                    supersede_note = f"\n\n> **Note:** This PR supersedes PR #{old_pr_num}"

                pr_body = f"""## Pipeline Deployment Workflow

**Service:** `{service_name}`
**Environment:** {environment}

### File Path
`{file_path}`

### Changes
- Added GitHub Actions deployment workflow for `{service_name}`
- Workflow triggers on push to `{base_branch}` branch{supersede_note}

### Deployment
Merge this PR to add the deployment workflow to the repository.

---
*Generated by {settings.app_name}*"""

                logger.info(f"Creating PR from {feature_branch} to {base_branch}")

                pr_result = await GitHubIntegration.create_pull_request(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    head=feature_branch,
                    base=base_branch,
                    title=pr_title,
                    body=pr_body,
                    draft=False
                )

                logger.info(f"Successfully created PR #{pr_result.get('number')}: {pr_result.get('html_url')}")

                # Smart PR: Cleanup old PR if this was a replacement
                if pr_action_result.get("action") == "replace" and pr_result.get("number"):
                    cleanup_old_pr(
                        GitHubIntegration=GitHubIntegration,
                        github_token=github_token,
                        github_base_url=settings.github_base_url,
                        owner=owner,
                        repo=repo,
                        old_pr_number=pr_action_result.get("old_pr_to_close"),
                        old_branch=pr_action_result.get("old_branch_to_delete"),
                        new_pr_number=pr_result.get("number")
                    )
                    # Mark old workflow as closed
                    if pr_action_result.get("old_workflow_id"):
                        try:
                            await self.gitops_workflow_repository.update_pr_status(
                                workflow_id=pr_action_result.get("old_workflow_id"),
                                new_status=PRStatusEnum.PR_CLOSED
                            )
                        except Exception as e:
                            logger.warning(f"Failed to update old workflow status: {e}")

                # === Secondary PR Creation (Aspora Strategy) ===
                # Create secondary PR for qa/sandbox/stage-env-copy if flag enabled
                secondary_pr_result = None
                if routing_info["secondary_pr_enabled"] and routing_info["secondary_pr_target"]:
                    secondary_target = routing_info["secondary_pr_target"]
                    logger.info(f"Creating secondary PR to {secondary_target}")

                    secondary_pr_result = await self._create_secondary_pr(
                        yaml_content=yaml_content,
                        github_repository=github_repository,
                        secondary_target=secondary_target,
                        service_name=service_name,
                        environment=environment,
                        github_token=github_token,
                        primary_pr_number=pr_result.get("number"),
                        file_path=file_path
                    )

                    if secondary_pr_result:
                        logger.info(f"Secondary PR created: #{secondary_pr_result.get('pr_number')}")
                    else:
                        logger.warning(f"Failed to create secondary PR to {secondary_target}")

                return {
                    "commit_sha": result.get("commit_sha"),
                    "file_path": file_path,
                    "commit_url": result.get("commit_url"),
                    "html_url": result.get("html_url"),
                    "feature_branch": feature_branch,
                    "base_branch": base_branch,
                    "pr_number": pr_result.get("number"),
                    "pr_url": pr_result.get("html_url"),
                    "pr_state": pr_result.get("state"),
                    "success": True,
                    # Include secondary PR info if created
                    "secondary_pr": secondary_pr_result
                }

        except Exception as e:
            logger.error(f"Failed to commit workflow file to GitHub: {str(e)}", exc_info=True)
            return None

    async def _get_repo_branch_names(
        self,
        github_token: str,
        owner: str,
        repo: str
    ) -> list:
        """
        Fetch all branch names from repository for strategy decision.

        Args:
            github_token: GitHub token
            owner: Repository owner
            repo: Repository name

        Returns:
            List of branch names
        """
        from app.integrations.github_integration import GitHubIntegration

        try:
            branches_result = await GitHubIntegration.fetch_repository_branches(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo
            )

            branches = branches_result.get("branches", [])
            branch_names = [b.get("name", "") for b in branches]
            logger.info(f"Fetched {len(branch_names)} branches for PR strategy decision")
            return branch_names

        except Exception as e:
            logger.warning(f"Failed to fetch branches for strategy: {e}, returning empty list")
            return []

    async def _create_secondary_pr(
        self,
        yaml_content: str,
        github_repository: str,
        secondary_target: str,
        service_name: str,
        environment: str,
        github_token: str,
        primary_pr_number: int,
        file_path: str
    ) -> Optional[Dict[str, Any]]:
        """
        Create secondary PR to qa/sandbox/stage-env-copy.

        For Aspora strategy: when user selects qa/sandbox/stage-env-copy and enables
        secondary PR flag, we create an additional PR to that branch.

        Args:
            yaml_content: Generated YAML content
            github_repository: Repository in format "owner/repo"
            secondary_target: Target branch (qa, sandbox, or stage-env-copy)
            service_name: Service name
            environment: Environment
            github_token: GitHub token
            primary_pr_number: Primary PR number for reference
            file_path: File path in repository

        Returns:
            Dict with secondary PR info or None if failed
        """
        from app.integrations.github_integration import GitHubIntegration
        import re

        try:
            owner, repo = github_repository.split("/", 1)

            # Sanitize service name for branch
            service_name_sanitized = re.sub(r'[\s_-]+', '-', service_name.strip()).strip('-').lower()

            # Create unique feature branch for secondary PR
            secondary_branch_name = f"pipeline/{service_name_sanitized}-{environment}-{secondary_target}"
            secondary_feature_branch = await self._generate_unique_branch_name(
                github_token=github_token,
                owner=owner,
                repo=repo,
                base_name=secondary_branch_name
            )

            logger.info(f"Creating secondary feature branch: {secondary_feature_branch} from {secondary_target}")

            # Create feature branch from secondary target
            branch_result = await GitHubIntegration.create_branch(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch_name=secondary_feature_branch,
                from_branch=secondary_target
            )

            if not branch_result:
                logger.error(f"Failed to create secondary feature branch from {secondary_target}")
                return None

            # Commit file to secondary feature branch
            commit_message = f"[Secondary] Add workflow for {service_name} ({environment}) - refs PR #{primary_pr_number}"
            result = await GitHubIntegration.update_or_create_file(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch=secondary_feature_branch,
                file_path=file_path,
                content=yaml_content,
                message=commit_message
            )

            if not result:
                logger.error("Failed to commit file to secondary feature branch")
                return None

            # Create secondary PR
            pr_title = f"[Secondary] Workflow for {service_name} - {environment} (refs #{primary_pr_number})"
            pr_body = f"""## Secondary PR - Pipeline Deployment Workflow

**Service:** `{service_name}`
**Environment:** {environment}
**Target Branch:** {secondary_target}

> **Note:** This is a secondary PR. The primary PR is #{primary_pr_number} targeting pre-prod/stage-env.

### File Path
`{file_path}`

### Changes
- Added GitHub Actions deployment workflow for `{service_name}`
- Workflow triggers on push to `{secondary_target}` branch

---
*Generated by {settings.app_name}*"""

            pr_result = await GitHubIntegration.create_pull_request(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                head=secondary_feature_branch,
                base=secondary_target,
                title=pr_title,
                body=pr_body,
                draft=False
            )

            logger.info(f"Secondary PR created: #{pr_result.get('number')}")

            return {
                "pr_number": pr_result.get("number"),
                "pr_url": pr_result.get("html_url"),
                "feature_branch": secondary_feature_branch,
                "base_branch": secondary_target,
                "commit_sha": result.get("commit_sha"),
                "success": True
            }

        except Exception as e:
            logger.error(f"Failed to create secondary PR: {str(e)}", exc_info=True)
            return None

    async def _commit_file_to_github(
        self,
        file_content: str,
        file_path: str,
        github_repository: str,
        branch_name: str,
        commit_message: str,
        github_pat: str
    ) -> Optional[Dict[str, Any]]:
        """
        Commit a file to GitHub repository (creates versioned files if exists).

        Args:
            file_content: File content to commit
            file_path: Path where file should be created in repo (e.g., "deployment/task-definition.json")
            github_repository: Repository in format "owner/repo"
            branch_name: Target branch
            commit_message: Commit message
            github_pat: GitHub Personal Access Token

        Returns:
            Dict with commit info or None if failed
        """
        from app.integrations.github_integration import GitHubIntegration
        from app.core.config import settings

        try:
            # Parse owner and repo from github_repository
            if "/" not in github_repository:
                logger.error(f"Invalid GitHub repository format: {github_repository}. Expected 'owner/repo'")
                return None

            owner, repo = github_repository.split("/", 1)

            logger.info(f"Committing file to GitHub: {github_repository}/{file_path}")

            # Commit file to GitHub
            result = await GitHubIntegration.commit_workflow_file(
                token=github_pat,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch=branch_name,
                file_path=file_path,
                content=file_content,
                message=commit_message
            )

            logger.info(f"Successfully committed file: {result['file_path']}")
            return result

        except Exception as e:
            logger.error(f"Failed to commit file to GitHub: {str(e)}", exc_info=True)
            return None

    async def _update_or_create_file_in_github(
        self,
        file_content: str,
        file_path: str,
        github_repository: str,
        branch_name: str,
        commit_message: str,
        github_pat: str
    ) -> Optional[Dict[str, Any]]:
        """
        Update an existing file or create it in GitHub repository (overwrites if exists).

        Args:
            file_content: File content to commit
            file_path: Path where file should be created/updated in repo
            github_repository: Repository in format "owner/repo"
            branch_name: Target branch
            commit_message: Commit message
            github_pat: GitHub Personal Access Token

        Returns:
            Dict with commit info or None if failed
        """
        from app.integrations.github_integration import GitHubIntegration
        from app.core.config import settings

        try:
            # Parse owner and repo from github_repository
            if "/" not in github_repository:
                logger.error(f"Invalid GitHub repository format: {github_repository}. Expected 'owner/repo'")
                return None

            owner, repo = github_repository.split("/", 1)

            logger.info(f"Updating/creating file in GitHub: {github_repository}/{file_path}")

            # Update or create file in GitHub
            result = await GitHubIntegration.update_or_create_file(
                token=github_pat,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch=branch_name,
                file_path=file_path,
                content=file_content,
                message=commit_message
            )

            logger.info(f"Successfully updated/created file: {result['file_path']}")
            return result

        except Exception as e:
            logger.error(f"Failed to update/create file in GitHub: {str(e)}", exc_info=True)
            return None

    async def _commit_pipeline_files_batch(
        self,
        yaml_content: str,
        dockerfile_content: str,
        github_repository: str,
        branch_name: str,
        service_name: str,
        environment: str,
        github_pat: str,
        folder: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Commit workflow YAML and Dockerfile in a SINGLE batch commit.
        This prevents triggering existing workflows multiple times.

        Args:
            yaml_content: Generated YAML workflow content
            dockerfile_content: Generated Dockerfile content
            github_repository: Repository in format "owner/repo"
            branch_name: Target branch
            service_name: Service name
            environment: Environment
            github_pat: GitHub Personal Access Token
            folder: Optional folder path within repo (Dockerfile will be placed here)

        Returns:
            Dict with commit info (commit_sha, file_path, commit_url) or None if failed
        """
        from app.integrations.github_integration import GitHubIntegration
        from app.core.config import settings

        try:
            # Parse owner and repo from github_repository
            if "/" not in github_repository:
                logger.error(f"Invalid GitHub repository format: {github_repository}. Expected 'owner/repo'")
                return None

            owner, repo = github_repository.split("/", 1)

            # Prepare list of files to commit
            files_to_commit = []

            # 1. Always include the workflow YAML file
            workflow_filename = generate_workflow_filename(service_name, environment)
            workflow_file_path = f".github/workflows/{workflow_filename}"
            files_to_commit.append({
                "path": workflow_file_path,
                "content": yaml_content
            })

            # 2. Always include the Dockerfile
            # Place Dockerfile in folder if specified, otherwise at root
            dockerfile_path = f"{folder}/Dockerfile" if folder else "Dockerfile"
            files_to_commit.append({
                "path": dockerfile_path,
                "content": dockerfile_content
            })

            # Generate commit message
            if len(files_to_commit) == 1:
                commit_message = generate_commit_message(service_name, environment)
            else:
                commit_message = f"Add CI/CD pipeline for {service_name} ({environment})"

            logger.info(f"Batch committing {len(files_to_commit)} file(s) to GitHub: {github_repository}")
            for file in files_to_commit:
                logger.info(f"  - {file['path']}")

            # Commit all files in a single batch
            result = await GitHubIntegration.commit_multiple_files(
                token=github_pat,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch=branch_name,
                files=files_to_commit,
                message=commit_message
            )

            logger.info(f"Successfully batch committed {len(files_to_commit)} files: {result['commit_sha']}")

            # Return result with workflow file path for backward compatibility
            return {
                "commit_sha": result["commit_sha"],
                "file_path": workflow_file_path,  # Primary workflow file path
                "commit_url": result["commit_url"],
                "files_committed": result["files_committed"]
            }

        except Exception as e:
            logger.error(f"Failed to batch commit files to GitHub: {str(e)}", exc_info=True)
            return None

    async def run_pipeline(
        self,
        pipeline_code: str,
        user_code: str = None,
        user_email: str = None
    ) -> Dict[str, Any]:
        """
        Trigger a pipeline run via GitHub workflow_dispatch API.

        This method:
        1. Fetches the pipeline by code
        2. Gets the workflow_file_path from deployment_config
        3. Creates a PipelineRunTrack record with PENDING status
        4. Triggers the workflow via GitHub workflow_dispatch API
        5. Polls to find the triggered workflow run
        6. Starts background polling for status updates

        Args:
            pipeline_code: Unique code of the pipeline to run
            user_code: User code (from JWT) for tracking
            user_email: User email to pass to workflow_dispatch

        Returns:
            Dict with run details:
            - status: success/error
            - message: Descriptive message
            - run_code: Unique code for the run
            - run_status: Pipeline run status
            - log_url: URL to view the workflow run
            - error_message: Error message (if failed)

        Raises:
            HTTPException: If pipeline not found or validation fails
        """
        from app.repository.pipeline_mst_repository import PipelineMstRepository
        from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
        from app.integrations.github_integration import GitHubIntegration
        from app.core.config import settings
        from datetime import datetime
        import asyncio

        pipeline_repo = PipelineMstRepository(self.db)
        run_track_repo = PipelineRunTrackRepository(self.db)

        # Step 1: Fetch pipeline by code
        logger.info(f"Fetching pipeline with code: {pipeline_code}")
        pipeline = await pipeline_repo.get_by_code(pipeline_code)

        if not pipeline:
            logger.error(f"Pipeline not found: {pipeline_code}")
            raise HTTPException(
                status_code=404,
                detail=f"Pipeline with code '{pipeline_code}' not found"
            )

        if not pipeline.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"Pipeline '{pipeline_code}' is not active"
            )

        # Step 2: Get workflow_file_path from deployment_config
        deployment_config = pipeline.deployment_config or {}
        workflow_file_path = deployment_config.get("workflow_file_path")

        if not workflow_file_path:
            # Fallback: Try to generate from service name + environment
            # This handles pipelines created before workflow_file_path was stored
            logger.warning(f"workflow_file_path not found in deployment_config for pipeline {pipeline_code}")
            raise HTTPException(
                status_code=400,
                detail=f"Pipeline '{pipeline_code}' does not have workflow_file_path configured. Please update the pipeline or use save_pipeline with workflow_file_path."
            )

        logger.info(f"Using workflow file: {workflow_file_path}")

        # Step 3: Generate unique run code
        run_code = generate_run_code(pipeline_code)
        logger.info(f"Generated run code: {run_code}")

        # Step 4: Create PipelineRunTrack record FIRST with PENDING status
        logger.info(f"Creating pipeline run track record: {run_code}")
        run_track = await run_track_repo.create(
            pipeline_mst_code=pipeline_code,
            code=run_code,
            status=PipelineRunStatusEnum.PENDING,
            log_url=None,
            commit_sha=None,
            github_run_id=None,
            error_message=None
        )
        await self.db.commit()

        # Step 5: Trigger workflow via workflow_dispatch
        try:
            # Extract repo owner and name from repo_url
            owner, repo = parse_repo_url(pipeline.repo_url)

            logger.info(f"Triggering pipeline via workflow_dispatch for {owner}/{repo}, workflow: {workflow_file_path}, branch: {pipeline.repo_branch}")

            # Get GitHub token for the org
            github_token = await self._get_github_token(owner)
            if not github_token:
                # Fallback to PAT from pipeline_vendor
                github_token = await self._get_github_token_for_pipeline(pipeline_code)

            # Pre-flight check: Fetch workflow file and validate required fields
            file_result = await GitHubIntegration.get_file_content(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                file_path=workflow_file_path,
                branch=pipeline.repo_branch
            )

            if file_result and file_result.get("exists"):
                workflow_content = file_result["content"]
                missing_fields = []

                if "workflow_dispatch:" not in workflow_content:
                    missing_fields.append("workflow_dispatch")
                if "triggered_by_user:" not in workflow_content:
                    missing_fields.append("triggered_by_user input")
                if "run-name:" not in workflow_content:
                    missing_fields.append("run-name")

                if missing_fields:
                    error_msg = f"Workflow is missing required fields: {', '.join(missing_fields)}. Use add_workflow_dispatch API to update the workflow."
                    # Update run track with error
                    await run_track_repo.update(
                        code=run_code,
                        status=PipelineRunStatusEnum.FAILED,
                        error_message=error_msg
                    )
                    await self.db.commit()

                    return {
                        "status": "error",
                        "message": f"Failed to trigger pipeline: {error_msg}",
                        "run_code": run_code,
                        "run_status": PipelineRunStatusEnum.FAILED.value,
                        "workflow_file": workflow_file_path,
                        "log_url": None,
                        "github_run_id": None,
                        "error_message": error_msg
                    }

            # Prepare workflow inputs for workflow_dispatch
            inputs = {}
            if user_email:
                inputs["triggered_by_user"] = user_email

            # Trigger workflow dispatch
            dispatch_result = await GitHubIntegration.trigger_workflow_dispatch(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                workflow_file=workflow_file_path,
                ref=pipeline.repo_branch,
                inputs=inputs if inputs else None
            )

            logger.info(f"Workflow dispatch triggered: {dispatch_result.get('message')}")

            # Step 6: Try to fetch the triggered workflow run (with retry)
            # workflow_dispatch doesn't return the run ID, so we need to poll for it
            workflow_run_url = None
            github_run_id = None
            run_status = PipelineRunStatusEnum.PENDING
            head_sha = None

            for attempt in range(5):  # Try 5 times with increasing delays
                if attempt > 0:
                    await asyncio.sleep(2 * attempt)  # 2s, 4s, 6s, 8s delays

                workflow_run = await GitHubIntegration.get_latest_workflow_run(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    workflow_file=workflow_file_path,
                    branch=pipeline.repo_branch
                )

                if workflow_run:
                    # Check if this is a recent run (within last 30 seconds)
                    created_at = workflow_run.get("created_at")
                    if created_at:
                        from datetime import datetime, timezone
                        run_created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        age_seconds = (now - run_created).total_seconds()

                        if age_seconds < 60:  # Run created within last 60 seconds
                            workflow_run_url = workflow_run.get("run_url")
                            github_run_id = workflow_run.get("run_id")
                            head_sha = workflow_run.get("head_sha")
                            workflow_status = workflow_run.get("status")
                            conclusion = workflow_run.get("conclusion")
                            logger.info(f"Found workflow run: {github_run_id} at {workflow_run_url}, status: {workflow_status}, age: {age_seconds}s")

                            # Map GitHub workflow status to our pipeline run status
                            run_status = map_github_status(workflow_status, conclusion)
                            break
                        else:
                            logger.info(f"Found workflow run but it's too old ({age_seconds}s), waiting for new run...")
                    else:
                        # No created_at, assume it's the right run
                        workflow_run_url = workflow_run.get("run_url")
                        github_run_id = workflow_run.get("run_id")
                        head_sha = workflow_run.get("head_sha")
                        break
                else:
                    logger.info(f"Workflow run not found yet (attempt {attempt + 1}/5)")

            # Step 7: Update run track with workflow details
            await run_track_repo.update(
                code=run_code,
                commit_sha=head_sha,
                log_url=workflow_run_url,
                github_run_id=github_run_id,
                status=run_status
            )
            await self.db.commit()

            # DISABLED: Pipeline polling causes connection pool exhaustion (holds connections for 60+ min)
            # TODO: Fix connection leak before re-enabling
            # if github_run_id and head_sha:
            #     start_pipeline_polling(
            #         run_code=run_code,
            #         pipeline_code=pipeline_code,
            #         commit_sha=head_sha,
            #         owner=owner,
            #         repo=repo,
            #         db_session=self.db,
            #         github_token=github_token
            #     )
            #     logger.info(f"Started background polling for run: {run_code}")

            return {
                "status": "success",
                "message": "Pipeline run triggered successfully via workflow_dispatch",
                "run_code": run_code,
                "run_status": run_status.value,
                "workflow_file": workflow_file_path,
                "log_url": workflow_run_url,
                "github_run_id": github_run_id,
                "error_message": None
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to trigger pipeline: {error_msg}", exc_info=True)

            # Update run track with error
            await run_track_repo.update(
                code=run_code,
                status=PipelineRunStatusEnum.FAILED,
                error_message=error_msg
            )
            await self.db.commit()

            return {
                "status": "error",
                "message": f"Failed to trigger pipeline: {error_msg}",
                "run_code": run_code,
                "run_status": PipelineRunStatusEnum.FAILED.value,
                "workflow_file": workflow_file_path,
                "log_url": None,
                "github_run_id": None,
                "error_message": error_msg
            }

    async def get_pipelines_by_service(
        self,
        transaction_code: str,
        table_name: str = "SERVICE_CONFIG",
        skip: int = 0,
        limit: int = 100,
        geo_loc_mst_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get all pipelines for a source entity with related data.

        This method fetches pipelines with JOINed data from:
        - pipeline_vendor_mst (for pipeline_agent_enum)
        - language_ref (for language name)

        Args:
            transaction_code: Source entity code (service_config.code or infrastructure_mst.code)
            skip: Number of records to skip for pagination
            limit: Maximum number of records to return
            geo_loc_mst_code: Optional filter by geographic location

        Returns:
            Dict containing:
            - total: Total count of pipelines
            - pipelines: List of pipeline detail dictionaries

        Raises:
            HTTPException: If validation fails or database error occurs
        """
        try:
            logger.info(f"Fetching pipelines for transaction_code: {transaction_code}")

            # Call repository method
            result = await self.pipeline_repo.get_pipelines_by_transaction_code(
                transaction_code=transaction_code,
                table_name=table_name,
                skip=skip,
                limit=limit,
                geo_loc_mst_code=geo_loc_mst_code,
            )

            logger.info(f"Found {result['total']} pipelines for {transaction_code}")
            return result

        except Exception as e:
            logger.error(f"Error fetching pipelines for transaction_code {transaction_code}: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch pipelines: {str(e)}"
            )

    async def get_pipeline_run_history(
        self,
        pipeline_mst_code: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        resource_transaction_code: Optional[str] = None,
        resource_table_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get pipeline run history with optional filtering by pipeline code.

        Args:
            pipeline_mst_code: Optional pipeline code to filter runs (None = all runs)
            skip: Number of records to skip for pagination
            limit: Maximum number of records to return
            resource_transaction_code: Optional — narrows to runs linked to this
                source entity via transaction_queue_code JSONB (used for shared
                infra-apply pipelines).
            resource_table_name: Source table discriminator paired with
                ``resource_transaction_code``.

        Returns:
            Dict containing:
            - total: Total count of pipeline runs
            - runs: List of run history dictionaries

        Raises:
            HTTPException: If database error occurs
        """
        try:
            logger.info(f"Fetching pipeline run history" + (f" for pipeline: {pipeline_mst_code}" if pipeline_mst_code else " (all pipelines)"))

            # Initialize run track repository
            from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
            run_track_repo = PipelineRunTrackRepository(self.db)

            # Call repository method
            result = await run_track_repo.get_run_history(
                pipeline_mst_code=pipeline_mst_code,
                skip=skip,
                limit=limit,
                resource_transaction_code=resource_transaction_code,
                resource_table_name=resource_table_name,
            )

            # Transform model instances to dictionaries
            runs_data = []
            for run in result["runs"]:
                runs_data.append({
                    "id": run.id,
                    "pipeline_mst_code": run.pipeline_mst_code,
                    "code": run.code,
                    "status": run.status.value if hasattr(run.status, 'value') else run.status,
                    "log_url": run.log_url,
                    "created_at": run.created_at,
                    "updated_at": run.updated_at,
                    "commit_sha": run.commit_sha,
                    "github_run_id": run.github_run_id,
                    "error_message": run.error_message,
                    "build_number": run.build_number,
                    "build_stages": run.build_stages,
                    "deploy_result": run.deploy_result,
                })

            logger.info(f"Found {result['total']} pipeline runs")
            return {
                "total": result["total"],
                "runs": runs_data
            }

        except Exception as e:
            logger.error(f"Error fetching pipeline run history: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch pipeline run history: {str(e)}"
            )

    async def add_workflow_dispatch(
        self,
        pipeline_code: str,
        tenant_code: str = None,
        user_code: str = None
    ) -> Dict[str, Any]:
        """
        Add workflow_dispatch trigger to an existing pipeline's workflow file.

        Steps:
        1. Fetch pipeline from DB
        2. Get GitHub token via hierarchy lookup
        3. Fetch workflow file content from GitHub
        4. Check if already has workflow_dispatch
        5. Insert workflow_dispatch: into the on: block
        6. Create feature branch and commit
        7. Create PR

        Args:
            pipeline_code: Pipeline code to update
            tenant_code: Tenant code for authorization
            user_code: User code for audit

        Returns:
            Dict with status, pr_url, pr_number, etc.
        """
        from app.integrations.github_integration import GitHubIntegration
        import re

        try:
            # Step 1: Get pipeline from DB
            pipeline = await self.pipeline_repo.get_by_code(pipeline_code)
            if not pipeline:
                return {
                    "status": "error",
                    "message": f"Pipeline not found: {pipeline_code}",
                    "pipeline_code": pipeline_code
                }

            # Get workflow_file_path from deployment_config
            deployment_config = pipeline.deployment_config or {}
            workflow_file_path = deployment_config.get("workflow_file_path")

            if not workflow_file_path:
                return {
                    "status": "error",
                    "message": "Pipeline does not have a workflow file path configured",
                    "pipeline_code": pipeline_code
                }

            repo_url = pipeline.repo_url
            repo_branch = pipeline.repo_branch

            # Resolve service_config from pipeline.transaction_code, then extract service + environment
            service_config = await self.service_config_repo.get_by_code(pipeline.transaction_code)
            service = await self.service_repo.get_by_code(service_config.services_mst_code) if service_config else None
            service_name = service.name if service else "unknown"
            environment = service_config.environment.value if service_config and hasattr(service_config.environment, 'value') else str(service_config.environment) if service_config else "unknown"
    
            # Parse owner/repo from repo_url
            owner, repo = parse_repo_url(repo_url)
            
            # Step 2: Get GitHub token for the org
            github_token = await self._get_github_token(owner)
            if not github_token:
                return {
                    "status": "error",
                    "message": "GitHub token not configured",
                    "pipeline_code": pipeline_code
                }

            # Step 3: Fetch workflow file content
            file_result = await GitHubIntegration.get_file_content(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                file_path=workflow_file_path,
                branch=repo_branch
            )

            if not file_result or not file_result.get("exists"):
                return {
                    "status": "error",
                    "message": f"Workflow file not found: {workflow_file_path}",
                    "pipeline_code": pipeline_code
                }

            original_content = file_result["content"]

            # Step 4: Check what needs to be added
            needs_workflow_dispatch = "workflow_dispatch:" not in original_content
            needs_triggered_by_user = "triggered_by_user:" not in original_content
            needs_run_name = "run-name:" not in original_content

            # If nothing needs to be added, return already_exists
            if not needs_workflow_dispatch and not needs_triggered_by_user and not needs_run_name:
                return {
                    "status": "already_exists",
                    "message": "Workflow already has all required fields (workflow_dispatch, triggered_by_user, run-name)",
                    "pipeline_code": pipeline_code
                }

            # Step 5: Apply necessary modifications
            updated_content = original_content

            # Add workflow_dispatch with triggered_by_user if needed
            if needs_workflow_dispatch:
                updated_content = self._insert_workflow_dispatch(updated_content)
            elif needs_triggered_by_user:
                # workflow_dispatch exists but no triggered_by_user input
                updated_content = self._insert_triggered_by_user_input(updated_content)

            # Add run-name if needed
            if needs_run_name:
                updated_content = self._insert_run_name(updated_content, service_name, environment)

            if updated_content == original_content:
                return {
                    "status": "error",
                    "message": "Could not find appropriate location to insert required fields",
                    "pipeline_code": pipeline_code
                }

            # Step 6: Create feature branch name with versioning
            service_name_sanitized = re.sub(r'[\s_-]+', '-', service_name.strip()).strip('-').lower()

            # Generate unique branch name by checking existing branches
            base_branch_name = f"workflow-dispatch/{service_name_sanitized}-{environment}"
            feature_branch = await self._generate_unique_branch_name(
                github_token=github_token,
                owner=owner,
                repo=repo,
                base_name=base_branch_name
            )

            # Create feature branch from base branch
            branch_result = await GitHubIntegration.create_branch(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch_name=feature_branch,
                from_branch=repo_branch
            )

            # Step 7: Commit updated file
            commit_message = f"Add workflow_dispatch trigger for {service_name} ({environment})"

            commit_result = await GitHubIntegration.update_or_create_file(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                branch=feature_branch,
                file_path=workflow_file_path,
                content=updated_content,
                message=commit_message
            )

            # Step 8: Check for existing PR or create new one
            existing_pr = await GitHubIntegration.find_open_pr(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                head=feature_branch,
                base=repo_branch
            )

            if existing_pr:
                # Create GitOps workflow detail for existing PR update
                gitops_workflow_id = None
                try:
                    workflow_data = make_gitops_workflow_detail(
                        git_repository=f"{owner}/{repo}",
                        git_branch=feature_branch,
                        git_commit_sha=commit_result.get("commit_sha", ""),
                        pr_number=existing_pr.get("number"),
                        pr_url=existing_pr.get("html_url", ""),
                        tenant_mst_code=tenant_code,
                        user_mst_code=user_code,
                        workflow_name=f"Add workflow_dispatch: {service_name} ({environment})",
                        transaction_code=pipeline_code,
                        table_name=WorkflowSourceTableEnum.PIPELINE
                    )
                    workflow = await self.gitops_workflow_repository.create(**workflow_data)
                    gitops_workflow_id = workflow.id

                    # Update the pipeline with the new gitops_workflow_id
                    await self.pipeline_repo.update(
                        db_obj=pipeline,
                        updates={"gitops_workflow_id": gitops_workflow_id}
                    )
                    await self.db.commit()

                    logger.info(f"GitOps workflow created: {workflow.code}, PR #{existing_pr.get('number')}")
                except Exception as e:
                    logger.error(f"Failed to create GitOps workflow tracking: {e}")
                    # Non-fatal - continue with success response

                return {
                    "status": "success",
                    "message": f"Updated existing PR #{existing_pr.get('number')}",
                    "pipeline_code": pipeline_code,
                    "pr_url": existing_pr.get("html_url"),
                    "pr_number": existing_pr.get("number"),
                    "commit_sha": commit_result.get("commit_sha"),
                    "feature_branch": feature_branch,
                    "gitops_workflow_id": gitops_workflow_id
                }

            # Create new PR
            pr_title = f"[Pipeline] Add workflow_dispatch trigger for {service_name} - {environment}"
            pr_body = f"""## Enable Manual Pipeline Trigger

**Service:** `{service_name}`
**Environment:** {environment}

### Changes
- Added `workflow_dispatch:` trigger to enable manual runs via GitHub Actions UI

### How to Use
After merging this PR, you can manually trigger the workflow from:
- GitHub Actions tab → Select the workflow → "Run workflow" button

---
*Generated by {settings.app_name}*"""

            pr_result = await GitHubIntegration.create_pull_request(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                head=feature_branch,
                base=repo_branch,
                title=pr_title,
                body=pr_body,
                draft=False
            )

            logger.info(f"Created PR #{pr_result.get('number')} to add workflow_dispatch for pipeline {pipeline_code}")

            # Create GitOps workflow detail for new PR
            gitops_workflow_id = None
            if pr_result.get("number"):
                try:
                    workflow_data = make_gitops_workflow_detail(
                        git_repository=f"{owner}/{repo}",
                        git_branch=feature_branch,
                        git_commit_sha=commit_result.get("commit_sha", ""),
                        pr_number=pr_result.get("number"),
                        pr_url=pr_result.get("html_url", ""),
                        tenant_mst_code=tenant_code,
                        user_mst_code=user_code,
                        workflow_name=f"Add workflow_dispatch: {service_name} ({environment})",
                        transaction_code=pipeline_code,
                        table_name=WorkflowSourceTableEnum.PIPELINE
                    )
                    workflow = await self.gitops_workflow_repository.create(**workflow_data)
                    gitops_workflow_id = workflow.id

                    # Update the pipeline with the new gitops_workflow_id
                    await self.pipeline_repo.update(
                        db_obj=pipeline,
                        updates={"gitops_workflow_id": gitops_workflow_id}
                    )
                    await self.db.commit()

                    logger.info(f"GitOps workflow created: {workflow.code}, PR #{pr_result.get('number')}")
                except Exception as e:
                    logger.error(f"Failed to create GitOps workflow tracking: {e}")
                    # Non-fatal - continue with success response

            return {
                "status": "success",
                "message": f"Created PR #{pr_result.get('number')} to add workflow_dispatch trigger",
                "pipeline_code": pipeline_code,
                "pr_url": pr_result.get("html_url"),
                "pr_number": pr_result.get("number"),
                "commit_sha": commit_result.get("commit_sha"),
                "feature_branch": feature_branch,
                "gitops_workflow_id": gitops_workflow_id
            }

        except Exception as e:
            logger.error(f"Failed to add workflow_dispatch for pipeline {pipeline_code}: {str(e)}", exc_info=True)
            return {
                "status": "error",
                "message": f"Failed to add workflow_dispatch: {str(e)}",
                "pipeline_code": pipeline_code
            }

    def _insert_workflow_dispatch(self, yaml_content: str) -> str:
        """
        Insert workflow_dispatch: trigger into the on: block of a GitHub Actions YAML file.

        Strategy:
        1. Find the 'on:' line
        2. Find the next top-level key after 'on:' (permissions:, env:, jobs:, etc.)
        3. Insert '  workflow_dispatch:' (2-space indent) just before that line

        Args:
            yaml_content: Original YAML content

        Returns:
            Updated YAML content with workflow_dispatch added, or original if not found
        """
        lines = yaml_content.split('\n')
        result_lines = []

        found_on_block = False
        on_block_indent = 0
        insertion_index = -1

        # Top-level keys that signal end of 'on:' block
        top_level_keys = ['permissions:', 'env:', 'jobs:', 'defaults:', 'concurrency:', 'run-name:']

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Find the 'on:' line (at root level, no leading spaces)
            if stripped == 'on:' and not line.startswith(' ') and not line.startswith('\t'):
                found_on_block = True
                on_block_indent = len(line) - len(line.lstrip())
                result_lines.append(line)
                continue

            # If we're inside the 'on:' block, look for the end
            if found_on_block:
                current_indent = len(line) - len(line.lstrip())

                # Check if this is a top-level key (same indent as 'on:')
                if stripped and current_indent <= on_block_indent:
                    for key in top_level_keys:
                        if stripped.startswith(key) or stripped == key.rstrip(':') + ':':
                            # Insert workflow_dispatch before this line
                            insertion_index = len(result_lines)
                            found_on_block = False
                            break

            result_lines.append(line)

        # If we found where to insert, do it
        if insertion_index > 0:
            # Insert workflow_dispatch with triggered_by_user input (standard for GitHub Actions)
            workflow_dispatch_block = [
                '  workflow_dispatch:',
                '    inputs:',
                '      triggered_by_user:',
                '        description: "Email or name of the user triggering this workflow"',
                '        required: true'
            ]
            for i, line in enumerate(workflow_dispatch_block):
                result_lines.insert(insertion_index + i, line)
            return '\n'.join(result_lines)

        # Fallback: If on: block found but no clear end, insert after on: block content
        # Find last line that's indented under on:
        if 'on:' in yaml_content:
            lines = yaml_content.split('\n')
            for i, line in enumerate(lines):
                if line.strip() == 'on:':
                    # Find the last line of the on: block
                    for j in range(i + 1, len(lines)):
                        current = lines[j]
                        if current.strip() and not current.startswith(' ') and not current.startswith('\t'):
                            # Insert workflow_dispatch block before this line
                            workflow_dispatch_block = [
                                '  workflow_dispatch:',
                                '    inputs:',
                                '      triggered_by_user:',
                                '        description: "Email or name of the user triggering this workflow"',
                                '        required: true'
                            ]
                            for idx, wf_line in enumerate(workflow_dispatch_block):
                                lines.insert(j + idx, wf_line)
                            return '\n'.join(lines)
                    break

        return yaml_content  # Return unchanged if can't find insertion point

    def _insert_triggered_by_user_input(self, yaml_content: str) -> str:
        """
        Insert triggered_by_user input under existing workflow_dispatch.

        For cases where workflow_dispatch exists but doesn't have the triggered_by_user input.

        Args:
            yaml_content: Original YAML content with workflow_dispatch

        Returns:
            Updated YAML content with triggered_by_user input added
        """
        lines = yaml_content.split('\n')
        result_lines = []

        for i, line in enumerate(lines):
            result_lines.append(line)
            stripped = line.strip()

            # Find workflow_dispatch: line
            if stripped == 'workflow_dispatch:':
                # Check if next line already has inputs:
                if i + 1 < len(lines) and 'inputs:' in lines[i + 1]:
                    # inputs: exists, we need to add the triggered_by_user under it
                    continue
                else:
                    # No inputs: yet, add the full inputs block
                    input_block = [
                        '    inputs:',
                        '      triggered_by_user:',
                        '        description: "Email or name of the user triggering this workflow"',
                        '        required: true'
                    ]
                    result_lines.extend(input_block)

            # If we find inputs: under workflow_dispatch, add triggered_by_user
            elif stripped == 'inputs:' and i > 0:
                # Check if previous context suggests this is under workflow_dispatch
                prev_lines = lines[max(0, i-5):i]
                is_under_workflow_dispatch = any('workflow_dispatch:' in pl for pl in prev_lines)

                if is_under_workflow_dispatch:
                    # Add triggered_by_user input after inputs:
                    triggered_by_user_block = [
                        '      triggered_by_user:',
                        '        description: "Email or name of the user triggering this workflow"',
                        '        required: true'
                    ]
                    result_lines.extend(triggered_by_user_block)

        return '\n'.join(result_lines)

    def _insert_run_name(self, yaml_content: str, service_name: str, environment: str) -> str:
        """
        Insert run-name after the name: line in a GitHub Actions YAML file.

        Args:
            yaml_content: Original YAML content
            service_name: Service name for the run-name template
            environment: Environment for the run-name template

        Returns:
            Updated YAML content with run-name added
        """
        lines = yaml_content.split('\n')
        result_lines = []

        for i, line in enumerate(lines):
            result_lines.append(line)
            stripped = line.strip()

            # Find the name: line at root level (not indented)
            if stripped.startswith('name:') and not line.startswith(' ') and not line.startswith('\t'):
                # Insert run-name after the name: line (conditional format)
                result_lines.append("run-name: >-")
                result_lines.append("  ${{ github.event.inputs.triggered_by_user != '' &&")
                result_lines.append(f"      format('Deploy {service_name} to {environment} (requested by {{0}})', github.event.inputs.triggered_by_user)")
                result_lines.append("      ||")
                result_lines.append(f"      format('Deploy {service_name} to {environment}')")
                result_lines.append("  }}")

        return '\n'.join(result_lines)

    async def _generate_unique_branch_name(
        self,
        github_token: str,
        owner: str,
        repo: str,
        base_name: str
    ) -> str:
        """
        Generate a unique branch name by appending a version number.

        Fetches all branches from the repository and finds the next available
        version number for the given base name pattern.

        Branch naming format: {base_name}-{version}
        Example: workflow-dispatch/my-service-dev-1, workflow-dispatch/my-service-dev-2

        Args:
            github_token: GitHub token for API access
            owner: Repository owner
            repo: Repository name
            base_name: Base branch name (e.g., "workflow-dispatch/my-service-dev")

        Returns:
            Unique branch name with version number (e.g., "workflow-dispatch/my-service-dev-1")
        """
        from app.integrations.github_integration import GitHubIntegration
        import re

        try:
            # Fetch all branches from the repository
            branches_result = await GitHubIntegration.fetch_repository_branches(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo
            )

            branches = branches_result.get("branches", [])
            branch_names = [b.get("name", "") for b in branches]

            # Find all branches matching the pattern {base_name}-{number}
            pattern = re.compile(rf"^{re.escape(base_name)}-(\d+)$")
            max_version = 0

            for branch_name in branch_names:
                match = pattern.match(branch_name)
                if match:
                    version = int(match.group(1))
                    if version > max_version:
                        max_version = version

            # Next version is max + 1 (or 1 if none exist)
            next_version = max_version + 1

            return f"{base_name}-{next_version}"

        except Exception as e:
            logger.warning(f"Failed to fetch branches for version check: {e}, using version 1")
            return f"{base_name}-1"

    async def sync_pipeline_for_service_config(
        self,
        service_code: str,
        repo_url: str,
        branch: str,
        environment: str,
        geo_loc_mst_code: str,
        service,  # ServicesMstModel - already fetched
        service_config,  # ServiceConfigModel - already fetched
        tenant_code: str = None,
        user_code: str = None,
        github_token: str = None
    ) -> Dict[str, Any]:
        """
        Create a new pipeline for a service config branch.

        This method follows the same flow as create_pipeline() but:
        - Skips Step 1 validation (data already validated in service_config)
        - Skips Step 2 duplicate check (checked upstream)
        - Has service and service_config already available (passed in)
        - Auto-generates pipeline_name instead of requiring it

        Flow (matching create_pipeline steps 3-14):
        Step 3: Use passed-in service (already validated)
        Step 4: Get geo_loc_mst, language_ref from service_config
        Step 5: Hierarchical pipeline vendor lookup
        Step 6: Hierarchical infrastructure lookup
        Step 7: Construct ECR repo URL and IAM role ARN
        Step 8: Extract config (port, dockerfile_path, build_path, other_paths)
        Step 9: Skip (Dockerfile handled separately)
        Step 10: Generate YAML from template
        Step 11: Commit workflow file to GitHub
        Step 11.5: Create GitOps workflow tracking
        Step 12: Save pipeline to database
        Step 13: Create pipeline run track + start polling
        Step 14: Return result dict

        Args:
            service_code: Service code
            repo_url: GitHub repository URL
            branch: Branch name
            environment: Environment (dev/staging/prod)
            geo_loc_mst_code: Geographic location code
            service: ServicesMstModel (already fetched)
            service_config: ServiceConfigModel (already fetched)
            tenant_code: Tenant code for tracking
            user_code: User code for tracking
            github_token: GitHub token (optional, reuses token from Terragrunt sync to avoid duplicate requests)

        Returns:
            Dict with status, action, pipeline_code, pr_url, etc.
        """
        from app.utils.naming_strategies import get_naming_strategy
        from app.core.enum import EnvironmentEnum

        try:
            logger.info(f"Syncing pipeline for service={service_code}, branch={branch}, env={environment}")

            # Step 3: Validate infrastructure vendor from service_config
            logger.info(f"[Pipeline Sync Step 3] Checking infra vendor: {service_config.infra_vendor_enum}")
            if service_config.infra_vendor_enum != InfraVendorEnum.aws:
                error_msg = f"Only AWS infrastructure is supported. Config uses: {service_config.infra_vendor_enum.value}"
                logger.error(f"[Pipeline Sync] Step 3 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }

            # Step 4: Get geo_loc_mst and language_ref
            logger.info(f"[Pipeline Sync Step 4] Getting geo_loc: {geo_loc_mst_code}")
            geo_loc_mst = await self.geo_loc_mst_repo.get_by_code(geo_loc_mst_code)
            if not geo_loc_mst:
                error_msg = f"Geographic location not found: {geo_loc_mst_code}"
                logger.error(f"[Pipeline Sync] Step 4 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }
            geo_loc_name_for_naming = geo_loc_mst.name.lower()

            # Get language reference from service_config.language_ref_code
            logger.info(f"[Pipeline Sync Step 4] Checking language_ref_code: {service_config.language_ref_code}")
            if not service_config.language_ref_code:
                error_msg = f"Language not configured in service_config for service '{service_code}'"
                logger.error(f"[Pipeline Sync] Step 4 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }

            language_ref = await self.language_repo.get_by_code(service_config.language_ref_code)
            if not language_ref:
                error_msg = f"Language reference not found: {service_config.language_ref_code}"
                logger.error(f"[Pipeline Sync] Step 4 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }
            logger.info(f"[Pipeline Sync Step 4] Language ref found: {language_ref.code}")

            # Step 5: Hierarchical pipeline vendor lookup
            logger.info(f"[Pipeline Sync Step 5] Looking up pipeline vendor for service={service_code}, env={environment}")
            pipeline_vendor = await self.pipeline_vendor_repo.get_by_service_hierarchy(
                service_code=service_code,
                resource_group_code=service.resource_group_mst_code,
                application_code=service.applications_mst_code,
                tenant_code=service.tenants_mst_code,
                environment=environment
            )

            if not pipeline_vendor:
                error_msg = f"No pipeline vendor configuration found for service '{service_code}' in environment '{environment}'"
                logger.error(f"[Pipeline Sync] Step 5 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }
            logger.info(f"[Pipeline Sync Step 5] Pipeline vendor found: {pipeline_vendor.code}")

            if pipeline_vendor.pipeline_agent_enum != PipelineAgentEnum.github_actions:
                error_msg = f"Pipeline vendor must be GitHub Actions. Found: {pipeline_vendor.pipeline_agent_enum.value}"
                logger.error(f"[Pipeline Sync] Step 5 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }

            # Extract GitHub PAT from pipeline vendor config
            github_pat = pipeline_vendor.auth_config.get("github_pat") if pipeline_vendor.auth_config else None
            logger.info(f"[Pipeline Sync Step 5] PAT configured: {bool(github_pat)}")

            # Step 6: Infrastructure lookup - prefer explicit selection, fallback to hierarchical lookup
            env_enum = EnvironmentEnum(environment)
            logger.info(f"[Pipeline Sync Step 6] Looking up infrastructure for geo_loc={geo_loc_mst_code}, env={environment}")
            infrastructure = await self._get_infrastructure_for_service_config(
                service_config=service_config,
                service=service,
                geo_loc_mst_code=geo_loc_mst_code,
                environment=env_enum
            )

            if not infrastructure:
                error_msg = f"Infrastructure not configured for geo_loc '{geo_loc_mst_code}' in environment '{environment}'"
                logger.error(f"[Pipeline Sync] Step 6 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }
            logger.info(f"[Pipeline Sync Step 6] Infrastructure found: {infrastructure.code}")

            infrastructure_config = infrastructure.locator if infrastructure.locator else {}
            aws_region = infrastructure_config.get("region")
            logger.info(f"[Pipeline Sync Step 6] AWS region from config: {aws_region}")
            if not aws_region:
                error_msg = "AWS region not found in infrastructure_config"
                logger.error(f"[Pipeline Sync] Step 6 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }

            # Step 7: Construct ECR repo URL and IAM role ARN using naming strategy
            account_id = infrastructure_config.get("account_id")
            logger.info(f"[Pipeline Sync Step 7] AWS account_id from config: {account_id}")
            if not account_id:
                error_msg = "AWS account_id not found in infrastructure_config"
                logger.error(f"[Pipeline Sync] Step 7 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }

            index = infrastructure_config.get("index", "01")
            naming_strategy = get_naming_strategy(service.tenant.code)
            application_name = service.application.name if service.application else service.tenant.code

            org_name = naming_strategy.generate_org_name(
                tenant_code=service.tenant.code,
                application_name=application_name
            )

            env_for_naming = "stage" if environment == "staging" else environment

            ecr_repo_name = naming_strategy.generate_ecr_repo_name(
                org_name=org_name,
                environment=env_for_naming,
                geo_loc_mst_code=geo_loc_name_for_naming,
                index=index,
                service_name=service.name
            )

            ecr_uri = naming_strategy.generate_ecr_uri(
                account_id=account_id,
                aws_region=aws_region,
                ecr_repo_name=ecr_repo_name
            )

            iam_role_arn = naming_strategy.generate_iam_role_arn(account_id=account_id)
            logger.info(f"Constructed ECR URI: {ecr_uri}, IAM role: {iam_role_arn}")

            # Step 8: Extract config from service_config
            logger.info(f"[Pipeline Sync Step 8] Extracting config from service_config")
            config = service_config.config
            if not config:
                error_msg = f"Service config is empty for service '{service_code}'"
                logger.error(f"[Pipeline Sync] Step 8 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }

            container_port = config.get("port")
            if not container_port:
                error_msg = f"Container port not configured in service_config for service '{service_code}'"
                logger.error(f"[Pipeline Sync] Step 8 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }
            container_port = int(container_port)
            logger.info(f"[Pipeline Sync Step 8] Config extracted - port: {container_port}")

            dockerfile_path = config.get("dockerfile_path")
            build_path = config.get("build_path")
            other_paths = config.get("other_paths")
            wire_enabled = config.get("wire_enabled", False)
            wire_path = config.get("wire_path")
            go_use_aws_secrets = config.get("go_use_aws_secrets", False)

            # Step 9: Skip (Dockerfile handled separately in service_config sync)

            # Step 10: Generate YAML from template
            logger.info(f"[Pipeline Sync Step 10] Generating YAML from template for language: {language_ref.code}")
            workflow_filename = generate_workflow_filename(service.name, environment)
            workflow_file_path = f".github/workflows/{workflow_filename}"
            logger.info(f"[Pipeline Sync Step 10] Workflow file path: {workflow_file_path}")

            yaml_content = await self._generate_yaml_from_template_simplified(
                language_ref=language_ref,
                pipeline_agent=pipeline_vendor.pipeline_agent_enum,
                ecr_uri=ecr_uri,
                iam_role_arn=iam_role_arn,
                region=aws_region,
                service=service,
                build_path=build_path,
                other_paths=other_paths,
                branch=branch,
                environment=environment,
                infrastructure_config=infrastructure_config,
                geo_loc_mst_code=geo_loc_name_for_naming,
                dockerfile_path=dockerfile_path,
                workflow_file_path=workflow_file_path,
                wire_enabled=wire_enabled,
                wire_path=wire_path,
                go_use_aws_secrets=go_use_aws_secrets,
                build_args=config.get("build_args")
            )

            # Step 11: Commit workflow file to GitHub
            logger.info(f"[Pipeline Sync Step 11] Committing workflow file to GitHub: {repo_url}, branch: {branch}")
            logger.info(f"Committing workflow file to GitHub: {repo_url}, branch: {branch}")
            github_commit_result = await self._commit_yaml_to_github(
                yaml_content=yaml_content,
                github_repository=repo_url,
                branch_name=branch,
                service_name=service.name,
                environment=environment,
                github_token=github_token,
                tenant_code=tenant_code
            )

            if not github_commit_result:
                error_msg = "Failed to commit workflow file to GitHub"
                logger.error(f"[Pipeline Sync] Step 11 failed: {error_msg}")
                return {
                    "branch": branch,
                    "status": "error",
                    "error": error_msg
                }
            logger.info(f"[Pipeline Sync Step 11] GitHub commit result: {github_commit_result}")

            github_commit_sha = github_commit_result.get("commit_sha")
            workflow_file_path = github_commit_result.get("file_path")
            github_commit_url = github_commit_result.get("commit_url")

            # Step 11.5: Generate pipeline_code ONCE and reuse for both workflow and pipeline
            # This ensures transaction_code matches pipeline_code exactly
            base_code = generate_pipeline_code(service.code, environment)

            # Add branch suffix but ensure total length <= 100 chars (database constraint)
            sanitized_branch = sanitize_name(branch)
            max_branch_len = 100 - len(base_code) - 1  # -1 for the hyphen

            if max_branch_len > 0:
                branch_suffix = sanitized_branch[:max_branch_len]
                pipeline_code = f"{base_code}-{branch_suffix}"
            else:
                # If base_code is already too long, use it as-is
                pipeline_code = base_code[:100]

            # Step 11.6: Create GitOps workflow tracking if PR was created
            gitops_workflow_id = None
            if github_commit_result.get("pr_number"):
                try:
                    # Auto-generate pipeline_name
                    pipeline_name = f"Pipeline for {service.name} - {environment} - {branch}"
                    workflow_data = make_gitops_workflow_detail(
                        git_repository=repo_url,
                        git_branch=github_commit_result.get("feature_branch"),
                        git_commit_sha=github_commit_sha,
                        pr_number=github_commit_result.get("pr_number"),
                        pr_url=github_commit_result.get("pr_url"),
                        tenant_mst_code=tenant_code,
                        user_mst_code=user_code,
                        workflow_name=f"Pipeline: {pipeline_name}",
                        transaction_code=pipeline_code,  # Use same pipeline_code
                        table_name=WorkflowSourceTableEnum.PIPELINE
                    )
                    workflow = await self.gitops_workflow_repository.create(**workflow_data)
                    gitops_workflow_id = workflow.id
                    logger.info(f"GitOps workflow created: {workflow.code}, PR #{github_commit_result.get('pr_number')}")
                except Exception as e:
                    logger.error(f"Failed to create GitOps workflow tracking: {e}")

            # Step 12: Save pipeline to database
            # Auto-generate pipeline_name (pipeline_code already generated above)
            pipeline_name = f"Pipeline for {service.name} - {environment} - {branch}"

            deployment_config = {
                "github_commit_sha": github_commit_sha,
                "geo_loc_mst_code": geo_loc_mst_code,
                "workflow_file_path": workflow_file_path
            }

            pipeline = await self.pipeline_repo.create(
                code=pipeline_code,
                name=pipeline_name,
                transaction_code=service_config.code,
                table_name="SERVICE_CONFIG",
                tenant_code=tenant_code,
                pipeline_vendor_mst_code=pipeline_vendor.code,
                repo_url=repo_url,
                repo_branch=branch,
                language_ref_code=language_ref.code,
                authentication_config={},
                deployment_config=deployment_config,
                gitops_workflow_id=gitops_workflow_id,
            )

            await self.db.commit()
            logger.info(f"Pipeline created: {pipeline.code}")

            # Step 13: Create initial pipeline run track record
            initial_run_code = None
            if github_commit_sha:
                from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
                run_track_repo = PipelineRunTrackRepository(self.db)

                initial_run_code = generate_run_code(pipeline.code)
                await run_track_repo.create(
                    pipeline_mst_code=pipeline.code,
                    code=initial_run_code,
                    status=PipelineRunStatusEnum.PENDING,
                    commit_sha=github_commit_sha,
                    log_url=None,
                    github_run_id=None,
                    error_message=None
                )
                await self.db.commit()

                # DISABLED: Pipeline polling causes connection pool exhaustion (holds connections for 60+ min)
                # TODO: Fix connection leak before re-enabling
                # owner, repo = parse_repo_url(repo_url)
                # polling_token = await self._get_github_token(owner) or github_pat
                # start_pipeline_polling(
                #     run_code=initial_run_code,
                #     pipeline_code=pipeline.code,
                #     commit_sha=github_commit_sha,
                #     owner=owner,
                #     repo=repo,
                #     db_session=self.db,
                #     github_token=polling_token
                # )
                # logger.info(f"Started background polling for: {initial_run_code}")

            # Step 14: Return result
            return {
                "branch": branch,
                "status": "success",
                "action": "created",
                "pipeline_code": pipeline.code,
                "pr_url": github_commit_result.get("pr_url"),
                "pr_number": github_commit_result.get("pr_number"),
                "commit_sha": github_commit_sha,
                "workflow_file_path": workflow_file_path,
                "gitops_workflow_id": gitops_workflow_id
            }

        except Exception as e:
            logger.error(f"Error syncing pipeline for branch {branch}: {str(e)}", exc_info=True)
            await self.db.rollback()
            return {
                "branch": branch,
                "status": "error",
                "error": str(e)
            }

    async def _check_and_update_workflow_yaml(
        self,
        pipeline,  # PipelineMstModel
        service,  # ServicesMstModel
        service_config,  # ServiceConfigModel
        tenant_code: str = None,
        user_code: str = None,
        github_token: str = None
    ) -> Dict[str, Any]:
        """
        Check if workflow YAML exists in GitHub and update if needed.

        Flow:
        1. Get workflow_file_path from pipeline.deployment_config
        2. Fetch file from GitHub
        3. If not found → generate and commit new YAML
        4. If found:
           a. Check for workflow_dispatch, add if missing
           b. Generate fresh YAML and compare
           c. Commit if different

        Args:
            pipeline: PipelineMstModel
            service: ServicesMstModel
            service_config: ServiceConfigModel
            tenant_code: Tenant code for tracking
            user_code: User code for tracking
            github_token: GitHub token (optional, reuses token from upstream to avoid duplicate requests)

        Returns:
            Dict with branch, status, action, pr_url, etc.
        """
        from app.integrations.github_integration import GitHubIntegration
        from app.utils.github_sync_helpers import should_skip_commit
        from app.utils.naming_strategies import get_naming_strategy
        from app.core.enum import EnvironmentEnum

        branch = pipeline.repo_branch
        repo_url = pipeline.repo_url
        environment = service_config.environment.value if hasattr(service_config.environment, 'value') else str(service_config.environment)

        try:
            logger.info(f"Checking/updating workflow YAML for pipeline {pipeline.code}, branch {branch}")

            # Step 1: Get workflow_file_path from deployment_config
            deployment_config = pipeline.deployment_config or {}
            workflow_file_path = deployment_config.get("workflow_file_path")

            # Parse repo URL
            owner, repo = parse_repo_url(repo_url)

            # Get GitHub token (uses passed token if available, otherwise fetches new one)
            if not github_token:
                github_token = await self._get_github_token(owner)
            else:
                logger.info("Using passed GitHub token for workflow YAML check/update")
            if not github_token:
                return {
                    "branch": branch,
                    "status": "error",
                    "error": "GitHub token not configured"
                }

            # Step 2: Fetch file from GitHub
            existing_content = None
            if workflow_file_path:
                try:
                    file_result = await GitHubIntegration.get_file_content(
                        token=github_token,
                        base_url=settings.github_base_url,
                        owner=owner,
                        repo=repo,
                        file_path=workflow_file_path,
                        branch=branch
                    )
                    if file_result and file_result.get("exists"):
                        existing_content = file_result.get("content")
                except Exception as e:
                    logger.warning(f"Failed to fetch workflow file: {e}")

            # Step 3: If YAML not found, generate and commit new YAML
            if not existing_content:
                logger.info(f"Workflow YAML not found for {pipeline.code}, generating new one")
                # Need to generate fresh YAML - run through steps 4-11
                return await self._regenerate_and_commit_yaml(
                    pipeline=pipeline,
                    service=service,
                    service_config=service_config,
                    tenant_code=tenant_code,
                    user_code=user_code,
                    action="yaml_not_found"
                )

            # Step 4: YAML found - check for workflow_dispatch
            needs_workflow_dispatch = "workflow_dispatch:" not in existing_content
            needs_triggered_by_user = "triggered_by_user:" not in existing_content
            needs_run_name = "run-name:" not in existing_content

            updated_content = existing_content

            # Add workflow_dispatch with triggered_by_user if needed
            if needs_workflow_dispatch:
                updated_content = self._insert_workflow_dispatch(updated_content)
                logger.info(f"Added workflow_dispatch to {pipeline.code}")
            elif needs_triggered_by_user:
                updated_content = self._insert_triggered_by_user_input(updated_content)
                logger.info(f"Added triggered_by_user to {pipeline.code}")

            # Add run-name if needed
            if needs_run_name:
                updated_content = self._insert_run_name(updated_content, service.name, environment)
                logger.info(f"Added run-name to {pipeline.code}")

            # Step 5: Generate fresh YAML and compare
            # We need to generate fresh YAML to compare against existing
            fresh_yaml = await self._generate_fresh_yaml_for_comparison(
                pipeline=pipeline,
                service=service,
                service_config=service_config
            )

            # Use should_skip_commit to check if content is the same
            if should_skip_commit(updated_content, fresh_yaml):
                logger.info(f"No changes needed for pipeline {pipeline.code}")
                return {
                    "branch": branch,
                    "status": "success",
                    "action": "no_change",
                    "pipeline_code": pipeline.code,
                    "message": "Workflow YAML is up to date"
                }

            # Content is different - need to commit update
            # Determine what changed
            if needs_workflow_dispatch or needs_triggered_by_user or needs_run_name:
                # Commit the workflow_dispatch/run-name additions
                final_content = updated_content
                commit_message = f"Add workflow_dispatch trigger for {service.name} ({environment})"
            else:
                # Content changed for other reasons - use fresh YAML
                final_content = fresh_yaml
                commit_message = f"Update pipeline workflow for {service.name} ({environment})"

            # Smart PR: Look up existing open PR from DB
            existing_pipeline_pr = await self._get_existing_pipeline_pr(
                pipeline_code=pipeline.code,
                owner=owner,
                repo=repo,
                github_token=github_token,
                tenant_code=tenant_code
            )

            # Commit update via feature branch and PR
            result = await self._commit_update_to_github(
                owner=owner,
                repo=repo,
                branch=branch,
                workflow_file_path=workflow_file_path,
                content=final_content,
                commit_message=commit_message,
                service_name=service.name,
                environment=environment,
                pipeline=pipeline,
                tenant_code=tenant_code,
                user_code=user_code,
                existing_pipeline_pr=existing_pipeline_pr
            )

            return {
                "branch": branch,
                "status": "success",
                "action": "updated",
                "pipeline_code": pipeline.code,
                "pr_url": result.get("pr_url"),
                "pr_number": result.get("pr_number"),
                "commit_sha": result.get("commit_sha"),
                "message": "Workflow YAML updated"
            }

        except Exception as e:
            logger.error(f"Error checking/updating workflow for {pipeline.code}: {str(e)}", exc_info=True)
            return {
                "branch": branch,
                "status": "error",
                "error": str(e),
                "pipeline_code": pipeline.code
            }

    async def _regenerate_and_commit_yaml(
        self,
        pipeline,
        service,
        service_config,
        tenant_code: str = None,
        user_code: str = None,
        action: str = "regenerated"
    ) -> Dict[str, Any]:
        """
        Regenerate YAML and commit to GitHub when file is missing.

        This reuses the sync_pipeline_for_service_config logic but doesn't
        create a new pipeline record (one already exists).
        """
        from app.utils.naming_strategies import get_naming_strategy
        from app.core.enum import EnvironmentEnum

        branch = pipeline.repo_branch
        repo_url = pipeline.repo_url
        environment = service_config.environment.value if hasattr(service_config.environment, 'value') else str(service_config.environment)
        geo_loc_mst_code = (pipeline.deployment_config or {}).get("geo_loc_mst_code", service_config.geo_loc_mst_code)

        try:
            # Get geo_loc_mst
            geo_loc_mst = await self.geo_loc_mst_repo.get_by_code(geo_loc_mst_code)
            if not geo_loc_mst:
                return {"branch": branch, "status": "error", "error": f"Geographic location not found: {geo_loc_mst_code}"}
            geo_loc_name_for_naming = geo_loc_mst.name.lower()

            # Get language reference
            language_ref = await self.language_repo.get_by_code(service_config.language_ref_code)
            if not language_ref:
                return {"branch": branch, "status": "error", "error": f"Language not found: {service_config.language_ref_code}"}

            # Get pipeline vendor
            pipeline_vendor = await self.pipeline_vendor_repo.get_by_service_hierarchy(
                service_code=service_config.services_mst_code,
                resource_group_code=service.resource_group_mst_code,
                application_code=service.applications_mst_code,
                tenant_code=service.tenants_mst_code,
                environment=environment
            )
            if not pipeline_vendor:
                return {"branch": branch, "status": "error", "error": "Pipeline vendor not found"}

            # Get infrastructure - prefer explicit selection, fallback to hierarchical lookup
            env_enum = EnvironmentEnum(environment)
            infrastructure = await self._get_infrastructure_for_service_config(
                service_config=service_config,
                service=service,
                geo_loc_mst_code=geo_loc_mst_code,
                environment=env_enum
            )
            if not infrastructure:
                return {"branch": branch, "status": "error", "error": "Infrastructure not found"}

            infrastructure_config = infrastructure.locator or {}
            aws_region = infrastructure_config.get("region")
            account_id = infrastructure_config.get("account_id")
            index = infrastructure_config.get("index", "01")

            if not aws_region or not account_id:
                return {"branch": branch, "status": "error", "error": "AWS region or account_id not found"}

            # Build ECR URI and IAM role ARN
            naming_strategy = get_naming_strategy(service.tenant.code)
            application_name = service.application.name if service.application else service.tenant.code
            org_name = naming_strategy.generate_org_name(tenant_code=service.tenant.code, application_name=application_name)
            env_for_naming = "stage" if environment == "staging" else environment

            ecr_repo_name = naming_strategy.generate_ecr_repo_name(
                org_name=org_name,
                environment=env_for_naming,
                geo_loc_mst_code=geo_loc_name_for_naming,
                index=index,
                service_name=service.name
            )
            ecr_uri = naming_strategy.generate_ecr_uri(account_id=account_id, aws_region=aws_region, ecr_repo_name=ecr_repo_name)
            iam_role_arn = naming_strategy.generate_iam_role_arn(account_id=account_id)

            # Extract config
            config = service_config.config or {}
            dockerfile_path = config.get("dockerfile_path")
            build_path = config.get("build_path")
            other_paths = config.get("other_paths")
            wire_enabled = config.get("wire_enabled", False)
            wire_path = config.get("wire_path")
            go_use_aws_secrets = config.get("go_use_aws_secrets", False)

            # Generate YAML
            workflow_filename = generate_workflow_filename(service.name, environment)
            workflow_file_path = f".github/workflows/{workflow_filename}"

            yaml_content = await self._generate_yaml_from_template_simplified(
                language_ref=language_ref,
                pipeline_agent=pipeline_vendor.pipeline_agent_enum,
                ecr_uri=ecr_uri,
                iam_role_arn=iam_role_arn,
                region=aws_region,
                service=service,
                build_path=build_path,
                other_paths=other_paths,
                branch=branch,
                environment=environment,
                infrastructure_config=infrastructure_config,
                geo_loc_mst_code=geo_loc_name_for_naming,
                dockerfile_path=dockerfile_path,
                workflow_file_path=workflow_file_path,
                wire_enabled=wire_enabled,
                wire_path=wire_path,
                go_use_aws_secrets=go_use_aws_secrets,
                build_args=config.get("build_args")
            )

            # Smart PR: Look up existing open PR from DB
            owner, repo = parse_repo_url(repo_url)
            github_token = await self._get_github_token(owner)
            existing_pipeline_pr = await self._get_existing_pipeline_pr(
                pipeline_code=pipeline.code,
                owner=owner,
                repo=repo,
                github_token=github_token,
                tenant_code=tenant_code
            )

            # Commit to GitHub
            github_commit_result = await self._commit_yaml_to_github(
                yaml_content=yaml_content,
                github_repository=repo_url,
                branch_name=branch,
                service_name=service.name,
                environment=environment,
                github_token=github_token,
                existing_pipeline_pr=existing_pipeline_pr,
                tenant_code=tenant_code
            )

            if not github_commit_result:
                return {"branch": branch, "status": "error", "error": "Failed to commit workflow to GitHub"}

            github_commit_sha = github_commit_result.get("commit_sha")

            # Update pipeline deployment_config with new workflow_file_path
            new_deployment_config = pipeline.deployment_config or {}
            new_deployment_config["workflow_file_path"] = github_commit_result.get("file_path")
            new_deployment_config["github_commit_sha"] = github_commit_sha

            await self.pipeline_repo.update(
                db_obj=pipeline,
                updates={
                    "deployment_config": new_deployment_config,
                }
            )

            # Create GitOps workflow tracking if PR created
            if github_commit_result.get("pr_number"):
                try:
                    workflow_data = make_gitops_workflow_detail(
                        git_repository=repo_url,
                        git_branch=github_commit_result.get("feature_branch"),
                        git_commit_sha=github_commit_sha,
                        pr_number=github_commit_result.get("pr_number"),
                        pr_url=github_commit_result.get("pr_url"),
                        tenant_mst_code=tenant_code,
                        user_mst_code=user_code,
                        workflow_name=f"Pipeline YAML: {service.name} ({environment})",
                        transaction_code=pipeline.code,
                        table_name=WorkflowSourceTableEnum.PIPELINE
                    )
                    workflow = await self.gitops_workflow_repository.create(**workflow_data)
                    await self.pipeline_repo.update(db_obj=pipeline, updates={"gitops_workflow_id": workflow.id})
                except Exception as e:
                    logger.error(f"Failed to create GitOps workflow: {e}")

            await self.db.commit()

            return {
                "branch": branch,
                "status": "success",
                "action": action,
                "pipeline_code": pipeline.code,
                "pr_url": github_commit_result.get("pr_url"),
                "pr_number": github_commit_result.get("pr_number"),
                "commit_sha": github_commit_sha,
                "workflow_file_path": github_commit_result.get("file_path")
            }

        except Exception as e:
            logger.error(f"Error regenerating YAML for {pipeline.code}: {str(e)}", exc_info=True)
            return {"branch": branch, "status": "error", "error": str(e)}

    async def _generate_fresh_yaml_for_comparison(
        self,
        pipeline,
        service,
        service_config
    ) -> str:
        """
        Generate fresh YAML content for comparison with existing file.

        This is a lightweight version that just generates YAML without committing.
        """
        from app.utils.naming_strategies import get_naming_strategy
        from app.core.enum import EnvironmentEnum

        branch = pipeline.repo_branch
        environment = service_config.environment.value if hasattr(service_config.environment, 'value') else str(service_config.environment)
        geo_loc_mst_code = (pipeline.deployment_config or {}).get("geo_loc_mst_code", service_config.geo_loc_mst_code)

        # Get geo_loc_mst
        geo_loc_mst = await self.geo_loc_mst_repo.get_by_code(geo_loc_mst_code)
        geo_loc_name_for_naming = geo_loc_mst.name.lower() if geo_loc_mst else "unknown"

        # Get language reference
        language_ref = await self.language_repo.get_by_code(service_config.language_ref_code)
        if not language_ref:
            return ""

        # Get pipeline vendor
        pipeline_vendor = await self.pipeline_vendor_repo.get_by_service_hierarchy(
            service_code=service_config.services_mst_code,
            resource_group_code=service.resource_group_mst_code,
            application_code=service.applications_mst_code,
            tenant_code=service.tenants_mst_code,
            environment=environment
        )
        if not pipeline_vendor:
            return ""

        # Get infrastructure - prefer explicit selection, fallback to hierarchical lookup
        env_enum = EnvironmentEnum(environment)
        infrastructure = await self._get_infrastructure_for_service_config(
            service_config=service_config,
            service=service,
            geo_loc_mst_code=geo_loc_mst_code,
            environment=env_enum
        )
        if not infrastructure:
            return ""

        infrastructure_config = infrastructure.locator or {}
        aws_region = infrastructure_config.get("region", "")
        account_id = infrastructure_config.get("account_id", "")
        index = infrastructure_config.get("index", "01")

        # Build ECR URI and IAM role ARN
        naming_strategy = get_naming_strategy(service.tenant.code)
        application_name = service.application.name if service.application else service.tenant.code
        org_name = naming_strategy.generate_org_name(tenant_code=service.tenant.code, application_name=application_name)
        env_for_naming = "stage" if environment == "staging" else environment

        ecr_repo_name = naming_strategy.generate_ecr_repo_name(
            org_name=org_name,
            environment=env_for_naming,
            geo_loc_mst_code=geo_loc_name_for_naming,
            index=index,
            service_name=service.name
        )
        ecr_uri = naming_strategy.generate_ecr_uri(account_id=account_id, aws_region=aws_region, ecr_repo_name=ecr_repo_name)
        iam_role_arn = naming_strategy.generate_iam_role_arn(account_id=account_id)

        # Extract config
        config = service_config.config or {}
        dockerfile_path = config.get("dockerfile_path")
        build_path = config.get("build_path")
        other_paths = config.get("other_paths")
        wire_enabled = config.get("wire_enabled", False)
        wire_path = config.get("wire_path")
        go_use_aws_secrets = config.get("go_use_aws_secrets", False)

        # Generate YAML
        workflow_filename = generate_workflow_filename(service.name, environment)
        workflow_file_path = f".github/workflows/{workflow_filename}"

        return await self._generate_yaml_from_template_simplified(
            language_ref=language_ref,
            pipeline_agent=pipeline_vendor.pipeline_agent_enum,
            ecr_uri=ecr_uri,
            iam_role_arn=iam_role_arn,
            region=aws_region,
            service=service,
            build_path=build_path,
            other_paths=other_paths,
            branch=branch,
            environment=environment,
            infrastructure_config=infrastructure_config,
            geo_loc_mst_code=geo_loc_name_for_naming,
            dockerfile_path=dockerfile_path,
            workflow_file_path=workflow_file_path,
            wire_enabled=wire_enabled,
            wire_path=wire_path,
            go_use_aws_secrets=go_use_aws_secrets,
            build_args=config.get("build_args")
        )

    async def _get_existing_pipeline_pr(
        self,
        pipeline_code: str,
        owner: str,
        repo: str,
        github_token: str,
        tenant_code: str = None
    ) -> Optional[Dict]:
        """
        Look up existing open PR for this pipeline from gitops_workflow_detail.

        Returns dict with git_branch, pr_number, workflow_id, commit_sha if found.
        The was_closed flag is set by validating PR status on GitHub.
        """
        from app.integrations.github_integration import GitHubIntegration

        try:
            existing_workflow = await self.gitops_workflow_repository.get_open_pr_by_transaction(
                transaction_code=pipeline_code,
                table_name=WorkflowSourceTableEnum.PIPELINE,
                tenant_mst_code=tenant_code
            )

            if not existing_workflow:
                return None

            # Validate PR is still open on GitHub
            was_closed = False
            try:
                pr_info = await GitHubIntegration.get_pull_request(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    pr_number=existing_workflow.pr_number
                )
                if pr_info and pr_info.get("state") != "open":
                    was_closed = True
                    logger.info(f"PR #{existing_workflow.pr_number} was closed/merged")
            except Exception as e:
                logger.warning(f"Could not validate PR status: {e}")
                was_closed = True

            return {
                "git_branch": existing_workflow.git_branch,
                "pr_number": existing_workflow.pr_number,
                "workflow_id": existing_workflow.id,
                "commit_sha": existing_workflow.git_commit_sha,
                "was_closed": was_closed
            }

        except Exception as e:
            logger.warning(f"Failed to look up existing PR for pipeline {pipeline_code}: {e}")
            return None

    async def _commit_update_to_github(
        self,
        owner: str,
        repo: str,
        branch: str,
        workflow_file_path: str,
        content: str,
        commit_message: str,
        service_name: str,
        environment: str,
        pipeline,
        tenant_code: str = None,
        user_code: str = None,
        existing_pipeline_pr: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Commit updated workflow file to GitHub via feature branch and PR.

        Uses Smart PR logic:
        1. No existing PR → create new
        2. Existing PR with same content → skip (return existing PR info)
        3. Existing PR with different content → replace (close old, create new)
        4. Existing PR was manually closed → create new
        """
        from app.integrations.github_integration import GitHubIntegration
        import re

        github_token = await self._get_github_token(owner)

        # Smart PR: Check if we should skip/create/replace
        pr_action_result = {"action": "create"}
        if existing_pipeline_pr:
            existing_pr_content = fetch_existing_pr_content(
                GitHubIntegration=GitHubIntegration,
                github_token=github_token,
                github_base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                file_path=workflow_file_path,
                branch=existing_pipeline_pr.get("git_branch")
            )
            pr_action_result = determine_pr_action(
                existing_pr_info=existing_pipeline_pr,
                existing_pr_content=existing_pr_content,
                new_content=content,
                owner=owner,
                repo=repo
            )

        # Handle skip action - return existing PR info
        if pr_action_result.get("action") == "skip":
            logger.info(f"Smart PR: Skipping - content unchanged for {service_name} ({environment})")
            return {
                "pr_url": pr_action_result.get("existing_pr_url"),
                "pr_number": pr_action_result.get("existing_pr_number"),
                "commit_sha": pr_action_result.get("existing_commit_sha"),
                "feature_branch": pr_action_result.get("existing_branch"),
                "status": "no_changes",
                "message": "Content unchanged - using existing PR"
            }

        # Generate unique branch name for new PR
        service_name_sanitized = re.sub(r'[\s_-]+', '-', service_name.strip()).strip('-').lower()
        base_branch_name = f"workflow-update/{service_name_sanitized}-{environment}"
        feature_branch = await self._generate_unique_branch_name(
            github_token=github_token,
            owner=owner,
            repo=repo,
            base_name=base_branch_name
        )

        # Create feature branch
        await GitHubIntegration.create_branch(
            token=github_token,
            base_url=settings.github_base_url,
            owner=owner,
            repo=repo,
            branch_name=feature_branch,
            from_branch=branch
        )

        # Commit updated file
        commit_result = await GitHubIntegration.update_or_create_file(
            token=github_token,
            base_url=settings.github_base_url,
            owner=owner,
            repo=repo,
            branch=feature_branch,
            file_path=workflow_file_path,
            content=content,
            message=commit_message
        )

        # Create new PR
        pr_title = f"[Pipeline] Update workflow for {service_name} - {environment}"

        # Add supersede note if replacing old PR
        supersede_note = ""
        if pr_action_result.get("action") == "replace":
            old_pr_num = pr_action_result.get("old_pr_to_close")
            supersede_note = f"\n\n> **Note:** This PR supersedes PR #{old_pr_num}"

        pr_body = f"""## Update Pipeline Workflow

**Service:** `{service_name}`
**Environment:** {environment}

### Changes
- Updated workflow configuration{supersede_note}

---
*Generated by pipeline sync*"""

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

        # Smart PR: Cleanup old PR if this was a replacement
        if pr_action_result.get("action") == "replace" and pr_result.get("number"):
            cleanup_old_pr(
                GitHubIntegration=GitHubIntegration,
                github_token=github_token,
                github_base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                old_pr_number=pr_action_result.get("old_pr_to_close"),
                old_branch=pr_action_result.get("old_branch_to_delete"),
                new_pr_number=pr_result.get("number")
            )
            # Mark old workflow as closed
            if pr_action_result.get("old_workflow_id"):
                try:
                    await self.gitops_workflow_repository.update_pr_status(
                        workflow_id=pr_action_result.get("old_workflow_id"),
                        new_status=PRStatusEnum.PR_CLOSED
                    )
                except Exception as e:
                    logger.warning(f"Failed to update old workflow status: {e}")

        # Create GitOps workflow tracking
        if pr_result.get("number"):
            try:
                workflow_data = make_gitops_workflow_detail(
                    git_repository=f"{owner}/{repo}",
                    git_branch=feature_branch,
                    git_commit_sha=commit_result.get("commit_sha", ""),
                    pr_number=pr_result.get("number"),
                    pr_url=pr_result.get("html_url", ""),
                    tenant_mst_code=tenant_code,
                    user_mst_code=user_code,
                    workflow_name=f"Update workflow: {service_name} ({environment})",
                    transaction_code=pipeline.code,
                    table_name=WorkflowSourceTableEnum.PIPELINE
                )
                workflow = await self.gitops_workflow_repository.create(**workflow_data)
                await self.pipeline_repo.update(db_obj=pipeline, updates={"gitops_workflow_id": workflow.id})
                await self.db.commit()
            except Exception as e:
                logger.error(f"Failed to create GitOps workflow tracking: {e}")

        return {
            "pr_url": pr_result.get("html_url"),
            "pr_number": pr_result.get("number"),
            "commit_sha": commit_result.get("commit_sha"),
            "feature_branch": feature_branch
        }
