from sqlalchemy import Column, String, Integer, TIMESTAMP, Enum as SqlEnum, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import PRStatusEnum, WorkflowSourceTableEnum


class GitopsWorkflowDetailModel(BaseModel):
    """
    Model for tracking GitHub PR and workflow execution details.

    This table stores shared workflow information for deployments.
    Multiple resources (alerts/infrastructure) can reference the same workflow.

    Relationship: MANY resources → ONE workflow (via gitops_workflow_id FK)

    Example:
        Bulk deployment of 25 alerts creates:
        - 25 alert_configs records
        - 1 gitops_workflow_detail record (this table)
        - All 25 alerts have gitops_workflow_id pointing to same workflow
    """

    __tablename__ = "gitops_workflow_detail"

    # GIT TRACKING
    git_repository = Column(
        String(200),
        nullable=True,
        comment="GitHub repository (e.g., 'regobs/datadog-terraform-repo')"
    )
    git_branch = Column(
        String(255),
        nullable=True,
        comment="Feature branch name"
    )
    git_commit_sha = Column(
        String(40),
        nullable=True,
        comment="Git commit SHA that triggered workflow"
    )

    # PULL REQUEST TRACKING
    pr_number = Column(
        Integer,
        nullable=True,
        comment="GitHub pull request number"
    )
    pr_url = Column(
        String(500),
        nullable=True,
        comment="Direct URL to pull request"
    )
    pr_status = Column(
        SqlEnum(PRStatusEnum, name="pr_status_enum"),
        nullable=True,
        comment="PR status: PR_OPEN, PR_MERGED, PR_CLOSED"
    )

    # GITHUB ACTIONS WORKFLOW (for future webhook integration)
    workflow_run_id = Column(
        String(100),
        nullable=True,
        comment="GitHub Actions workflow run ID"
    )
    workflow_run_url = Column(
        String(500),
        nullable=True,
        comment="Direct URL to workflow run logs"
    )
    workflow_run_outputs = Column(
        JSONB,
        nullable=True,
        comment="Terraform outputs (monitor IDs, ARNs, etc.)"
    )

    # WORKFLOW TIMING (for future webhook integration)
    run_initiated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="When workflow execution started"
    )
    run_completed_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="When workflow execution completed"
    )

    # TENANT AND USER TRACKING
    tenant_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Tenant code this workflow belongs to"
    )
    user_mst_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="SET NULL"),
        nullable=True,
        comment="User code who created this workflow"
    )

    # POLYMORPHIC REFERENCE (for reverse lookup to source entity)
    transaction_code = Column(
        String(100),
        nullable=True,
        index=True,
        comment="Code of the entity that created this workflow (e.g., service_config.code)"
    )
    table_name = Column(
        SqlEnum(WorkflowSourceTableEnum, name="workflow_source_table_enum"),
        nullable=True,
        index=True,
        comment="Source table name: SERVICE_CONFIG, ALERT_CONFIG, INFRASTRUCTURE, KONG_ROUTE, PIPELINE, SERVICE_CONFIG_DOCKERFILE"
    )

    # RELATIONSHIPS
    # Back-reference to alerts using this workflow
    alert_configs = relationship(
        "AlertConfigModel",
        back_populates="gitops_workflow",
        foreign_keys="[AlertConfigModel.gitops_workflow_id]"
    )

    # Back-reference to Kong routes using this workflow
    kong_route_configs = relationship(
        "KongRouteConfigModel",
        back_populates="gitops_workflow",
        foreign_keys="[KongRouteConfigModel.gitops_workflow_id]"
    )

    # Back-reference to infrastructure records using this workflow
    infrastructure_records = relationship(
        "InfrastructureMstModel",
        back_populates="gitops_workflow",
        foreign_keys="[InfrastructureMstModel.gitops_workflow_id]"
    )

    # Back-reference to service configs using this workflow
    service_configs = relationship(
        "ServiceConfigModel",
        back_populates="gitops_workflow",
        foreign_keys="[ServiceConfigModel.gitops_workflow_id]"
    )

    # Back-reference to pipelines using this workflow
    pipelines = relationship(
        "PipelineMstModel",
        back_populates="gitops_workflow",
        foreign_keys="[PipelineMstModel.gitops_workflow_id]"
    )

    # Relationship to tenant (for filtering by tenant)
    tenant = relationship("TenantsMstModel", foreign_keys=[tenant_mst_code])

    # Relationship to user (for displaying user email in PR listing)
    user = relationship("UserMstModel", foreign_keys=[user_mst_code])

    # Back-reference to junction table for Dockerfile PRs
    dockerfile_config_links = relationship(
        "ServiceConfigDockerfileWorkflowModel",
        back_populates="gitops_workflow",
        cascade="all, delete-orphan"
    )

    # Back-reference to transaction_queue_workflow_mapping
    transaction_queue_mappings = relationship(
        "TransactionQueueWorkflowMappingModel",
        back_populates="gitops_workflow",
        cascade="all, delete-orphan"
    )
