from sqlalchemy import (
    Column,
    Boolean,
    ForeignKey,
    Enum as SqlEnum,
    String
)
from sqlalchemy.dialects.postgresql import JSONB
from app.db.models.base_model import BaseModel
from app.core.enum import ObsVendorEnum


class ObsVendorAccountsMstModel(BaseModel):
    """
    Observability Vendor Accounts Master table.
    Stores vendor-specific account configurations (e.g., Datadog, Grafana, Loki, CloudWatch).

    Business Rule: Every vendor account MUST be associated with a tenant (tenant is mandatory).
    Hierarchical scoping: Service > Application > Tenant (tenant is the fallback level).
    """

    __tablename__ = "obs_vendor_accounts_mst"

    # Vendor info
    obs_vendor_enum = Column(
        SqlEnum(ObsVendorEnum, name="obs_vendor_t"),
        nullable=False,
        default=ObsVendorEnum.datadog,
    )
    # Scope references - tenant is MANDATORY (business rule)
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
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

    services_mst_code = Column(String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=True,
    )

    # Capability flags
    has_metrics = Column(Boolean, nullable=False, default=True)
    has_logs = Column(Boolean, nullable=False, default=True)
    has_traces = Column(Boolean, nullable=False, default=False)

    # Vendor authentication/config
    auth_config = Column(JSONB, nullable=False, default=dict)

    def __repr__(self):
        return (
            f"<ObsVendorAccountsMst(id={self.id}, vendor='{self.obs_vendor_enum}', "
            f"name='{self.name}', metrics={self.has_metrics}, logs={self.has_logs}, "
            f"traces={self.has_traces})>"
        )
