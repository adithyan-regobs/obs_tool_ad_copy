"""
Naming strategy utilities for pipeline resources.

This module provides pluggable naming strategies per tenant.
Each tenant can have its own naming convention for:
- Organization name
- ECS service name
- ECR repository name
"""

from .base import NamingStrategy
from .aspora import AsporaNamingStrategy
from .default import DefaultNamingStrategy

__all__ = [
    "NamingStrategy",
    "AsporaNamingStrategy",
    "DefaultNamingStrategy",
    "get_naming_strategy",
]


def get_naming_strategy(tenant_code: str) -> NamingStrategy:
    """
    Get the appropriate naming strategy for a tenant.

    Args:
        tenant_code: Tenant code

    Returns:
        NamingStrategy instance for the tenant

    Examples:
        >>> strategy = get_naming_strategy("aspora")
        >>> isinstance(strategy, AsporaNamingStrategy)
        True

        >>> strategy = get_naming_strategy("vance")
        >>> isinstance(strategy, AsporaNamingStrategy)
        True

        >>> strategy = get_naming_strategy("other_tenant")
        >>> isinstance(strategy, DefaultNamingStrategy)
        True
    """
    # Both aspora and vance use the same naming strategy
    if tenant_code.lower() in ("aspora", "vance"):
        return AsporaNamingStrategy()

    return DefaultNamingStrategy()
