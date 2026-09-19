"""
Search Tools for Service Config Agent.

Tools for searching services by parameter criteria and semantic search.
Thin orchestrators that delegate to resolvers and repository.
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from app.core.enum import EnvironmentEnum
from app.services.service_config_agent.tools.base import ToolResult
from app.services.service_config_agent.tools.parameter_resolver import ParameterResolver


@runtime_checkable
class SearchRepositoryProtocol(Protocol):
    """Contract for search repository."""

    async def get_parameter_stats(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        parameter_name: str,
        infra_vendor: Optional[str] = None,
        infrastructure_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get parameter statistics across configs."""
        ...


@runtime_checkable
class VectorMatcherProtocol(Protocol):
    """Contract for vector parameter matching."""

    async def match_parameter(self, query: str) -> Any:
        """Match query to canonical parameter."""
        ...


class SearchServicesByParameter:
    """
    Tool to find services matching parameter criteria.

    Thin orchestrator that:
    1. Delegates parameter resolution to ParameterResolver
    2. Delegates search to repository
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
    }

    SIDECAR_PARAMETER_MAP = {
        "enable_datadog_sidecar": ("datadog", None),
        "enable_otel_sidecar": ("otel", None),
    }

    def __init__(
        self,
        parameter_resolver: ParameterResolver,
        repository: SearchRepositoryProtocol,
    ):
        """
        Initialize tool with dependencies.

        Args:
            parameter_resolver: Resolver for parameter names
            repository: Repository for search operations
        """
        self._param_resolver = parameter_resolver
        self._repo = repository

    async def execute(
        self,
        parameter: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        value: Optional[Any] = None,
        operator: str = "eq",
        infra_vendor: Optional[str] = None,
        infrastructure_type: Optional[str] = None,
        geo_loc_code: Optional[str] = None,
    ) -> ToolResult:
        """
        Find services matching parameter criteria.

        Args:
            parameter: Parameter name (supports aliases)
            tenant_code: Tenant code
            environment: Target environment
            value: Optional value to filter by
            operator: Comparison operator (eq, gt, lt, contains, exists)
            infra_vendor: Optional infrastructure vendor filter
            infrastructure_type: Optional infrastructure type filter
            geo_loc_code: Optional geographic location filter (mumbai, virginia, etc.)

        Returns:
            ToolResult with matching services or error
        """
        # Resolve parameter name
        param_resolution = await self._param_resolver.resolve(parameter)

        if not param_resolution["resolved"]:
            return ToolResult(
                success=False,
                error=param_resolution.get("error", f"Could not resolve parameter '{parameter}'"),
                suggestions=param_resolution.get("suggestions"),
            )

        canonical_param = param_resolution["canonical_name"]

        # Get parameter stats (includes values and outliers)
        stats = await self._repo.get_parameter_stats(
            tenant_code=tenant_code,
            environment=environment,
            parameter_name=canonical_param,
            infra_vendor=infra_vendor,
            infrastructure_type=infrastructure_type,
            geo_loc_code=geo_loc_code,
        )

        # Build location description for messages
        location_desc = environment.value
        if geo_loc_code:
            location_desc = f"{environment.value}/{geo_loc_code}"

        if not stats.get("values"):
            return ToolResult(
                success=False,
                error=f"No services found with parameter '{canonical_param}' in {location_desc}",
            )

        # Filter by value if specified
        matching_services = self._filter_by_value(stats, value, operator)

        # Extract stats for pre-formatting
        most_common = stats.get("most_common")
        total_configs = stats.get("total_configs", 0)
        values = stats.get("values", {})

        # Build pre-formatted statistics summary to prevent LLM hallucination
        # LLMs reliably copy text but unreliably follow "don't compute" instructions
        summary_parts = []
        if most_common:
            most_common_count = values.get(most_common, 0)
            summary_parts.append(f"Most common value: {most_common} ({most_common_count} configs)")
        summary_parts.append(f"Total configs with this parameter: {total_configs}")

        # List unique values without counts (prevents LLM from computing its own stats)
        unique_values = list(values.keys())
        if len(unique_values) <= 10:
            summary_parts.append(f"Unique values: {', '.join(str(v) for v in unique_values)}")
        else:
            summary_parts.append(f"Unique values: {len(unique_values)} distinct values")

        # Add outliers if present
        outliers = stats.get("outliers", [])
        if outliers:
            outlier_strs = [f"{o['service']}={o['value']}" for o in outliers[:3]]
            summary_parts.append(f"Outliers: {', '.join(outlier_strs)}")

        result_data = {
            "parameter": canonical_param,
            "environment": environment.value,
            "statistics_summary": "\n".join(summary_parts),  # Pre-formatted for LLM
            "matching_services": matching_services,
            # Structured data for programmatic use (but not raw distribution)
            "total_configs": total_configs,
            "unique_values_count": len(unique_values),
            # For autofill extraction
            "most_common_value": most_common,
        }

        # Include geo_loc if filtered
        if geo_loc_code:
            result_data["geo_loc"] = geo_loc_code

        return ToolResult(
            success=True,
            data=result_data,
        )

    def _filter_by_value(
        self,
        stats: Dict[str, Any],
        value: Optional[Any],
        operator: str,
    ) -> List[Dict[str, Any]]:
        """Filter services by value using operator."""
        if value is None:
            # Return all services with this parameter
            return [
                {"value": v, "count": c}
                for v, c in stats.get("values", {}).items()
            ]

        value_str = str(value)
        values = stats.get("values", {})

        if operator == "eq":
            count = values.get(value_str, 0)
            return [{"value": value_str, "count": count}] if count > 0 else []

        if operator == "exists":
            return [
                {"value": v, "count": c}
                for v, c in values.items()
            ]

        if operator == "contains":
            return [
                {"value": v, "count": c}
                for v, c in values.items()
                if value_str.lower() in v.lower()
            ]

        # Numeric comparisons
        try:
            target_num = float(value)
            matching = []
            for v, c in values.items():
                try:
                    v_num = float(v)
                    if operator == "gt" and v_num > target_num:
                        matching.append({"value": v, "count": c})
                    elif operator == "lt" and v_num < target_num:
                        matching.append({"value": v, "count": c})
                except ValueError:
                    continue
            return matching
        except ValueError:
            return []


class SemanticParameterSearch:
    """
    Tool for vector search of parameters by natural language.

    Thin orchestrator that delegates to VectorParameterMatcher.
    """

    def __init__(self, vector_matcher: VectorMatcherProtocol):
        """
        Initialize tool with vector matcher.

        Args:
            vector_matcher: VectorParameterMatcher instance
        """
        self._matcher = vector_matcher

    async def execute(self, query: str) -> ToolResult:
        """
        Search for parameters by natural language query.

        Args:
            query: Natural language query (e.g., "heap size", "memory limit")

        Returns:
            ToolResult with matching parameters or error
        """
        if not query:
            return ToolResult(
                success=False,
                error="No search query provided",
            )

        # Delegate to vector matcher
        match = await self._matcher.match_parameter(query)

        if match.canonical_name is None:
            return ToolResult(
                success=False,
                error=f"No parameters matching '{query}' found",
            )

        # Build result with match info
        result_data = {
            "query": query,
            "canonical_name": match.canonical_name,
            "confidence": match.confidence.value if hasattr(match.confidence, "value") else str(match.confidence),
            "score": match.score,
            "definition": match.definition,
        }

        # Include alternatives for uncertain matches
        if match.alternatives:
            result_data["alternatives"] = match.alternatives

        return ToolResult(
            success=True,
            data=result_data,
        )


@runtime_checkable
class ConfigVectorizerProtocol(Protocol):
    """Contract for config vectorizer."""

    async def search_similar_configs(
        self,
        query: str,
        tenant_code: str,
        environment: Optional[str] = None,
        geo_loc: Optional[str] = None,
        limit: int = 5,
        score_threshold: float = 0.3,
    ) -> List[Dict[str, Any]]:
        """Search for configs similar to query."""
        ...


class SemanticConfigSearch:
    """
    Tool for vector search across service configurations.

    Enables queries like "Find services similar to payment-service"
    or "Show me high-memory services with autoscaling".
    """

    def __init__(self, vectorizer: ConfigVectorizerProtocol):
        """
        Initialize tool with config vectorizer.

        Args:
            vectorizer: ConfigVectorizer instance
        """
        self._vectorizer = vectorizer

    async def execute(
        self,
        query: str,
        tenant_code: str,
        environment: Optional[EnvironmentEnum] = None,
        geo_loc_code: Optional[str] = None,
        limit: int = 5,
    ) -> ToolResult:
        """
        Search for configs similar to a natural language query.

        Args:
            query: Natural language query
            tenant_code: Tenant code to filter by
            environment: Optional environment filter
            geo_loc_code: Optional geo location filter
            limit: Maximum results

        Returns:
            ToolResult with matching configs or error
        """
        if not query:
            return ToolResult(
                success=False,
                error="No search query provided",
            )

        # Convert environment enum to string if provided
        env_str = environment.value if environment else None

        # Search for similar configs
        results = await self._vectorizer.search_similar_configs(
            query=query,
            tenant_code=tenant_code,
            environment=env_str,
            geo_loc=geo_loc_code,
            limit=limit,
        )

        if not results:
            return ToolResult(
                success=False,
                error=f"No configs matching '{query}' found",
            )

        return ToolResult(
            success=True,
            data={
                "query": query,
                "matches": results,
                "total_matches": len(results),
            },
        )
