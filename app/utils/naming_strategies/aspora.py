"""
Aspora-specific naming strategy.

Naming conventions for tenant=aspora:
- Organization name:
  - If application = "core" -> "vance-core"
  - Otherwise -> {application_name} (sanitized)
- ECS service name: {org_name}-{env}-{geo_loc_mst}-{index}-{service_name}-svc
- ECR repo name: {org_name}/{env}/{geo_loc_mst}/{index}/{service_name}
"""

from .base import NamingStrategy


class AsporaNamingStrategy(NamingStrategy):
    """
    Naming strategy for Aspora tenant.

    Special case: If application/product = "core", org name becomes "vance-core"
    Otherwise uses application name as the organization name.
    Service names follow: {org_name}-{env}-{region}-{index}-{service}-svc
    """

    # Application name mappings for Aspora
    # If application name matches key, use value as org name
    APPLICATION_MAPPINGS = {
        "core": "vance-core",
    }

    def generate_org_name(
        self,
        tenant_code: str,
        application_name: str
    ) -> str:
        """
        Generate organization name for Aspora.

        Special case: If application = "core" -> "vance-core"
        Otherwise: org name = application name (sanitized)

        Args:
            tenant_code: Tenant code (ignored for Aspora)
            application_name: Application/product name

        Returns:
            Organization name

        Examples:
            >>> strategy = AsporaNamingStrategy()
            >>> strategy.generate_org_name("aspora", "core")
            'vance-core'

            >>> strategy.generate_org_name("aspora", "payments")
            'payments'
        """
        # Check if application has a special mapping
        sanitized_app = self.sanitize_for_aws(application_name)
        if sanitized_app in self.APPLICATION_MAPPINGS:
            return self.APPLICATION_MAPPINGS[sanitized_app]

        return sanitized_app

    def generate_ecs_service_name(
        self,
        application_name: str,
        environment: str,
        geo_loc_mst_code: str,
        index: str,
        service_name: str
    ) -> str:
        """
        Generate ECS service name for Aspora.

        Format varies by environment:
        - Dev: {org_name}-stage-{geo_loc_mst}-{index}-{service_name}-dev-service-svc
        - Stage: {org_name}-stage-{geo_loc_mst}-{index}-{service_name}-service-svc
        - Prod: {org_name}-prod-{geo_loc_mst}-{index}-{service_name}-service-svc

        Note: Both dev and stage use "stage" in the environment part of the name.

        Uses org_name from generate_org_name (applies special mappings like core -> vance-core)

        Args:
            application_name: Application/product name (e.g., "core")
            environment: Environment (dev, stage, prod)
            geo_loc_mst_code: Geographic location code (e.g., "mumbai")
            index: Index (e.g., "01")
            service_name: Service name

        Returns:
            ECS service name

        Examples:
            >>> strategy = AsporaNamingStrategy()
            >>> strategy.generate_ecs_service_name("core", "dev", "mumbai", "01", "backoffice")
            'vance-core-stage-mumbai-01-backoffice-dev-service-svc'

            >>> strategy.generate_ecs_service_name("core", "stage", "mumbai", "01", "backoffice")
            'vance-core-stage-mumbai-01-backoffice-service-svc'
        """
        # Get org name (applies special mappings like core -> vance-core)
        org_name = self.generate_org_name(tenant_code="aspora", application_name=application_name)

        # Map environment for naming: dev and stage both use "stage"
        env_lower = environment.lower()
        if env_lower in ("dev", "stage"):
            env_for_naming = "stage"
        else:
            env_for_naming = env_lower

        # Determine suffix based on environment
        # Dev gets "-dev-service-svc", others get "-service-svc"
        if env_lower == "dev":
            suffix = "dev-service-svc"
        else:
            suffix = "service-svc"

        return (
            f"{org_name}-"
            f"{self.sanitize_for_aws(env_for_naming)}-"
            f"{self.sanitize_for_aws(geo_loc_mst_code)}-"
            f"{self.sanitize_for_aws(index)}-"
            f"{self.sanitize_for_aws(service_name)}-{suffix}"
        )

    def generate_ecr_repo_name(
        self,
        org_name: str,
        environment: str,
        geo_loc_mst_code: str,
        index: str,
        service_name: str
    ) -> str:
        """
        Generate ECR repository name for Aspora.

        Format varies by environment:
        - Dev: {org_name}/stage/{geo_loc_mst}/{index}/{service_name}-dev-service
        - Stage: {org_name}/stage/{geo_loc_mst}/{index}/{service_name}-service
        - Prod: {org_name}/prod/{geo_loc_mst}/{index}/{service_name}-service

        Note: Both dev and stage use "stage" in the environment part of the path.

        Args:
            org_name: Organization name (from generate_org_name)
            environment: Environment (dev, stage, prod)
            geo_loc_mst_code: Geographic location code (e.g., "mumbai")
            index: Index (e.g., "01")
            service_name: Service name

        Returns:
            ECR repository name (path portion)

        Examples:
            >>> strategy = AsporaNamingStrategy()
            >>> strategy.generate_ecr_repo_name("vance-core", "dev", "mumbai", "01", "backoffice")
            'vance-core/stage/mumbai/01/backoffice-dev-service'

            >>> strategy.generate_ecr_repo_name("vance-core", "stage", "mumbai", "01", "backoffice")
            'vance-core/stage/mumbai/01/backoffice-service'
        """
        # Map environment for naming: dev and stage both use "stage"
        env_lower = environment.lower()
        if env_lower in ("dev", "stage"):
            env_for_naming = "stage"
        else:
            env_for_naming = env_lower

        # Determine suffix based on environment
        # Dev gets "-dev-service", others get "-service"
        if env_lower == "dev":
            suffix = "-dev-service"
        else:
            suffix = "-service"

        return (
            f"{self.sanitize_for_aws(org_name)}/"
            f"{self.sanitize_for_aws(env_for_naming)}/"
            f"{self.sanitize_for_aws(geo_loc_mst_code)}/"
            f"{self.sanitize_for_aws(index)}/"
            f"{self.sanitize_for_aws(service_name)}{suffix}"
        )
