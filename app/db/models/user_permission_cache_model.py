"""
User Permission Cache Model

Stores pre-computed/expanded permissions for fast lookups.
Custom table (not using BaseModel) - only essential fields.

Cache Format (JSONB):
[
    {"service_mst_code": "transfer_service", "policy_ref_code": "service_admin"},
    {"service_mst_code": "payment_service", "policy_ref_code": "service_manager"}
]
"""

from sqlalchemy import Column, String, BigInteger, ForeignKey, Enum as SqlEnum, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import relationship
from sqlalchemy import text
from app.db.models.base_model import Base
from app.core.enum import EnvironmentEnum


class UserPermissionCacheModel(Base):
    """
    Cache table for fast permission lookups.

    One row per user + tenant + environment.
    Permissions stored as JSONB array.
    """

    __tablename__ = "user_permission_cache"

    # Primary key
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Which user this cache belongs to
    user_mst_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User this cache belongs to"
    )

    # Tenant isolation
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant for data isolation"
    )

    # Environment (nullable = all environments)
    environment = Column(
        SqlEnum(EnvironmentEnum, name="environment_enum", create_type=False),
        nullable=True,
        index=True,
        comment="Environment scope (NULL = all environments)"
    )

    # JSONB array of permissions
    permissions = Column(
        JSONB,
        nullable=False,
        default=[],
        comment="Array of {service_mst_code, policy_ref_code}"
    )

    # Timestamps
    created_at = Column(
        TIMESTAMP(timezone=True),
        server_default=text('now()'),
        nullable=True
    )

    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True
    )

    # Unique constraint: one cache row per user + tenant + environment
    __table_args__ = (
        UniqueConstraint(
            'user_mst_code', 'tenants_mst_code', 'environment',
            name='uq_permission_cache_user_tenant_env'
        ),
    )

    # Relationships
    user = relationship("UserMstModel", foreign_keys=[user_mst_code])
    tenant = relationship("TenantsMstModel", foreign_keys=[tenants_mst_code])

    def __repr__(self) -> str:
        env = self.environment or "all"
        count = len(self.permissions) if self.permissions else 0
        return f"<UserPermissionCache(user='{self.user_mst_code}', env='{env}', count={count})>"
