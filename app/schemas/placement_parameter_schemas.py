"""
Placement Parameter Schemas

Nested dropdown payload used by the placement-parameters merged endpoint:
the products, environments and geographic locations that actually have
infrastructure behind them, in a single response.
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class PlacementOptionsRequest(BaseModel):
    """Optional body. `infra_type` narrows the tree to placements holding
    infrastructure of that type, so the EKS form is never offered a product
    or region that has only, say, an S3 bucket. Omit it for any type."""

    infra_type: Optional[str] = Field(
        None,
        description=(
            "infrastructuretype_ref code to narrow placements by, e.g. "
            "'eks_infrastructuretype_ref'. Omit for any infrastructure type."
        ),
    )


class PlacementGeoLocation(BaseModel):
    geo_loc_mst_code: str = Field(..., description="Geographic location code")
    geo_loc_name: str = Field(..., description="Geographic location name")


class PlacementEnvironment(BaseModel):
    environment_enum: str = Field(..., description="Environment enum value (e.g. 'stage')")
    environment_label: str = Field(..., description="Human-readable environment label")
    geo_locations: List[PlacementGeoLocation] = Field(
        ...,
        description="Geographic locations available for this environment",
    )


class PlacementProduct(BaseModel):
    applications_mst_code: str = Field(..., description="Application master code (UUID)")
    product_code: str = Field(..., description="Product code (same as applications_mst_code)")
    product_name: str = Field(..., description="Product / application display name")
    environments: List[PlacementEnvironment] = Field(
        ...,
        description="Environments available for this product",
    )


class PlacementParametersResponse(BaseModel):
    tenant_code: str = Field(..., description="Tenant code from JWT")
    products: List[PlacementProduct] = Field(
        ...,
        description="Products with their environments and geo-locations",
    )
