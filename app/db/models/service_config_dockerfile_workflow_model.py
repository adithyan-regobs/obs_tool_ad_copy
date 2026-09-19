"""
Junction table model for linking service_configs to gitops_workflow_detail for Dockerfile PRs.
Allows multiple Dockerfile PRs (one per branch per repository) per service config.
"""
from sqlalchemy import Column, BigInteger, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class ServiceConfigDockerfileWorkflowModel(BaseModel):
    """
    Junction table linking service_configs to gitops_workflow_detail for Dockerfile PRs.

    This enables many-to-many relationship:
    - One service_config can have multiple Dockerfile PRs (one per branch per repository)
    - One gitops_workflow_detail can be linked to one service_config (for Dockerfile PRs)

    Unique constraint: (service_config_id, branch, repository)
    - Allows different repositories to have the same branch name
    - Each (config, branch, repo) combination has exactly one workflow
    """

    __tablename__ = "service_config_dockerfile_workflows"

    # Foreign keys
    service_config_id = Column(
        BigInteger,
        ForeignKey("service_configs.id", ondelete="CASCADE"),
        nullable=False,
        comment="FK to service_configs"
    )
    gitops_workflow_id = Column(
        BigInteger,
        ForeignKey("gitops_workflow_detail.id", ondelete="CASCADE"),
        nullable=False,
        comment="FK to gitops_workflow_detail"
    )

    # Branch and repository for uniqueness
    branch = Column(
        String(255),
        nullable=False,
        comment="Base branch name (main, stage, etc.)"
    )
    repository = Column(
        String(200),
        nullable=False,
        comment="GitHub repository (owner/repo)"
    )

    # Table constraints
    __table_args__ = (
        UniqueConstraint(
            'service_config_id', 'branch', 'repository',
            name='uq_scdw_config_branch_repo'
        ),
    )

    # Relationships
    service_config = relationship(
        "ServiceConfigModel",
        back_populates="dockerfile_workflow_links"
    )
    gitops_workflow = relationship(
        "GitopsWorkflowDetailModel",
        back_populates="dockerfile_config_links"
    )

    def __repr__(self):
        return (
            f"<ServiceConfigDockerfileWorkflow("
            f"id={self.id}, config_id={self.service_config_id}, "
            f"workflow_id={self.gitops_workflow_id}, branch='{self.branch}', "
            f"repo='{self.repository}')>"
        )
