from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, DateTime, Boolean, func, BigInteger, String, ForeignKey, Enum as SqlEnum, Integer, Numeric
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship

from app.core.enum import ThresholdUnitEnum, ComparatorEnum, SeverityEnum, SignalKindEnum


Base = declarative_base()

class BaseModel(Base):
    __abstract__ = True  # prevents creating a table for this class
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(100), nullable=False, unique=True)
    name = Column(String(255), nullable=False)
    description = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    is_deleted = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    @declared_attr
    def __tablename__(cls):
        """Automatically use class name as table name (lowercased)."""
        return cls.__name__.lower()

    def soft_delete(self):
        """Soft delete this record by setting is_deleted=True and is_active=False"""
        self.is_deleted = True
        self.is_active = False

class AlertBaseConfig():
    """Base Alert Config Columns"""
    #TODO: Check singal Kind for other vendors (grafana, cloudwatch)
    signal_kind = Column(
        SqlEnum(SignalKindEnum, name="signal_kind_t"),
        nullable=False,
        default=SignalKindEnum.metric,
    )
    comparator = Column(SqlEnum(ComparatorEnum), nullable=False, default=ComparatorEnum.gt)
    threshold_value = Column(Numeric(12, 4), nullable=False)
    threshold_unit = Column(SqlEnum(ThresholdUnitEnum), nullable=False)
    eval_window = Column(Integer, nullable=False, default=5)        # lookback window
    for_duration = Column(Integer, nullable=False, default=10)      # must persist this long
    no_data = Column(Boolean, default=True)
    severity = Column(
        SqlEnum(SeverityEnum),
        nullable=False,
        default=SeverityEnum.P2,
    )