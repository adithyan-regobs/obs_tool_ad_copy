from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum, BigInteger, Integer, DateTime, Boolean, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import Base
from app.core.enum import PipelineRunStatusEnum


class PipelineRunTrackModel(Base):
    """
    Pipeline Run Tracking table.
    Stores individual pipeline execution runs and their status.
    Note: Inherits from Base (not BaseModel) as it doesn't need name/description fields.
    """

    __tablename__ = "pipeline_run_track"

    # Primary key
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Foreign Key to pipeline_mst
    pipeline_mst_code = Column(
        String(100),
        ForeignKey("pipeline_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Run identifier/code (e.g., "pipeline_01_run_1")
    code = Column(
        String(100),
        nullable=False,
        unique=True,
    )

    # Pipeline run status
    status = Column(
        SqlEnum(PipelineRunStatusEnum, name='pipeline_run_status_enum'),
        nullable=False,
        default=PipelineRunStatusEnum.PENDING,
    )

    # Log URL (GitHub Actions run URL, Jenkins build URL, etc.)
    log_url = Column(
        String(500),
        nullable=True,
    )

    # Commit SHA that triggered this pipeline run
    commit_sha = Column(
        String(100),
        nullable=True,
    )

    # GitHub Actions run ID (for tracking and webhooks)
    github_run_id = Column(
        String(100),
        nullable=True,
    )

    # Jenkins build number (for webhook lookup by pipeline_mst_code + build_number)
    build_number = Column(
        Integer,
        nullable=True,
    )

    # Transaction queue codes linked to this build run
    transaction_queue_code = Column(
        JSONB,
        nullable=True,
    )

    # Stage breakdown: [{name, status, duration_secs, started_at}]
    build_stages = Column(
        JSONB,
        nullable=True,
    )

    # Deploy result from final webhook: {alb_url, build_url, build_result, completed_at}
    deploy_result = Column(
        JSONB,
        nullable=True,
    )

    # Generic vendor deployment identifier:
    #   GitHub Actions → GitHub Actions run ID
    #   Jenkins        → Jenkins build number (string form)
    #   Temporal       → Temporal workflow_id
    vendor_deployment_id = Column(
        String(255),
        nullable=True,
    )

    # Error message if pipeline trigger or execution failed
    error_message = Column(
        Text,
        nullable=True,
    )

    # Common fields
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    is_deleted = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    # Relationship to PipelineMstModel
    pipeline = relationship(
        "PipelineMstModel",
        back_populates="pipeline_runs",
        foreign_keys=[pipeline_mst_code],
    )

    def __repr__(self):
        return (
            f"<PipelineRunTrack(id={self.id}, code='{self.code}', "
            f"pipeline_code='{self.pipeline_mst_code}', status='{self.status.value}')>"
        )
