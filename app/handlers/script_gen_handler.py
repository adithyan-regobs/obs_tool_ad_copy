"""
Script Generation Handler

Orchestrates script generation by calling appropriate components:
- If tenant == "aspora" → call Aspora's script gen components
- If tenant has custom component → use it
- Otherwise → call default component (TODO: not implemented yet)

Components are stored in: app/plugin/{tenant}/script_gen_components/
"""

import inspect
import logging
from typing import Dict, Any, Type, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.pr_workflow_context import PRWorkflowContext
from app.plugin.aspora.script_gen_components import (
    AsporaS3ScriptGenComponent,
    AsporaSqsScriptGenComponent,
    AsporaDynamoDbScriptgenComponent,
    AsporaDockerScriptGenComponent,
    AsporaKongRouteScriptGenComponent,
    AsporaKongRouteScriptGenComponentV2,
    AsporaDatabaseUserManagementScriptGenComponent,
    AsporaDatabaseMultiUserManagementScriptGenComponent,
    AsporaDatabaseSeparateUsersFileScriptGenComponent,
    AsporaDbCreationScriptGenComponent,
    AsporaEcsTerragruntScriptGenComponent,
    AsporaEksTerragruntScriptGenComponent,
    AsporaK8sManifestsScriptGenComponent,
    AsporaWorkflowScriptGenComponent,
    AsporaEksScriptGenComponent,
    AsporaEksDeploymentScriptGenComponent,
    AsporaAtlantisScriptGenComponent,
    AsporaEnvConfigsScriptGenComponent,
    AsporaEnvSecretsScriptGenComponent,
    AsporaEksRollbackScriptGenComponent,
)
from app.plugin.default.default_eks_deploy_script_gen_component import DefaultEksDeployScriptGenComponent
from app.plugin.default.default_eks_workflow_script_gen_component import DefaultEksWorkflowScriptGenComponent
from app.plugin.default.default_ecs_workflow_script_gen_component import DefaultEcsWorkflowScriptGenComponent
from app.plugin.default.default_dockerfile_script_gen_component import DefaultDockerfileScriptGenComponent
from app.plugin.default.default_eks_jenkins_gen_component import DefaultEksJenkinsGenComponent
from app.plugin.default.k8s_helm_script_gen_component import K8sHelmScriptGenComponent
from app.plugin.default.k8s_job_script_gen_component import K8sJobScriptGenComponent
from app.plugin.default.default_dynamodb_script_gen_component import DefaultDynamoDbScriptGenComponent
from app.plugin.default.default_s3_script_gen_component import DefaultS3ScriptGenComponent
from app.plugin.default.default_sqs_script_gen_component import DefaultSqsScriptGenComponent
from app.plugin.default.default_redis_script_gen_component import DefaultRedisScriptGenComponent
from app.plugin.default.default_aurora_script_gen_component import DefaultAuroraScriptGenComponent
from app.plugin.default.default_aurora_database_creation_script_gen_component import DefaultAuroraDatabaseCreationScriptGenComponent
from app.plugin.default.default_eks_service_ops_gen_component import DefaultEksServiceOpsGenComponent
from app.plugin.default.default_model_serving_jenkins_gen_component import DefaultModelServingJenkinsGenComponent
from app.plugin.default.default_aws_role_gen_component import DefaultAwsRoleGenComponent
from app.repository.transaction_queue_repository import TransactionQueueRepository

logger = logging.getLogger(__name__)


class ScriptGenHandler:
    _tenant_script_gen_map: Dict[str, Dict[str, Type]] = {
        "aspora": {
            'create_bucket': AsporaS3ScriptGenComponent,
            'create_queue': AsporaSqsScriptGenComponent,
            'table_management': AsporaDynamoDbScriptgenComponent,
            'add_route': AsporaKongRouteScriptGenComponent,
            'add_route_v2': AsporaKongRouteScriptGenComponentV2,
            'database_creation':AsporaDbCreationScriptGenComponent,
            'user_management': AsporaDatabaseMultiUserManagementScriptGenComponent,
            'user_management_separate_file': AsporaDatabaseSeparateUsersFileScriptGenComponent,
            #TODO change case types
            'ecs_terragrunt': AsporaEcsTerragruntScriptGenComponent,
            'eks_terragrunt': AsporaEksTerragruntScriptGenComponent,
            'ecs_pipeline': AsporaWorkflowScriptGenComponent,
            'eks_pipeline_config': AsporaEksScriptGenComponent,
            'eks_pipeline_workflow': AsporaWorkflowScriptGenComponent,
            'eks_pipeline_deployment': AsporaEksDeploymentScriptGenComponent,
            'eks_helm': AsporaEksScriptGenComponent,
            'ecs_dockerfile': AsporaDockerScriptGenComponent,
            'atlantis': AsporaAtlantisScriptGenComponent,
            'ecs_env_configs': AsporaEnvConfigsScriptGenComponent,
            'ecs_env_secrets': AsporaEnvSecretsScriptGenComponent,
            # k8s-manifests repo (one component dispatches by key)
            'k8s_chart_meta': AsporaK8sManifestsScriptGenComponent,
            'k8s_chart_values': AsporaK8sManifestsScriptGenComponent,
            'k8s_chart_template': AsporaK8sManifestsScriptGenComponent,
            'k8s_env_values': AsporaK8sManifestsScriptGenComponent,
            'k8s_rollback': AsporaEksRollbackScriptGenComponent,
        },
        "vance": {
            'create_bucket': AsporaS3ScriptGenComponent,
            'create_queue': AsporaSqsScriptGenComponent,
            'table_management': AsporaDynamoDbScriptgenComponent,
            'add_route': AsporaKongRouteScriptGenComponent,
            'add_route_v2': AsporaKongRouteScriptGenComponentV2,
            'database_creation':AsporaDbCreationScriptGenComponent,
            'user_management': AsporaDatabaseMultiUserManagementScriptGenComponent,
            'user_management_separate_file': AsporaDatabaseSeparateUsersFileScriptGenComponent,
            #TODO change case types
            'ecs_terragrunt': AsporaEcsTerragruntScriptGenComponent,
            'eks_terragrunt': AsporaEksTerragruntScriptGenComponent,
            'ecs_pipeline': AsporaWorkflowScriptGenComponent,
            'eks_pipeline_config': AsporaEksScriptGenComponent,
            'eks_pipeline_workflow': AsporaWorkflowScriptGenComponent,
            'eks_pipeline_deployment': AsporaEksDeploymentScriptGenComponent,
            'eks_helm': AsporaEksScriptGenComponent,
            'ecs_dockerfile': AsporaDockerScriptGenComponent,
            'atlantis': AsporaAtlantisScriptGenComponent,
            'ecs_env_configs': AsporaEnvConfigsScriptGenComponent,
            'ecs_env_secrets': AsporaEnvSecretsScriptGenComponent,
            # k8s-manifests repo (one component dispatches by key)
            'k8s_chart_meta': AsporaK8sManifestsScriptGenComponent,
            'k8s_chart_values': AsporaK8sManifestsScriptGenComponent,
            'k8s_chart_template': AsporaK8sManifestsScriptGenComponent,
            'k8s_env_values': AsporaK8sManifestsScriptGenComponent,
            'k8s_rollback': AsporaEksRollbackScriptGenComponent,
        },
        # Default: generic service onboarding for tenants without a custom implementation
        "default": {
            'default_eks_deployment': DefaultEksDeployScriptGenComponent,
            'default_eks_workflow': DefaultEksWorkflowScriptGenComponent,
            'default_ecs_workflow': DefaultEcsWorkflowScriptGenComponent,
            'default_dockerfile': DefaultDockerfileScriptGenComponent,
            'eks_jenkins_pipeline': DefaultEksJenkinsGenComponent,
            'model_serving_jenkins_pipeline': DefaultModelServingJenkinsGenComponent,
            'model_serving_eks_deployment': DefaultEksDeployScriptGenComponent,
            'k8s_postgres_create_server': K8sHelmScriptGenComponent,
            'k8s_postgres_create_database': K8sJobScriptGenComponent,
            'k8s_postgres_create_user': K8sJobScriptGenComponent,
            'table_management': DefaultDynamoDbScriptGenComponent,
            'create_bucket': DefaultS3ScriptGenComponent,
            'create_queue': DefaultSqsScriptGenComponent,
            'create_redis': DefaultRedisScriptGenComponent,
            'create_server': DefaultAuroraScriptGenComponent,
            'database_creation': DefaultAuroraDatabaseCreationScriptGenComponent,
            'eks_service_ops': DefaultEksServiceOpsGenComponent,
            'default_aws_role': DefaultAwsRoleGenComponent,
        },
    }

    @classmethod
    async def generate_script(
        cls,
        tenant: str,
        queue_dict: Dict,
        file_location: Any,  # FileLocationItem
        workflow_context: PRWorkflowContext,
        gitops_queue_repo: TransactionQueueRepository,
        db: AsyncSession = None,
        upload_to_s3: bool = True,
    ) -> Dict:
        """
        Public use-case entry point

        Generates script for a single file location.

        Args:
            tenant: Tenant code
            queue_id: Queue ID for tracking in workflow context
            config_snapshot: Already-merged parameters dict (queue config_snapshot + file_location.config)
            file_location: Single FileLocationItem with repo, file_path, script_gen_key, etc.
            workflow_context: PR workflow context (passed by reference)

        Returns:
            Dictionary with file_location, script_content, and full_file_path
        """
        try:
            # Get script generator for this file's infra type (script_gen_key).
            case_type = file_location.script_gen_key
            generator = cls.get_script_generator(tenant, case_type)
            # Call component's generate method with all required parameters
            # Components will store results directly in workflow_context and commit to git
            script_content = generator.generate(
                tenant=tenant,
                repository=gitops_queue_repo,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                db=db,
                # False on the PREVIEW path: rendering a preview must not
                # rewrite the S3 artifacts or the row's script_access_key —
                # those belong to the deploy. Every component takes this flag.
                upload_to_s3=upload_to_s3,
            )

            if inspect.isawaitable(script_content):
                script_content = await script_content

            # Collect result for return
            result = {
                'file_location': file_location,
                'script_content': script_content,
                'full_file_path': file_location.file_path  # Already contains repo/branch/path
            }

            return result

        except Exception as e:
            logger.error(
                f"Failed to generate script for file {file_location.file_path}: {e}",
                exc_info=True
            )
            raise

    @classmethod
    def get_script_generator(cls, tenant: str, case_type: str) -> Type:
        """
        Resolve concrete script generator.

        Lookup order:
          1. Tenant-specific map (e.g., "aspora", "vance")
          2. "default" map (generic implementations, e.g., default EKS onboarding)
        """
        tenant_map = cls._tenant_script_gen_map.get(tenant, {})
        script_generator_cls = tenant_map.get(case_type)

        if script_generator_cls is None:
            # Fall back to the default map (works for all tenants)
            script_generator_cls = cls._tenant_script_gen_map.get("default", {}).get(case_type)

        if script_generator_cls is None:
            logger.error(f"No script generator for tenant '{tenant}' and case_type '{case_type}'")
            raise NotImplementedError(
                f"No script generator configured for tenant='{tenant}' case_type='{case_type}'"
            )

        return script_generator_cls()
