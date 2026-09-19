from sqlalchemy import Column, String, ForeignKey, JSON, Enum as SqlEnum
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum, InfraVendorEnum

class InfraVendorAccountsMstModel(BaseModel):
    __tablename__ = "infra_vendor_accounts_mst"

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=True,
    )
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=True,
    )
    resource_group_mst_code = Column(String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=True,
    )

    auth_config = Column(JSON, nullable=False, default=dict)

    environments_enum = Column(
        SqlEnum(EnvironmentEnum, name="enviornment_enum"),
        nullable=False,
    )
    infra_vendor_enum = Column(
        SqlEnum(InfraVendorEnum, name="infra_vendor_enum"),
        nullable=False
    )

    def __repr__(self):
        return (
            f"<InfraVendorAccountsMst(id={self.id}, code='{self.code}', "
            f"vendor='{self.infra_vendor_enum.value}', env='{self.environments_enum.value}')>"
        )


class ZoneMast(BaseModel):
    __tablename__ = "zone_mst"
    infra_provider_enum = Column(
        SqlEnum(InfraVendorEnum, name="infra_provider_enum"),
        nullable=False
    )

    def __repr__(self):
        return (
            f"<ZoneMast(id={self.id}, code='{self.code}', "
            f"name='{self.name}', provider='{self.infra_provider_enum.value}')>"
        )