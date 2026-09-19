from sqlalchemy import (
    Column,
    ForeignKey,
    String,
    Enum as SqlEnum,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum


class SidecarConfigModel(BaseModel):
    """
    Sidecar Configuration Model
    Stores default sidecar container configurations scoped to application, resource group, and environment.

    These are reusable sidecar definitions (e.g., Datadog Agent, Nutific) with default CPU/RAM values
    that services can reference and override in their service_configs.
    """

    __tablename__ = "sidecar_configs"

    # Foreign Keys - All REQUIRED for scoping
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Application this sidecar belongs to"
    )
    resource_group_mst_code = Column(
        String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Resource group this sidecar belongs to"
    )
    environment = Column(
        SqlEnum(EnvironmentEnum, name="environment_enum"),
        nullable=False,
        comment="Environment (dev/staging/prod)"
    )

    # Configuration (default CPU/RAM values)
    config = Column(
        JSONB,
        nullable=False,
        comment="Default configuration JSON: {cpu: '250', ram: '250'}"
    )

    # Table constraints
    __table_args__ = (
        UniqueConstraint(
            "applications_mst_code",
            "resource_group_mst_code",
            "environment",
            "name",
            name="uq_sidecar_config_app_rg_env_name"
        ),
        CheckConstraint(
            "config ? 'cpu' AND config ? 'ram'",
            name="check_sidecar_config_has_cpu_ram"
        ),
    )

    # Relationships
    application = relationship(
        "ApplicationsMstModel",
        back_populates="sidecar_configs",
        foreign_keys=[applications_mst_code]
    )
    resource_group = relationship(
        "ResourceGroupMstModel",
        back_populates="sidecar_configs",
        foreign_keys=[resource_group_mst_code]
    )

    def __repr__(self):
        return (
            f"<SidecarConfig(id={self.id}, code='{self.code}', "
            f"name='{self.name}', app='{self.applications_mst_code}', "
            f"rg='{self.resource_group_mst_code}', env='{self.environment}')>"
        )
