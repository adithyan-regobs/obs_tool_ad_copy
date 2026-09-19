"""
Status Tools for Service Config Agent.

Tools for checking deployment status and service dependencies.
Thin orchestrators that delegate to resolvers and repository.
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from app.core.enum import EnvironmentEnum
from app.services.service_config_agent.tools.base import ToolResult
from app.services.service_config_agent.tools.service_resolver import ServiceResolver


@runtime_checkable
class StatusRepositoryProtocol(Protocol):
    """Contract for status repository."""

    async def get_services_with_config_status(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
    ) -> List[dict]:
        """Get services with config status."""
        ...

    async def get_service_config_with_workflow(
        self,
        service_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        tenant_code: str,
    ) -> Optional[Any]:
        """Get service config with gitops workflow details."""
        ...

    async def get_service_dependencies(
        self,
        service_code: str,
        tenant_code: str,
    ) -> List[Any]:
        """Get service dependencies."""
        ...

    async def get_infrastructure_dependents(
        self,
        infrastructure_code: str,
        tenant_code: str,
    ) -> List[Any]:
        """Get services that depend on an infrastructure."""
        ...


class GetDeploymentStatus:
    """
    Tool to check deployment status of a service.

    Thin orchestrator that:
    1. Delegates service resolution to ServiceResolver
    2. Delegates data access to repository
    """

    def __init__(
        self,
        service_resolver: ServiceResolver,
        repository: StatusRepositoryProtocol,
    ):
        """
        Initialize tool with dependencies.

        Args:
            service_resolver: Resolver for service names
            repository: Repository for status data access
        """
        self._resolver = service_resolver
        self._repo = repository

    async def execute(
        self,
        service_name: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
    ) -> ToolResult:
        """
        Check deployment status of a service.

        Args:
            service_name: Service name (supports typos)
            tenant_code: Tenant code
            environment: Target environment
            geo_loc_code: Geographic location code

        Returns:
            ToolResult with deployment status or error
        """
        # Get available services
        services = await self._repo.get_services_with_config_status(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_code=geo_loc_code,
        )

        # Resolve service name
        resolution = self._resolver.resolve(service_name, services)

        if not resolution["resolved"]:
            return ToolResult(
                success=False,
                error=resolution.get("error", f"Could not resolve service '{service_name}'"),
                suggestions=resolution.get("suggestions"),
                matched_services=resolution.get("matched_services"),
            )

        # Get config with workflow details
        config = await self._repo.get_service_config_with_workflow(
            service_code=resolution["service_code"],
            environment=environment,
            geo_loc_code=geo_loc_code,
            tenant_code=tenant_code,
        )

        if not config:
            return ToolResult(
                success=True,
                data={
                    "service_code": resolution["service_code"],
                    "service_name": resolution["service_name"],
                    "environment": environment.value,
                    "geo_loc": geo_loc_code,
                    "is_deployed": False,
                    "status": "NOT_CONFIGURED",
                    "message": f"No config found for '{resolution['service_name']}' in {environment.value}/{geo_loc_code}",
                },
            )

        # Build status from config and workflow
        status_data = self._build_status(config, resolution)

        return ToolResult(
            success=True,
            data=status_data,
        )

    def _build_status(self, config: Any, resolution: Dict[str, Any]) -> Dict[str, Any]:
        """Build deployment status from config."""
        status_data = {
            "service_code": resolution["service_code"],
            "service_name": resolution["service_name"],
            "environment": config.environment.value if hasattr(config.environment, "value") else str(config.environment),
            "geo_loc": config.geo_loc_mst_code,
            "is_deployed": False,
            "status": "UNKNOWN",
        }

        # Check deployment status
        if hasattr(config, "deployment_status") and config.deployment_status:
            status_value = config.deployment_status.value if hasattr(config.deployment_status, "value") else str(config.deployment_status)
            status_data["status"] = status_value
            status_data["is_deployed"] = status_value == "DEPLOYED"

        # Add workflow info if available
        if hasattr(config, "gitops_workflow") and config.gitops_workflow:
            workflow = config.gitops_workflow
            status_data["workflow"] = {
                "pr_number": workflow.pr_number,
                "pr_url": workflow.pr_url,
                "pr_status": workflow.pr_status.value if workflow.pr_status and hasattr(workflow.pr_status, "value") else str(workflow.pr_status) if workflow.pr_status else None,
                "workflow_run_url": workflow.workflow_run_url,
                "run_completed_at": workflow.run_completed_at.isoformat() if workflow.run_completed_at else None,
            }

        # Add last updated info
        if hasattr(config, "updated_at") and config.updated_at:
            status_data["last_updated"] = config.updated_at.isoformat()

        return status_data


class GetServiceDependencies:
    """
    Tool to get service dependency mappings.

    Thin orchestrator that:
    1. Delegates service resolution to ServiceResolver
    2. Delegates data access to repository
    """

    def __init__(
        self,
        service_resolver: ServiceResolver,
        repository: StatusRepositoryProtocol,
    ):
        """
        Initialize tool with dependencies.

        Args:
            service_resolver: Resolver for service names
            repository: Repository for dependency data access
        """
        self._resolver = service_resolver
        self._repo = repository

    async def execute(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        service_name: Optional[str] = None,
        infrastructure_code: Optional[str] = None,
    ) -> ToolResult:
        """
        Get service-infrastructure dependency mappings.

        Args:
            tenant_code: Tenant code
            environment: Target environment
            geo_loc_code: Geographic location code
            service_name: Optional service name to get dependencies for
            infrastructure_code: Optional infrastructure code to get dependents for

        Returns:
            ToolResult with dependency mappings or error
        """
        if not service_name and not infrastructure_code:
            return ToolResult(
                success=False,
                error="Either service_name or infrastructure_code must be provided",
            )

        if service_name:
            return await self._get_service_dependencies(
                service_name=service_name,
                tenant_code=tenant_code,
                environment=environment,
                geo_loc_code=geo_loc_code,
            )
        else:
            return await self._get_infrastructure_dependents(
                infrastructure_code=infrastructure_code,
                tenant_code=tenant_code,
            )

    async def _get_service_dependencies(
        self,
        service_name: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
    ) -> ToolResult:
        """Get dependencies for a service."""
        # Get available services for resolution
        services = await self._repo.get_services_with_config_status(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_code=geo_loc_code,
        )

        # Resolve service name
        resolution = self._resolver.resolve(service_name, services)

        if not resolution["resolved"]:
            return ToolResult(
                success=False,
                error=resolution.get("error", f"Could not resolve service '{service_name}'"),
                suggestions=resolution.get("suggestions"),
                matched_services=resolution.get("matched_services"),
            )

        # Get dependencies
        dependencies = await self._repo.get_service_dependencies(
            service_code=resolution["service_code"],
            tenant_code=tenant_code,
        )

        # Format dependencies
        formatted_deps = []
        for dep in dependencies:
            formatted_deps.append({
                "infrastructure_code": dep.infrastructure_mst_code,
                "infrastructure_name": dep.infrastructure.name if hasattr(dep, "infrastructure") and dep.infrastructure else None,
                "infrastructure_type": dep.infrastructure.infrastructuretype_ref_code if hasattr(dep, "infrastructure") and dep.infrastructure else None,
            })

        return ToolResult(
            success=True,
            data={
                "service_code": resolution["service_code"],
                "service_name": resolution["service_name"],
                "dependencies": formatted_deps,
                "dependency_count": len(formatted_deps),
            },
        )

    async def _get_infrastructure_dependents(
        self,
        infrastructure_code: str,
        tenant_code: str,
    ) -> ToolResult:
        """Get services that depend on an infrastructure."""
        dependents = await self._repo.get_infrastructure_dependents(
            infrastructure_code=infrastructure_code,
            tenant_code=tenant_code,
        )

        # Format dependents
        formatted_deps = []
        for dep in dependents:
            formatted_deps.append({
                "service_code": dep.services_mst_code,
                "service_name": dep.service.name if hasattr(dep, "service") and dep.service else None,
            })

        return ToolResult(
            success=True,
            data={
                "infrastructure_code": infrastructure_code,
                "dependents": formatted_deps,
                "dependent_count": len(formatted_deps),
            },
        )
