from typing import Tuple
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.infrastructuretype_ref_service import InfrastructureTypeRefService
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

router = APIRouter()


@router.get("/get-all-infrastructure-types", summary="Get All Infrastructure Types")
async def get_all_infrastructure_types(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all infrastructure types for dropdown selection.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    This endpoint retrieves all non-deleted infrastructure types,
    typically used for populating dropdown lists in UI forms.

    **Response:**
    ```json
    {
        "total": 10,
        "infrastructure_types": [
            {
                "code": "ec2",
                "name": "AWS EC2"
            },
            {
                "code": "rds",
                "name": "AWS RDS"
            },
            {
                "code": "lambda",
                "name": "AWS Lambda"
            }
        ]
    }
    ```

    **Use Case:**
    - Populate dropdown in "Create Datadog Alert Query" modal
    - Filter infrastructure types by vendor, family, or capabilities
    - Display user-friendly names with codes for API calls

    **Returns:**
    - `total`: Total count of infrastructure types
    - `infrastructure_types`: List of objects with `code` and `name`

    **Raises:**
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET ALL INFRASTRUCTURE TYPES - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print("="*80 + "\n")

        service = InfrastructureTypeRefService(db)
        result = await service.get_all_infrastructure_types()
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get-infrastructure-types-with-defaults", summary="Get Infrastructure Types with Default Policies")
async def get_infrastructure_types_with_defaults(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get only infrastructure types that have default monitoring policies.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    This endpoint is used for the Add Alert Policy Modal to ensure users
    can only create policy overrides for infrastructure types that already
    have default policies defined in the monitoring_policy_defaults_ref table.

    **Response:**
    ```json
    {
        "total": 7,
        "infrastructure_types": [
            {
                "code": "ecs_fargate_infrastructuretype_ref",
                "name": "ECS Fargate",
                "infra_vendor": "aws"
            },
            {
                "code": "lambda_infrastructuretype_ref",
                "name": "AWS Lambda",
                "infra_vendor": "aws"
            }
        ]
    }
    ```

    **Use Case:**
    - Populate infrastructure type dropdown in "Add Alert Policy" modal
    - Prevents users from creating overrides for infrastructure types without defaults
    - Ensures monitoring_policy_defaults_ref_code will always be filled

    **Returns:**
    - `total`: Count of infrastructure types with at least one default policy
    - `infrastructure_types`: List of objects with `code`, `name`, and `infra_vendor`

    **Raises:**
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET INFRASTRUCTURE TYPES WITH DEFAULTS - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print("="*80 + "\n")

        service = InfrastructureTypeRefService(db)
        result = await service.get_infrastructure_types_with_default_policies()
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
