"""
Recommendation Tools for Service Config Agent.

Thin orchestrators for data-driven recommendations and config validation.
Delegate business logic to recommendation_builder.
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from app.core.enum import EnvironmentEnum, ServiceTypeEnum
from app.services.service_config_agent.tools.base import ToolResult
from app.services.service_config_agent.tools.recommendation_builder import (
    build_recommendation,
    build_recommendations_summary,
    calculate_validation_score,
    validate_value,
)
from app.services.service_config_agent.tools.recommendation_defaults import (
    DEFAULT_RECOMMENDATION_PARAMETERS,
)


@runtime_checkable
class RecommendationRepositoryProtocol(Protocol):
    """Contract for recommendation repository."""

    async def get_parameter_stats_by_service_type(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        parameter_name: str,
        service_type: ServiceTypeEnum,
        geo_loc_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get parameter stats filtered by service type."""
        ...


def _parse_service_type(service_type: str) -> Optional[ServiceTypeEnum]:
    """Parse service type string to enum."""
    if service_type is None:
        return None
    try:
        normalized = service_type.upper().replace(" ", "_").replace("-", "_")
        if normalized in ("API", "APIS"):
            return ServiceTypeEnum.API
        if normalized in ("BACKGROUND_SERVICE", "BACKGROUND", "WORKER", "WORKERS", "BACKGROUND_SERVICES"):
            return ServiceTypeEnum.BACKGROUND_SERVICE
        return ServiceTypeEnum(normalized)
    except ValueError:
        return None


class GetRecommendations:
    """
    Tool to get data-driven parameter recommendations.

    Thin orchestrator that:
    1. Delegates data access to repository
    2. Delegates recommendation building to recommendation_builder
    """

    def __init__(self, repository: RecommendationRepositoryProtocol):
        """
        Initialize tool with dependencies.

        Args:
            repository: Repository for data access
        """
        self._repo = repository

    async def execute(
        self,
        service_type: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        parameters: List[str] = None,
        geo_loc_code: Optional[str] = None,
    ) -> ToolResult:
        """
        Get data-driven recommendations for a service type.

        Args:
            service_type: "API" or "BACKGROUND_SERVICE"
            tenant_code: Tenant code
            environment: Target environment
            parameters: Optional list of specific parameters
            geo_loc_code: Optional geographic location filter

        Returns:
            ToolResult with recommendations data
        """
        # Parse service_type
        svc_type_enum = _parse_service_type(service_type)
        if svc_type_enum is None:
            return ToolResult(
                success=False,
                error=f"Invalid service_type '{service_type}'. Must be 'API' or 'BACKGROUND_SERVICE'.",
            )

        # Determine parameters to recommend
        params_to_check = parameters or DEFAULT_RECOMMENDATION_PARAMETERS

        # Query stats and build recommendations
        recommendations = {}
        total_configs_sampled = 0

        for param in params_to_check:
            stats = await self._repo.get_parameter_stats_by_service_type(
                tenant_code=tenant_code,
                environment=environment,
                parameter_name=param,
                service_type=svc_type_enum,
                geo_loc_code=geo_loc_code,
            )

            recommendations[param] = build_recommendation(param, stats)
            total_configs_sampled = max(total_configs_sampled, stats.get("total_configs", 0))

        # Build summary
        summary = build_recommendations_summary(
            recommendations=recommendations,
            total_configs=total_configs_sampled,
            service_type=svc_type_enum.value,
            environment=environment.value,
        )

        return ToolResult(
            success=True,
            data={
                "service_type": svc_type_enum.value,
                "environment": environment.value,
                "recommendations": recommendations,
                "summary": summary,
            },
        )


class ValidateConfig:
    """
    Tool to validate config values against common patterns.

    Thin orchestrator that:
    1. Delegates data access to repository
    2. Delegates validation logic to recommendation_builder
    """

    def __init__(self, repository: RecommendationRepositoryProtocol):
        """
        Initialize tool with dependencies.

        Args:
            repository: Repository for data access
        """
        self._repo = repository

    async def execute(
        self,
        config: Dict[str, Any],
        service_type: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: Optional[str] = None,
    ) -> ToolResult:
        """
        Validate config values against common patterns.

        Args:
            config: Dict of parameter values to validate
            service_type: "API" or "BACKGROUND_SERVICE"
            tenant_code: Tenant code
            environment: Target environment
            geo_loc_code: Optional geographic location filter

        Returns:
            ToolResult with validation results
        """
        # Parse service_type
        svc_type_enum = _parse_service_type(service_type)
        if svc_type_enum is None:
            return ToolResult(
                success=False,
                error=f"Invalid service_type '{service_type}'. Must be 'API' or 'BACKGROUND_SERVICE'.",
            )

        if not config:
            return ToolResult(
                success=False,
                error="No config parameters provided to validate.",
            )

        # Query stats and validate each parameter
        validation_results = {}

        for param, user_value in config.items():
            stats = await self._repo.get_parameter_stats_by_service_type(
                tenant_code=tenant_code,
                environment=environment,
                parameter_name=param,
                service_type=svc_type_enum,
                geo_loc_code=geo_loc_code,
            )

            validation_results[param] = validate_value(param, user_value, stats)

        # Calculate score and summary
        score, summary = calculate_validation_score(validation_results)

        return ToolResult(
            success=True,
            data={
                "validation_results": validation_results,
                "overall_score": f"{score}%",
                "summary": f"{summary} for {svc_type_enum.value} services",
                "service_type": svc_type_enum.value,
                "environment": environment.value,
            },
        )
