from typing import Optional
from uuid import uuid4
from datetime import datetime
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    String,
    Enum as SqlEnum,
    Integer,
    BigInteger,
    TIMESTAMP,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, Mapped
from sqlalchemy import func, Boolean
from app.db.models.base_model import BaseModel, AlertBaseConfig
from app.core.enum import IntegrationStatusEnum, DeploymentStatusEnum

class AlertConfigModel(BaseModel, AlertBaseConfig):
    """
    Alert Configuration Model
    Represents per-service or per-resource alerting thresholds and policies.
    """
    __tablename__ = "alert_configs"
    # Foreign Keys
    monitoring_policy_defaults_ref_code = Column(String(100), ForeignKey("monitoring_policy_defaults_ref.code", ondelete="CASCADE"), nullable=True)
    # Datadog
    obs_vendor_accounts_mst_code = Column(String(100), ForeignKey("obs_vendor_accounts_mst.code", ondelete="CASCADE"), nullable=False)
    # Either Service or Infra (no foreign keys since tables don't exist yet)
    services_mst_code = Column(String(100), nullable=True)
    infrastructure_mst_code = Column(String(100), nullable=True)

    # Vendor sync tracking fields
    vendor_status = Column(SqlEnum(IntegrationStatusEnum), nullable=False, default=IntegrationStatusEnum.INITIATED)
    vendor_monitor_id = Column(String(100), nullable=True)  # Vendor's monitor ID (e.g., Datadog monitor ID)
    monitor_vendor_reference_identifier = Column(String(255), nullable=True)  # Internal identifier for terragrunt/IaC (e.g., "service-infra_type-alert_type")
    vendor_error = Column(String(500), nullable=True)  # Error message if sync failed
    vendor_status_updated_at = Column(DateTime(timezone=True), nullable=True)
    vendor_retry_count = Column(Integer, default=0)
    vendor_last_sync_attempt = Column(DateTime(timezone=True), nullable=True)

    # DEPLOYMENT WORKFLOW TRACKING
    creation_status = Column(
        SqlEnum(DeploymentStatusEnum, name="deployment_status_enum"),
        nullable=True,
        default=DeploymentStatusEnum.INITIATED,
        comment="Deployment workflow status"
    )
    creation_status_updated_by = Column(
        String(255),
        nullable=True,
        comment="Email or GitHub username of user/system that updated status"
    )
    creation_status_updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Timestamp of last status update"
    )
    gitops_workflow_id = Column(
        BigInteger,
        ForeignKey("gitops_workflow_detail.id", ondelete="SET NULL"),
        nullable=True,
        comment="Foreign key to gitops_workflow_detail (MANY alerts → ONE workflow for bulk)"
    )
    resource_identifier = Column(
        String(500),
        nullable=True,
        comment="Monitor ID or AWS ARN (populated after vendor creation)"
    )

    # RELATIONSHIPS
    gitops_workflow = relationship(
        "GitopsWorkflowDetailModel",
        back_populates="alert_configs",
        foreign_keys=[gitops_workflow_id]
    )

    def __repr__(self):
        return (
            f"<AlertConfig(id={self.id}, service_code={self.services_mst_code}, "
            f"comparator='{self.comparator}', threshold={self.threshold_value} "
            f"{self.threshold_unit}, severity='{self.severity}')>"
        )
