from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum
from sqlalchemy.orm import relationship

from app.db.models.base_model import BaseModel
from app.core.enum import WorkspaceStatusEnum


class WorkspaceMstModel(BaseModel):
    __tablename__ = "workspace_mst"

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    status = Column(
        SqlEnum(WorkspaceStatusEnum, name="workspace_status_enum"),
        nullable=False,
        default=WorkspaceStatusEnum.active,
    )

    # Relationships
    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenants_mst_code],
    )

    user_maps = relationship(
        "WorkspaceUserMapModel",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )

    applications = relationship(
        "ApplicationsMstModel",
        back_populates="workspace",
    )

    def __repr__(self):
        return (
            f"<WorkspaceMst(id={self.id}, code='{self.code}', "
            f"name='{self.name}', tenants_mst_code='{self.tenants_mst_code}')>"
        )
