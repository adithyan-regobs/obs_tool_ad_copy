from sqlalchemy import (
    Column,
    ForeignKey,
    String,
)
from app.db.models.base_model import BaseModel, AlertBaseConfig


class MonitoringPolicyOverridesMstModel(BaseModel, AlertBaseConfig):
    """
    Monitoring Policy Overrides Master Table

    Purpose:
    --------
    Allows hierarchical customization of default monitoring policies at different scoping levels.
    Tenant admins, application owners, or resource group owners can override default thresholds,
    evaluation windows, severity levels, etc. to match their specific business requirements.

    Override Hierarchy (Most Specific → Least Specific):
    ---------------------------------------------------
    1. Resource Group + Infrastructure Type + Tenant (MOST SPECIFIC)
    2. Resource Group + Tenant
    3. Application + Infrastructure Type + Tenant
    4. Application + Tenant
    5. Infrastructure Type + Tenant
    6. Tenant only
    7. Infrastructure Type only (global cross-tenant override)
    8. Default (monitoring_policy_defaults_ref) - fallback

    Specificity Rules:
    ------------------
    When multiple overrides exist, the most specific one wins based on scoring:
    - Resource Group scope = 8 points
    - Infrastructure Type filter = 4 points
    - Application scope = 2 points
    - Tenant scope = 1 point

    Higher total score = more specific = takes precedence


    Real-World Examples:
    --------------------

    Example 1: Tenant-Wide Stricter CPU Policy
    -------------------------------------------
    Scenario:
        Tenant "acme_corp" wants stricter CPU monitoring across all their infrastructure
        because they run high-availability services that can't tolerate CPU spikes.

    Override Entry:
        code = "acme_cpu_override_01"
        name = "Acme Corp Strict CPU Policy"
        monitoring_policy_defaults_ref_code = "ec2_cpu_default"
        tenants_mst_code = "acme_corp"
        applications_mst_code = NULL
        resource_group_mst_code = NULL
        infrastructuretype_ref_code = NULL
        threshold_value = 70  (default is 80)
        severity = P1  (default is P2)
        eval_window = 5

    Result:
        All EC2 instances under "acme_corp" tenant now trigger CPU alerts at 70% instead of 80%,
        with P1 severity instead of P2. This applies across ALL their applications and resource groups.

    """

    __tablename__ = "monitoring_policy_overrides_mst"

    # Reference to the base/default policy being overridden
    monitoring_policy_defaults_ref_code = Column(
        String(100),
        ForeignKey("monitoring_policy_defaults_ref.code", ondelete="CASCADE"),
        nullable=True,
        comment="Base policy being overridden (e.g., 'ec2_cpu_default', 'rds_memory_default'). NULL = standalone override without default policy."
    )

    # Alert type - denormalized for easier querying without JOINs
    alerttype_ref_code = Column(
        String(100),
        ForeignKey("alerttype_ref.code", ondelete="CASCADE"),
        nullable=False,
        comment="Alert type (e.g., 'cpu_util', 'memory_usage') - denormalized from default policy for performance"
    )

    # Scoping Foreign Keys - All nullable to allow partial/hierarchical scoping
    # The combination of non-null FKs determines override specificity

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Tenant scope - if set, override applies to this tenant only. NULL = cross-tenant (global) override"
    )

    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Application scope - if set, override applies to this application. Typically used with tenants_mst_code"
    )

    resource_group_mst_code = Column(
        String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Resource group scope - MOST SPECIFIC service grouping level. Typically used with applications_mst_code and tenants_mst_code"
    )

    infrastructuretype_ref_code = Column(
        String(100),
        ForeignKey("infrastructuretype_ref.code", ondelete="CASCADE"),
        nullable=True,
        comment="Infrastructure type filter (ec2, rds, vm, eks, etc.) - if set, override only applies to this infrastructure type"
    )

    def __repr__(self):
        scope_parts = []
        if self.resource_group_mst_code:
            scope_parts.append(f"resource_group={self.resource_group_mst_code}")
        if self.applications_mst_code:
            scope_parts.append(f"app={self.applications_mst_code}")
        if self.tenants_mst_code:
            scope_parts.append(f"tenant={self.tenants_mst_code}")
        if self.infrastructuretype_ref_code:
            scope_parts.append(f"infra={self.infrastructuretype_ref_code}")

        scope_str = ", ".join(scope_parts) if scope_parts else "global"

        return (
            f"<MonitoringPolicyOverridesMst(id={self.id}, "
            f"code='{self.code}', "
            f"base_policy='{self.monitoring_policy_defaults_ref_code}', "
            f"scope=[{scope_str}], "
            f"threshold={self.threshold_value})>"
        )
