"""
Geographic Location Master Schemas
Pydantic schemas for geo_loc_mst API endpoints
"""
from pydantic import BaseModel
from typing import List


class GeoLocMstResponse(BaseModel):
    """Single geographic location response"""
    code: str
    name: str

    class Config:
        from_attributes = True


class GeoLocMstListResponse(BaseModel):
    """List of geographic locations response"""
    total: int
    geo_locs: List[GeoLocMstResponse]
