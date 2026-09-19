from sqlalchemy import Column, String, ForeignKey, BigInteger, Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import WorkflowSourceTableEnum


class PipelineMstModel(BaseModel):
    """
    Pipeline Master table.
    Stores pipeline configurations for CI/CD workflows.

    Uses polymorphic reference (transaction_code + table_name) to link to the
    source entity: service_config for service pipelines, infrastructure_mst for
    infrastructure pipelines (Postgres Helm, MySQL, Grafana, etc.).
    """

    __tablename__ = "pipeline_mst"

    # Polymorphic reference to source entity
    transaction_code = Column(
        String(100),
        nullable=False,
        index=True,
        comment="Polymorphic reference: service_config.code or infrastructure_mst.code"
    )

    # Discriminator for polymorphic reference
    table_name = Column(
        SqlEnum(WorkflowSourceTableEnum, name="workflow_source_table_enum", create_type=False),
        nullable=False,
        comment="Discriminator: SERVICE_CONFIG, INFRASTRUCTURE, etc."
    )

    # Tenant code for direct tenant scoping
    tenant_code = Column(
        String(100),
        nullable=False,
        index=True,
        comment="Tenant code for direct tenant scoping"
    )

    # Foreign Key to pipeline_vendor_mst
    pipeline_vendor_mst_code = Column(
        String(100),
        ForeignKey("pipeline_vendor_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Repository URL
    repo_url = Column(
        String(500),
        nullable=False,
    )

    # Repository branch
    repo_branch = Column(
        String(255),
        nullable=False,
        default="main",
    )

    # Foreign Key to language_ref (nullable for infrastructure/Helm pipelines)
    language_ref_code = Column(
        String(100),
        ForeignKey("language_ref.code", ondelete="RESTRICT"),
        nullable=True,
    )

    # Authentication configuration (JSONB)
    # Stores only authentication credentials (e.g., GitHub PAT, AWS credentials)
    # Example: {"github_pat": "ghp_..."}
    authentication_config = Column(
        JSONB,
        nullable=True,
    )

    # Deployment configuration (JSONB)
    # Stores:
    # - github_commit_sha: Last commit SHA
    # - geo_loc_mst_code: Geographic location code (from frontend)
    deployment_config = Column(
        JSONB,
        nullable=True,
        comment="Deployment configuration: github_commit_sha, geo_loc_mst_code"
    )

    # GitOps Workflow tracking (links to PR)
    gitops_workflow_id = Column(
        BigInteger,
        ForeignKey("gitops_workflow_detail.id", ondelete="SET NULL"),
        nullable=True,
        comment="Foreign key to gitops_workflow_detail for PR tracking"
    )

    # Relationship to PipelineVendorMstModel
    pipeline_vendor = relationship(
        "PipelineVendorMstModel",
        back_populates="pipelines",
        foreign_keys=[pipeline_vendor_mst_code],
    )

    # Relationship to LanguageRefModel
    language_ref = relationship(
        "LanguageRefModel",
        back_populates="pipelines",
        foreign_keys=[language_ref_code],
    )

    # Relationship to PipelineRunTrackModel
    pipeline_runs = relationship(
        "PipelineRunTrackModel",
        back_populates="pipeline",
    )

    # Relationship to GitopsWorkflowDetailModel
    gitops_workflow = relationship(
        "GitopsWorkflowDetailModel",
        back_populates="pipelines",
        foreign_keys=[gitops_workflow_id]
    )

    def __repr__(self):
        return (
            f"<PipelineMst(id={self.id}, code='{self.code}', "
            f"name='{self.name}', repo_url='{self.repo_url}', "
            f"branch='{self.repo_branch}', "
            f"transaction_code='{self.transaction_code}', "
            f"table_name='{self.table_name}')>"
        )
