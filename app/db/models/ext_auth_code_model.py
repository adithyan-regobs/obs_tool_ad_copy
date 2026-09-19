"""
Database model for external client auth codes
"""
from sqlalchemy import Column, String, BigInteger, DateTime, Boolean, Text, ForeignKey, Index, func
from sqlalchemy.orm import relationship
from app.db.models.base_model import Base


class ExtAuthCodeModel(Base):
    """
    Stores short-lived, single-use auth codes for external client login (VSCode, MCP, etc.).

    Each code maps to a validated Clerk JWT and user/tenant context.
    """

    __tablename__ = "ext_auth_code"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Auth code fields
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    code_prefix = Column(String(16), nullable=False, index=True)
    client_id = Column(String(255), nullable=False, index=True)

    # User/Tenant context
    user_mst_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Token payload
    clerk_jwt = Column(Text, nullable=False)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Optional state for CSRF protection
    state = Column(String(512), nullable=True)

    # Lifecycle fields
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_deleted = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    # Relationships
    user = relationship("UserMstModel", foreign_keys=[user_mst_code])
    tenant = relationship("TenantsMstModel", foreign_keys=[tenants_mst_code])

    __table_args__ = (
        Index("idx_ext_auth_code_client_expires", "client_id", "expires_at"),
        Index("idx_ext_auth_code_user_tenant", "user_mst_code", "tenants_mst_code"),
    )

    def __repr__(self):
        return (
            f"<ExtAuthCode(id={self.id}, client_id='{self.client_id}', "
            f"user='{self.user_mst_code}', expires_at='{self.expires_at}', used_at='{self.used_at}')>"
        )
