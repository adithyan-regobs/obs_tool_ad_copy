from uuid import uuid4
from typing import Dict, Any, Optional


def make_application(
    tenant_code: str,
    application_name: str,
    description: Optional[str] = None,
    workspace_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Factory to create application domain object.

    Generates unique application code and sets default values for
    creating a new application record.

    Args:
        tenant_code: Tenant code (UUID from tenant lookup)
        application_name: Application name from request
        description: Optional application description

    Returns:
        Dictionary with all fields for repository create method

    Example:
        >>> app_data = make_application(
        ...     tenant_code="550e8400-e29b-41d4-a716-446655440000",
        ...     application_name="Payment Service",
        ...     description="Payment processing application"
        ... )
        >>> app = await repository.create(**app_data)
    """
    application_code = str(uuid4())

    return {
        "code": application_code,
        "name": application_name,
        "description": description,
        "tenants_mst_code": tenant_code,
        "workspace_code": workspace_code,
        "is_deleted": False,
        "is_active": True
    }
