"""
API endpoints for Region Reference
"""
from typing import Tuple
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.core.enum import InfraVendorEnum
from app.services.region_ref_service import RegionRefService

router = APIRouter()


@router.get("", summary="Get Regions by Vendor")
async def get_regions_by_vendor(
    vendor: InfraVendorEnum = Query(..., description="Infrastructure vendor"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all available regions for a specific infrastructure vendor.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    This endpoint is used for populating the cascading region dropdown
    in the service creation/edit forms.

    **Parameters:**
    - `vendor`: Infrastructure vendor (aws, azure, gcp, or on_prem)

    **Response:**
    ```json
    {
        "vendor": "aws",
        "supports_custom": false,
        "regions": [
            {
                "id": 1,
                "code": "aws-us-east-1",
                "name": "AWS US East (N. Virginia)",
                "description": "Primary AWS region in Virginia",
                "infra_vendor_enum": "aws",
                "region_identifier": "us-east-1",
                "display_order": 1,
                "is_active": true,
                "created_at": "2025-02-01T00:00:00Z"
            }
        ],
        "total": 15
    }
    ```

    **Use Case:**
    - User selects "AWS" as vendor in service form
    - Frontend calls this endpoint with `vendor=aws`
    - Response populates the region dropdown
    - For on_prem, `supports_custom=true` indicates custom input is allowed

    **Returns:**
    - `vendor`: The requested vendor
    - `supports_custom`: Whether vendor allows custom region input (true for on_prem)
    - `regions`: List of available regions ordered by display_order
    - `total`: Total number of regions

    **Raises:**
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET REGIONS BY VENDOR - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print(f"   Vendor: {vendor.value}")
        print("="*80 + "\n")

        service = RegionRefService(db)
        result = await service.get_regions_by_vendor(vendor.value)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
