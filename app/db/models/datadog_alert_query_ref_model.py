from sqlalchemy import (
    Column,
    String,
    Text,
    ForeignKey,
    UniqueConstraint,
    Enum as SqlEnum,
)
from app.db.models.base_model import BaseModel
from app.core.enum import SignalKindEnum


class DatadogAlertQueryRefModel(BaseModel):
    """
    Datadog Alert Query Reference Table (Global Constant Reference)

    Stores constant Datadog query templates for building monitors dynamically.
    This is a reference table containing fixed query patterns.
    """

    __tablename__ = "datadog_alert_query_ref"

    # Composite key to identify unique query patterns
    infrastructuretype_ref_code = Column(
        String(100),
        ForeignKey("infrastructuretype_ref.code", ondelete="CASCADE"),
        nullable=False,
    )

    alerttype_ref_code = Column(
        String(100),
        ForeignKey("alerttype_ref.code", ondelete="CASCADE"),
        nullable=False,
    )

    signal_kind = Column(
        SqlEnum(SignalKindEnum, name="signal_kind_t"),
        nullable=False,
        default=SignalKindEnum.metric,
    )

    # Query template with placeholders for runtime substitution
    query_template = Column(
        Text,
        nullable=False,
    )

    # Composite unique constraint
    __table_args__ = (
        UniqueConstraint(
            'infrastructuretype_ref_code',
            'alerttype_ref_code',
            'signal_kind',
            name='uq_infra_alert_signal'
        ),
    )

    def __repr__(self):
        return (
            f"<DatadogAlertQueryRef(id={self.id}, "
            f"infra='{self.infrastructuretype_ref_code}', "
            f"alert='{self.alerttype_ref_code}', "
            f"signal='{self.signal_kind}')>"
        )
