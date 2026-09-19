"""Log Provider Configuration Model.

Stores per-tenant log provider credentials (CloudWatch IAM role, Datadog API keys, etc.).
Each tenant can have multiple providers configured, with one marked as default.
"""

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    String,
    Enum as SqlEnum,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.core.enum import LogProviderEnum
from app.db.models.base_model import BaseModel


class LogProviderConfigModel(BaseModel):
    __tablename__ = "log_provider_config"

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this log provider belongs to",
    )

    provider = Column(
        SqlEnum(LogProviderEnum, name="log_provider_enum", create_type=False),
        nullable=False,
        comment="Log provider type: CLOUDWATCH or DATADOG",
    )

    auth_config = Column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Provider credentials: CloudWatch={assume_role_arn, region, ...}, Datadog={api_key_secret_arn, site, ...}",
    )

    is_default = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this is the tenant's default log provider",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenants_mst_code",
            "provider",
            name="uq_log_provider_config_tenant_provider",
        ),
    )

    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenants_mst_code],
    )
