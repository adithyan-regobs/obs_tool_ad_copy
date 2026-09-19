"""
Default naming strategy for non-Aspora tenants.

Naming conventions:
- Organization name: {tenant_code}
- ECS service name: {tenant}-{env}-{region}-{index}-{service}-svc
- ECR repo name: {org_name}/{env}/{region}/{index}/{service_name}
"""

from .base import NamingStrategy


class DefaultNamingStrategy(NamingStrategy):
    """
    Default naming strategy for generic tenants.

    Uses tenant code as the organization name.
    """

    def generate_org_name(
        self,
        tenant_code: str,
        application_name: str
    ) -> str:
        """
        Generate organization name using tenant code.

        Args:
            tenant_code: Tenant code
            application_name: Application name (ignored in default strategy)

        Returns:
            Organization name (sanitized tenant code)

        Examples:
            >>> strategy = DefaultNamingStrategy()
            >>> strategy.generate_org_name("my_tenant", "any_app")
            'my-tenant'
        """
        return self.sanitize_for_aws(tenant_code)

    def generate_ecs_service_name(
        self,
        application_name: str,
        environment: str,
        geo_loc_mst_code: str,
        index: str,
        service_name: str
    ) -> str:
        """
        Generate ECS service name for default tenants.

        Format: {application_name}-{env}-{geo_loc_mst}-{index}-{service_name}-svc

        Args:
            application_name: Application/product name
            environment: Environment (dev, staging, prod)
            geo_loc_mst_code: Geographic location code (e.g., "mumbai")
            index: Index (e.g., "01")
            service_name: Service name

        Returns:
            ECS service name

        Examples:
            >>> strategy = DefaultNamingStrategy()
            >>> strategy.generate_ecs_service_name("myapp", "prod", "useast1", "01", "api")
            'myapp-prod-useast1-01-api-svc'
        """
        return (
            f"{self.sanitize_for_aws(application_name)}-"
            f"{self.sanitize_for_aws(environment)}-"
            f"{self.sanitize_for_aws(geo_loc_mst_code)}-"
            f"{self.sanitize_for_aws(index)}-"
            f"{self.sanitize_for_aws(service_name)}-svc"
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
        Generate ECR repository name for default tenants.

        Format: {org_name}/{env}/{geo_loc_mst}/{index}/{service_name}

        Args:
            org_name: Organization name
            environment: Environment (dev, staging, prod)
            geo_loc_mst_code: Geographic location code (e.g., "mumbai")
            index: Index (e.g., "01")
            service_name: Service name

        Returns:
            ECR repository name (path portion)

        Examples:
            >>> strategy = DefaultNamingStrategy()
            >>> strategy.generate_ecr_repo_name("myorg", "prod", "useast1", "01", "api")
            'myorg/prod/useast1/01/api'
        """
        return (
            f"{self.sanitize_for_aws(org_name)}/"
            f"{self.sanitize_for_aws(environment)}/"
            f"{self.sanitize_for_aws(geo_loc_mst_code)}/"
            f"{self.sanitize_for_aws(index)}/"
            f"{self.sanitize_for_aws(service_name)}"
        )
