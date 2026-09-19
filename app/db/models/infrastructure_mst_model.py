from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum, BigInteger, TIMESTAMP
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum, DeploymentStatusEnum, ResourceStatusEnum, ResourceDeploymentStatusEnum

class InfrastructureMstModel(BaseModel):
    # Infra type
    __tablename__ = "infrastructure_mst"

    infrastructuretype_ref_code = Column(
        String(100),
        ForeignKey("infrastructuretype_ref.code", ondelete="CASCADE"),
        nullable=False,
    )

    infra_vendor_accounts_mst_code = Column(
        String(100),
        ForeignKey("infra_vendor_accounts_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    resource_group_mst_code = Column(
        String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=True,  # Optional - infrastructure can exist without a resource group
    )

    geo_loc_mst_code = Column(
        String(100),
        ForeignKey("geo_loc_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Geographic location this infrastructure belongs to"
    )

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    environments_enum = Column(
        SqlEnum(EnvironmentEnum, name="enviornment_enum"),
        nullable=False,
    )

    # canonical identity for this resource kind (keys depend on the kind)
    # examples:
    #   EC2: {"instance_id":"i-0abc123","region":"ap-south-1"}
    #   RDS: {"db_instance_id":"orders-db","region":"ap-south-1"}
    #   EKS Pod: {"cluster":"eks-a","namespace":"payments","pod":"pay-7f4c","container":"svc"}
    #   MSK: {"cluster_name":"msk-1","region":"ap-south-1"}
    locator = Column(
        JSONB,
        nullable=True,
    )

    # UI-ready deployment status (displayed on canvas node badge)
    status = Column(
        SqlEnum(ResourceStatusEnum, name="resource_status_enum", create_type=False),
        nullable=False,
        default=ResourceStatusEnum.DRAFT,
        comment="UI-ready deployment status"
    )
    status_updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Timestamp of last status update"
    )

    # Temporal GitOps workflow deployment status (granular pipeline states)
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
        String(1000),
        nullable=True,
        comment="Last deployment error message for display in UI"
    )
    iac_locked_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Set when an IaC state lock (Atlantis/OpenTofu) blocks deployment; NULL when unlocked"
    )

    # DEPLOYMENT WORKFLOW TRACKING (GitOps) — detailed workflow state
    infra_status = Column(
        SqlEnum(DeploymentStatusEnum, name="deployment_status_enum"),
        nullable=True,
        default=DeploymentStatusEnum.INITIATED,
        comment="Deployment workflow status for infrastructure resource"
    )
    infra_status_updated_by = Column(
        String(255),
        nullable=True,
        comment="Email or GitHub username of user/system that updated status"
    )
    infra_status_updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Timestamp of last status update"
    )
    gitops_workflow_id = Column(
        BigInteger,
        ForeignKey("gitops_workflow_detail.id", ondelete="SET NULL"),
        nullable=True,
        comment="Foreign key to gitops_workflow_detail (MANY infrastructure → ONE workflow)"
    )
    resource_identifier = Column(
        String(500),
        nullable=True,
        comment="AWS ARN or resource identifier (populated after vendor creation)"
    )
    variable_ids = Column(
        ARRAY(BigInteger),
        nullable=True,
        default=[],
        comment="List of variable_mst.id values associated with this infrastructure resource"
    )

    # RELATIONSHIPS
    # Relationship to InfraVendorAccountsMstModel
    infra_vendor_account = relationship(
        "InfraVendorAccountsMstModel",
        foreign_keys=[infra_vendor_accounts_mst_code],
    )

    # Relationship to ServiceDependencyMapModel
    dependents = relationship(
        "ServiceDependencyMapModel",
        back_populates="infrastructure"
    )

    # Relationship to GitOps Workflow
    gitops_workflow = relationship(
        "GitopsWorkflowDetailModel",
        back_populates="infrastructure_records",
        foreign_keys=[gitops_workflow_id]
    )

    # Relationship to Geographic Location
    geo_loc = relationship(
        "GeoLocMstModel",
        foreign_keys=[geo_loc_mst_code]
    )

    def __repr__(self):
        return (
            f"<InfrastructureMst(id={self.id}, code='{self.code}', "
            f"name='{self.name}', type='{self.infrastructuretype_ref_code}', "
            f"env='{self.environments_enum.value}')>"
        )