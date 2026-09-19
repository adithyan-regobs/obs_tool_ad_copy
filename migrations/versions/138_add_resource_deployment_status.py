"""Add resource_deployment_status_enum and deployment_status columns

Revision ID: 138_add_resource_deployment_status
Revises: 137_add_approval_status_enums
Create Date: 2026-06-02

Changes:
  - Create new resource_deployment_status_enum Postgres type
  - Add deployment_status, deployment_status_updated_at, deployment_error_message
    to infrastructure_mst
  - Add deployment_status, deployment_status_updated_at, deployment_error_message
    to service_configs
"""

from alembic import op
import sqlalchemy as sa

revision = "138_add_resource_deployment_status"
down_revision = "137_add_approval_status_enums"
branch_labels = None
depends_on = None

_ENUM_VALUES = [
    "starting_planning",
    "planning",
    "planned_successfully",
    "plan_failed",
    "starting_applying",
    "applying",
    "applied_successfully",
    "apply_failed",
    "starting_approval",
    "approved_successfully",
    "approval_failed",
]


def upgrade() -> None:
    resource_deployment_status_enum = sa.Enum(
        *_ENUM_VALUES,
        name="resource_deployment_status_enum",
    )
    resource_deployment_status_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "infrastructure_mst",
        sa.Column("deployment_status", sa.Enum(name="resource_deployment_status_enum", create_type=False), nullable=True),
    )
    op.add_column(
        "infrastructure_mst",
        sa.Column("deployment_status_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "infrastructure_mst",
        sa.Column("deployment_error_message", sa.String(1000), nullable=True),
    )

    op.add_column(
        "service_configs",
        sa.Column("deployment_status", sa.Enum(name="resource_deployment_status_enum", create_type=False), nullable=True),
    )
    op.add_column(
        "service_configs",
        sa.Column("deployment_status_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "service_configs",
        sa.Column("deployment_error_message", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("service_configs", "deployment_error_message")
    op.drop_column("service_configs", "deployment_status_updated_at")
    op.drop_column("service_configs", "deployment_status")

    op.drop_column("infrastructure_mst", "deployment_error_message")
    op.drop_column("infrastructure_mst", "deployment_status_updated_at")
    op.drop_column("infrastructure_mst", "deployment_status")

    sa.Enum(name="resource_deployment_status_enum").drop(op.get_bind(), checkfirst=True)