"""Add referenced transaction columns to variable_mst

Revision ID: 146_add_referenced_transaction_to_variable_mst
Revises: 145_add_plan_verification_statuses_to_resource_deployment_status
Create Date: 2026-07-04

Changes:
  - Add nullable 'referenced_transaction_code' to variable_mst — polymorphic
    code of the resource this variable points at (e.g. the infrastructure that
    owns the bucket a service variable refers to)
  - Add nullable 'referenced_table_name' to variable_mst — which table that
    code lives in
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "146_add_referenced_transaction_to_variable_mst"
down_revision = "145_add_plan_verification_statuses_to_resource_deployment_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "variable_mst",
        sa.Column(
            "referenced_transaction_code",
            sa.String(100),
            nullable=True,
            comment="Polymorphic: code of the referenced resource (e.g. infrastructure code)",
        ),
    )
    op.add_column(
        "variable_mst",
        sa.Column(
            "referenced_table_name",
            postgresql.ENUM(name="workflow_source_table_enum", create_type=False),
            nullable=True,
            comment="Polymorphic: which table the referenced_transaction_code lives in",
        ),
    )


def downgrade() -> None:
    op.drop_column("variable_mst", "referenced_table_name")
    op.drop_column("variable_mst", "referenced_transaction_code")
