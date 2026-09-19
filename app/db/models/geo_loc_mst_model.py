"""
Geographic Location Master Model

Business/Deployment regions (not AWS regions).
Tenant-specific regions used in Deployment tab's Region dropdown.
"""
from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship

from app.db.models.base_model import BaseModel


class GeoLocMstModel(BaseModel):
    """Geographic Location Master - Business/Deployment regions (not AWS regions)"""

    __tablename__ = "geo_loc_mst"

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Relationships
    tenant = relationship("TenantsMstModel", foreign_keys=[tenants_mst_code])

    def __repr__(self):
        return f"<GeoLocMst(code='{self.code}', name='{self.name}')>"
