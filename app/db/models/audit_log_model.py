from sqlalchemy import Column, String, ForeignKey, BigInteger, DateTime, func, Index, Enum as SqlEnum, Text
from sqlalchemy.dialects.postgresql import JSONB, INET
from sqlalchemy.orm import relationship
from app.db.models.base_model import Base
from app.core.enum import AuditActionEnum


class AuditActorModel(Base):
    """
    Audit Actor table.
    Stores information about who performed the action (user, system, API client, etc.).
    One actor can perform many events.
    """

    __tablename__ = "audit_actor"

    # Primary key
    actor_id = Column(BigInteger, primary_key=True, autoincrement=True)

    # User information
    user_id = Column(
        BigInteger,
        ForeignKey("user_mst.id", ondelete="SET NULL"),
        nullable=True,  # Nullable for system/anonymous actors
        comment="Foreign key to user_mst table"
    )

    username = Column(
        String(100),
        nullable=True,
        comment="Username or email for quick reference"
    )

    role = Column(
        String(100),
        nullable=True,
        comment="User role at the time of action (admin, user, api_client, system)"
    )

    # Request context
    ip_address = Column(
        INET,
        nullable=True,
        comment="IP address of the actor"
    )

    user_agent = Column(
        String(500),
        nullable=True,
        comment="User agent string (browser, API client, etc.)"
    )

    # Partition key. The table is range-partitioned by created_at (pg_partman,
    # 14-day retention -> S3 archive), and a partitioned table's PK must
    # include the partition key, hence the composite (actor_id, created_at).
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        primary_key=True,
        comment="Row creation time; range-partition key"
    )

    # Relationships
    user = relationship(
        "UserMstModel",
        foreign_keys=[user_id],
    )

    # No DB-level FK from audit_event.actor_id (Postgres cannot point an FK at
    # a partitioned table without dragging the partition key into every child
    # table, and it would block the daily partition drops), so the join is
    # declared explicitly here.
    events = relationship(
        "AuditEventModel",
        back_populates="actor",
        primaryjoin="AuditActorModel.actor_id == foreign(AuditEventModel.actor_id)",
        cascade="all, delete-orphan"
    )

    # Indexes
    __table_args__ = (
        Index('idx_audit_actor_user_id', 'user_id'),
        Index('idx_audit_actor_username', 'username'),
        Index('idx_audit_actor_ip', 'ip_address'),
    )

    def __repr__(self):
        return f"<AuditActor(actor_id={self.actor_id}, username='{self.username}', role='{self.role}')>"


class AuditEventModel(Base):
    """
    Audit Event table.
    Stores what happened, when, and where.
    Links to actor (who) and field changes (detailed what).
    """

    __tablename__ = "audit_event"

    # Primary key
    event_id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Timestamp - when did this happen?
    # Also the range-partition key (pg_partman, 14-day retention -> S3
    # archive); partitioned-table PKs must include it, hence composite
    # (event_id, event_time).
    event_time = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        primary_key=True,
        comment="Timestamp when the event occurred; range-partition key"
    )

    # Event details - what happened?
    event_name = Column(
        SqlEnum(AuditActionEnum, name='audit_action_enum'),
        nullable=False,
        comment="Type of action: CREATE, UPDATE, DELETE, LOGIN, etc."
    )

    event_source = Column(
        String(255),
        nullable=True,
        comment="Source of the event: API endpoint, background job, system, etc."
    )

    # Actor - who did it?
    # Plain column, no ForeignKey: audit_actor is partitioned and Postgres
    # rejects FKs that reference a partitioned table (and the FK would block
    # the daily partition drops). Integrity is app-enforced.
    actor_id = Column(
        BigInteger,
        nullable=False,
        comment="References audit_actor.actor_id (no DB-level FK)"
    )

    # Resource - what was affected?
    resource_type = Column(
        String(255),
        nullable=False,
        comment="Type of resource: service, application, user, alert_policy, etc."
    )

    resource_id = Column(
        String(255),
        nullable=True,
        comment="Code of the affected resource (matches the 'code' column from other tables)"
    )

    # Status and correlation
    status = Column(
        String(50),
        nullable=True,
        comment="Status of the event: success, failure, pending, etc."
    )

    correlation_id = Column(
        String(100),
        nullable=True,
        comment="Correlation ID for distributed tracing"
    )

    # Tenant information (for multi-tenant filtering)
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant code for data isolation"
    )

    # Request and response payloads
    request_payload = Column(
        JSONB,
        nullable=True,
        comment="Request payload (sanitized - no passwords/secrets)"
    )

    response_payload = Column(
        JSONB,
        nullable=True,
        comment="Response payload (sanitized)"
    )

    # Relationships (explicit primaryjoin because the DB-level FKs between the
    # audit tables were dropped for partitioning)
    actor = relationship(
        "AuditActorModel",
        back_populates="events",
        primaryjoin="foreign(AuditEventModel.actor_id) == AuditActorModel.actor_id",
    )

    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenants_mst_code],
    )

    field_changes = relationship(
        "AuditFieldChangeModel",
        back_populates="event",
        primaryjoin="AuditEventModel.event_id == foreign(AuditFieldChangeModel.event_id)",
        cascade="all, delete-orphan"
    )

    # Indexes
    __table_args__ = (
        Index('idx_audit_event_time', 'event_time'),
        Index('idx_audit_event_name', 'event_name'),
        Index('idx_audit_event_actor', 'actor_id'),
        Index('idx_audit_event_resource', 'resource_type', 'resource_id'),
        Index('idx_audit_event_tenant', 'tenants_mst_code'),
        Index('idx_audit_event_tenant_time', 'tenants_mst_code', 'event_time'),
        Index('idx_audit_event_correlation', 'correlation_id'),
        Index('idx_audit_event_status', 'status'),
    )

    def __repr__(self):
        return (
            f"<AuditEvent(event_id={self.event_id}, "
            f"event_name='{self.event_name}', "
            f"resource='{self.resource_type}', "
            f"event_time='{self.event_time}')>"
        )


class AuditFieldChangeModel(Base):
    """
    Audit Field Change table.
    Stores individual field-level changes for UPDATE operations.
    This allows for granular change tracking and easy reporting.
    """

    __tablename__ = "audit_field_change"

    # Primary key
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Event reference - which event does this change belong to?
    # Plain column, no ForeignKey: audit_event is partitioned and Postgres
    # rejects FKs that reference a partitioned table (and the FK would block
    # the daily partition drops). Rows are written by the audit trigger in the
    # same request that created the event.
    event_id = Column(
        BigInteger,
        nullable=False,
        comment="References audit_event.event_id (no DB-level FK)"
    )

    # Field information
    field_name = Column(
        String(200),
        nullable=False,
        comment="Name of the field that changed"
    )

    old_value = Column(
        Text,
        nullable=True,
        comment="Previous value of the field (as string)"
    )

    new_value = Column(
        Text,
        nullable=True,
        comment="New value of the field (as string)"
    )

    # Partition key. Range-partitioned by created_at (pg_partman, 14-day
    # retention -> S3 archive); partitioned-table PKs must include it, hence
    # composite (id, created_at).
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        primary_key=True,
        comment="Row creation time; range-partition key"
    )

    # Relationships
    event = relationship(
        "AuditEventModel",
        back_populates="field_changes",
        primaryjoin="foreign(AuditFieldChangeModel.event_id) == AuditEventModel.event_id",
    )

    # Indexes
    __table_args__ = (
        Index('idx_audit_field_change_event', 'event_id'),
        Index('idx_audit_field_change_field', 'field_name'),
    )

    def __repr__(self):
        return (
            f"<AuditFieldChange(id={self.id}, "
            f"event_id={self.event_id}, "
            f"field='{self.field_name}')>"
        )
