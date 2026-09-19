"""
Config Tools for Service Config Agent.

Tools for fetching service configs and parameter values.
Thin orchestrators that delegate to resolvers and repository.
"""
import logging
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

from app.core.enum import EnvironmentEnum
from app.services.service_config_agent.tools.base import ToolResult
from app.services.service_config_agent.tools.service_resolver import ServiceResolver
from app.services.service_config_agent.tools.parameter_resolver import ParameterResolver


@runtime_checkable
class ConfigRepositoryProtocol(Protocol):
    """Contract for config repository."""

    async def get_services_with_config_status(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
    ) -> List[dict]:
        """Get services with config status."""
        ...

    async def get_existing_config(
        self,
        service_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        tenant_code: str,
    ) -> Optional[Any]:
        """Get existing config."""
        ...


class GetServiceConfig:
    """
    Tool to fetch full config JSON for a service.

    Thin orchestrator that:
    1. Delegates service resolution to ServiceResolver
    2. Delegates data access to repository
    """

    def __init__(
        self,
        service_resolver: ServiceResolver,
        repository: ConfigRepositoryProtocol,
    ):
        """
        Initialize tool with dependencies.

        Args:
            service_resolver: Resolver for service names
            repository: Repository for config data access
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
        Fetch full config JSON for a service.

        Args:
            service_name: Service name (supports typos)
            tenant_code: Tenant code
            environment: Target environment
            geo_loc_code: Geographic location code

        Returns:
            ToolResult with config JSON or error
        """
        # Get available services
        services = await self._repo.get_services_with_config_status(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_code=geo_loc_code,
        )

        # Log available services
        service_names = [s.get("service_name", s.get("name", "unknown")) for s in services]
        logger.info(
            f"[GET_SERVICE_CONFIG] DB returned {len(services)} services for "
            f"{tenant_code}/{environment.value}/{geo_loc_code}: {service_names[:10]}{'...' if len(service_names) > 10 else ''}"
        )

        # Resolve service name
        resolution = self._resolver.resolve(service_name, services)

        # Log resolution result
        logger.info(
            f"[GET_SERVICE_CONFIG] Resolver result for '{service_name}': "
            f"resolved={resolution.get('resolved')}, "
            f"service_code={resolution.get('service_code')}, "
            f"confidence={resolution.get('confidence')}, "
            f"suggestions={resolution.get('suggestions')}"
        )

        if not resolution["resolved"]:
            return ToolResult(
                success=False,
                error=resolution.get("error", f"Could not resolve service '{service_name}'"),
                suggestions=resolution.get("suggestions"),
                matched_services=resolution.get("matched_services"),
            )

        # Fetch config
        config = await self._repo.get_existing_config(
            service_code=resolution["service_code"],
            environment=environment,
            geo_loc_code=geo_loc_code,
            tenant_code=tenant_code,
        )

        # Log config result
        logger.info(
            f"[GET_SERVICE_CONFIG] Config lookup for '{resolution['service_code']}' "
            f"in {environment.value}/{geo_loc_code}: found={config is not None}"
        )

        if not config:
            return ToolResult(
                success=False,
                error=f"No config found for '{resolution['service_name']}' in {environment.value}/{geo_loc_code}",
            )

        return ToolResult(
            success=True,
            data={
                "service_code": resolution["service_code"],
                "service_name": resolution["service_name"],
                "environment": environment.value,
                "geo_loc": geo_loc_code,
                "config": config.config,
                "sidecar_config": config.sidecar_config,
            },
        )


class GetParameterValue:
    """
    Tool to get specific parameter value for a service.

    Thin orchestrator that:
    1. Delegates service resolution to ServiceResolver
    2. Delegates parameter resolution to ParameterResolver
    3. Delegates data access to repository
    """

    # Map canonical parameter names to config paths
    CANONICAL_TO_CONFIG_PATH = {
        "memory": "ram",
        "container_port": "port",
        "health_check_path": "health",
        "enable_autoscaling": "autoscaling.enabled",
        "min_task_count": "autoscaling.min",
        "max_task_count": "autoscaling.max",
        "desired_count": "autoscaling.desired",
        "ebs_volume": "ebs.volume",
        "ebs_size": "ebs.size",
        "ebs_type": "ebs.type",
    }

    # Map sidecar parameters
    SIDECAR_PARAMETER_MAP = {
        "enable_datadog_sidecar": ("datadog", None),
        "datadog_sidecar_cpu": ("datadog", "cpu"),
        "datadog_sidecar_memory": ("datadog", "ram"),
        "enable_otel_sidecar": ("otel", None),
        "otel_sidecar_cpu": ("otel", "cpu"),
        "otel_sidecar_memory": ("otel", "ram"),
    }

    def __init__(
        self,
        service_resolver: ServiceResolver,
        parameter_resolver: ParameterResolver,
        repository: ConfigRepositoryProtocol,
    ):
        """
        Initialize tool with dependencies.

        Args:
            service_resolver: Resolver for service names
            parameter_resolver: Resolver for parameter names
            repository: Repository for config data access
        """
        self._service_resolver = service_resolver
        self._param_resolver = parameter_resolver
        self._repo = repository

    async def execute(
        self,
        service_name: str,
        parameter: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
    ) -> ToolResult:
        """
        Get specific parameter value for a service.

        Args:
            service_name: Service name (supports typos)
            parameter: Parameter name (supports aliases)
            tenant_code: Tenant code
            environment: Target environment
            geo_loc_code: Geographic location code

        Returns:
            ToolResult with parameter value or error
        """
        # Get available services
        services = await self._repo.get_services_with_config_status(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_code=geo_loc_code,
        )

        # Resolve service name
        service_resolution = self._service_resolver.resolve(service_name, services)

        if not service_resolution["resolved"]:
            return ToolResult(
                success=False,
                error=service_resolution.get("error", f"Could not resolve service '{service_name}'"),
                suggestions=service_resolution.get("suggestions"),
                matched_services=service_resolution.get("matched_services"),
            )

        # Resolve parameter name
        param_resolution = await self._param_resolver.resolve(parameter)

        if not param_resolution["resolved"]:
            return ToolResult(
                success=False,
                error=param_resolution.get("error", f"Could not resolve parameter '{parameter}'"),
                suggestions=param_resolution.get("suggestions"),
            )

        # Fetch config
        config = await self._repo.get_existing_config(
            service_code=service_resolution["service_code"],
            environment=environment,
            geo_loc_code=geo_loc_code,
            tenant_code=tenant_code,
        )

        if not config:
            return ToolResult(
                success=False,
                error=f"No config found for '{service_resolution['service_name']}' in {environment.value}/{geo_loc_code}",
            )

        # Extract parameter value
        canonical_param = param_resolution["canonical_name"]
        value = self._extract_value(
            config.config or {},
            config.sidecar_config or [],
            canonical_param,
        )

        if value is None:
            return ToolResult(
                success=False,
                error=f"Parameter '{canonical_param}' not found in config for '{service_resolution['service_name']}'",
            )

        return ToolResult(
            success=True,
            data={
                "service_code": service_resolution["service_code"],
                "service_name": service_resolution["service_name"],
                "parameter": canonical_param,
                "value": value,
                "environment": environment.value,
                "geo_loc": geo_loc_code,
            },
        )

    def _extract_value(
        self,
        config_dict: Dict[str, Any],
        sidecar_config: List[Dict[str, Any]],
        parameter_name: str,
    ) -> Any:
        """Extract parameter value from config."""
        # Check sidecar parameters first
        if parameter_name in self.SIDECAR_PARAMETER_MAP:
            return self._extract_sidecar_value(parameter_name, sidecar_config)

        # Map canonical name to config path
        config_path = self.CANONICAL_TO_CONFIG_PATH.get(parameter_name, parameter_name)

        # Handle nested paths
        if "." in config_path:
            parts = config_path.split(".")
            current = config_dict
            for part in parts:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return None
            return current

        return config_dict.get(config_path)

    def _extract_sidecar_value(
        self,
        parameter_name: str,
        sidecar_config: List[Dict[str, Any]],
    ) -> Any:
        """Extract value from sidecar config."""
        if not sidecar_config:
            return None

        sidecar_keyword, field_name = self.SIDECAR_PARAMETER_MAP.get(
            parameter_name, (None, None)
        )
        if not sidecar_keyword:
            return None

        for sidecar in sidecar_config:
            sidecar_name = (sidecar.get("name") or "").lower()
            sidecar_code = (sidecar.get("sidecar_config_code") or "").lower()
            is_match = sidecar_keyword in sidecar_name or sidecar_keyword in sidecar_code

            if is_match:
                if field_name is None:
                    return sidecar.get("enabled", True)
                return sidecar.get(field_name)

        return None
