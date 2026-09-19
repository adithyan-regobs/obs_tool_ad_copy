from uuid import uuid4
from app.schemas.service_schemas import CreateServiceRequest


def make_service(
    data: CreateServiceRequest,
    tenant_code: str
) -> dict:
    """
    Factory to build a Service domain object from validated CreateServiceRequest data.

    Args:
        data: CreateServiceRequest schema with user input
        tenant_code: Tenant code (from JWT authentication)

    Returns:
        Dictionary containing service fields ready for repository create method

    This isolates object creation logic (like code generation, defaults, normalization)
    from the repository and service layers.
    """
    # Generate unique code for this service using UUID
    service_code = str(uuid4())

    # Return dictionary for repository create method
    # NOTE: infrastructuretype_ref_code and infra_vendor_enum have been moved to service_config
    service_data = {
        "code": service_code,
        "name": data.service_name,
        "tenants_mst_code": tenant_code,  # From JWT authentication, not from request
        "applications_mst_code": data.application_code,
        "resource_group_mst_code": data.resource_group_code,
        "service_type": data.service_type,
        "is_active": data.is_active,
        "is_public_facing": data.is_public_facing,
        "is_deleted": False,
    }

    return service_data
