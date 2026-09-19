from sqlalchemy import (
    Column,
    String,
    ForeignKey,
    CheckConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class ResourceGroupMstModel(BaseModel):
    """
    Resource Group Master table.
    Stores resource group information linked to an application.
    """

    __tablename__ = "resource_group_mst"

    # Resource Group details
    kind = Column(String(100), nullable=False)

    # Foreign Key to ApplicationsMst
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Relationships
    application = relationship(
        "ApplicationsMstModel",
        back_populates="resource_groups",
        foreign_keys=[applications_mst_code],
    )

    services = relationship(
        "ServicesMstModel",
        back_populates="resource_group"
    )

    sidecar_configs = relationship(
        "SidecarConfigModel",
        back_populates="resource_group",
        cascade="all, delete-orphan"
    )

    chat_infos = relationship(
        "ChatInfoModel",
        back_populates="resource_group",
        cascade="all, delete-orphan"
    )

    # Table constraints
    __table_args__ = (
        CheckConstraint("kind IN ('service', 'infra')", name="check_kind_valid"),
        UniqueConstraint("applications_mst_code", "name", name="uq_app_group_appcode_name"),
    )

    def __repr__(self):
        return (
            f"<ResourceGroupMst(id={self.id}, name='{self.name}', kind='{self.kind}', application_code='{self.applications_mst_code}', "
            f"code='{self.code}'>"
        )
