"""
Tools for Service Config Agent.

Provides data access tools with fuzzy matching support.
"""

# Types
from app.services.service_config_agent.tools.base import (
    ToolResult,
    ServiceResolutionResult,
    ParameterResolutionResult,
)

# Resolvers
from app.services.service_config_agent.tools.service_resolver import (
    ServiceResolver,
    ServiceMatcherProtocol,
)
from app.services.service_config_agent.tools.parameter_resolver import (
    ParameterResolver,
    ParameterHelperProtocol,
)

# Config Tools
from app.services.service_config_agent.tools.config_tools import (
    GetServiceConfig,
    GetParameterValue,
    ConfigRepositoryProtocol,
)

# Search Tools
from app.services.service_config_agent.tools.search_tools import (
    SearchServicesByParameter,
    SemanticParameterSearch,
    SemanticConfigSearch,
    SearchRepositoryProtocol,
    VectorMatcherProtocol,
    ConfigVectorizerProtocol,
)

# Compare Tools
from app.services.service_config_agent.tools.compare_tools import (
    CompareConfigs,
    CompareEnvironments,
    CompareRepositoryProtocol,
)

# Status Tools
from app.services.service_config_agent.tools.status_tools import (
    GetDeploymentStatus,
    GetServiceDependencies,
    StatusRepositoryProtocol,
)

__all__ = [
    # Types
    "ToolResult",
    "ServiceResolutionResult",
    "ParameterResolutionResult",
    # Resolvers
    "ServiceResolver",
    "ParameterResolver",
    "ServiceMatcherProtocol",
    "ParameterHelperProtocol",
    # Config Tools
    "GetServiceConfig",
    "GetParameterValue",
    "ConfigRepositoryProtocol",
    # Search Tools
    "SearchServicesByParameter",
    "SemanticParameterSearch",
    "SemanticConfigSearch",
    "SearchRepositoryProtocol",
    "VectorMatcherProtocol",
    "ConfigVectorizerProtocol",
    # Compare Tools
    "CompareConfigs",
    "CompareEnvironments",
    "CompareRepositoryProtocol",
    # Status Tools
    "GetDeploymentStatus",
    "GetServiceDependencies",
    "StatusRepositoryProtocol",
]
