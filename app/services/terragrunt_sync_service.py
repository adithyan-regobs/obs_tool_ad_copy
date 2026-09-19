"""
Terragrunt Sync Service

Service layer for syncing service configurations to local Terragrunt HCL files.
This service creates and updates HCL configuration files based on database config values.
"""
import logging
import re
import shutil
from pathlib import Path
from typing import Dict, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.db.models.service_config_model import ServiceConfigModel
from app.services.dockerfile_sync_service import DockerfileSyncService
from app.services.dockerfile_generation_service import DockerfileGenerationService
from app.utils.github_sync_helpers import should_skip_commit

logger = logging.getLogger(__name__)

# Tenant groups for branch logic
VANCE_ASPORA_TENANTS = {"vance", "aspora"}

# Map field names to anchor fields (fields in the same section to insert after)
# Order matters - we try each anchor in order until one is found
FIELD_ANCHOR_MAP = {
    # Container Config - insert after memory, cpu, or container_port
    "container_port": ["memory", "cpu"],
    "cpu": ["container_port", "memory"],
    "memory": ["cpu", "container_port"],

    # ALB Routing - insert after each other
    "service_path": ["listener_rule_priority"],
    "listener_rule_priority": ["service_path"],

    # Health Check - insert after health_check_path or before autoscaling fields
    "health_check_path": ["listener_rule_priority", "service_path", "memory"],

    # Autoscaling - insert after each other in order
    "enable_autoscaling": ["health_check_path", "memory"],
    "desired_count": ["enable_autoscaling"],
    "min_task_count": ["desired_count", "enable_autoscaling"],
    "max_task_count": ["min_task_count", "desired_count"],

    # Datadog Sidecar - insert after each other
    "enable_datadog_sidecar": ["max_task_count", "desired_count", "enable_autoscaling"],
    "datadog_secret_arn": ["enable_datadog_sidecar", "datadog_log_source"],
    "datadog_log_source": ["enable_datadog_sidecar"],
    "datadog_sidecar_cpu": ["datadog_log_source", "enable_datadog_sidecar"],
    "datadog_sidecar_memory": ["datadog_sidecar_cpu", "datadog_log_source"],
    "datadog_logs_enabled": ["datadog_sidecar_memory", "datadog_sidecar_cpu", "enable_datadog_sidecar"],

    # Feature Toggles - insert after other toggles
    "enable_ulimits": ["alb_attachment", "create_service", "create_alb"],
}

# Mapping of product-env-region to encrypted dummy secret values
DUMMY_SECRETS_MAPPING = {
    "core-stage-mumbai": "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFkIl9tlXjOZg4B+B/K1KEzAAAAaDBmBgkqhkiG9w0BBwagWTBXAgEAMFIGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMzWDqQ33SavsJVpWrAgEQgCWmD05d1AdGjJNDHWYu47oTSmKNSjmBq5mtO3wvSr4K3R87m2Wm",
    "core-prod-london": "AQICAHjAfH0s/mY6tiB3hNy775bFAf/KDHt8LHd/BtH2djiKMwFyHm6O2Fz3lQVzZFYCmbdTAAAAZzBlBgkqhkiG9w0BBwagWDBWAgEAMFEGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMFjYCb4gR9ohAvbPjAgEQgCTkTO6TL77eQKUqsnYPXfI7ZzgmWsj8j8+FSIw66LlqOQL7rSo=",
    "core-prod-mumbai": "AQICAHjwEJsUl4NAwm2LVgKaxe8VI3nWVsPlMMDL1tFPfl/s8AEC3BEn8e30CZ4ePxDNQQEJAAAAZDBiBgkqhkiG9w0BBwagVTBTAgEAME4GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMbYOU8R2SG14doKjNAgEQgCHvoZ+w44pHH+KGb3Nnf3PUZh/GUws9nbC/i88mTTfrb7Q=",
    "falcon-stage-mumbai": "AQICAHh2yyxMp2H7t1xSeylfRX58eWVqg3VEmGGcucb/1FjL/QEppPAzGbuMzICv1mwXzuwuAAAAaDBmBgkqhkiG9w0BBwagWTBXAgEAMFIGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMO6LylcdFd/Z3N+ZPAgEQgCVayzX3ATEDe7EM15BNDJz69vBACEbAgeGj2+z7lnU/oHxW55HV",
    "falcon-prod-london": "AQICAHjkAuvo1uup+0hHL4HC7ksdPvZrhOPVxWCxL+tKsNxC6QEODIbe+ugm7HsIl5UFITtQAAAAZDBiBgkqhkiG9w0BBwagVTBTAgEAME4GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM78h7n2UMikXuDpuVAgEQgCGvBGu7u6SztWKqYQ15IyKyoCRiUcHt+CMDkrVff7LmMRU=",
}

# Map AWS region codes to short names for dummy secret lookup
AWS_REGION_SHORT_NAMES = {
    "ap-south-1": "mumbai",
    "eu-west-2": "london",
}

# Datadog parameters that trigger dependency infrastructure check
DATADOG_PARAMS = {
    "enable_datadog_sidecar",
    "datadog_log_source",
    "datadog_sidecar_cpu",
    "datadog_sidecar_memory",
    "datadog_secret_arn",
    "datadog_logs_enabled",
}

# Datadog dependency block template to insert when missing
DATADOG_DEPENDENCY_BLOCK = '''dependency "datadog_api_key" {
  config_path = "../../secrets/datadog-configs"
  mock_outputs = {
    secret_manager_arn = "arn:aws:secretsmanager:ap-south-1:111111122222:secret:datadog_api_key-123456"
  }
}'''


class TerragruntSyncService:
    """
    Service for syncing service configs to local Terragrunt HCL files.

    Handles:
    - Creating service folders
    - Copying template files
    - Updating HCL field values
    - Pushing to GitHub repository
    """

    def __init__(self, db: AsyncSession = None):
        """Initialize Terragrunt sync service with template paths"""
        self.db = db

        # Get base directory (obs_tool/)
        self.base_dir = Path(__file__).resolve().parent.parent.parent
        self.templates_dir = self.base_dir / "templates" / "terragrunt" / "services"

        # New standardized templates (4 templates)
        self.api_common_alb_template = self.templates_dir / "api-common-alb.hcl"
        self.worker_no_alb_template = self.templates_dir / "worker-no-alb.hcl"
        self.api_new_alb_template = self.templates_dir / "api-new-alb.hcl"
        self.ops_tools_template = self.templates_dir / "ops-tools.hcl"

        # Legacy template references (for backward compatibility during migration)
        self.template_file = self.api_common_alb_template
        self.no_alb_template_file = self.worker_no_alb_template

        # Initialize Dockerfile sync service (for Datadog modifications)
        self._dockerfile_sync_service = DockerfileSyncService()

        # Initialize Dockerfile generation service (for creating new standardized Dockerfiles)
        self._dockerfile_generation_service = DockerfileGenerationService()

    async def _get_github_token(self, owner: str) -> str:
        """Get GitHub App installation token for the given org."""
        from app.utils.github_app_token import get_token_for_org
        db = self.db
        if db is None:
            async with AsyncSessionLocal() as db:
                return await get_token_for_org(owner, db)
        return await get_token_for_org(owner, db)

    @staticmethod
    def _normalize_environment_for_path(environment: str, tenant: str = "") -> str:
        """
        Normalize environment name for folder paths.

        Path mapping varies by tenant:
        - Default tenants: prod→prod, qa→qa, stage→stage, staging→staging, dev→dev
        - Vance/Aspora tenants: prod→prod, qa→qa, stage/staging/dev→stage

        Args:
            environment: Environment name (dev, staging, qa, stage, prod)
            tenant: Tenant identifier (for tenant-specific path logic)

        Returns:
            Normalized environment name for folder path
        """
        env_lower = environment.lower()
        tenant_lower = tenant.lower() if tenant else ""

        # Prod always maps to 'prod' for all tenants
        if env_lower == "prod":
            return "prod"

        # Stage always maps to 'stage' for all tenants
        if env_lower == "stage":
            return "stage"

        # QA always maps to 'qa' for all tenants
        if env_lower == "qa":
            return "qa"

        # Vance/Aspora: staging and dev both map to 'stage'
        if tenant_lower in VANCE_ASPORA_TENANTS:
            return "stage"

        # Default tenants: staging → 'staging', dev → 'dev'
        return env_lower

    @staticmethod
    def _normalize_environment_for_display(environment: str, tenant: str = "") -> str:
        """
        Normalize environment name for display purposes (PR title, atlantis name, feature branch).

        Display mapping varies by tenant:
        - Default tenants: prod→prod, staging→staging, dev→dev (no change)
        - Vance/Aspora tenants: prod→prod, staging/stage→stage, dev→dev

        This is DIFFERENT from path normalization where dev→stage for vance/aspora.

        Args:
            environment: Environment name (dev, staging, stage, prod)
            tenant: Tenant identifier (for tenant-specific display logic)

        Returns:
            Normalized environment name for display
        """
        env_lower = environment.lower()
        tenant_lower = tenant.lower() if tenant else ""

        # Prod always maps to 'prod' for all tenants
        if env_lower == "prod":
            return "prod"

        # Dev always stays as 'dev' for all tenants (different from path normalization!)
        if env_lower == "dev":
            return "dev"

        # Vance/Aspora: staging → 'stage' for display
        if tenant_lower in VANCE_ASPORA_TENANTS and env_lower in ("staging", "stage"):
            return "stage"

        # Default tenants: return as-is
        return env_lower

    @staticmethod
    def _normalize_service_for_path(service_name: str, environment: str, tenant: str = "") -> str:
        """
        Normalize service name for folder paths.

        Ensures service names always end with '-service' suffix.
        Service naming varies by tenant:
        - Default tenants: {base}-service (e.g., "casa" → "casa-service")
        - Vance/Aspora tenants:
          - dev → {base}-dev-service (e.g., "casa" → "casa-dev-service")
          - stage/staging/prod → {base}-service (e.g., "casa" → "casa-service")

        Args:
            service_name: Sanitized service name (may or may not have -service suffix)
            environment: Environment name (dev, staging, stage, prod)
            tenant: Tenant identifier (for tenant-specific path logic)

        Returns:
            Normalized service name with -service suffix
        """
        tenant_lower = tenant.lower() if tenant else ""
        env_lower = environment.lower()

        # Strip -service suffix to get base name (if present)
        if service_name.endswith("-service"):
            base_name = service_name[:-8]  # Remove "-service" (8 chars)
        else:
            base_name = service_name

        # Vance/Aspora + dev: insert -dev before -service
        if tenant_lower in VANCE_ASPORA_TENANTS and env_lower == "dev":
            return f"{base_name}-dev-service"

        # All other cases: just add -service suffix
        return f"{base_name}-service"

    @staticmethod
    def _get_envs_folder_name(environment: str, tenant: str = "") -> str:
        """
        Get the envs folder name based on tenant and environment.

        - Default tenants: always 'envs'
        - Vance/Aspora + dev: 'envs-dev'
        - Vance/Aspora + other envs: 'envs'

        Args:
            environment: Environment name (dev, staging, stage, prod)
            tenant: Tenant identifier

        Returns:
            Folder name ('envs' or 'envs-dev')
        """
        tenant_lower = tenant.lower() if tenant else ""
        env_lower = environment.lower()

        if tenant_lower in VANCE_ASPORA_TENANTS and env_lower == "dev":
            return "envs-dev"

        return "envs"

    @staticmethod
    def _get_service_name_for_env_files(service_name: str) -> str:
        """
        Get service name for env file paths (always {base}-service, no -dev- insertion).

        For env files, service name is always normalized to {base}-service format
        without any -dev- insertion, regardless of tenant or environment.

        Args:
            service_name: Sanitized service name (may or may not have -service suffix)

        Returns:
            Service name with -service suffix
        """
        if service_name.endswith("-service"):
            return service_name
        return f"{service_name}-service"

    @staticmethod
    def _normalize_region_for_path(region: str) -> str:
        """
        Normalize region for folder paths.
        Removes 'aws-' prefix if present.

        Args:
            region: AWS region (e.g., 'aws-ap-south-1' or 'ap-south-1')

        Returns:
            Normalized region (e.g., 'ap-south-1')
        """
        return region.replace("aws-", "").strip()

    @staticmethod
    def _get_aws_region_from_geo_loc(geo_loc: str) -> str:
        """
        Map business/deployment region (geo_loc) to AWS region.

        Args:
            geo_loc: Geographic location code (e.g., 'mumbai', 'uk')

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

    @staticmethod
    def _normalize_geo_loc_for_atlantis(geo_loc: str) -> str:
        """
        Normalize geo_loc for atlantis entry names (core-prod only).

        Maps various geo_loc codes to standardized location names.

        Args:
            geo_loc: Geographic location code (e.g., 'mumbai', 'uk', 'aspora-london')

        Returns:
            Normalized location name (e.g., 'london', 'mumbai')
        """
        mapping = {
            "mumbai": "mumbai",
            "aspora-mumbai": "mumbai",
            "region-aspora-mumbai": "mumbai",
            "london": "london",
            "uk": "london",
            "aspora-london": "london",
            "aspora-uk": "london",
            "region-aspora-london": "london",
            "us": "us",
            "aspora-us": "us",
            "region-aspora-us": "us",
        }
        return mapping.get(geo_loc.lower(), "london")  # Default: london

    @staticmethod
    def _get_common_infra_path(environment: str) -> str:
        """
        Get the common_infra dependency path based on environment.

        Args:
            environment: Environment name (dev, staging, stage, prod)

        Returns:
            Path for common_infra dependency:
            - dev → "../common-dev-infra"
            - stage/staging/prod → "../common-infra"
        """
        env_lower = environment.lower()
        if env_lower == "dev":
            return "../common-dev-infra"
        return "../common-infra"

    @staticmethod
    def _get_services_folder_name(environment: str, service_type: str = None) -> str:
        """
        Get the services folder name based on environment and service type.

        Args:
            environment: Environment name (dev, staging, stage, prod)
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Returns:
            Folder name for services:
            - OPS_TOOLS → "ops-tools" (regardless of environment - works for stage and prod)
            - dev → "services-dev"
            - stage/staging/prod → "services"
        """
        # OPS_TOOLS services go to ops-tools folder regardless of environment (stage or prod)
        if service_type == "OPS_TOOLS":
            return "ops-tools"

        env_lower = environment.lower()
        if env_lower == "dev":
            return "services-dev"
        return "services"

    @staticmethod
    def _get_atlantis_branch_pattern(environment: str, tenant: str = "") -> str:
        """
        Map environment to git branch pattern for atlantis.yaml.

        Branch mapping varies by tenant:
        - Default tenants: prod→main, stage→stage, staging→staging, dev→dev
        - Vance/Aspora tenants: prod→main, stage→stage, staging→stage, dev→stage

        Args:
            environment: Environment name (dev, staging, stage, prod)
            tenant: Tenant identifier (for tenant-specific branch logic)

        Returns:
            Git branch name for atlantis pattern
        """
        env_lower = environment.lower()
        tenant_lower = tenant.lower() if tenant else ""

        # Prod always maps to 'main' for all tenants
        if env_lower == "prod":
            return "main"

        # Stage always maps to 'stage' for all tenants
        if env_lower == "stage":
            return "stage"

        # Vance/Aspora: staging and dev both map to 'stage'
        if tenant_lower in VANCE_ASPORA_TENANTS:
            return "stage"

        # Default tenants: staging → 'staging', dev → 'dev'
        if env_lower == "staging":
            return "staging"

        # dev → 'dev' (default)
        return "dev"

    @staticmethod
    def _get_pr_base_branch(environment: str, tenant: str = "") -> str:
        """
        Get the base branch for PR creation.

        For Vance/Aspora tenants with prod environment, PRs go to stage first.
        All other cases follow the atlantis branch pattern.

        Args:
            environment: Environment name (dev, staging, stage, prod)
            tenant: Tenant identifier (for tenant-specific branch logic)

        Returns:
            Git branch name for PR base branch
        """
        env_lower = environment.lower()
        tenant_lower = tenant.lower() if tenant else ""

        # Vance/Aspora + prod → PR goes to stage first
        if tenant_lower in VANCE_ASPORA_TENANTS and env_lower == "prod":
            return "stage"

        # All other cases: same as atlantis pattern
        return TerragruntSyncService._get_atlantis_branch_pattern(environment, tenant)

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """
        Sanitize a name for use in file paths and branch names.

        Converts spaces, underscores, and hyphens to single hyphens,
        strips leading/trailing hyphens, and lowercases.

        Args:
            name: Raw name string

        Returns:
            Sanitized name (lowercase, hyphen-separated)

        Example:
            "My Service_Name" -> "my-service-name"
        """
        return re.sub(r'[\s_-]+', '-', name.strip()).strip('-').lower()

    def _build_github_file_path(
        self,
        product_name: str,
        service_name: str,
        environment: str,
        region: str,
        tenant: str = "",
        version_index: str = "01",
        service_type: str = None
    ) -> str:
        """
        Build the GitHub file path for a service's terragrunt.hcl file.

        Uses tenant-aware path normalization for environment and service name.

        Args:
            product_name: Application/product name
            service_name: Service name
            environment: Environment (dev, staging, prod)
            region: AWS region
            tenant: Tenant identifier (for tenant-specific path logic)
            version_index: Infrastructure version index (default "01")
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS) - determines folder

        Returns:
            Full GitHub file path

        Example:
            "environment/core-stage-01/ap-south-1/services/my-service/terragrunt.hcl"
            "environment/core-stage-01/ap-south-1/ops-tools/my-ops-tool/terragrunt.hcl"
        """
        product_sanitized = self._sanitize_name(product_name)
        service_sanitized = self._sanitize_name(service_name)
        service_for_path = self._normalize_service_for_path(service_sanitized, environment, tenant)
        env_sanitized = self._normalize_environment_for_path(environment, tenant)
        region_sanitized = self._normalize_region_for_path(region)
        services_folder = self._get_services_folder_name(environment, service_type)

        return f"environment/{product_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/{services_folder}/{service_for_path}/terragrunt.hcl"

    def _build_feature_branch_name(
        self,
        service_name: str,
        environment: str,
        hosting_type_name: str,
        geo_loc_mst_code: str = "",
        alb_selection: str = "",
        tenant: str = ""
    ) -> str:
        """
        Build timestamp-based feature branch name for service config PR.

        Uses hosting type short code and timestamp for shorter, unique branch names.
        Each save & sync creates a new branch.

        Args:
            service_name: Service name
            environment: Environment (normalized for vance/aspora: staging→stage, dev→stage)
            hosting_type_name: Full hosting type display name (e.g., "AWS ECS Fargate")
            geo_loc_mst_code: Kept for backwards compatibility (unused)
            alb_selection: Kept for backwards compatibility (unused)
            tenant: Tenant identifier for environment normalization

        Returns:
            Feature branch name

        Example:
            "ecs/service-my-service-stage-1702401234"
        """
        import time
        from app.utils.naming_helpers import get_hosting_type_short_code

        service_sanitized = self._sanitize_name(service_name)
        env_normalized = self._normalize_environment_for_display(environment, tenant)
        hosting_code = get_hosting_type_short_code(hosting_type_name)
        timestamp = int(time.time())

        return f"{hosting_code}/service-{service_sanitized}-{env_normalized}-{timestamp}"

    def _add_atlantis_ecs_entry(
        self,
        atlantis_content: str,
        file_path: str,
        product_name: str,
        env: str,
        service_name: str,
        tenant: str = "",
        geo_loc: str = ""
    ) -> str:
        """
        Add a new ECS service project entry to atlantis.yaml at the beginning of projects list.

        Args:
            atlantis_content: Current atlantis.yaml content
            file_path: Full terragrunt file path (e.g., "environment/core-stage-01/ap-south-1/services/my-service/terragrunt.hcl")
            product_name: Product name (e.g., "core")
            env: Environment (e.g., "dev", "staging", "prod")
            service_name: Service name
            tenant: Tenant identifier (for tenant-specific branch logic)
            geo_loc: Geographic location code for core-prod atlantis naming (e.g., 'mumbai', 'london')

        Returns:
            Modified atlantis.yaml content with new project entry
        """
        # Derive dir from file_path by removing "/terragrunt.hcl"
        dir_path = file_path.replace("/terragrunt.hcl", "")

        # Sanitize values for name construction
        product_sanitized = self._sanitize_name(product_name)
        service_sanitized = self._sanitize_name(service_name)

        # For atlantis NAME: use simple service name (no -dev- insertion)
        # Vance/Aspora: core-stage-casa-service (NOT core-dev-casa-dev-service)
        service_for_name = self._get_service_name_for_env_files(service_sanitized)

        # For atlantis NAME: use display normalization
        # Vance/Aspora: dev → dev, staging → stage
        env_for_name = self._normalize_environment_for_display(env, tenant)

        # Get branch pattern for this environment and tenant (for atlantis.yaml)
        branch_pattern = self._get_atlantis_branch_pattern(env, tenant)

        # Build name and workflow for ECS service
        # Include normalized geo_loc for core-prod only
        if product_sanitized == "core" and env_for_name == "prod" and geo_loc:
            geo_loc_normalized = self._normalize_geo_loc_for_atlantis(geo_loc)
            name = f"{product_sanitized}-{env_for_name}-{geo_loc_normalized}-{service_for_name}"
        else:
            name = f"{product_sanitized}-{env_for_name}-{service_for_name}"
        workflow = "terragrunt"

        # Check if this entry already exists (by name)
        if f"name: {name}" in atlantis_content:
            logger.info(f"Atlantis project entry already exists: {name}")
            return atlantis_content

        # Build the new project entry with exact formatting (2-space indents)
        new_entry = f"  - name: {name}\n"
        new_entry += f"    dir: {dir_path}\n"
        new_entry += f"    workflow: {workflow}\n"
        new_entry += f"    branch: /{branch_pattern}/\n"

        # Find the projects: section and insert at the beginning
        # Preserve blank line after "projects:" if it exists
        if 'projects:\n\n' in atlantis_content:
            # Has blank line - preserve it
            atlantis_content = atlantis_content.replace(
                'projects:\n\n',
                f'projects:\n\n{new_entry}',
                1  # Only replace first occurrence
            )
            logger.info(f"Added atlantis project entry: {name}")
        elif 'projects:\n' in atlantis_content:
            # No blank line - add entry directly
            atlantis_content = atlantis_content.replace(
                'projects:\n',
                f'projects:\n{new_entry}',
                1  # Only replace first occurrence
            )
            logger.info(f"Added atlantis project entry: {name}")
        else:
            logger.warning("Could not find projects: section in atlantis.yaml")

        return atlantis_content

    async def _get_previous_contributors_from_db(
        self,
        service_config_code: str,
        tenant: str
    ) -> list:
        """
        Fetch all previous users who contributed to this service config.

        Queries gitops_workflow_detail table to find all users who created
        previous PRs for this service config.

        Args:
            service_config_code: The service config code
            tenant: Tenant code

        Returns:
            List of contributor dicts: [{"name": "John Doe", "email": "john@example.com"}]
        """
        try:
            from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository

            async with AsyncSessionLocal() as session:
                repo = GitopsWorkflowDetailRepository(session)
                contributors_data = await repo.get_distinct_contributors_by_transaction(
                    transaction_code=service_config_code,
                    tenant_code=tenant
                )

                contributors = []
                for contributor in contributors_data:
                    first_name = contributor.get("first_name", "")
                    last_name = contributor.get("last_name", "")
                    name = f"{first_name} {last_name}".strip() or "Unknown"
                    contributors.append({
                        "name": name,
                        "email": contributor.get("email")
                    })

                logger.info(f"[COLLABORATIVE-PR] Found {len(contributors)} previous contributors from database")
                return contributors

        except Exception as e:
            logger.error(f"[COLLABORATIVE-PR] Error fetching contributors from database: {e}")
            return []  # Return empty list on error - don't block PR creation

    def _add_co_authors_to_message(
        self,
        message: str,
        contributors: list,
        current_user_email: str = None
    ) -> str:
        """
        Add co-authors to a commit message using Git co-author syntax.

        Args:
            message: Original commit message
            contributors: List of contributor dicts with 'name' and 'email'
            current_user_email: Current user's email (to exclude from co-authors)

        Returns:
            Enhanced commit message with co-authors
        """
        if not contributors:
            return message

        # Filter out current user and prepare co-author lines
        co_authors = []
        for contributor in contributors:
            email = contributor.get("email")
            name = contributor.get("name", "Unknown")

            # Skip current user (they're already the author)
            if current_user_email and email == current_user_email:
                continue

            co_authors.append(f"Co-authored-by: {name} <{email}>")

        if not co_authors:
            return message

        # Add co-authors to message
        enhanced_message = f"{message}\n\n"
        enhanced_message += "\n".join(co_authors)

        logger.info(f"[COLLABORATIVE-PR] Added {len(co_authors)} co-authors to commit message")
        return enhanced_message

    async def sync_config_to_hcl(
        self,
        service_config: ServiceConfigModel,
        service,  # ServicesMstModel with relationships loaded
        tenant: str,
        github_repository: Optional[str] = None,
        github_branch: str = "stage",
        push_to_github: bool = True,
        user_email: str = None,
        existing_dockerfile_prs: Optional[Dict[str, Dict]] = None,
        existing_terragrunt_pr: Optional[Dict] = None
    ) -> Dict[str, str]:
        """
        Generate Terragrunt HCL content in memory and push directly to GitHub (no local storage).

        Args:
            service_config: ServiceConfigModel with config data
            service: ServicesMstModel with service and application info (relationships loaded)
            tenant: Tenant identifier from auth token (for future decision logic)
            github_repository: GitHub repository in format "owner/repo" (optional)
            github_branch: Target GitHub branch (default: "stage")
            push_to_github: Whether to push to GitHub (default: True)
            user_email: User email (from JWT) for PR attribution
            existing_dockerfile_prs: Optional dict of existing open Dockerfile PRs by branch
                for smart PR replacement logic
            existing_terragrunt_pr: Optional dict of existing open Terragrunt PR info
                for smart PR replacement logic

        Returns:
            Dict with status, message, and github_sync result

        Example:
            {
                "status": "success",
                "message": "Terragrunt HCL synced to GitHub successfully",
                "github_sync": {"status": "success", "commit_sha": "abc123", ...}
            }
        """
        try:
            service_name = service.name
            logger.info(f"Starting Terragrunt sync for service: {service_name}")

            # Check if this is a no-alb configuration
            is_no_alb = self._is_no_alb(service_config.config)
            if is_no_alb:
                logger.info("Detected no-alb configuration, using no-alb template")

            # Get product name from application relationship
            product_name = service.application.name if service.application else ""

            # Get service_type from service model
            service_type = service.service_type.value if service.service_type else ""

            # Get the appropriate template file (prod templates for Vance/Aspora)
            template_file = self._get_template_file(
                service_config.config,
                environment=service_config.environment.value,
                tenant=tenant,
                product_name=product_name,
                service_type=service_type
            )

            # Build field mapping from config
            # Get sidecar_config (it's a JSONB column, not a relationship)
            sidecar_config = getattr(service_config, 'sidecar_config', None)

            # Get language name from language_ref relationship (should be loaded by caller)
            language_name = None
            if service_config.language_ref:
                language_name = service_config.language_ref.name
                logger.info(f"Language name for terragrunt: {language_name}")

            field_mapping = self._build_field_mapping(
                service_config.config,
                sidecar_config,
                language_name,
                is_no_alb=is_no_alb
            )

            logger.info(f"Built field mapping for {service_name}")

            # Push to GitHub if enabled (HCL generation happens inside push_to_github
            # to allow fetching existing content and using it as base)
            github_result = None
            if push_to_github and github_repository:
                # Run the full infra-repo provisioning flow if the repo is missing
                # (clone source, env files, secrets, infra_mst record) — same as
                # first-time org creation, not just a bare GitHub create-repo API call.
                if "/" in github_repository:
                    owner, repo_only = github_repository.split("/", 1)
                    try:
                        from app.services.org_infrastructure_service import OrgInfrastructureService
                        ensure_result = await OrgInfrastructureService(self.db).ensure_infra_repo_exists(
                            owner=owner,
                            repo_name=repo_only,
                        )
                        if ensure_result.get("auto_provisioned"):
                            logger.info(
                                f"Auto-provisioned missing infrastructure repo {github_repository}"
                            )
                    except Exception as e:
                        logger.error(
                            f"Failed to ensure repo {github_repository} exists: {e}"
                        )
                        raise

                github_result = await self.push_to_github(
                    service_config,
                    service,
                    field_mapping,
                    github_repository,
                    github_branch,
                    tenant=tenant,
                    template_file=template_file,
                    user_email=user_email,
                    existing_dockerfile_prs=existing_dockerfile_prs,
                    existing_terragrunt_pr=existing_terragrunt_pr
                )
                logger.info(f"GitHub push result: {github_result['status']}")
            elif push_to_github and not github_repository:
                logger.warning("GitHub push enabled but repository not provided")
                github_result = {
                    "status": "skipped",
                    "message": "GitHub repository not provided"
                }

            return {
                "status": "success",
                "message": "Terragrunt HCL synced to GitHub successfully",
                "github_sync": github_result
            }

        except Exception as e:
            logger.error(f"Terragrunt sync failed for {service_name}: {str(e)}", exc_info=True)
            return {
                "status": "error",
                "message": f"Terragrunt sync failed: {str(e)}"
            }

    def _get_hcl_filename(self, config: Optional[Dict]) -> str:
        """
        Determine HCL filename based on alb_selection.

        Args:
            config: Service config dict

        Returns:
            HCL filename (e.g., "existing-alb.hcl", "no-alb.hcl", "new-alb.hcl")
        """
        if not config:
            return "existing-alb.hcl"  # Default

        alb_selection = config.get("alb_selection", "existing_alb")

        filename_mapping = {
            "no_alb": "no-alb.hcl",
            "existing_alb": "existing-alb.hcl",
            "create_new_alb": "new-alb.hcl"
        }

        filename = filename_mapping.get(alb_selection, "existing-alb.hcl")
        logger.info(f"ALB selection: {alb_selection}, filename: {filename}")

        return filename

    def _get_template_file(
        self,
        config: Optional[Dict],
        environment: str = "",
        tenant: str = "",
        product_name: str = "",
        service_type: str = ""
    ) -> Path:
        """
        Get the correct template file path based on service_type, alb_selection, and tenant.

        Template selection logic:
        - service_type == "OPS_TOOLS" → ops-tools.hcl (no Datadog, no Kong, alarms disabled)
        - service_type == "BACKGROUND_SERVICE" → worker-no-alb.hcl (workers without ALB)
        - alb_selection == "no_alb" → worker-no-alb.hcl (workers without ALB)
        - alb_selection == "create_new_alb" → api-new-alb.hcl (API with dedicated ALB)
        - alb_selection == "existing_alb" (default) → api-common-alb.hcl (API with shared ALB)

        Tenant-specific templates (future):
        - Currently all tenants use the same templates
        - Structure in place for future tenant-specific templates

        Args:
            config: Service config dict
            environment: Environment name (dev, staging, prod) - reserved for future rules
            tenant: Tenant identifier - for future tenant-specific templates
            product_name: Product name - reserved for future rules
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Returns:
            Path to the appropriate template file
        """
        # OPS_TOOLS services use dedicated template (no Datadog, no Kong, alarms disabled)
        if service_type == "OPS_TOOLS":
            logger.info(f"Using ops-tools template (tenant={tenant}, service_type={service_type})")
            return self.ops_tools_template

        # BACKGROUND_SERVICE always uses worker-no-alb template (ignore alb_selection)
        if service_type == "BACKGROUND_SERVICE":
            logger.info(f"Using worker-no-alb template (tenant={tenant}, service_type={service_type})")
            return self.worker_no_alb_template

        if not config:
            logger.info(f"Using api-common-alb template (tenant={tenant}, no config)")
            return self.api_common_alb_template

        alb_selection = config.get("alb_selection", "existing_alb")

        if alb_selection == "no_alb":
            logger.info(f"Using worker-no-alb template (tenant={tenant})")
            return self.worker_no_alb_template
        elif alb_selection == "create_new_alb":
            logger.info(f"Using api-new-alb template (tenant={tenant})")
            return self.api_new_alb_template
        else:
            # Default: existing_alb or any other value
            logger.info(f"Using api-common-alb template (tenant={tenant})")
            return self.api_common_alb_template

    def _is_no_alb(self, config: Optional[Dict]) -> bool:
        """
        Check if the configuration uses no ALB.

        Args:
            config: Service config dict

        Returns:
            True if no ALB is configured
        """
        if not config:
            return False
        return config.get("alb_selection") == "no_alb"

    def _apply_environment_rules(
        self,
        content: str,
        environment: str,
        product_name: str = "",
        tenant: str = "",
        field_mapping: Optional[Dict[str, str]] = None,
        service_type: str = ""
    ) -> str:
        """
        Apply environment-based rules to HCL content.

        Rules applied (same for ALL tenants):
        - Rule 1: Priority alarms
          - Prod: Priority alarm SNS topics enabled (uncommented)
          - Stage/Staging/Dev: Priority alarm SNS topics commented out
        - Rule 2: capacity_provider_name
          - OPS_TOOLS: always enabled (uncommented) - uses EC2 capacity provider
          - Dev: enabled (uncommented)
          - Stage/Staging: commented out (uses Fargate)
          - Prod + Core: enabled (uncommented)
          - Prod + Falcon: commented out
        - Rule 3: create_alarms
          - Prod: true
          - Stage/Staging/Dev: false
        - Rule 4: enable_datadog_sidecar (API value takes priority)
          - Prod/Stage/Staging: true
          - Dev: false

        Note: Rule 4 is skipped if explicit value provided via API (field_mapping).
        Note: Uses RAW environment value (not normalized). Normalization is only for path/branch logic.

        Args:
            content: HCL content string
            environment: Environment name (dev, staging, stage, prod)
            product_name: Product name (e.g., "core", "falcon")
            tenant: Tenant identifier - not used for rules, kept for logging
            field_mapping: Optional dict of API-provided field values (Rule 4 skips if field present)
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Returns:
            Modified HCL content with environment-specific values
        """
        # Use raw environment for rules (not normalized - normalization is only for path/branch logic)
        env_lower = environment.lower()
        product_lower = product_name.lower() if product_name else ""
        is_prod = env_lower == "prod"

        logger.info(f"Applying environment rules for: env={env_lower}, product={product_name}, tenant={tenant}")

        # Priority alarm SNS topics
        priority_alarm_fields = [
            "devops_p0_alarm_sns_topic_arn",
            "devops_p1_alarm_sns_topic_arn",
            "devs_p0_alarm_sns_topic_arn",
            "devs_p1_alarm_sns_topic_arn"
        ]

        # Rule 1: Priority alarms - comment out for non-prod (same rule for ALL tenants)
        should_comment_priority_alarms = not is_prod

        if should_comment_priority_alarms:
            # Comment out priority alarm SNS topics (using // style)
            for field in priority_alarm_fields:
                pattern = rf'^(\s*)({field}\s*=\s*[^\n]+)$'
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(
                        pattern,
                        r'\1// \2',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.debug(f"Commented out {field}")

            logger.info(f"Commented out priority alarms (tenant={tenant}, env={environment})")
        else:
            # Uncomment for prod (handles both # and // comment styles)
            for field in priority_alarm_fields:
                pattern = rf'^(\s*)(#|//)\s*({field}\s*=\s*[^\n]+)$'
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(
                        pattern,
                        r'\1\3',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.debug(f"Uncommented {field}")

            logger.info(f"Priority alarms enabled (tenant={tenant}, env={environment})")

        # Rule 2: capacity_provider_name - comment out for:
        # - stage/staging environment (all products) - EXCEPT OPS_TOOLS
        # - prod + falcon product
        # OPS_TOOLS always uses EC2 capacity provider (never Fargate)
        is_ops_tools = service_type == "OPS_TOOLS"
        should_comment_capacity_provider = (
            not is_ops_tools and (
                env_lower in ["stage", "staging"] or
                (env_lower == "prod" and product_lower == "falcon")
            )
        )

        if should_comment_capacity_provider:
            # Comment out capacity_provider_name in inputs section (using // style)
            pattern = r'^(\s*)(capacity_provider_name\s*=\s*[^\n]+)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1// \2',
                    content,
                    flags=re.MULTILINE
                )
                logger.info(f"Commented out capacity_provider_name (tenant={tenant}, env={environment}, product={product_name})")
        else:
            # Ensure capacity_provider_name is uncommented for dev, prod-core, and OPS_TOOLS (handles both # and // styles)
            pattern = r'^(\s*)(#|//)\s*(capacity_provider_name\s*=\s*[^\n]+)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1\3',
                    content,
                    flags=re.MULTILINE
                )
                logger.debug(f"Uncommented capacity_provider_name (tenant={tenant}, env={environment}, product={product_name}, service_type={service_type})")

        # Rule 3: create_alarms - false for non-prod, true for prod
        if not is_prod:
            # Set create_alarms = false for dev/stage/staging
            pattern = r'^(\s*)(create_alarms\s*=\s*)true(\s*)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1\2false\3',
                    content,
                    flags=re.MULTILINE
                )
                logger.info(f"Set create_alarms = false (env={environment})")
        else:
            # Ensure create_alarms = true for prod
            pattern = r'^(\s*)(create_alarms\s*=\s*)false(\s*)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1\2true\3',
                    content,
                    flags=re.MULTILINE
                )
                logger.debug(f"Set create_alarms = true (env={environment})")

        # Rule 3b: alb_arn_suffix - uncomment for prod (when create_alarms is true), comment for dev/stage
        # alb_arn_suffix is needed for CloudWatch ALB metrics in prod
        if is_prod:
            # Uncomment alb_arn_suffix for prod
            alb_suffix_pattern = r'^(\s*)(//\s*)(alb_arn_suffix\s*=\s*[^\n]+)'
            if re.search(alb_suffix_pattern, content, re.MULTILINE):
                content = re.sub(alb_suffix_pattern, r'\1\3', content, flags=re.MULTILINE)
                logger.info(f"Uncommented alb_arn_suffix (env={environment}, create_alarms=true)")
        else:
            # Comment out alb_arn_suffix for dev/stage
            alb_suffix_uncommented = r'^(\s*)(alb_arn_suffix\s*=\s*[^\n]+)'
            match = re.search(alb_suffix_uncommented, content, re.MULTILINE)
            if match:
                line_start = content.rfind('\n', 0, match.start()) + 1
                line_prefix = content[line_start:match.start()]
                if '//' not in line_prefix:
                    content = re.sub(alb_suffix_uncommented, r'\1// \2', content, flags=re.MULTILINE)
                    logger.info(f"Commented out alb_arn_suffix (env={environment})")

        # Rule 4: enable_datadog_sidecar - false for dev only, true for stage/staging/prod
        # Skip if API provided explicit value
        if not (field_mapping and "enable_datadog_sidecar" in field_mapping):
            is_dev = env_lower == "dev"

            if is_dev:
                # Set enable_datadog_sidecar = false for dev
                pattern = r'^(\s*)(enable_datadog_sidecar\s*=\s*)true(\s*)$'
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(
                        pattern,
                        r'\1\2false\3',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.info(f"Set enable_datadog_sidecar = false (env={environment})")
            else:
                # Ensure enable_datadog_sidecar = true for stage/staging/prod
                pattern = r'^(\s*)(enable_datadog_sidecar\s*=\s*)false(\s*)$'
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(
                        pattern,
                        r'\1\2true\3',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.info(f"Set enable_datadog_sidecar = true (env={environment})")
        else:
            logger.info(f"Skipping enable_datadog_sidecar rule - API value provided")

        return content

    def _ensure_service_folder(self, service_name: str) -> Path:
        """
        Create service folder if it doesn't exist.

        Args:
            service_name: Service name for folder

        Returns:
            Path to service folder

        Raises:
            OSError: If folder creation fails
        """
        service_folder = self.templates_dir / service_name
        service_folder.mkdir(parents=True, exist_ok=True)
        logger.info(f"Service folder created/verified: {service_folder}")
        return service_folder

    def _copy_template_if_needed(self, target_path: Path) -> None:
        """
        Copy template file to target path.

        Args:
            target_path: Target file path

        Raises:
            FileNotFoundError: If template file doesn't exist
            OSError: If file copy fails
        """
        if not self.template_file.exists():
            raise FileNotFoundError(f"Template file not found: {self.template_file}")

        shutil.copy(self.template_file, target_path)

    def _build_field_mapping(
        self,
        config: Optional[Dict],
        sidecar_config: Optional[list] = None,
        language_name: Optional[str] = None,
        is_no_alb: bool = False
    ) -> Dict[str, str]:
        """
        Build field mapping from config dict to Terragrunt field names.

        Transforms database field names to Terragrunt HCL field names and formats values.
        When is_no_alb is True, autoscaling-related fields are excluded as they don't exist
        in the no-alb template.

        Args:
            config: Service config dict from JSONB
            sidecar_config: Sidecar config list from JSONB [{sidecar_config_code, enabled, cpu, ram, name}]
            language_name: Language name from language_ref table (e.g., "java", "python")
            is_no_alb: Whether this is a no-ALB configuration (skips autoscaling fields)

        Returns:
            Dict mapping Terragrunt field names to formatted values

        Example:
            {
                "enable_autoscaling": "true",
                "desired_count": "4",
                "cpu": "3500",
                "memory": "7500",
                "service_path": "\\"/*\\"",
                "enable_ulimits": "true",
                "enable_datadog_sidecar": "true",
                "datadog_log_source": "\\"java\\""
            }
        """
        field_mapping = {}

        # Datadog sidecar configuration from sidecar_config
        if sidecar_config:
            # Check if datadog sidecar is enabled and extract its cpu/ram
            datadog_enabled = False
            datadog_cpu = None
            datadog_ram = None
            datadog_logs_enabled = None
            for sidecar in sidecar_config:
                # Check both name and sidecar_config_code for "datadog"
                sidecar_name = (sidecar.get('name') or '').lower()
                sidecar_code = (sidecar.get('sidecar_config_code') or '').lower()
                is_datadog = 'datadog' in sidecar_name or 'datadog' in sidecar_code
                if is_datadog and sidecar.get('enabled', False):
                    datadog_enabled = True
                    datadog_cpu = sidecar.get('cpu')
                    datadog_ram = sidecar.get('ram')
                    datadog_logs_enabled = sidecar.get('datadog_logs_enabled')
                    break
            field_mapping["enable_datadog_sidecar"] = str(datadog_enabled).lower()
            # Add datadog sidecar cpu and memory if available
            if datadog_cpu:
                field_mapping["datadog_sidecar_cpu"] = str(datadog_cpu)
            if datadog_ram:
                field_mapping["datadog_sidecar_memory"] = str(datadog_ram)
            # Add datadog_logs_enabled if set
            if datadog_logs_enabled is not None:
                field_mapping["datadog_logs_enabled"] = str(datadog_logs_enabled).lower()
        else:
            field_mapping["enable_datadog_sidecar"] = "false"

        # Datadog log source from language_ref (base language only, no version)
        if language_name:
            # Extract base language name (e.g., "go 1.23" -> "go", "python 3.12" -> "python")
            base_language = language_name.split()[0].lower()
            field_mapping["datadog_log_source"] = f'"{base_language}"'

        if not config:
            return field_mapping

        # Auto Scaling Configuration - SKIP for no_alb (fields don't exist in no-alb template)
        if not is_no_alb:
            # Extract autoscaling config (nested structure)
            autoscaling = config.get("autoscaling", {})

            if "enabled" in autoscaling:
                field_mapping["enable_autoscaling"] = str(autoscaling["enabled"]).lower()

            if autoscaling.get("desired"):
                field_mapping["desired_count"] = autoscaling["desired"]

            if autoscaling.get("min"):
                field_mapping["min_task_count"] = autoscaling["min"]

            if autoscaling.get("max"):
                field_mapping["max_task_count"] = autoscaling["max"]
        else:
            logger.info("Skipping autoscaling fields for no-alb configuration")

        # Resource Allocation
        if config.get("port"):
            field_mapping["container_port"] = config["port"]

        if config.get("cpu"):
            # Convert CPU from vCPU to CPU units (1 vCPU = 1024 CPU units)
            # Database stores "2" (vCPU) -> Terragrunt needs 2048 (CPU units)
            cpu_value = config["cpu"]
            try:
                cpu_in_vcpu = float(cpu_value)
                cpu_in_units = int(cpu_in_vcpu * 1024)
                field_mapping["cpu"] = str(cpu_in_units)
            except (ValueError, TypeError):
                # If conversion fails, use the value as-is
                field_mapping["cpu"] = str(cpu_value)
                logger.warning(f"Could not convert CPU value '{cpu_value}' to units, using as-is")

        if config.get("ram"):
            # Convert RAM from GB to MB (1 GB = 1024 MB)
            # Database stores "4" (GB) -> Terragrunt needs 4096 (MB)
            ram_value = config["ram"]
            try:
                ram_in_gb = float(ram_value)
                ram_in_mb = int(ram_in_gb * 1024)
                field_mapping["memory"] = str(ram_in_mb)
            except (ValueError, TypeError):
                # If conversion fails, use the value as-is
                field_mapping["memory"] = str(ram_value)
                logger.warning(f"Could not convert RAM value '{ram_value}' to MB, using as-is")

        # Service Configuration - ALB only (service_path and listener_rule_priority don't exist in no-alb template)
        if not is_no_alb:
            if config.get("service_path"):
                # String values need quotes in HCL
                field_mapping["service_path"] = f'"{config["service_path"]}"'

            if config.get("listener_rule_priority"):
                field_mapping["listener_rule_priority"] = config["listener_rule_priority"]

            # Health Check - ALB only (commented out in no-alb template)
            if config.get("health"):
                # String values need quotes in HCL
                field_mapping["health_check_path"] = f'"{config["health"]}"'

            # HTTP Scaling - ALB only
            if "http_scaling_enabled" in config:
                field_mapping["http_scaling_enabled"] = str(config["http_scaling_enabled"]).lower()

            if config.get("http_scaling_target_value"):
                field_mapping["http_scaling_target_value"] = config["http_scaling_target_value"]
        else:
            logger.info("Skipping ALB-only fields (service_path, listener_rule_priority, health_check_path, http_scaling) for no-alb configuration")

        # Process Limits
        if "enable_ulimits" in config:
            field_mapping["enable_ulimits"] = str(config["enable_ulimits"]).lower()

        logger.info(f"Built field mapping with {len(field_mapping)} fields (no_alb={is_no_alb})")
        return field_mapping

    def _add_field_to_inputs_end(self, content: str, field_name: str, new_value: str) -> str:
        """
        Add field before the closing brace of inputs block.

        This is the fallback method when no anchor fields are found.

        Args:
            content: HCL content string
            field_name: Name of the field to add
            new_value: Value for the field

        Returns:
            Updated HCL content with the new field added at end of inputs block
        """
        # Find inputs = { and its matching }
        inputs_match = re.search(r'inputs\s*=\s*\{', content)
        if not inputs_match:
            logger.warning("Could not find 'inputs' block to add field")
            return content

        # Find matching closing brace by counting braces
        start_pos = inputs_match.end()
        brace_count = 1
        end_pos = start_pos

        for i, char in enumerate(content[start_pos:], start_pos):
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_pos = i
                    break

        # Find the start of the line containing the closing brace
        # to insert before it (preserving indentation of the brace)
        line_start = content.rfind('\n', 0, end_pos)
        if line_start == -1:
            line_start = 0
        else:
            line_start += 1  # Move past the newline

        new_field_line = f"  {field_name} = {new_value}\n"
        return content[:line_start] + new_field_line + content[line_start:]

    def _add_field_after_anchor(self, content: str, field_name: str, new_value: str) -> str:
        """
        Add a new field after an anchor field in HCL content.

        Looks for anchor fields (related fields in the same section) and inserts
        the new field after the last found anchor. Falls back to end of inputs block.

        Args:
            content: HCL content string
            field_name: Name of the field to add
            new_value: Value for the field

        Returns:
            Updated HCL content with the new field added
        """
        anchors = FIELD_ANCHOR_MAP.get(field_name, [])

        for anchor in anchors:
            # Pattern to find the anchor field and its value (entire line)
            anchor_pattern = rf'(\n\s*{anchor}\s*=\s*[^\n]+)'
            match = re.search(anchor_pattern, content)
            if match:
                # Insert after this anchor field's line
                insert_pos = match.end(1)
                new_field_line = f"\n  {field_name} = {new_value}"
                return content[:insert_pos] + new_field_line + content[insert_pos:]

        # Fallback: insert before the closing } of inputs block
        return self._add_field_to_inputs_end(content, field_name, new_value)

    def _has_datadog_params(self, content: str, field_mapping: Dict[str, str]) -> bool:
        """
        Check if any Datadog parameters are present in content or being inserted via field_mapping.

        Args:
            content: HCL content string
            field_mapping: Dict of field names to new values being inserted

        Returns:
            True if any Datadog parameters are found, False otherwise
        """
        # Check if any datadog params are in the field_mapping (being inserted)
        if any(param in field_mapping for param in DATADOG_PARAMS):
            return True

        # Check if any datadog params already exist in the content
        for param in DATADOG_PARAMS:
            if re.search(rf'\b{param}\s*=', content):
                return True

        return False

    def _ensure_datadog_dependency(self, content: str) -> str:
        """
        Ensure dependency "datadog_api_key" block exists in HCL content.

        If missing, insert it before the 'dependencies {' block.
        Uses simple pattern matching to avoid content corruption.

        Args:
            content: HCL content string

        Returns:
            Updated HCL content with datadog dependency block
        """
        # Check if datadog_api_key dependency already exists
        if re.search(r'dependency\s+"datadog_api_key"\s*\{', content):
            logger.info("datadog_api_key dependency already exists")
            return content  # Already exists

        logger.info("datadog_api_key dependency not found, inserting...")

        # Simple approach: find 'dependencies {' line and insert before it
        # This avoids complex nested brace matching which can corrupt content
        deps_block_pattern = r'(\n)(dependencies\s*\{)'
        match = re.search(deps_block_pattern, content)

        if match:
            insert_pos = match.start(1)
            logger.info("Inserting datadog dependency before dependencies block")
            return content[:insert_pos] + "\n\n" + DATADOG_DEPENDENCY_BLOCK + "\n" + content[insert_pos:]

        logger.warning("Could not find 'dependencies' block for insertion point")
        return content

    def _ensure_datadog_in_dependencies_paths(self, content: str) -> str:
        """
        Ensure '../../secrets/datadog-configs' is in the dependencies paths array.

        Uses simple pattern matching to avoid content corruption.

        Args:
            content: HCL content string

        Returns:
            Updated HCL content with datadog path in dependencies
        """
        datadog_path = '"../../secrets/datadog-configs"'

        # Check if already present in the paths array (not elsewhere in content)
        # Look specifically for it inside paths = [...]
        paths_array_match = re.search(r'paths\s*=\s*\[[^\]]*\]', content)
        if paths_array_match and '../../secrets/datadog-configs' in paths_array_match.group(0):
            logger.info("datadog-configs path already in dependencies paths array")
            return content

        logger.info("Adding datadog-configs to dependencies paths...")

        # Simple pattern: find paths array and insert before closing bracket
        # [^\]]+ matches everything inside the array (non-empty)
        pattern = r'(paths\s*=\s*\[[^\]]+)\]'

        def add_datadog_path(match):
            return match.group(1) + ', ' + datadog_path + ']'

        new_content = re.sub(pattern, add_datadog_path, content, count=1)

        if new_content == content:
            logger.warning("Could not find paths array to add datadog-configs")

        return new_content

    def _update_hcl_values(self, hcl_path: Path, field_mapping: Dict[str, str]) -> None:
        """
        Update HCL file values using regex replacement.

        Preserves HCL formatting and only updates specified fields.

        Args:
            hcl_path: Path to HCL file
            field_mapping: Dict of field names to new values

        Raises:
            IOError: If file read/write fails
        """
        # Read file content
        with open(hcl_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Update each field
        updates_count = 0
        for field_name, new_value in field_mapping.items():
            # Pattern: field_name (as whole word) followed by optional whitespace, =, optional whitespace, and the old value
            # Uses word boundary \b to prevent matching substrings (e.g., "cpu" shouldn't match "datadog_sidecar_cpu")
            # This matches: "field_name = old_value" or "field_name=old_value"
            pattern = rf'(\b{field_name}\s*=\s*)([^\n]+)'

            # Check if field exists in file
            if re.search(pattern, content):
                # Replace with: field_name = new_value (preserve spacing before =)
                # Use lambda to avoid regex backreference issues when new_value contains digits
                content = re.sub(
                    pattern,
                    lambda m: m.group(1) + new_value,
                    content
                )
                updates_count += 1
                logger.debug(f"Updated field: {field_name} = {new_value}")
            else:
                # Add the field after an anchor field
                content = self._add_field_after_anchor(content, field_name, new_value)
                updates_count += 1
                logger.info(f"Added new field: {field_name} = {new_value}")

        # Write updated content back
        with open(hcl_path, 'w', encoding='utf-8') as f:
            f.write(content)

        logger.info(f"Updated {updates_count}/{len(field_mapping)} fields in HCL file")

    def _generate_hcl_content(
        self,
        field_mapping: Dict[str, str],
        existing_content: Optional[str] = None,
        template_file: Optional[Path] = None,
        environment: Optional[str] = None,
        service_name: Optional[str] = None,
        tenant: Optional[str] = None,
        product_name: Optional[str] = None,
        service_type: Optional[str] = None
    ) -> str:
        """
        Generate HCL content in memory with field values applied.

        Uses existing content from GitHub as base if available, otherwise uses template.
        This ensures only changed fields are modified, preserving any manual edits.

        Args:
            field_mapping: Dict of field names to new values
            existing_content: Optional existing HCL content from GitHub to use as base
            template_file: Optional template file path (defaults to existing-alb template)
            environment: Optional environment name for dynamic path adjustments (dev, staging, prod)
            service_name: Optional service name for env file paths
            tenant: Optional tenant for env folder naming and rule application
            product_name: Optional product name for rule application (e.g., "core", "falcon")
            service_type: Optional service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Returns:
            HCL content as string with updated field values

        Raises:
            FileNotFoundError: If template file doesn't exist (when no existing content)
        """
        # Use the provided template or default to existing-alb template
        template_path = template_file or self.template_file

        # Use existing content as base if available, otherwise read from template
        if existing_content:
            content = existing_content
            logger.info("Using existing HCL content from GitHub as base")
        else:
            if not template_path.exists():
                raise FileNotFoundError(f"Template file not found: {template_path}")

            with open(template_path, 'r', encoding='utf-8') as f:
                content = f.read()
            logger.info(f"Using template file as base: {template_path.name}")

        # Check if any Datadog parameters are present or being inserted
        # If so, ensure the Datadog dependency infrastructure exists
        if self._has_datadog_params(content, field_mapping):
            logger.info("Datadog parameters detected, ensuring dependency infrastructure...")
            # Ensure datadog dependency block exists
            content = self._ensure_datadog_dependency(content)
            # Ensure datadog path in dependencies
            content = self._ensure_datadog_in_dependencies_paths(content)
            # Add datadog_secret_arn to field_mapping if not already present in content or mapping
            if 'datadog_secret_arn' not in field_mapping and not re.search(r'\bdatadog_secret_arn\s*=', content):
                field_mapping['datadog_secret_arn'] = 'dependency.datadog_api_key.outputs.secret_manager_arn'
                logger.info("Added datadog_secret_arn to field_mapping")
            # Disable OTel sidecar when Datadog is enabled (they are mutually exclusive)
            if 'enable_otel_sidecar' not in field_mapping:
                field_mapping['enable_otel_sidecar'] = 'false'
                logger.info("Setting enable_otel_sidecar = false (Datadog enabled)")

        # Update each field in memory
        updates_count = 0
        for field_name, new_value in field_mapping.items():
            # Pattern: field_name (as whole word) followed by optional whitespace, =, optional whitespace, and the old value
            # Uses word boundary \b to prevent matching substrings (e.g., "cpu" shouldn't match "datadog_sidecar_cpu")
            pattern = rf'(\b{field_name}\s*=\s*)([^\n]+)'

            # Check if field exists
            if re.search(pattern, content):
                # Replace with: field_name = new_value (preserve spacing before =)
                content = re.sub(
                    pattern,
                    lambda m: m.group(1) + new_value,
                    content
                )
                updates_count += 1
                logger.debug(f"Updated field in memory: {field_name} = {new_value}")
            else:
                # Add the field after an anchor field
                content = self._add_field_after_anchor(content, field_name, new_value)
                updates_count += 1
                logger.info(f"Added new field: {field_name} = {new_value}")

        # Update common_infra dependency path based on environment
        # dev → ../common-dev-infra, stage/prod → ../common-infra
        if environment:
            common_infra_path = self._get_common_infra_path(environment)

            # Pattern to match config_path inside dependency "common_infra" block
            # This specifically targets the common_infra dependency block
            common_infra_pattern = r'(dependency\s+"common_infra"\s*\{[^}]*config_path\s*=\s*)"[^"]*"'
            if re.search(common_infra_pattern, content, re.DOTALL):
                content = re.sub(
                    common_infra_pattern,
                    rf'\1"{common_infra_path}"',
                    content,
                    flags=re.DOTALL
                )
                logger.info(f"Updated common_infra config_path to: {common_infra_path}")
            else:
                logger.warning("Could not find common_infra dependency block to update config_path")

            # Also update the dependencies block paths
            # Replace "../common-infra" or "../common-dev-infra" with the correct path
            dependencies_pattern = r'(dependencies\s*\{[^}]*paths\s*=\s*\[[^\]]*)"\.\./(common-infra|common-dev-infra)"'
            if re.search(dependencies_pattern, content, re.DOTALL):
                content = re.sub(
                    dependencies_pattern,
                    rf'\1"{common_infra_path}"',
                    content,
                    flags=re.DOTALL
                )
                logger.info(f"Updated dependencies paths to use: {common_infra_path}")
            else:
                logger.debug("Dependencies block path pattern not found (may already be correct)")

        # Uncomment and update service_container_secrets and service_container_configs paths
        # Skip for Falcon services (aspora/vance tenants) - env files are not created for these
        service_lower = (service_name or "").lower()
        falcon_services = {
            "falcon-api", "falcon-consumer", "falcon-worker",
            "falcon-api-dev-service", "falcon-consumer-dev-service", "falcon-worker-dev-service"
        }
        skip_env_file_uncomment = (
            (product_name or "").lower() == "falcon" and
            service_lower in falcon_services and
            (tenant or "").lower() in VANCE_ASPORA_TENANTS
        )

        if skip_env_file_uncomment:
            logger.info(
                f"Skipping env file uncommenting for Falcon service: "
                f"product={product_name}, service={service_name}, tenant={tenant}"
            )

        if environment and service_name and not skip_env_file_uncomment:
            service_sanitized = self._sanitize_name(service_name)
            service_for_env = self._get_service_name_for_env_files(service_sanitized)
            envs_folder = self._get_envs_folder_name(environment, tenant or "")

            # Build the correct paths based on service_type
            # OPS_TOOLS: use dev-tools shared folder (../../envs/dev-tools/secure/{service}-secrets.json)
            # Standard: use service-specific folder (../../envs/{service}/secure/{service}-secrets.json)
            if service_type == "OPS_TOOLS":
                secrets_path = f"../../{envs_folder}/dev-tools/secure/{service_for_env}-secrets.json"
                configs_path = f"../../{envs_folder}/dev-tools/non-secure/{service_for_env}-configs.json"
                logger.info(f"Using OPS_TOOLS env path pattern with dev-tools folder")
            else:
                secrets_path = f"../../{envs_folder}/{service_for_env}/secure/{service_for_env}-secrets.json"
                configs_path = f"../../{envs_folder}/{service_for_env}/non-secure/{service_for_env}-configs.json"

            # Uncomment and update service_container_secrets
            # Pattern matches: # or // service_container_secrets = jsondecode(file("..."))
            secrets_pattern = r'(#|//)\s*service_container_secrets\s*=\s*jsondecode\(file\("[^"]*"\)\)'
            secrets_replacement = f'service_container_secrets = jsondecode(file("{secrets_path}"))'
            if re.search(secrets_pattern, content):
                content = re.sub(secrets_pattern, secrets_replacement, content)
                logger.info(f"Uncommented and updated service_container_secrets path to: {secrets_path}")
            else:
                # Check if already uncommented, update the path
                secrets_uncommented_pattern = r'(service_container_secrets\s*=\s*jsondecode\(file\(")[^"]*("\)\))'
                if re.search(secrets_uncommented_pattern, content):
                    content = re.sub(secrets_uncommented_pattern, rf'\1{secrets_path}\2', content)
                    logger.info(f"Updated service_container_secrets path to: {secrets_path}")

            # Uncomment and update service_container_configs
            # Pattern matches: # or // service_container_configs = jsondecode(file("..."))
            configs_pattern = r'(#|//)\s*service_container_configs\s*=\s*jsondecode\(file\("[^"]*"\)\)'
            configs_replacement = f'service_container_configs = jsondecode(file("{configs_path}"))'
            if re.search(configs_pattern, content):
                content = re.sub(configs_pattern, configs_replacement, content)
                logger.info(f"Uncommented and updated service_container_configs path to: {configs_path}")
            else:
                # Check if already uncommented, update the path
                configs_uncommented_pattern = r'(service_container_configs\s*=\s*jsondecode\(file\(")[^"]*("\)\))'
                if re.search(configs_uncommented_pattern, content):
                    content = re.sub(configs_uncommented_pattern, rf'\1{configs_path}\2', content)
                    logger.info(f"Updated service_container_configs path to: {configs_path}")

        # Handle existing_alb_arn based on http_scaling_enabled
        # When http_scaling_enabled is true → uncomment existing_alb_arn
        # When http_scaling_enabled is false → comment out existing_alb_arn
        http_scaling_enabled = field_mapping.get("http_scaling_enabled") == "true"
        if http_scaling_enabled:
            # Uncomment existing_alb_arn if it's commented
            # Pattern matches: // existing_alb_arn = ... (with optional leading whitespace)
            alb_arn_commented_pattern = r'^(\s*)(//\s*)(existing_alb_arn\s*=\s*[^\n]+)'
            if re.search(alb_arn_commented_pattern, content, re.MULTILINE):
                content = re.sub(alb_arn_commented_pattern, r'\1\3', content, flags=re.MULTILINE)
                logger.info("Uncommented existing_alb_arn (http_scaling_enabled is true)")
        else:
            # Comment out existing_alb_arn if it's not already commented
            # Pattern matches: existing_alb_arn = ... (not starting with //)
            alb_arn_uncommented_pattern = r'^(\s*)(existing_alb_arn\s*=\s*[^\n]+)'
            # Only match if not already commented (negative lookbehind for //)
            if re.search(alb_arn_uncommented_pattern, content, re.MULTILINE):
                # Check if the line is already commented
                match = re.search(alb_arn_uncommented_pattern, content, re.MULTILINE)
                if match:
                    line_start = content.rfind('\n', 0, match.start()) + 1
                    line_prefix = content[line_start:match.start()]
                    # Only comment if not already commented
                    if '//' not in line_prefix:
                        content = re.sub(alb_arn_uncommented_pattern, r'\1// \2', content, flags=re.MULTILINE)
                        logger.info("Commented out existing_alb_arn (http_scaling_enabled is false)")

        # Note: alb_arn_suffix is handled in _apply_environment_rules() based on is_prod flag
        # (not here, because create_alarms is not in field_mapping - it's set by environment rules)

        # Apply environment and tenant-based rules only for NEW services (not updates)
        # Rules: priority alarms, capacity_provider_name, create_alarms, datadog
        if environment and existing_content is None:
            content = self._apply_environment_rules(
                content,
                environment,
                product_name=product_name or "",
                tenant=tenant or "",
                field_mapping=field_mapping,
                service_type=service_type or ""
            )

        # For OPS_TOOLS: Always ensure capacity_provider_name is uncommented (even on updates)
        # OPS_TOOLS uses EC2 capacity provider, never Fargate
        if service_type == "OPS_TOOLS" and existing_content is not None:
            pattern = r'^(\s*)(#|//)\s*(capacity_provider_name\s*=\s*[^\n]+)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1\3',
                    content,
                    flags=re.MULTILINE
                )
                logger.info(f"Uncommented capacity_provider_name for existing OPS_TOOLS service")

        logger.info(f"Generated HCL content with {updates_count}/{len(field_mapping)} fields updated")
        return content

    async def push_to_github(
        self,
        service_config: ServiceConfigModel,
        service,  # ServicesMstModel with application relationship loaded
        field_mapping: Dict[str, str],
        github_repository: str,
        github_branch: str,
        tenant: str,
        template_file: Optional[Path] = None,
        user_email: str = None,
        existing_dockerfile_prs: Optional[Dict[str, Dict]] = None,
        existing_terragrunt_pr: Optional[Dict] = None
    ) -> Dict[str, str]:
        """
        Push Terragrunt HCL content to GitHub repository using PR workflow.

        Fetches existing content from GitHub, generates HCL using it as base,
        compares with new content, and creates PR only if there are changes.

        Smart PR Replacement: If existing_terragrunt_pr is provided, compares
        content with that PR's branch. If no changes, skips. If changes, creates
        new PR and closes the old one.

        Args:
            service_config: ServiceConfigModel with config data
            service: ServicesMstModel with service and application info (relationships loaded)
            field_mapping: Dict mapping field names to values for HCL generation
            existing_dockerfile_prs: Optional dict of existing open Dockerfile PRs by branch
                for smart PR replacement logic
            existing_terragrunt_pr: Optional dict with existing open Terragrunt PR info
                {git_branch, pr_number, workflow_id} for smart PR replacement
            github_repository: GitHub repository in format "owner/repo"
            github_branch: Target branch name (base branch for PR)
            tenant: Tenant identifier from auth token (for future decision logic)
            template_file: Optional template file path (for no-alb vs existing-alb)
            user_email: User email (from JWT) for PR attribution

        Returns:
            Dict with status, PR info, and GitHub commit info
        """
        try:
            from app.integrations.github_integration import GitHubIntegration

            # Validate GitHub repository format
            if not github_repository or "/" not in github_repository:
                logger.warning(f"Invalid GitHub repository format: '{github_repository}', skipping GitHub push")
                return {
                    "status": "skipped",
                    "message": "GitHub repository not configured or invalid format"
                }

            # Split repository into owner and repo
            owner, repo = github_repository.split("/", 1)

            # Get GitHub credentials via centralized token helper
            github_token = await self._get_github_token(owner)
            github_base_url = settings.github_base_url

            if not github_token:
                logger.warning("GitHub token not configured, skipping GitHub push")
                return {
                    "status": "skipped",
                    "message": "GitHub token not configured"
                }

            # Get application name via relationship
            application = service.application
            if not application:
                raise ValueError(f"Service {service.code} has no associated application")

            product_name = application.name
            service_name = service.name
            environment = service_config.environment.value
            version_index = settings.infra_version_index or "01"
            # Get service type for folder routing (OPS_TOOLS → ops-tools/)
            service_type = service.service_type.value if service.service_type else None

            # Get AWS region from geo_loc_mst_code
            region = self._get_aws_region_from_geo_loc(service_config.geo_loc_mst_code)

            # Sanitize names for file paths (using helper methods)
            product_name_sanitized = self._sanitize_name(product_name)
            service_name_sanitized = self._sanitize_name(service_name)
            env_sanitized = self._normalize_environment_for_path(environment, tenant)
            region_sanitized = self._normalize_region_for_path(region)

            # Build GitHub file path using helper method
            # OPS_TOOLS services go to ops-tools/ folder, others go to services/
            github_file_path = self._build_github_file_path(
                product_name=product_name,
                service_name=service_name,
                environment=environment,
                region=region,
                tenant=tenant,
                version_index=version_index,
                service_type=service_type
            )

            # Base branch for PR (determined by environment and tenant)
            base_branch = self._get_pr_base_branch(environment, tenant)

            # Build feature branch name using helper method
            alb_selection = service_config.config.get("alb_selection", "existing-alb") if service_config.config else "existing-alb"
            hosting_type_name = service_config.infrastructure_type.name if service_config.infrastructure_type else "AWS ECS Fargate"

            feature_branch = self._build_feature_branch_name(
                service_name=service_name,
                environment=environment,
                hosting_type_name=hosting_type_name,
                tenant=tenant
            )
            alb_type_sanitized = alb_selection.replace("_", "-")  # Keep for PR title display
            logger.info(f"Using timestamp-based branch name: {feature_branch}")

            # Step 2: Fetch existing content from base branch FIRST (before creating branch)
            # For updates: use existing file as base to preserve manual edits
            # For creates: use template
            existing_content = None
            try:
                existing_file = await GitHubIntegration.get_file_content(
                    token=github_token,
                    base_url=github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=github_file_path,
                    branch=base_branch
                )
                if existing_file and existing_file.get("exists"):
                    existing_content = existing_file.get("content")
                    logger.info(f"Found existing HCL file in {base_branch}: {github_file_path}")
                else:
                    logger.info(f"No existing HCL file found in {base_branch}, will use template")
            except Exception as e:
                logger.warning(f"Could not fetch existing file from GitHub: {e}, will use template")

            # Step 3: Generate HCL content using existing content as base (or template if no existing)
            # Pass environment for dynamic common_infra path (dev → common-dev-infra, stage/prod → common-infra)
            # Pass service_name, tenant, product_name, and service_type for env file paths and rule application
            hcl_content = self._generate_hcl_content(
                field_mapping,
                existing_content,
                template_file,
                environment,
                service_name,
                tenant,
                product_name,
                service_type
            )
            logger.info(f"Generated HCL content for {service_name}")

            # SMART PR REPLACEMENT: If we have an existing open PR, compare content with that PR's branch
            # If PR was manually closed (was_closed=True), skip comparison and create new PR
            old_pr_to_close = None
            old_branch_to_delete = None
            old_workflow_id = None

            if existing_terragrunt_pr:
                existing_pr_branch = existing_terragrunt_pr.get("git_branch")
                existing_pr_number = existing_terragrunt_pr.get("pr_number")
                existing_workflow_id = existing_terragrunt_pr.get("workflow_id")
                pr_was_closed = existing_terragrunt_pr.get("was_closed", False)

                if pr_was_closed:
                    # PR was manually closed on GitHub - need to create new PR
                    logger.info(f"Previous Terragrunt PR #{existing_pr_number} was closed - creating replacement PR")
                    old_workflow_id = existing_workflow_id  # Track for DB update
                else:
                    logger.info(f"Found existing open Terragrunt PR #{existing_pr_number} on branch {existing_pr_branch}")

                    # Fetch HCL content from the existing PR's branch
                    try:
                        existing_pr_file = await GitHubIntegration.get_file_content(
                            token=github_token,
                            base_url=github_base_url,
                            owner=owner,
                            repo=repo,
                            file_path=github_file_path,
                            branch=existing_pr_branch
                        )

                        if existing_pr_file and existing_pr_file.get("exists"):
                            existing_pr_content = existing_pr_file.get("content", "")

                            # Compare existing PR content with new generated content
                            if should_skip_commit(existing_pr_content, hcl_content):
                                logger.info(f"No changes compared to existing PR #{existing_pr_number} - skipping HCL update")

                                # Even if HCL hasn't changed, we should still check if Dockerfile needs modification
                                # (e.g., advanced_options changed DD_TRACE_ENABLED but cpu/ram stayed same)
                                dockerfile_result = None
                                should_modify = self._dockerfile_sync_service.should_modify_dockerfile(service_config, field_mapping)
                                logger.info(f"[no_changes path] should_modify_dockerfile returned: {should_modify}")
                                if should_modify:
                                    try:
                                        enable_datadog = field_mapping.get("enable_datadog_sidecar") == "true"
                                        env_normalized = self._normalize_environment_for_display(environment, tenant)
                                        geo_loc_sanitized = self._sanitize_name(service_config.geo_loc_mst_code)
                                        dockerfile_result = await self._dockerfile_sync_service.modify_dockerfile_for_datadog(
                                            service_config=service_config,
                                            service=service,
                                            github_token=github_token,
                                            github_base_url=github_base_url,
                                            service_sanitized=service_name_sanitized,
                                            env_normalized=env_normalized,
                                            geo_loc_sanitized=geo_loc_sanitized,
                                            user_email=user_email,
                                            enable_datadog=enable_datadog,
                                            existing_prs_by_branch=existing_dockerfile_prs,
                                            tenant_code=tenant
                                        )
                                        logger.info(f"[no_changes path] Dockerfile modification result: {dockerfile_result.get('status') if dockerfile_result else 'None'}")
                                    except Exception as e:
                                        logger.error(f"[no_changes path] Dockerfile modification failed (non-blocking): {e}")
                                        dockerfile_result = {"status": "error", "message": str(e)}

                                return {
                                    "status": "no_changes",
                                    "message": f"No changes needed - existing PR #{existing_pr_number} is up to date",
                                    "github_path": github_file_path,
                                    "pr_number": existing_pr_number,
                                    "feature_branch": existing_pr_branch,
                                    "pr_url": f"https://github.com/{owner}/{repo}/pull/{existing_pr_number}",
                                    "commit_sha": existing_terragrunt_pr.get("commit_sha"),
                                    "dockerfile_modifications": dockerfile_result
                                }

                            # Content has changed - we'll create new PR and close old one
                            logger.info(f"Content changed - will replace PR #{existing_pr_number} with new PR")
                            old_pr_to_close = existing_pr_number
                            old_branch_to_delete = existing_pr_branch
                            old_workflow_id = existing_workflow_id
                    except Exception as e:
                        logger.warning(f"Could not fetch content from existing PR branch: {e}")
                        # Continue with normal flow if we can't compare

            # Step 3.5: Check if content has changed (skip PR if no changes)
            # Only check if existing content exists (for updates, not creates) AND no existing PR
            if existing_content and not existing_terragrunt_pr and should_skip_commit(existing_content, hcl_content):
                logger.info(f"No changes detected for {service_name} in {environment}, skipping HCL PR creation")

                # Even if HCL hasn't changed, we should still check if Dockerfile needs modification
                dockerfile_result = None
                should_modify = self._dockerfile_sync_service.should_modify_dockerfile(service_config, field_mapping)
                logger.info(f"[base branch no_changes path] should_modify_dockerfile returned: {should_modify}")
                if should_modify:
                    try:
                        enable_datadog = field_mapping.get("enable_datadog_sidecar") == "true"
                        env_normalized = self._normalize_environment_for_display(environment, tenant)
                        geo_loc_sanitized = self._sanitize_name(service_config.geo_loc_mst_code)
                        dockerfile_result = await self._dockerfile_sync_service.modify_dockerfile_for_datadog(
                            service_config=service_config,
                            service=service,
                            github_token=github_token,
                            github_base_url=github_base_url,
                            service_sanitized=service_name_sanitized,
                            env_normalized=env_normalized,
                            geo_loc_sanitized=geo_loc_sanitized,
                            user_email=user_email,
                            enable_datadog=enable_datadog,
                            existing_prs_by_branch=existing_dockerfile_prs,
                            tenant_code=tenant
                        )
                        logger.info(f"[base branch no_changes path] Dockerfile modification result: {dockerfile_result.get('status') if dockerfile_result else 'None'}")
                    except Exception as e:
                        logger.error(f"[base branch no_changes path] Dockerfile modification failed (non-blocking): {e}")
                        dockerfile_result = {"status": "error", "message": str(e)}

                return {
                    "status": "no_changes",
                    "message": f"No changes detected for {service_name} - HCL content is identical",
                    "github_path": github_file_path,
                    "dockerfile_modifications": dockerfile_result
                }

            # Step 4: Create feature branch from base branch (only if there are changes)
            # With timestamp-based names, branch is always new (unique timestamp)
            await GitHubIntegration.create_branch(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo,
                branch_name=feature_branch,
                from_branch=base_branch
            )
            logger.info(f"Created feature branch {feature_branch} from {base_branch}")

            # Step 5: Fetch previous contributors for collaborative PR
            previous_contributors = []
            try:
                previous_contributors = await self._get_previous_contributors_from_db(
                    service_config_code=service_config.code,
                    tenant=tenant
                )
            except Exception as e:
                logger.warning(f"[COLLABORATIVE-PR] Failed to fetch contributors: {e}")
                # Continue without co-authors - don't block PR creation

            # Step 6: Commit message
            commit_message = f"Update Terragrunt config for {service_name} in {environment} environment"

            # Add co-authors to commit message if previous contributors exist
            if previous_contributors:
                commit_message = self._add_co_authors_to_message(
                    message=commit_message,
                    contributors=previous_contributors,
                    current_user_email=user_email
                )

            logger.info(f"Pushing to GitHub: {github_repository}/{github_file_path} on branch {feature_branch}")

            # Step 7: Push to feature branch using GitHubIntegration.update_or_create_file()
            result = await GitHubIntegration.update_or_create_file(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo,
                branch=feature_branch,
                file_path=github_file_path,
                content=hcl_content,
                message=commit_message
            )

            logger.info(f"Successfully pushed to feature branch: {result.get('commit_sha')}")

            # Step 7.5: Update atlantis.yaml with ECS service entry
            logger.info("Fetching atlantis.yaml from feature branch")
            try:
                atlantis_file = await GitHubIntegration.get_file_content(
                    token=github_token,
                    base_url=github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path="atlantis.yaml",
                    branch=feature_branch
                )

                if atlantis_file and atlantis_file.get("exists"):
                    logger.info("Updating atlantis.yaml with new ECS service project entry")
                    atlantis_content = atlantis_file["content"]

                    # Add ECS service project entry
                    updated_atlantis = self._add_atlantis_ecs_entry(
                        atlantis_content=atlantis_content,
                        file_path=github_file_path,
                        product_name=product_name,
                        env=environment,
                        service_name=service_name,
                        tenant=tenant,
                        geo_loc=service_config.geo_loc_mst_code
                    )

                    # Only commit if content changed (entry was added)
                    if updated_atlantis != atlantis_content:
                        await GitHubIntegration.update_or_create_file(
                            token=github_token,
                            base_url=github_base_url,
                            owner=owner,
                            repo=repo,
                            branch=feature_branch,
                            file_path="atlantis.yaml",
                            content=updated_atlantis,
                            message=f"Update atlantis.yaml for ECS service {service_name}"
                        )
                        logger.info("Successfully updated atlantis.yaml")
                    else:
                        logger.info("atlantis.yaml already has this entry, skipping update")
                else:
                    logger.warning("atlantis.yaml not found in repository, skipping atlantis update")
            except Exception as atlantis_error:
                logger.error(f"Failed to update atlantis.yaml: {str(atlantis_error)}")
                # Don't fail the entire operation if atlantis update fails

            # Step 7.6: Create env files if they don't exist (non-blocking)
            # Pass raw values - method handles sanitization internally
            # Pass service_type for OPS_TOOLS to use dev-tools folder
            env_files_result = await self._create_env_files_if_not_exist(
                github_token=github_token,
                github_base_url=github_base_url,
                owner=owner,
                repo=repo,
                branch=feature_branch,
                product_name=product_name,
                service_name=service_name,
                environment=environment,
                region=region,
                tenant=tenant,
                version_index=version_index,
                service_type=service_type
            )
            logger.info(f"Env files result: {env_files_result.get('message')}")

            # Step 8: Create PR (always new - timestamp ensures unique branch)
            # Format ALB type for display
            alb_display = alb_type_sanitized.replace("-", " ").title()  # no-alb -> No Alb

            # Determine action (Create vs Update) by checking if file exists in base branch
            base_branch_file = await GitHubIntegration.get_file_content(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo,
                file_path=github_file_path,
                branch=base_branch
            )
            is_new_service = not (base_branch_file and base_branch_file.get("exists"))
            action = "Create" if is_new_service else "Update"
            action_past = "Created" if is_new_service else "Updated"

            # Use display normalization (vance/aspora: dev→dev, staging→stage)
            env_display = self._normalize_environment_for_display(environment, tenant)

            # Determine service type label for PR (OPS_TOOLS → Ops Tools, others → ECS Service)
            is_ops_tools = service_type == "OPS_TOOLS"
            service_type_label = "Ops Tools" if is_ops_tools else "ECS Service"

            # Build atlantis project name: {product}-{env}-{service}
            # Use _get_service_name_for_env_files to match atlantis.yaml entry naming
            service_for_atlantis = self._get_service_name_for_env_files(service_name_sanitized)
            atlantis_project_name = f"{product_name_sanitized}-{env_display}-{service_for_atlantis}"

            pr_title = f"[{service_type_label}] {action} {service_name} config - {env_display}"
            pr_body = f"""## {service_type_label} Configuration {action}

**Service:** `{service_name}`
**Product:** `{product_name}`
**Environment:** {env_display}
**Region:** {region}
**ALB Type:** {alb_display}

### File Path
`{github_file_path}`

### Changes
- {action_past} Terragrunt configuration for `{service_name}`
- Infrastructure managed via Terragrunt

### Deployment
Merge this PR to trigger GitOps workflow → Terragrunt Apply → {service_type_label} {action}

**Plan:** `atlantis plan -p {atlantis_project_name}`
**Apply:** `atlantis apply -p {atlantis_project_name}`"""

            # Add previous contributors section if any exist
            if previous_contributors:
                pr_body += "\n\n### Previous Contributors\n"
                for contributor in previous_contributors:
                    email = contributor.get("email")
                    name = contributor.get("name", "Unknown")
                    # Skip current user
                    if email != user_email:
                        pr_body += f"- {name} ({email})\n"

            pr_body += f"""

---
*Generated by {settings.app_name}*
*Requested by: {user_email}*"""

            # Add supersedes note if replacing old PR
            if old_pr_to_close:
                pr_body += f"\n\n---\n*Supersedes PR #{old_pr_to_close}*"

            logger.info(f"Creating PR from {feature_branch} to {base_branch}")

            pr_result = await GitHubIntegration.create_pull_request(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo,
                head=feature_branch,
                base=base_branch,
                title=pr_title,
                body=pr_body,
                draft=False
            )

            logger.info(f"Successfully created PR #{pr_result.get('number')}: {pr_result.get('html_url')}")

            # Close old PR and delete old branch (if replacing)
            if old_pr_to_close:
                try:
                    await GitHubIntegration.close_pull_request(
                        token=github_token,
                        base_url=github_base_url,
                        owner=owner,
                        repo=repo,
                        pr_number=old_pr_to_close,
                        comment=f"Superseded by PR #{pr_result.get('number')}"
                    )
                    logger.info(f"Closed old Terragrunt PR #{old_pr_to_close}")
                except Exception as e:
                    logger.warning(f"Failed to close old PR #{old_pr_to_close}: {e}")

                try:
                    await GitHubIntegration.delete_branch(
                        token=github_token,
                        base_url=github_base_url,
                        owner=owner,
                        repo=repo,
                        branch=old_branch_to_delete
                    )
                    logger.info(f"Deleted old branch {old_branch_to_delete}")
                except Exception as e:
                    logger.warning(f"Failed to delete old branch {old_branch_to_delete}: {e}")

            # Handle Dockerfile operations (non-blocking)
            dockerfile_result = None
            config = service_config.config or {}

            # First check if we need to GENERATE a new Dockerfile (generate_dockerfile flag)
            dockerfile_generated = False
            if config.get("generate_dockerfile", False):
                logger.info("=== Checking if Dockerfile GENERATION is needed (new PR path) ===")
                if self._dockerfile_generation_service.should_generate_dockerfile(service_config, service_config.language_ref):
                    try:
                        dockerfile_result = await self._dockerfile_generation_service.generate_and_commit_dockerfile(
                            service_config=service_config,
                            service=service,
                            language_ref=service_config.language_ref,
                            github_token=github_token,
                            github_base_url=github_base_url,
                            user_email=user_email
                        )
                        logger.info(f"Dockerfile generation result: {dockerfile_result.get('status')}")
                        # Only mark as generated if a NEW Dockerfile was actually created
                        # If status is "skipped" (Dockerfile already exists), allow Datadog modification
                        dockerfile_generated = dockerfile_result.get("status") == "success"
                    except Exception as e:
                        logger.error(f"Dockerfile generation failed (non-blocking): {e}")
                        dockerfile_result = {"status": "error", "message": str(e)}

            # If Dockerfile was not generated (either generate_dockerfile=False OR Dockerfile already exists),
            # check if we need to MODIFY existing Dockerfile for Datadog
            if not dockerfile_generated:
                logger.info("=== Checking if Dockerfile modification is needed (new PR path) ===")
                should_modify = self._dockerfile_sync_service.should_modify_dockerfile(service_config, field_mapping)
                logger.info(f"should_modify_dockerfile returned: {should_modify}")
                if should_modify:
                    try:
                        enable_datadog = field_mapping.get("enable_datadog_sidecar") == "true"
                        # Compute sanitized values for Dockerfile branch naming
                        env_normalized = self._normalize_environment_for_display(environment, tenant)
                        geo_loc_sanitized = self._sanitize_name(service_config.geo_loc_mst_code)
                        dockerfile_result = await self._dockerfile_sync_service.modify_dockerfile_for_datadog(
                            service_config=service_config,
                            service=service,
                            github_token=github_token,
                            github_base_url=github_base_url,
                            service_sanitized=service_name_sanitized,
                            env_normalized=env_normalized,
                            geo_loc_sanitized=geo_loc_sanitized,
                            user_email=user_email,
                            enable_datadog=enable_datadog,
                            existing_prs_by_branch=existing_dockerfile_prs,
                            tenant_code=tenant
                        )
                        logger.info(f"Dockerfile modification result: {dockerfile_result.get('status')}")
                    except Exception as e:
                        logger.error(f"Dockerfile modification failed (non-blocking): {e}")
                        dockerfile_result = {"status": "error", "message": str(e)}

            return {
                "status": "success",
                "github_path": github_file_path,
                "commit_sha": result.get("commit_sha"),
                "commit_url": result.get("commit_url"),
                "feature_branch": feature_branch,
                "base_branch": base_branch,
                "pr_number": pr_result.get("number"),
                "pr_url": pr_result.get("html_url"),
                "pr_state": pr_result.get("state"),
                "env_files": env_files_result,
                "dockerfile_modifications": dockerfile_result,
                "github_token": github_token,  # Pass token for downstream use (pipeline sync)
                "old_workflow_id": old_workflow_id,  # For DB status update (smart PR replacement)
                "message": f"Terragrunt file committed and PR #{pr_result.get('number')} created to {base_branch}"
            }

        except Exception as e:
            logger.error(f"GitHub push failed: {str(e)}", exc_info=True)
            return {
                "status": "error",
                "message": f"GitHub push failed: {str(e)}"
            }

    def _get_env_file_paths(
        self,
        product_name: str,
        service_name: str,
        environment: str,
        region: str,
        tenant: str = "",
        version_index: str = "01",
        service_type: str = None
    ) -> tuple:
        """
        Get the file paths for env config files (configs.json and secrets.json).

        Accepts raw values and sanitizes internally for consistency.
        Uses tenant-aware path normalization for environment.
        For env files: uses envs-dev folder for Vance/Aspora + dev, service name stays simple.
        For OPS_TOOLS: uses dev-tools shared folder instead of service-specific folder.

        Args:
            product_name: Product/application name (raw)
            service_name: Service name (raw)
            environment: Environment (dev, staging, prod)
            region: AWS region
            tenant: Tenant identifier (for tenant-specific path logic)
            version_index: Infrastructure version index (default "01")
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Returns:
            Tuple of (configs_path, secrets_path)

        Example (default tenant):
            ("environment/core-dev-01/ap-south-1/envs/casa-service/non-secure/casa-service-configs.json", ...)

        Example (vance/aspora + dev):
            ("environment/core-stage-01/ap-south-1/envs-dev/casa-service/non-secure/casa-service-configs.json", ...)

        Example (OPS_TOOLS):
            ("environment/core-stage-01/ap-south-1/envs/dev-tools/non-secure/test-ops-service-configs.json", ...)
        """
        product_sanitized = self._sanitize_name(product_name)
        service_sanitized = self._sanitize_name(service_name)

        # For env files: use simple service name (no -dev- insertion)
        service_for_env = self._get_service_name_for_env_files(service_sanitized)

        env_sanitized = self._normalize_environment_for_path(environment, tenant)
        region_sanitized = self._normalize_region_for_path(region)

        # Get envs folder name (envs or envs-dev for Vance/Aspora + dev)
        envs_folder = self._get_envs_folder_name(environment, tenant)

        # OPS_TOOLS: use dev-tools shared folder instead of service-specific folder
        # Standard: use service-specific folder
        if service_type == "OPS_TOOLS":
            env_subfolder = "dev-tools"
            logger.info(f"Using OPS_TOOLS env path with dev-tools folder")
        else:
            env_subfolder = service_for_env

        base_path = f"environment/{product_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/{envs_folder}/{env_subfolder}"
        configs_path = f"{base_path}/non-secure/{service_for_env}-configs.json"
        secrets_path = f"{base_path}/secure/{service_for_env}-secrets.json"
        return configs_path, secrets_path

    def _get_dummy_secret_value(self, product: str, environment: str, region: str, tenant: str = "") -> str:
        """
        Get the appropriate dummy secret value based on product, env, and region.

        Lookup priority:
        1. Exact match: {product}-{env}-{region}
        2. Fallback: any entry matching {env}-{region}
        3. Worst case: any entry matching {region}

        Args:
            product: Product name (e.g., 'core', 'falcon')
            environment: Environment name (e.g., 'stage', 'prod', 'dev')
            region: AWS region code (e.g., 'ap-south-1', 'eu-west-2')
            tenant: Tenant name (e.g., 'vance', 'aspora')

        Returns:
            Encrypted dummy secret value for AWS_REGION
        """
        region_short = AWS_REGION_SHORT_NAMES.get(region, region)
        product_lower = product.lower()  # Normalize product name to lowercase
        # Normalize environment for Vance/Aspora: staging → stage (mapping uses 'stage')
        env_lower = environment.lower()
        if tenant.lower() in VANCE_ASPORA_TENANTS and env_lower == "staging":
            env_normalized = "stage"
        else:
            env_normalized = env_lower

        # Try exact match
        exact_key = f"{product_lower}-{env_normalized}-{region_short}"
        if exact_key in DUMMY_SECRETS_MAPPING:
            return DUMMY_SECRETS_MAPPING[exact_key]

        # Fallback 1: match {env}-{region}
        env_region_suffix = f"-{env_normalized}-{region_short}"
        for key, value in DUMMY_SECRETS_MAPPING.items():
            if key.endswith(env_region_suffix):
                return value

        # Fallback 2: match {region}
        region_suffix = f"-{region_short}"
        for key, value in DUMMY_SECRETS_MAPPING.items():
            if key.endswith(region_suffix):
                return value

        # Ultimate fallback: return first available value
        return next(iter(DUMMY_SECRETS_MAPPING.values()))

    def _get_env_file_content(self, service_name: str, product: str, environment: str, region: str, tenant: str = "") -> tuple:
        """
        Get placeholder content for env config files.

        Args:
            service_name: Service name (unused for now, but available for future customization)
            product: Product name (e.g., 'core', 'falcon')
            environment: Environment name (e.g., 'stage', 'prod', 'dev')
            region: AWS region code (e.g., 'ap-south-1', 'eu-west-2')
            tenant: Tenant name (e.g., 'vance', 'aspora')

        Returns:
            Tuple of (configs_content, secrets_content) as JSON strings
        """
        import json
        configs_content = json.dumps({"dummy_config": "dummy-value"}, indent=2) + "\n"
        encrypted_value = self._get_dummy_secret_value(product, environment, region, tenant)
        secrets_content = json.dumps({
            "AWS_REGION": encrypted_value
        }, indent=2) + "\n"
        return configs_content, secrets_content

    async def _create_env_files_if_not_exist(
        self,
        github_token: str,
        github_base_url: str,
        owner: str,
        repo: str,
        branch: str,
        product_name: str,
        service_name: str,
        environment: str,
        region: str,
        tenant: str = "",
        version_index: str = "01",
        service_type: str = None
    ) -> Dict[str, str]:
        """
        Create env config files (configs.json and secrets.json) if they don't exist.

        This is a non-blocking operation - failures are logged but don't raise exceptions.
        Only creates files on first-time service creation (skips if files already exist).

        Accepts raw values - delegates sanitization to _get_env_file_paths().

        Args:
            github_token: GitHub authentication token
            github_base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Target branch name
            product_name: Product/application name (raw)
            service_name: Service name (raw)
            environment: Environment (dev, staging, prod)
            region: AWS region
            tenant: Tenant identifier (for tenant-specific path logic)
            version_index: Infrastructure version index (default "01")
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Returns:
            Dict with status and details of created/skipped files
        """
        from app.integrations.github_integration import GitHubIntegration

        result = {
            "status": "success",
            "configs_file": None,
            "secrets_file": None,
            "message": ""
        }

        # ===== EXCLUSION CHECK: Skip env files for Falcon services =====
        # Use existing VANCE_ASPORA_TENANTS constant for consistency
        service_lower = service_name.lower()
        falcon_services = {
            "falcon-api", "falcon-consumer", "falcon-worker",
            "falcon-api-dev-service", "falcon-consumer-dev-service", "falcon-worker-dev-service"
        }
        if (
            product_name.lower() == "falcon" and
            service_lower in falcon_services and
            tenant.lower() in VANCE_ASPORA_TENANTS
        ):
            logger.warning(
                f"Skipping env files creation for Falcon service: "
                f"product={product_name}, service={service_name}, tenant={tenant}"
            )
            return {
                "status": "skipped",
                "configs_file": None,
                "secrets_file": None,
                "message": f"Skipped for Falcon service ({tenant} tenant)"
            }
        # ===== END EXCLUSION CHECK =====

        try:
            # Get file paths (method handles sanitization)
            configs_path, secrets_path = self._get_env_file_paths(
                product_name=product_name,
                service_name=service_name,
                environment=environment,
                region=region,
                tenant=tenant,
                version_index=version_index,
                service_type=service_type
            )

            # Get placeholder content
            configs_content, secrets_content = self._get_env_file_content(
                service_name, product_name, environment, region, tenant
            )

            files_created = []
            files_skipped = []

            # Check and create configs.json
            try:
                existing_configs = await GitHubIntegration.get_file_content(
                    token=github_token,
                    base_url=github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=configs_path,
                    branch=branch
                )
                if existing_configs and existing_configs.get("exists"):
                    logger.info(f"Env configs file already exists, skipping: {configs_path}")
                    files_skipped.append("configs.json")
                else:
                    # Create configs file
                    await GitHubIntegration.update_or_create_file(
                        token=github_token,
                        base_url=github_base_url,
                        owner=owner,
                        repo=repo,
                        branch=branch,
                        file_path=configs_path,
                        content=configs_content,
                        message=f"Add env configs for {service_name}"
                    )
                    logger.info(f"Created env configs file: {configs_path}")
                    result["configs_file"] = configs_path
                    files_created.append("configs.json")
            except Exception as e:
                logger.warning(f"Failed to check/create configs file: {e}")

            # Check and create secrets.json
            try:
                existing_secrets = await GitHubIntegration.get_file_content(
                    token=github_token,
                    base_url=github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=secrets_path,
                    branch=branch
                )
                if existing_secrets and existing_secrets.get("exists"):
                    logger.info(f"Env secrets file already exists, skipping: {secrets_path}")
                    files_skipped.append("secrets.json")
                else:
                    # Create secrets file
                    await GitHubIntegration.update_or_create_file(
                        token=github_token,
                        base_url=github_base_url,
                        owner=owner,
                        repo=repo,
                        branch=branch,
                        file_path=secrets_path,
                        content=secrets_content,
                        message=f"Add env secrets for {service_name}"
                    )
                    logger.info(f"Created env secrets file: {secrets_path}")
                    result["secrets_file"] = secrets_path
                    files_created.append("secrets.json")
            except Exception as e:
                logger.warning(f"Failed to check/create secrets file: {e}")

            # Build result message
            if files_created:
                result["message"] = f"Created: {', '.join(files_created)}"
            if files_skipped:
                if result["message"]:
                    result["message"] += f"; Skipped (exists): {', '.join(files_skipped)}"
                else:
                    result["message"] = f"Skipped (exists): {', '.join(files_skipped)}"

            if not result["message"]:
                result["message"] = "No env files created"

            logger.info(f"Env files creation result: {result['message']}")

        except Exception as e:
            # Non-blocking - log warning but don't fail
            logger.warning(f"Env files creation failed (non-blocking): {e}")
            result["status"] = "warning"
            result["message"] = f"Env files creation skipped due to error: {str(e)}"

        return result

    async def generate_hcl_preview(
        self,
        db: AsyncSession,
        services_mst_code: str,
        environment: str,
        tenant_code: str,
        config: Dict[str, Any],
        geo_loc_mst_code: str,
        alb_selection: Optional[str] = "existing_alb",
        language_ref_code: Optional[str] = None,
        sidecar_config: Optional[list] = None,
        deployment_strategy: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Generate Terragrunt HCL content for preview (no GitHub push).

        Reuses existing _build_field_mapping() and _generate_hcl_content() functions.
        This method handles database lookup to fetch service details and generates HCL in memory.

        Args:
            db: Database session
            services_mst_code: Service code (e.g., "auth-service")
            environment: Environment (dev, staging, prod)
            tenant_code: Tenant code
            config: Service configuration dict (cpu, memory, port, autoscaling, etc.)
            geo_loc_mst_code: Geo location code
            alb_selection: ALB selection (existing_alb, new_alb, no_alb)
            language_ref_code: Language reference code (e.g., "java", "python")
            sidecar_config: Datadog sidecar configuration
            deployment_strategy: Deployment strategy

        Returns:
            Dict containing:
            - hcl_content: Generated HCL as string
            - template_used: Template file name
            - environment: Environment
            - service_name: Service name
            - field_mapping: Field mapping used for generation
        """
        from app.repository.services_mst_repository import ServicesMstRepository
        from app.repository.language_ref_repository import LanguageRefRepository

        logger.info(f"[TERRAGRUNT_PREVIEW] Generating HCL preview for service {services_mst_code} in {environment}")

        # Step 1: Fetch service details
        services_repo = ServicesMstRepository(db)
        service_obj = await services_repo.get_by(code=services_mst_code)
        if not service_obj:
            raise ValueError(f"Service not found: {services_mst_code}")

        service_name = service_obj.name
        application_code = service_obj.applications_mst_code

        # Step 2: Get language name from language_ref_code
        language_name = "Java"  # Default
        if language_ref_code:
            language_repo = LanguageRefRepository(db)
            language_ref = await language_repo.get_by_code(language_ref_code)
            if language_ref:
                language_name = language_ref.name

        # Get service_type from service model
        service_type = service_obj.service_type.value if service_obj.service_type else ""

        logger.info(f"[TERRAGRUNT_PREVIEW] Service: {service_name}, Application: {application_code}, Language: {language_name}, ServiceType: {service_type}")

        # Step 3: Determine template file based on alb_selection and service_type
        # Add alb_selection to config for _get_template_file logic
        config_with_alb = config.copy()
        config_with_alb["alb_selection"] = alb_selection

        template_file = self._get_template_file(
            config=config_with_alb,
            environment=environment,
            tenant=tenant_code,
            product_name=application_code or "",
            service_type=service_type
        )
        logger.info(f"[TERRAGRUNT_PREVIEW] Using template: {template_file.name}")

        # Step 4: Build field mapping using existing function
        is_no_alb = alb_selection == "no_alb"
        field_mapping = self._build_field_mapping(
            config=config,
            sidecar_config=sidecar_config,
            language_name=language_name,
            is_no_alb=is_no_alb
        )
        logger.info(f"[TERRAGRUNT_PREVIEW] Built field mapping with {len(field_mapping)} fields")

        # Step 5: Generate HCL content using existing function
        hcl_content = self._generate_hcl_content(
            field_mapping=field_mapping,
            existing_content=None,  # Use template as base
            template_file=template_file,
            environment=environment,
            service_name=service_name,
            tenant=tenant_code,
            product_name=application_code,
            service_type=service_type
        )

        logger.info(f"[TERRAGRUNT_PREVIEW] Generated HCL content ({len(hcl_content)} characters)")

        return {
            "hcl_content": hcl_content,
            "template_used": template_file.name,
            "environment": environment,
            "service_name": service_name,
            "field_mapping": field_mapping
        }
