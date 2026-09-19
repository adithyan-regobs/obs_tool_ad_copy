"""
Service User Permission Model

Maps WHO (user or role type) has WHAT (policy) on WHICH resource (service/RG/app)
in WHAT environment.

Scope Hierarchy:
- Service level: services_mst_code is set, RG and App are NULL
- RG level: resource_group_mst_code is set, Service and App are NULL
- App level: applications_mst_code is set, Service and RG are NULL

Assignment Types:
- User-based: user_mst_code is set, role_type_ref_code is NULL
- Role-based: role_type_ref_code is set, user_mst_code is NULL (applies to ALL users with this role type)
"""

from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum, CheckConstraint
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum


class ServiceUserPermissionModel(BaseModel):
    """
    Service User Permission table.

    Links users/roles to policies on specific resources with environment scope.
    """

    __tablename__ = "service_user_permission"

    # ========== TENANT ISOLATION ==========
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant for data isolation"
    )

    # ========== WHO: User OR Role (mutually exclusive) ==========
    user_mst_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Direct user assignment (NULL if role-based)"
    )

    role_type_ref_code = Column(
        String(100),
        ForeignKey("role_type_ref.code", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Role type assignment (NULL if user-based). Applies to ALL users with this role type."
    )

    # ========== WHAT: Policy ==========
    policy_ref_code = Column(
        String(100),
        ForeignKey("policy_ref.code", ondelete="RESTRICT"),
        nullable=False,
        comment="Policy type (service_admin, service_manager, service_support)"
    )

    # ========== WHERE: Scope (only ONE should be set) ==========
    services_mst_code = Column(
        String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Service-level scope (NULL for RG or App level)"
    )

    resource_group_mst_code = Column(
        String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="RG-level scope (NULL for Service or App level)"
    )

    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="App-level scope (NULL for Service or RG level)"
    )

    # ========== ENVIRONMENT ==========
    environment = Column(
        SqlEnum(EnvironmentEnum, name="environment_enum", create_type=False),
        nullable=True,
        comment="Environment scope (NULL = all environments)"
    )

    # ========== CONSTRAINTS ==========
    __table_args__ = (
        # User OR Role Type - exactly one must be set
        CheckConstraint(
            "(user_mst_code IS NOT NULL AND role_type_ref_code IS NULL) OR "
            "(user_mst_code IS NULL AND role_type_ref_code IS NOT NULL)",
            name="chk_user_or_role_type_exclusive"
        ),
        # At least one scope must be set
        CheckConstraint(
            "services_mst_code IS NOT NULL OR "
            "resource_group_mst_code IS NOT NULL OR "
            "applications_mst_code IS NOT NULL",
            name="chk_at_least_one_scope"
        ),
    )

    # ========== RELATIONSHIPS ==========
    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenants_mst_code],
    )

    user = relationship(
        "UserMstModel",
        foreign_keys=[user_mst_code],
    )

    role_type = relationship(
        "RoleTypeRefModel",
        foreign_keys=[role_type_ref_code],
    )

    policy = relationship(
        "PolicyRefModel",
        foreign_keys=[policy_ref_code],
    )

    service = relationship(
        "ServicesMstModel",
        foreign_keys=[services_mst_code],
    )

    resource_group = relationship(
        "ResourceGroupMstModel",
        foreign_keys=[resource_group_mst_code],
    )

    application = relationship(
        "ApplicationsMstModel",
        foreign_keys=[applications_mst_code],
    )

    def __repr__(self) -> str:
        """String representation for debugging."""
        who = self.user_mst_code or f"role_type:{self.role_type_ref_code}"
        where = self.services_mst_code or self.resource_group_mst_code or self.applications_mst_code
        return f"<ServiceUserPermission({who} -> {self.policy_ref_code} on {where})>"
