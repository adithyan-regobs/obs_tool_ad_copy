from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class ApplicationsMstModel(BaseModel):
    """
    Applications Master table.
    Stores application information within tenants.
    """

    __tablename__ = "applications_mst"

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    workspace_code = Column(
        String(100),
        ForeignKey("workspace_mst.code", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    tenant = relationship(
        "TenantsMstModel",
        back_populates="applications",
        foreign_keys=[tenants_mst_code],
    )

    workspace = relationship(
        "WorkspaceMstModel",
        back_populates="applications",
        foreign_keys=[workspace_code],
    )

    services = relationship(
        "ServicesMstModel",
        back_populates="application",
        cascade="all, delete-orphan"
    )

    resource_groups = relationship(
        "ResourceGroupMstModel",
        back_populates="application",
        cascade="all, delete-orphan"
    )

    sidecar_configs = relationship(
        "SidecarConfigModel",
        back_populates="application",
        cascade="all, delete-orphan"
    )

    service_dependencies = relationship(
        "ServiceDependencyMapModel",
        back_populates="application"
    )

    chat_infos = relationship(
        "ChatInfoModel",
        back_populates="application",
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return (
            f"<ApplicationsMst(id={self.id}, tenants_mst_code='{self.tenants_mst_code}', "
            f"application_code='{self.code}', "
            f"application_name='{self.name}')>"
        )
