"""
Workflow Preview Service

Generates GitHub Actions workflow YAML previews for ECS services.
Matches the exact flow used in create/update service config endpoints.
"""
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.language_ref_repository import LanguageRefRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.geo_loc_mst_repository import GeoLocMstRepository
from app.services.pipeline_mgmt_service import PipelineMgmtService
from app.core.enum import PipelineAgentEnum
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.language_ref_model import LanguageRefModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel


class WorkflowPreviewService:
    """Service for generating ECS workflow YAML previews"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.language_ref_repo = LanguageRefRepository(db)
        self.services_repo = ServicesMstRepository(db)
        self.infrastructure_repo = InfrastructureMstRepository(db)
        self.geo_loc_repo = GeoLocMstRepository(db)

    async def generate_workflow_preview(
        self,
        infrastructure_ref_type: str,
        infrastructure_mst_code: str,
        services_mst_code: str,
        environment: str,
        geo_loc_mst_code: str,
        branch: str,
        language_ref_code: str,
        build_path: Optional[str] = None,
        dockerfile_path: Optional[str] = None,
        other_paths: Optional[list] = None,
        wire_enabled: bool = False,
        wire_path: Optional[str] = None,
        go_use_aws_secrets: bool = False,
        build_args: Optional[list] = None,
        repository: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate ECS workflow YAML preview matching create/update service config flow.

        This method follows the EXACT same steps as:
        - ServiceConfigService._sync_ecs_pipeline() for ECS

        Args:
            infrastructure_ref_type: "ecs_ec2_infrastructuretype_ref"
            infrastructure_mst_code: Infrastructure instance code
            services_mst_code: Service code
            environment: Environment (dev, staging, prod)
            geo_loc_mst_code: Geographic location code
            branch: Git branch name
            language_ref_code: Language reference code
            build_path: Build path for JAR/Go build
            dockerfile_path: Dockerfile path
            other_paths: Additional trigger paths
            wire_enabled: Enable Wire for Go
            wire_path: Wire path for Go
            go_use_aws_secrets: Enable AWS Secrets for Go
            build_args: Custom Docker build args
            repository: GitHub repository URL (not used for ECS)

        Returns:
            Dict with yaml_content, template_used, and metadata
        """
        # Validate infrastructure type
        if infrastructure_ref_type != "ecs_ec2_infrastructuretype_ref":
            raise ValueError(f"Unsupported infrastructure type: {infrastructure_ref_type}. Only ECS is supported.")

        # Step 1: Fetch service
        service = await self.services_repo.get_by_code(services_mst_code)
        if not service:
            raise ValueError(f"Service not found: {services_mst_code}")

        # Step 2: Fetch infrastructure
        infrastructure = await self.infrastructure_repo.get_by_code(infrastructure_mst_code)
        if not infrastructure:
            raise ValueError(f"Infrastructure not found: {infrastructure_mst_code}")

        # Step 3: Fetch language reference
        language_ref = await self.language_ref_repo.get_by_code(language_ref_code)
        if not language_ref:
            raise ValueError(f"Language reference not found: {language_ref_code}")

        # Generate ECS workflow
        return await self._generate_ecs_workflow(
            service=service,
            infrastructure=infrastructure,
            language_ref=language_ref,
            environment=environment,
            branch=branch,
            geo_loc_mst_code=geo_loc_mst_code,
            build_path=build_path,
            dockerfile_path=dockerfile_path,
            other_paths=other_paths,
            wire_enabled=wire_enabled,
            wire_path=wire_path,
            go_use_aws_secrets=go_use_aws_secrets,
            build_args=build_args
        )

    async def _generate_ecs_workflow(
        self,
        service: ServicesMstModel,
        infrastructure: InfrastructureMstModel,
        language_ref: LanguageRefModel,
        environment: str,
        branch: str,
        geo_loc_mst_code: str,
        build_path: Optional[str],
        dockerfile_path: Optional[str],
        other_paths: Optional[list],
        wire_enabled: bool,
        wire_path: Optional[str],
        go_use_aws_secrets: bool,
        build_args: Optional[list]
    ) -> Dict[str, Any]:
        """
        Generate ECS workflow YAML using the EXACT same logic as PipelineMgmtService.

        This matches the flow in pipeline_mgmt_service.py:sync_pipeline_for_service_config()
        Steps 4-8: Get geo_loc, fetch infrastructure, construct AWS values, extract config, generate YAML
        """
        from app.utils.naming_strategies import get_naming_strategy

        # Step 4: Get geo_loc_mst (to get geo_loc NAME for naming strategy)
        geo_loc_mst = await self.geo_loc_repo.get_by_code(geo_loc_mst_code)
        if not geo_loc_mst:
            raise ValueError(f"Geographic location not found: {geo_loc_mst_code}")
        geo_loc_name_for_naming = geo_loc_mst.name.lower()

        # Step 6: Extract infrastructure_config (locator JSONB)
        infrastructure_config = infrastructure.locator or {}
        aws_region = infrastructure_config.get("region")
        if not aws_region:
            raise ValueError("AWS region not found in infrastructure.locator")

        account_id = infrastructure_config.get("account_id")
        if not account_id:
            raise ValueError("AWS account_id not found in infrastructure.locator")

        cluster_name = infrastructure_config.get("cluster_name")
        if not cluster_name:
            raise ValueError("cluster_name not found in infrastructure.locator")

        index = infrastructure_config.get("index", "01")

        # Step 7: Construct AWS values using naming strategy (matching PipelineMgmtService)
        naming_strategy = get_naming_strategy(service.tenant.code)
        application_name = service.application.name if service.application else service.tenant.code

        # Generate org name
        org_name = naming_strategy.generate_org_name(
            tenant_code=service.tenant.code,
            application_name=application_name
        )

        # Environment for naming (staging -> stage)
        env_for_naming = "stage" if environment == "staging" else environment

        # Generate ECR repo name (using geo_loc NAME not CODE)
        ecr_repo_name = naming_strategy.generate_ecr_repo_name(
            org_name=org_name,
            environment=env_for_naming,
            geo_loc_mst_code=geo_loc_name_for_naming,  # Use NAME, not code
            index=index,
            service_name=service.name
        )

        # Generate ECR URI
        ecr_uri = naming_strategy.generate_ecr_uri(
            account_id=account_id,
            aws_region=aws_region,
            ecr_repo_name=ecr_repo_name
        )

        # Generate IAM role ARN
        iam_role_arn = naming_strategy.generate_iam_role_arn(account_id=account_id)

        # Generate ECS service name (using geo_loc NAME not CODE)
        ecs_service = naming_strategy.generate_ecs_service_name(
            application_name=application_name,
            environment=env_for_naming,
            geo_loc_mst_code=geo_loc_name_for_naming,  # Use NAME, not code
            index=index,
            service_name=service.name
        )

        # Use PipelineMgmtService._generate_yaml_from_template_simplified()
        pipeline_service = PipelineMgmtService(self.db)

        # Build infrastructure_config dict for the method
        infrastructure_config_for_method = {
            "cluster_name": cluster_name,
            "index": index
        }

        # Call the protected method
        yaml_content = await pipeline_service._generate_yaml_from_template_simplified(
            language_ref=language_ref,
            pipeline_agent=PipelineAgentEnum.github_actions,
            ecr_uri=ecr_uri,
            iam_role_arn=iam_role_arn,
            region=aws_region,
            service=service,
            build_path=build_path,
            other_paths=other_paths,
            branch=branch,
            environment=environment,
            infrastructure_config=infrastructure_config_for_method,
            geo_loc_mst_code=geo_loc_mst_code,
            dockerfile_path=dockerfile_path,
            workflow_file_path=None,
            wire_enabled=wire_enabled,
            wire_path=wire_path,
            go_use_aws_secrets=go_use_aws_secrets,
            build_args=build_args
        )

        # Get template path
        template_file = self._get_template_path(language_ref, "ecs")

        return {
            "yaml_content": yaml_content,
            "template_used": template_file,
            "infrastructure_type": "ecs",
            "language": language_ref.name,
            "service_name": service.name,
            "environment": environment,
            "derived_values": {
                "ecr_repository": ecr_uri,
                "aws_role_arn": iam_role_arn,
                "ecs_cluster": cluster_name,
                "ecs_service": ecs_service,
                "aws_region": aws_region,
                "geo_loc_name": geo_loc_name_for_naming
            }
        }

    def _get_template_path(self, language_ref: LanguageRefModel, infra_type: str) -> str:
        """Get the template file path based on language and infrastructure type."""
        if infra_type == "ecs":
            code = language_ref.code.upper()
            if "MAVEN" in code:
                return "templates/github-actions/java-maven.yml"
            elif "GRADLE" in code:
                return "templates/github-actions/java-gradle.yml"
            elif "GO" in code:
                return "templates/github-actions/go.yml"
            elif "NODE" in code:
                return "templates/github-actions/nodejs.yml"
            elif "PYTHON" in code:
                return "templates/github-actions/python.yml"
            else:
                return "templates/github-actions/java-gradle.yml"
        else:
            return "templates/github-actions/java-gradle.yml"
