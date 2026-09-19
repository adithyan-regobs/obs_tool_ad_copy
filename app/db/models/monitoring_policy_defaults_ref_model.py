from typing import Optional
from sqlalchemy import (
    Column,
    BigInteger,
    Text,
    Numeric,
    Integer,
    Boolean,
    ForeignKey,
    CheckConstraint,
)
from sqlalchemy.orm import relationship, Mapped
from app.db.models.base_model import BaseModel, AlertBaseConfig
from app.core.enum import ComparatorEnum, ThresholdUnitEnum, SeverityEnum, SignalKindEnum


class MonitoringPolicyDefaultsRefModel(BaseModel, AlertBaseConfig):
    """
    Monitoring Policy Defaults Reference
    Defines default alerting thresholds and rules per resource kind.
    """

    __tablename__ = "monitoring_policy_defaults_ref"

    # Resource kind (FK to resource_kinds_ref.code)
    infrastructuretype_ref_code = Column(
        Text,
        ForeignKey("infrastructuretype_ref.code", ondelete="CASCADE"),
        nullable=False,
    )

    # e.g. 'http_4xx_rate', 'cpu_util'
    alerttype_ref_code = Column(
        Text, 
        ForeignKey("alerttype_ref.code", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("severity IN ('P1','P2','P3','P4')", name="chk_policy_severity"),
    )

    def __repr__(self):
        return (
            f"<MonitoringPolicyDefaultsRef(id={self.id}, "
            f"resource_kind='{self.infrastructuretype_ref_code}', "
            f"alert_type='{self.alerttype_ref_code}', "
            f"comparator='{self.comparator}', "
            f"threshold={self.threshold_value} {self.threshold_unit}, "
            f"severity='{self.severity}')>"
        )
