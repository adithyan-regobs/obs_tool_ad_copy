from typing import Tuple
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.alerttype_ref_service import AlertTypeRefService
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

router = APIRouter()


@router.get("/get-active-alert-types", summary="Get Active Alert Types")
async def get_active_alert_types( 
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all active alert types for dropdown selection.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    This endpoint retrieves all active (is_active=True) alert types,
    typically used for populating dropdown lists in UI forms.

    **Response:**
    ```json
    {
        "total": 15,
        "alert_types": [
            {
                "code": "cpu_util",
                "name": "CPU Utilization"
            },
            {
                "code": "memory_util",
                "name": "Memory Utilization"
            },
            {
                "code": "http_4xx_rate",
                "name": "HTTP 4xx Error Rate"
            }
        ]
    }
    ```

    **Use Case:**
    - Populate dropdown in "Create Datadog Alert Query" modal
    - Filter alert types by active status
    - Display user-friendly names with codes for API calls

    **Returns:**
    - `total`: Total count of active alert types
    - `alert_types`: List of objects with `code` and `name`

    **Raises:**
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET ACTIVE ALERT TYPES - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   User Email: {user.email_id}")
        print(f"   Tenant Code: {tenant.code}")
        print(f"   Tenant Name: {tenant.name}")
        print("="*80 + "\n")

        service = AlertTypeRefService(db)
        result = await service.get_active_alert_types()

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get-alert-types-for-infrastructure", summary="Get Alert Types for Infrastructure Type")
async def get_alert_types_for_infrastructure(
    infrastructuretype_ref_code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get only alert types that have default policies for a specific infrastructure type.

    This endpoint is used for the Add Alert Policy Modal to ensure users can only
    select alert type + infrastructure type combinations that have default policies.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    **Query Parameters:**
    - `infrastructuretype_ref_code`: Infrastructure type code (e.g., "ecs_fargate_infrastructuretype_ref")

    **Response:**
    ```json
    {
        "total": 17,
        "alert_types": [
            {
                "code": "http_4xx_rate",
                "name": "HTTP 4XX Rate"
            },
            {
                "code": "http_5xx_rate",
                "name": "HTTP 5XX Rate"
            },
            {
                "code": "cpu_utilization",
                "name": "CPU Utilization"
            }
        ]
    }
    ```

    **Use Case:**
    - Populate alert type dropdown in "Add Alert Policy" modal after infrastructure type is selected
    - Prevents users from creating overrides for combinations without default policies
    - Ensures monitoring_policy_defaults_ref_code will always be filled

    **Returns:**
    - `total`: Count of alert types with default policies for this infrastructure type
    - `alert_types`: List of objects with `code` and `name`

    **Raises:**
    - `400`: Invalid infrastructure type code
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET ALERT TYPES FOR INFRASTRUCTURE - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Infrastructure Type: {infrastructuretype_ref_code}")
        print("="*80 + "\n")

        service = AlertTypeRefService(db)
        result = await service.get_alert_types_for_infrastructure(infrastructuretype_ref_code)

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) 
