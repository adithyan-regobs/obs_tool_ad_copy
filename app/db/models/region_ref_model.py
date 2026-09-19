from sqlalchemy import Column, String, Integer, Boolean, CheckConstraint, UniqueConstraint
from app.db.models.base_model import BaseModel


class RegionRefModel(BaseModel):
    """
    Region Reference Table

    Provides predefined region data for cloud vendors (AWS, Azure, GCP) and
    optional on-prem datacenter regions. Used for:
    1. FK validation for services_mst.region_ref_code
    2. Populating cascading dropdown in UI
    3. Ensuring vendor-region coupling is valid

    NOT a source of truth for service regions - services_mst stores the actual
    region data (either via FK or custom string).
    """

    __tablename__ = "region_ref"

    # Vendor-region coupling
    infra_vendor_enum = Column(
        String(50),
        nullable=False,
        comment="Infrastructure vendor: aws, azure, gcp, or on_prem"
    )

    region_identifier = Column(
        String(100),
        nullable=False,
        comment="Actual region code used by vendor (e.g., us-east-1, eastus, us-central1)"
    )

    # UI helpers
    display_order = Column(
        Integer,
        default=0,
        nullable=False,
        comment="Sort order for UI dropdowns (lower numbers appear first)"
    )

    # Table constraints
    __table_args__ = (
        UniqueConstraint(
            'infra_vendor_enum',
            'region_identifier',
            name='uq_region_ref_vendor_region'
        ),
        CheckConstraint(
            "infra_vendor_enum IN ('aws', 'azure', 'gcp', 'on_prem')",
            name='chk_region_ref_vendor'
        ),
    )

    def __repr__(self):
        return (
            f"<RegionRef(id={self.id}, code='{self.code}', "
            f"vendor='{self.infra_vendor_enum}', region='{self.region_identifier}')>"
        )
