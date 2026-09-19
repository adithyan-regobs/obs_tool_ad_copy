"""
Factory for Region Reference response transformations
"""
from typing import List, Dict, Any
from app.db.models.region_ref_model import RegionRefModel
from app.core.enum import InfraVendorEnum


def make_region_response(region: RegionRefModel) -> Dict[str, Any]:
    """
    Factory to transform RegionRefModel to API response format.

    Args:
        region: RegionRefModel instance from database

    Returns:
        Dictionary containing region data for API response

    This isolates response transformation logic from the service layer.
    """
    return {
        "id": region.id,
        "code": region.code,
        "name": region.name,
        "description": region.description,
        "infra_vendor_enum": region.infra_vendor_enum,
        "region_identifier": region.region_identifier,
        "display_order": region.display_order,
        "is_active": region.is_active,
        "created_at": region.created_at.isoformat() if region.created_at else None,
    }


def make_regions_list_response(
    vendor: str,
    regions: List[RegionRefModel]
) -> Dict[str, Any]:
    """
    Factory to build regions list response for cascading dropdown.

    Args:
        vendor: Infrastructure vendor ('aws', 'azure', 'gcp', 'on_prem')
        regions: List of RegionRefModel instances from database

    Returns:
        Dictionary containing:
            - vendor: The requested vendor
            - supports_custom: Whether custom region input is allowed (true for on_prem)
            - regions: List of region dictionaries
            - total: Total count of regions

    This isolates response construction logic from the service layer.
    """
    # Transform each region model to response format
    region_list = [make_region_response(region) for region in regions]

    return {
        "vendor": vendor,
        "supports_custom": vendor == InfraVendorEnum.on_prem.value,
        "regions": region_list,
        "total": len(region_list)
    }
