from uuid import uuid4
from typing import Dict, Any
from app.core.enum import ResourceGroupKindEnum


def make_resource_group_mst(
    name: str,
    kind: str,
    tenant_code: str,
    application_code: str,
    description: str = None
) -> Dict[str, Any]:
    """
    Factory to create a resource group with specified parameters.

    Args:
        name: Resource group name
        kind: Resource group kind ('service' or 'infra')
        tenant_code: Tenant code
        application_code: Application code
        description: Optional description

    Returns:
        Dictionary with all fields for repository create method

    Example:
        >>> rg_data = make_resource_group_mst(
        ...     name="default-infra",
        ...     kind="infra",
        ...     tenant_code="vance",
        ...     application_code="core"
        ... )
        >>> rg = await repository.create(**rg_data)
    """
    resource_group_code = str(uuid4())

    if description is None:
        description = f"{kind.capitalize()} resource group for {application_code}"

    return {
        "code": resource_group_code,
        "name": name,
        "description": description,
        "kind": kind,
        "applications_mst_code": application_code,
        "tenants_mst_code": tenant_code,
        "is_deleted": False,
        "is_active": True
    }


def make_default_resource_group(
    application_code: str,
    application_name: str,
    tenant_code: str
) -> Dict[str, Any]:
    """
    Factory to create default resource group for a new application.

    Generates unique resource group code and sets dummy/default values
    for auto-creating a resource group when an application is created.

    Args:
        application_code: Code of the created application (UUID)
        application_name: Name of application (used for descriptive RG name)
        tenant_code: Tenant code (UUID from tenant lookup)

    Returns:
        Dictionary with all fields for repository create method

    Example:
        >>> rg_data = make_default_resource_group(
        ...     application_code="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        ...     application_name="Payment Service",
        ...     tenant_code="550e8400-e29b-41d4-a716-446655440000"
        ... )
        >>> rg = await repository.create(**rg_data)
    """
    resource_group_code = str(uuid4())

    return {
        "code": resource_group_code,
        "name": "Default service group",  # Descriptive dummy name
        "description": "default description",
        "kind": ResourceGroupKindEnum.service,  # Default to service type
        "applications_mst_code": application_code,
        "tenants_mst_code": tenant_code,  
        "is_deleted": False,
        "is_active": True
    }
