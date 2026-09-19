from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    String,
    Text,
    TIMESTAMP,
    Enum as SqlEnum,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum, InfraVendorEnum, LogProviderEnum, ResourceStatusEnum, ResourceDeploymentStatusEnum


class ServiceConfigModel(BaseModel):
    """
    Service Configuration Model
    Stores service-specific configuration for each environment (dev/staging/prod).

    Contains:
    - Resource allocation (CPU, RAM, Port)
    - Health check configuration
    - Scaling configuration (min task count, max task count, desired count)
    - EBS storage configuration
    - Language reference (language_ref_code FK - resolve name/version from language_ref table)
    - Sidecar container overrides (references sidecar_configs by code)
    """

    __tablename__ = "service_configs"

    # Foreign Keys
    tenant_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this configuration belongs to (from JWT)"
    )
    services_mst_code = Column(
        String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Service this configuration belongs to"
    )
    infrastructuretype_ref_code = Column(
        String(100),
        ForeignKey("infrastructuretype_ref.code"),
        nullable=False,
        comment="Infrastructure type (ECS, Lambda, EC2, etc.)"
    )
    infra_vendor_enum = Column(
        SqlEnum(InfraVendorEnum, name='infra_vendor_enum'),
        nullable=False,
        default=InfraVendorEnum.aws,
        comment="Infrastructure vendor (AWS, GCP, Azure, On-Prem)"
    )
    environment = Column(
        SqlEnum(EnvironmentEnum, name="environment_enum"),
        nullable=False,
        comment="Environment (dev/staging/prod)"
    )
    geo_loc_mst_code = Column(
        String(100),
        ForeignKey("geo_loc_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Business region this configuration belongs to"
    )
    alb_selection = Column(
        String(50),
        nullable=False,
        default="existing_alb",
        comment="ALB selection type: no_alb, existing_alb, create_new_alb"
    )
    language_ref_code = Column(
        String(100),
        ForeignKey("language_ref.code"),
        nullable=True,
        comment="Language reference FK - for relationship to language_ref table"
    )
    infrastructure_mst_code = Column(
        String(100),
        ForeignKey("infrastructure_mst.code", ondelete="SET NULL"),
        nullable=True,
        comment="Infrastructure instance (cluster) this config is deployed to"
    )
    gitops_workflow_id = Column(
        BigInteger,
        ForeignKey("gitops_workflow_detail.id", ondelete="SET NULL"),
        nullable=True,
        comment="Foreign key to gitops_workflow_detail for Terragrunt PR tracking"
    )
    dockerfile_gitops_workflow_id = Column(
        BigInteger,
        ForeignKey("gitops_workflow_detail.id", ondelete="SET NULL"),
        nullable=True,
        comment="Foreign key to gitops_workflow_detail for Dockerfile PR tracking"
    )
    sync_status = Column(
        String(20),
        nullable=False,
        default="NEVER_SYNCED",
        comment="Sync status: NEVER_SYNCED, PENDING_SYNC, SYNCED"
    )
    status = Column(
        SqlEnum(ResourceStatusEnum, name="resource_status_enum", create_type=True),
        nullable=False,
        default=ResourceStatusEnum.DRAFT,
        comment="UI-ready deployment status"
    )
    status_updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Timestamp of last status update"
    )
    deployment_status = Column(
        SqlEnum(ResourceDeploymentStatusEnum, name="resource_deployment_status_enum", create_type=False, values_callable=lambda obj: [e.value for e in obj]),
        nullable=True,
        comment="Temporal GitOps deployment workflow state"
    )
    deployment_status_updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    deployment_error_message = Column(
        Text,
        nullable=True,
        comment="Last deployment error message for display in UI"
    )
    iac_locked_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Set when an IaC state lock (Atlantis/OpenTofu) blocks deployment; NULL when unlocked"
    )

    # Configuration fields (JSONB)
    config = Column(
        JSONB,
        nullable=True,
        comment="Main service configuration: {cpu, ram, port, health, min_task_count, max_task_count, desired_count, ebs: {volume, size, type}}"
    )
    sidecar_config = Column(
        JSONB,
        nullable=True,
        default=[],
        comment="Sidecar overrides: [{sidecar_config_code, cpu, ram}] - name comes from JOIN with sidecar_configs table"
    )
    log_provider = Column(
        SqlEnum(LogProviderEnum, name="log_provider_enum", create_type=False),
        nullable=True,
        default=None,
        comment="Override log provider for this service. NULL = auto-detect (Datadog if sidecar enabled, else tenant default)"
    )
    deployment_strategy = Column(
        JSONB,
        nullable=True,
        comment="Deployment strategy configuration: {strategy: 'rolling'|'canary'|'bluegreen'|'recreate', rolling: {...}, canary: {...}, blueGreen: {...}}"
    )
    dockerfile = Column(
        Text,
        nullable=True,
        comment="Dockerfile content for EKS deployments when generate_dockerfile is enabled"
    )
    variable_ids = Column(
        ARRAY(BigInteger),
        nullable=True,
        default=[],
        comment="List of variable_mst.id values associated with this service config"
    )

    # Table constraints
    __table_args__ = (
        UniqueConstraint(
            "tenant_mst_code",
            "services_mst_code",
            "environment",
            "geo_loc_mst_code",
            "alb_selection",
            "infrastructuretype_ref_code",
            "infrastructure_mst_code",
            "infra_vendor_enum",
            name="uq_service_config_tenant_service_env_geo_loc_alb_infra"
        ),
    )

    # Relationships
    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenant_mst_code]
    )
    service = relationship(
        "ServicesMstModel",
        back_populates="service_configs",
        foreign_keys=[services_mst_code]
    )
    infrastructure_type = relationship(
        "InfrastructureTypeRefModel",
        back_populates="service_configs",
        foreign_keys=[infrastructuretype_ref_code]
    )
    language_ref = relationship(
        "LanguageRefModel",
        back_populates="service_configs",
        foreign_keys=[language_ref_code]
    )
    geo_loc = relationship(
        "GeoLocMstModel",
        foreign_keys=[geo_loc_mst_code]
    )
    infrastructure = relationship(
        "InfrastructureMstModel",
        foreign_keys=[infrastructure_mst_code]
    )
    gitops_workflow = relationship(
        "GitopsWorkflowDetailModel",
        back_populates="service_configs",
        foreign_keys=[gitops_workflow_id]
    )
    dockerfile_gitops_workflow = relationship(
        "GitopsWorkflowDetailModel",
        foreign_keys=[dockerfile_gitops_workflow_id]
    )

    # Junction table relationship for multiple Dockerfile PRs (one per branch per repo)
    dockerfile_workflow_links = relationship(
        "ServiceConfigDockerfileWorkflowModel",
        back_populates="service_config",
        cascade="all, delete-orphan",
        lazy="selectin"
    )

    def __repr__(self):
        return (
            f"<ServiceConfig(id={self.id}, code='{self.code}', "
            f"service='{self.services_mst_code}', env='{self.environment}', "
            f"language='{self.language_ref_code}')>"
        )
