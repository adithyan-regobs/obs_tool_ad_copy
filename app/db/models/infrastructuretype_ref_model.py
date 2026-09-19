from sqlalchemy import Column, Boolean, Enum
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import InfraFamilyEnum, InfraVendorEnum

class InfrastructureTypeRefModel(BaseModel):
    __tablename__ = "infrastructuretype_ref"

    infra_vendor = Column(
        Enum(InfraVendorEnum, name="infra_vendor_enum"),
        nullable=False
    )

    infra_family = Column (
        Enum(InfraFamilyEnum, name="infra_family_enum"),
        nullable=False
    )

    # -- capability flags (for UI & validation)
    has_log = Column(Boolean, default=True, nullable=False)
    has_metrics = Column(Boolean, default=True, nullable=False)
    has_traces = Column(Boolean, default=False, nullable=False)

    # Relationship to ServiceConfigModel
    service_configs = relationship(
        "ServiceConfigModel",
        back_populates="infrastructure_type",
    )

    def __repr__(self):
        return (
            f"<InfrastructureTypeRef(id={self.id}, code='{self.code}', "
            f"name='{self.name}', provider='{self.infra_provider.value}', "
            f"family='{self.infra_family.value}')>"
        )
