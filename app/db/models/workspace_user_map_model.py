from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.models.base_model import BaseModel
from app.core.enum import WorkspaceRoleEnum


class WorkspaceUserMapModel(BaseModel):
    __tablename__ = "workspace_user_mapping"

    workspace_code = Column(
        String(100),
        ForeignKey("workspace_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    user_mst_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    role = Column(
        SqlEnum(WorkspaceRoleEnum, name="workspace_role_enum"),
        nullable=False,
        default=WorkspaceRoleEnum.read_only,
    )

    __table_args__ = (
        UniqueConstraint("workspace_code", "user_mst_code", name="uq_workspace_user_mapping"),
    )

    # Relationships
    workspace = relationship(
        "WorkspaceMstModel",
        back_populates="user_maps",
        foreign_keys=[workspace_code],
    )

    user = relationship(
        "UserMstModel",
        foreign_keys=[user_mst_code],
    )

    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenants_mst_code],
    )

    def __repr__(self):
        return (
            f"<WorkspaceUserMap(workspace='{self.workspace_code}', "
            f"user='{self.user_mst_code}', role='{self.role}')>"
        )
