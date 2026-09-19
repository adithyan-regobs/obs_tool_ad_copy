from sqlalchemy import (
    Boolean,
    Column,
    String,
    Text,
    ForeignKey,
    BigInteger,
    Enum as SqlEnum,
    CheckConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import (
    EnvironmentEnum,
    VariableTypeEnum,
    VariableScopeTypeEnum,
    VariableDataTypeEnum,
    SecretProviderEnum,
    WorkflowSourceTableEnum,
)


class VariableMstModel(BaseModel):
    """
    Variables and secrets for any resource (service config, infrastructure, etc.).

    scope_type determines ownership rules:
      - GLOBAL  → table_name and transaction_code must be NULL (shared variable)
      - INFRA   → table_name and transaction_code must be present (resource-specific)

    value vs referenced_variable_id (at most one):
      - Direct value   → value is set, referenced_variable_id is NULL
      - Reference       → value is NULL, referenced_variable_id points to another variable_mst row
      - Secret          → both NULL; actual value lives in external provider

    variable_type discriminator:
      - VARIABLE → plaintext value stored in `value` column
      - SECRET   → actual value lives externally; `secret_provider` + `variable_cloud_identifier` locate it
    """

    __tablename__ = "variable_mst"

    __table_args__ = (
        CheckConstraint(
            "(scope_type = 'GLOBAL' AND table_name IS NULL AND transaction_code IS NULL) "
            "OR (scope_type = 'INFRA' AND table_name IS NOT NULL AND transaction_code IS NOT NULL)",
            name="chk_scope_infra",
        ),
        CheckConstraint(
            "NOT (value IS NOT NULL AND referenced_variable_id IS NOT NULL)",
            name="chk_value_or_reference",
        ),
        # One live key per owning resource: a (transaction_code, table_name)
        # owner may hold each key only once. Partial (live rows only) so a
        # soft-deleted row never blocks re-creating the same key. GLOBAL rows
        # (NULL owner) are not constrained — Postgres treats NULLs as distinct.
        Index(
            "uq_variable_mst_owner_key",
            "transaction_code",
            "table_name",
            "key",
            unique=True,
            postgresql_where=text("is_deleted IS NOT TRUE"),
        ),
    )

    # ── Scope ────────────────────────────────────────────────────────────────
    scope_type = Column(
        SqlEnum(VariableScopeTypeEnum, name="variable_scope_type_enum"),
        nullable=False,
        comment="GLOBAL (shared across infra) or INFRA (belongs to a specific resource)",
    )

    # ── Polymorphic ownership (required when scope_type = INFRA) ─────────────
    table_name = Column(
        SqlEnum(WorkflowSourceTableEnum, name="workflow_source_table_enum"),
        nullable=True,
        comment="Polymorphic: which table owns this variable (SERVICE_CONFIG, INFRASTRUCTURE, etc.)",
    )

    transaction_code = Column(
        String(100),
        nullable=True,
        comment="Polymorphic: code of the owning entity in the source table",
    )

    # ── Variable identity ────────────────────────────────────────────────────
    key = Column(
        String(255),
        nullable=False,
        comment="Variable name (e.g. DATABASE_URL, API_KEY, PORT)",
    )

    value = Column(
        Text,
        nullable=True,
        comment="Plaintext value. NULL when this variable `ref`erences another variable",
    )

    variable_type = Column(
        SqlEnum(VariableTypeEnum, name="variable_type_enum"),
        nullable=False,
        comment="Discriminator: VARIABLE (plaintext) or SECRET (external ref)",
    )

    # ── Variable reference (self-referencing FK) ─────────────────────────────
    referenced_variable_id = Column(
        BigInteger,
        ForeignKey("variable_mst.id", ondelete="SET NULL"),
        nullable=True,
        comment="Points to another variable_mst row. Mutually exclusive with value",
    )

    referenced_variable = relationship(
        "VariableMstModel",
        remote_side="VariableMstModel.id",
        foreign_keys=[referenced_variable_id],
        lazy="joined",
    )

    # ── Polymorphic reference to another resource (optional) ─────────────────
    referenced_transaction_code = Column(
        String(100),
        nullable=True,
        comment="Polymorphic: code of the referenced resource (e.g. infrastructure code)",
    )

    referenced_table_name = Column(
        SqlEnum(WorkflowSourceTableEnum, name="workflow_source_table_enum"),
        nullable=True,
        comment="Polymorphic: which table the referenced_transaction_code lives in",
    )

    # ── Secret provider & cloud identifier ───────────────────────────────────
    secret_provider = Column(
        SqlEnum(
            SecretProviderEnum,
            name="secret_provider_enum",
            values_callable=lambda enum: [e.value for e in enum],
        ),
        nullable=True,
        comment="External secret backend: aws_secrets_manager, hashicorp_vault, aws_ssm, etc.",
    )

    variable_cloud_identifier = Column(
        String(500),
        nullable=True,
        comment="Provider-specific path/identifier (ARN, Vault path, SSM parameter name, K8s secret key)",
    )

    # ── Write-only (SECRET rows only) ────────────────────────────────────────
    # Once true the value can still be written and overwritten, but no read API
    # ever returns it — listings send the key with the value masked. The flag is
    # one-way: turning it back off would expose a value the setter intended
    # nobody to read.
    is_write_only = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment=(
            "SECRET only: value is write-once-and-overwrite but never readable "
            "back through DevLift APIs"
        ),
    )

    # ── Data type hint ───────────────────────────────────────────────────────
    data_type = Column(
        SqlEnum(VariableDataTypeEnum, name="variable_data_type_enum"),
        nullable=False,
        default=VariableDataTypeEnum.string,
        server_default=VariableDataTypeEnum.string.value,
        comment="Data type of the value: string, integer, boolean, json",
    )

    # ── Scoping ──────────────────────────────────────────────────────────────
    environments_enum = Column(
        SqlEnum(EnvironmentEnum, name="enviornment_enum"),
        nullable=True,
        comment="Environment scope: dev / stage / qa / prod",
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
        comment="Extra context: source, parameter_type, rotation_enabled, etc.",
    )

    def __repr__(self):
        owner = f"{self.table_name}:{self.transaction_code}" if self.table_name else "GLOBAL"
        ref = f" -> ref({self.referenced_variable_id})" if self.referenced_variable_id else ""
        return (
            f"<VariableMst(id={self.id}, key='{self.key}', "
            f"scope='{self.scope_type}', type='{self.variable_type}', "
            f"owner={owner}{ref})>"
        )
