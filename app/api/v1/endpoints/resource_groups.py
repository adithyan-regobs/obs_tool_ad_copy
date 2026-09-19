from typing import Tuple, Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.resource_group_schemas import (
    CreateResourceGroupRequest,
    ResourceGroupCreateResponse,
)
from app.services.resource_group_mst_service import ResourceGroupMstService
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

router = APIRouter()


@router.get("/get-all-resource-groups", summary="Get All Resource Groups")
async def get_all_resource_groups(
    application_code: Optional[str] = Query(None, description="Optional application code filter"),
    kind: Optional[str] = Query(
        None,
        description="Optional kind filter: 'service' (groups that hold services) or 'infra'",
    ),
    skip: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(100, ge=1, le=500, description="Page size"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all resource groups for a tenant with optional application filter.

    This endpoint retrieves all resource groups with complete information,
    typically used for populating dropdown lists in UI forms.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: only returns resource groups for authenticated user's tenant
        - tenant_code automatically injected from JWT token

    **Query Parameters:**
    - `application_code` (optional): Filter by application code
    - `kind` (optional): Filter by kind — `service` for the groups a service can be
      placed in, `infra` for the ones holding S3/SQS/DynamoDB and the like. Omit for
      both. Callers choosing a group for a SERVICE should pass `service`.
    - `skip` (optional): Pagination offset (default: 0)
    - `limit` (optional): Page size (default: 100, max: 500)

    **Response:**
    ```json
    {
        "total": 10,
        "skip": 0,
        "limit": 100,
        "resource_groups": [
            {
                "id": "uuid",
                "code": "rg_001",
                "name": "Production Resources",
                "kind": "service",
                "applications_mst_code": "app_001",
                "application_name": "My Application",
                "tenants_mst_code": "tenant_001",
                "is_active": true,
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00"
            }
        ]
    }
    ```

    **Use Case:**
    - Populate dropdown in "Create Monitoring Policy Override" modal
    - Filter resource groups by application
    - Display resource groups with application context

    **Security:**
    - Enforces tenant isolation - only returns resource groups for authenticated user's tenant

    **Returns:**
    - `total`: Total count of resource groups
    - `skip`: Pagination offset used
    - `limit`: Page size used
    - `resource_groups`: List of objects with resource group details

    **Raises:**
    - `400`: Bad request (validation error)
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log tenant isolation
        print("\n" + "="*80)
        print("🔍 GET ALL RESOURCE GROUPS - TENANT ISOLATION CHECK")
        print(f"   User Code: {user.code}")
        print(f"   User Email: {user.email_id}")
        print(f"   Tenant Code: {tenant.code}")
        print(f"   Tenant Name: {tenant.name}")
        print(f"   Application Filter: {application_code}")
        print(f"   Kind Filter: {kind}")
        print("="*80 + "\n")

        service = ResourceGroupMstService(db)
        result = await service.get_all_resource_groups(
            tenant_code=tenant.code,
            user_code=user.code,
            application_code=application_code,
            kind=kind,
            skip=skip,
            limit=limit
        )

        return {
            "total": result["total"],
            "skip": skip,
            "limit": limit,
            "resource_groups": result["resource_groups"]
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/create-resource-group",
    response_model=ResourceGroupCreateResponse,
    summary="Create Resource Group",
)
async def create_resource_group(
    data: CreateResourceGroupRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a resource group under one application.

    Resource groups are what roles are granted on: `admin` / `approver` held on
    a group is inherited by every service parented under it. This is the only
    way to make one — previously they existed solely as the default group
    auto-created with an application.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: the application must belong to the
          authenticated user's tenant; tenant_code comes from the JWT, never
          from the request body

    Note: this writes app_db only. A new group has no presence in OpenFGA and
    therefore no members, so services assigned to it are reachable by org owners
    alone until someone grants a role on it in Access Control.
    """
    user, tenant = user_and_tenant
    service = ResourceGroupMstService(db)
    try:
        result = await service.create_resource_group(
            tenant_code=tenant.code,
            name=data.name,
            application_code=data.application_code,
            description=data.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ResourceGroupCreateResponse(**result)
