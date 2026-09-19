from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum, WorkflowSourceTableEnum


class ResourceConnectionMstModel(BaseModel):
    """
    Canvas edge between two resources, with optional operational payload.

    Both endpoints are polymorphic (transaction_code + table_name pairs), so
    one table models every connection kind: service→infra (service uses an
    S3 bucket), service→service, and infra→infra (e.g. RDS → Redshift).

    Direction convention: source = the initiator/consumer (the service that
    uses the bucket, the origin of a data flow); target = the dependency /
    provider being consumed.

    permission / network_policy are independent nullable payloads — an edge
    may carry either, both, or neither (a plain visual connection drawn on
    the canvas before anything is configured).

    environments_enum is derived at write time from the two endpoints (both
    must live in the same environment) — never accepted from the client.
    Denormalized here, like on variable_mst, because polymorphic codes can't
    be joined cheaply when fetching all edges for a tenant + environment.
    """

    __tablename__ = "resource_connection_mst"

    __table_args__ = (
        UniqueConstraint(
            "source_transaction_code",
            "source_table_name",
            "target_transaction_code",
            "target_table_name",
            "environments_enum",
            name="uq_resource_connection_edge",
        ),
    )

    # ── Edge endpoints (polymorphic) ─────────────────────────────────────────
    source_transaction_code = Column(
        String(100),
        nullable=False,
        comment="Code of the consuming/initiating resource (e.g. service_config code)",
    )

    source_table_name = Column(
        SqlEnum(WorkflowSourceTableEnum, name="workflow_source_table_enum"),
        nullable=False,
        comment="Table the source code lives in (SERVICE_CONFIG, INFRASTRUCTURE, ...)",
    )

    target_transaction_code = Column(
        String(100),
        nullable=False,
        comment="Code of the dependency/provider resource (e.g. infrastructure_mst code)",
    )

    target_table_name = Column(
        SqlEnum(WorkflowSourceTableEnum, name="workflow_source_table_enum"),
        nullable=False,
        comment="Table the target code lives in (SERVICE_CONFIG, INFRASTRUCTURE, ...)",
    )

    # ── Operational payload (either, both, or neither) ───────────────────────
    permission = Column(
        JSONB,
        nullable=True,
        comment='Access grants, discriminated by type: {"type": "s3", "actions": ["read", "write"]} '
                'or {"type": "db", "db_user": "goms_service", "grants": ["SELECT", "INSERT"]}',
    )

    network_policy = Column(
        JSONB,
        nullable=True,
        comment="Network reachability config: security groups, ports, CIDRs, etc.",
    )

    # ── Scoping ──────────────────────────────────────────────────────────────
    environments_enum = Column(
        SqlEnum(EnvironmentEnum, name="environment_enum"),
        nullable=False,
        comment="Environment of both endpoints (validated equal at write time)",
    )

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Extensibility ────────────────────────────────────────────────────────
    metadata_json = Column(
        "metadata",
        JSONB,
        nullable=True,
        default={},
        comment="Extra context: edge style hints, provisioning state, etc.",
    )

    def __repr__(self):
        return (
            f"<ResourceConnectionMst(id={self.id}, "
            f"{self.source_table_name}:{self.source_transaction_code} -> "
            f"{self.target_table_name}:{self.target_transaction_code}, "
            f"env='{self.environments_enum}')>"
        )
