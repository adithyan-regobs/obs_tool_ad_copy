"""
Compare Tools for Service Config Agent.

Tools for comparing service configs.
Thin orchestrators that delegate to resolvers and repository.

Includes pre-formatted markdown tables to prevent LLM inconsistency.
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from app.core.enum import EnvironmentEnum
from app.services.service_config_agent.tools.base import ToolResult
from app.services.service_config_agent.tools.service_resolver import ServiceResolver
from app.utils.service_config_chat.table_formatter import (
    format_service_comparison_table,
    format_environment_comparison_table,
)


@runtime_checkable
class CompareRepositoryProtocol(Protocol):
    """Contract for compare repository."""

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


class CompareConfigs:
    """
    Tool to compare configs between two services.

    Thin orchestrator that:
    1. Delegates service resolution to ServiceResolver
    2. Delegates data access to repository
    3. Computes diff between configs
    """

    def __init__(
        self,
        service_resolver: ServiceResolver,
        repository: CompareRepositoryProtocol,
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
        service_a: str,
        service_b: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        parameters: Optional[List[str]] = None,
    ) -> ToolResult:
        """
        Compare configs between two services.

        Args:
            service_a: First service name (supports typos)
            service_b: Second service name (supports typos)
            tenant_code: Tenant code
            environment: Target environment
            geo_loc_code: Geographic location code
            parameters: Optional list of parameters to compare

        Returns:
            ToolResult with comparison diff or error
        """
        # Get available services
        services = await self._repo.get_services_with_config_status(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_code=geo_loc_code,
        )

        # Resolve service A
        resolution_a = self._resolver.resolve(service_a, services)
        if not resolution_a["resolved"]:
            return ToolResult(
                success=False,
                error=f"Could not resolve service '{service_a}': {resolution_a.get('error', '')}",
                suggestions=resolution_a.get("suggestions"),
                matched_services=resolution_a.get("matched_services"),
            )

        # Resolve service B
        resolution_b = self._resolver.resolve(service_b, services)
        if not resolution_b["resolved"]:
            return ToolResult(
                success=False,
                error=f"Could not resolve service '{service_b}': {resolution_b.get('error', '')}",
                suggestions=resolution_b.get("suggestions"),
                matched_services=resolution_b.get("matched_services"),
            )

        # Fetch both configs
        config_a = await self._repo.get_existing_config(
            service_code=resolution_a["service_code"],
            environment=environment,
            geo_loc_code=geo_loc_code,
            tenant_code=tenant_code,
        )

        config_b = await self._repo.get_existing_config(
            service_code=resolution_b["service_code"],
            environment=environment,
            geo_loc_code=geo_loc_code,
            tenant_code=tenant_code,
        )

        if not config_a:
            return ToolResult(
                success=False,
                error=f"No config found for '{resolution_a['service_name']}' in {environment.value}/{geo_loc_code}",
            )

        if not config_b:
            return ToolResult(
                success=False,
                error=f"No config found for '{resolution_b['service_name']}' in {environment.value}/{geo_loc_code}",
            )

        # Compute diff
        diff = self._compute_diff(
            config_a.config or {},
            config_b.config or {},
            parameters,
        )

        # Pre-format comparison table to prevent LLM inconsistency
        formatted_table = format_service_comparison_table(
            service_a_name=resolution_a["service_name"],
            service_b_name=resolution_b["service_name"],
            differences=diff["differences"],
            only_in_a=diff["only_in_a"],
            only_in_b=diff["only_in_b"],
            identical_count=len(diff["identical"]),
        )

        return ToolResult(
            success=True,
            data={
                "service_a": {
                    "code": resolution_a["service_code"],
                    "name": resolution_a["service_name"],
                },
                "service_b": {
                    "code": resolution_b["service_code"],
                    "name": resolution_b["service_name"],
                },
                "environment": environment.value,
                "geo_loc": geo_loc_code,
                "comparison_table": formatted_table,  # Pre-formatted for LLM
                "differences_count": len(diff["differences"]),
                "only_in_a_count": len(diff["only_in_a"]),
                "only_in_b_count": len(diff["only_in_b"]),
                "identical_count": len(diff["identical"]),
            },
        )

    def _compute_diff(
        self,
        config_a: Dict[str, Any],
        config_b: Dict[str, Any],
        parameters: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Compute diff between two config dicts."""
        # Flatten nested configs for comparison
        flat_a = self._flatten_config(config_a)
        flat_b = self._flatten_config(config_b)

        # Filter to requested parameters if specified
        if parameters:
            flat_a = {k: v for k, v in flat_a.items() if k in parameters}
            flat_b = {k: v for k, v in flat_b.items() if k in parameters}

        all_keys = set(flat_a.keys()) | set(flat_b.keys())

        differences = {}
        only_in_a = {}
        only_in_b = {}
        identical = {}

        for key in sorted(all_keys):
            in_a = key in flat_a
            in_b = key in flat_b

            if in_a and in_b:
                if flat_a[key] != flat_b[key]:
                    differences[key] = {
                        "a": flat_a[key],
                        "b": flat_b[key],
                    }
                else:
                    identical[key] = flat_a[key]
            elif in_a:
                only_in_a[key] = flat_a[key]
            else:
                only_in_b[key] = flat_b[key]

        return {
            "differences": differences,
            "only_in_a": only_in_a,
            "only_in_b": only_in_b,
            "identical": identical,
        }

    def _flatten_config(
        self,
        config: Dict[str, Any],
        prefix: str = "",
    ) -> Dict[str, Any]:
        """Flatten nested config dict."""
        result = {}
        for key, value in config.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(self._flatten_config(value, full_key))
            else:
                result[full_key] = value
        return result


class CompareEnvironments:
    """
    Tool to compare same service across environments.

    Thin orchestrator that:
    1. Delegates service resolution to ServiceResolver
    2. Delegates data access to repository
    3. Computes diff between environment configs
    """

    def __init__(
        self,
        service_resolver: ServiceResolver,
        repository: CompareRepositoryProtocol,
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
        env_a: EnvironmentEnum,
        env_b: EnvironmentEnum,
        tenant_code: str,
        geo_loc_code: str,
    ) -> ToolResult:
        """
        Compare same service across environments.

        Args:
            service_name: Service name (supports typos)
            env_a: First environment
            env_b: Second environment
            tenant_code: Tenant code
            geo_loc_code: Geographic location code

        Returns:
            ToolResult with comparison diff or error
        """
        # Get services from first environment for resolution
        services = await self._repo.get_services_with_config_status(
            tenant_code=tenant_code,
            environment=env_a,
            geo_loc_code=geo_loc_code,
        )

        # Resolve service name
        resolution = self._resolver.resolve(service_name, services)
        if not resolution["resolved"]:
            return ToolResult(
                success=False,
                error=f"Could not resolve service '{service_name}': {resolution.get('error', '')}",
                suggestions=resolution.get("suggestions"),
                matched_services=resolution.get("matched_services"),
            )

        service_code = resolution["service_code"]
        resolved_name = resolution["service_name"]

        # Fetch config from env_a
        config_a = await self._repo.get_existing_config(
            service_code=service_code,
            environment=env_a,
            geo_loc_code=geo_loc_code,
            tenant_code=tenant_code,
        )

        # Fetch config from env_b
        config_b = await self._repo.get_existing_config(
            service_code=service_code,
            environment=env_b,
            geo_loc_code=geo_loc_code,
            tenant_code=tenant_code,
        )

        if not config_a:
            return ToolResult(
                success=False,
                error=f"No config found for '{resolved_name}' in {env_a.value}/{geo_loc_code}",
            )

        if not config_b:
            return ToolResult(
                success=False,
                error=f"No config found for '{resolved_name}' in {env_b.value}/{geo_loc_code}",
            )

        # Compute diff
        diff = self._compute_diff(
            config_a.config or {},
            config_b.config or {},
        )

        # Pre-format comparison table to prevent LLM inconsistency
        formatted_table = format_environment_comparison_table(
            service_name=resolved_name,
            env_a=env_a.value,
            env_b=env_b.value,
            differences=diff["differences"],
            only_in_a=diff["only_in_a"],
            only_in_b=diff["only_in_b"],
            identical_count=len(diff["identical"]),
        )

        return ToolResult(
            success=True,
            data={
                "service_code": service_code,
                "service_name": resolved_name,
                "env_a": env_a.value,
                "env_b": env_b.value,
                "geo_loc": geo_loc_code,
                "comparison_table": formatted_table,  # Pre-formatted for LLM
                "differences_count": len(diff["differences"]),
                "only_in_a_count": len(diff["only_in_a"]),
                "only_in_b_count": len(diff["only_in_b"]),
                "identical_count": len(diff["identical"]),
            },
        )

    def _compute_diff(
        self,
        config_a: Dict[str, Any],
        config_b: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compute diff between two config dicts."""
        flat_a = self._flatten_config(config_a)
        flat_b = self._flatten_config(config_b)

        all_keys = set(flat_a.keys()) | set(flat_b.keys())

        differences = {}
        only_in_a = {}
        only_in_b = {}
        identical = {}

        for key in sorted(all_keys):
            in_a = key in flat_a
            in_b = key in flat_b

            if in_a and in_b:
                if flat_a[key] != flat_b[key]:
                    differences[key] = {
                        "env_a": flat_a[key],
                        "env_b": flat_b[key],
                    }
                else:
                    identical[key] = flat_a[key]
            elif in_a:
                only_in_a[key] = flat_a[key]
            else:
                only_in_b[key] = flat_b[key]

        return {
            "differences": differences,
            "only_in_a": only_in_a,
            "only_in_b": only_in_b,
            "identical": identical,
        }

    def _flatten_config(
        self,
        config: Dict[str, Any],
        prefix: str = "",
    ) -> Dict[str, Any]:
        """Flatten nested config dict."""
        result = {}
        for key, value in config.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(self._flatten_config(value, full_key))
            else:
                result[full_key] = value
        return result
