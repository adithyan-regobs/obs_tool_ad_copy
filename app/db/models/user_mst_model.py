from app.db.models.base_model import BaseModel
from app.core.enum import AuthProviderEnum

from sqlalchemy import Column, String, ForeignKey, BigInteger, Boolean, Enum as SqlEnum
from sqlalchemy.orm import relationship

class UserMstModel(BaseModel):
    """
    User Master table.
    Stores user information within tenants.
    """

    __tablename__ = "user_mst"
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    email_id = Column(String(100), nullable=False)
    auth_provider_id = Column(String(255), unique=True, nullable=True)
    auth_provider = Column(SqlEnum(AuthProviderEnum, name="auth_provider_t"), nullable=True, default=AuthProviderEnum.manual)
    is_org_owner = Column(Boolean, default=False, nullable=False)
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    chat_infos = relationship(
        "ChatInfoModel",
        back_populates="user",
        cascade="all, delete-orphan"
    )

    assigned_tickets = relationship(
        "TicketModel",
        back_populates="assigned_user",
        # No cascade - DB handles it via FK ondelete="CASCADE"
    )

    def __repr__(self):
        return (
            f"<UserMst(id={self.id}, email='{self.email_id}', "
            f"name='{self.first_name} {self.last_name}', tenants_mst_code='{self.tenants_mst_code}')>"
        )


class RoleMst(BaseModel):
    __tablename__ = "role_mst"
    user_mst_id = Column(BigInteger, ForeignKey("user_mst.id"), nullable=False)

    # Link to role type reference (e.g., 'be', 'fe', 'admin')
    role_type_ref_code = Column(
        String(100),
        ForeignKey("role_type_ref.code", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Role type (links to role_type_ref for permission lookups)"
    )

    # Relationship to role type
    role_type = relationship("RoleTypeRefModel", foreign_keys=[role_type_ref_code])

    def __repr__(self):
        return (
            f"<RoleMst(id={self.id}, code='{self.code}', "
            f"name='{self.name}', role_type='{self.role_type_ref_code}', user_id={self.user_mst_id})>"
        )


class TeamMst(BaseModel):
    __tablename__ = "team_mst"
    user_mst_id = Column(BigInteger, ForeignKey("user_mst.id"), nullable=False)

    def __repr__(self):
        return (
            f"<TeamMst(id={self.id}, code='{self.code}', "
            f"name='{self.name}', user_id={self.user_mst_id})>"
        )

# class UserRoleMap(BaseModel):
#     __tablename__ = "role_mst"
#     user_mst_id = Column(BigInteger, ForeignKey("user_mst.id"), nullable=False)

# class TeamRoleMap(BaseModel):
#     __tablename__ = "role_mst"
#     user_mst_id = Column(BigInteger, ForeignKey("user_mst.id"), nullable=False)
