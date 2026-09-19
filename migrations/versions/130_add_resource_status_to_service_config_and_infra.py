"""Add status column (ResourceStatusEnum) to service_configs and infrastructure_mst

Revision ID: 130_add_resource_status_to_service_config_and_infra
Revises: 129_add_infra_apply_statuses_to_deployment_status_enum
Create Date: 2026-04-22

Adds a UI-ready ``status`` column to both ``service_configs`` and
``infrastructure_mst`` tables.  This replaces the fragile client-side
status derivation (from transaction_queue + pipeline_run_track) with a
single persisted value updated by the Jenkins/pipeline webhooks.

Backfills existing rows:
- service_configs with a non-null alb_url in config → ONLINE
- infrastructure_mst with infra_status = ACTIVE → ONLINE
- infrastructure_mst with infra_status = FAILED → FAILED
- Everything else → DRAFT (column default)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "130_add_resource_status_to_service_config_and_infra"
down_revision: Union[str, None] = "129_add_infra_apply_statuses_to_deployment_status_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create the enum type
    resource_status_enum = sa.Enum(
        "DRAFT", "INITIALISING", "PROVISIONING", "BUILDING",
        "DEPLOYING", "VERIFYING", "ONLINE", "FAILED",
        name="resource_status_enum",
    )
    resource_status_enum.create(op.get_bind(), checkfirst=True)

    # 2. Add columns to service_configs (with server_default so NOT NULL works on existing rows)
    op.add_column(
        "service_configs",
        sa.Column(
            "status",
            sa.Enum("DRAFT", "INITIALISING", "PROVISIONING", "BUILDING",
                    "DEPLOYING", "VERIFYING", "ONLINE", "FAILED",
                    name="resource_status_enum", create_type=False),
            nullable=False,
            server_default="DRAFT",
            comment="UI-ready deployment status",
        ),
    )
    op.add_column(
        "service_configs",
        sa.Column(
            "status_updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Timestamp of last status update",
        ),
    )

    # 3. Add columns to infrastructure_mst
    op.add_column(
        "infrastructure_mst",
        sa.Column(
            "status",
            sa.Enum("DRAFT", "INITIALISING", "PROVISIONING", "BUILDING",
                    "DEPLOYING", "VERIFYING", "ONLINE", "FAILED",
                    name="resource_status_enum", create_type=False),
            nullable=False,
            server_default="DRAFT",
            comment="UI-ready deployment status",
        ),
    )
    op.add_column(
        "infrastructure_mst",
        sa.Column(
            "status_updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Timestamp of last status update",
        ),
    )

    # 4. Backfill service_configs: rows with alb_url → ONLINE
    op.execute("""
        UPDATE service_configs
        SET status = 'ONLINE', status_updated_at = NOW()
        WHERE config->>'alb_url' IS NOT NULL
          AND config->>'alb_url' != ''
    """)

    # 5. Backfill infrastructure_mst: ACTIVE → ONLINE, FAILED → FAILED
    op.execute("""
        UPDATE infrastructure_mst
        SET status = 'ONLINE', status_updated_at = NOW()
        WHERE infra_status = 'ACTIVE'
    """)
    op.execute("""
        UPDATE infrastructure_mst
        SET status = 'FAILED', status_updated_at = NOW()
        WHERE infra_status = 'FAILED'
    """)


def downgrade() -> None:
    op.drop_column("infrastructure_mst", "status_updated_at")
    op.drop_column("infrastructure_mst", "status")
    op.drop_column("service_configs", "status_updated_at")
    op.drop_column("service_configs", "status")
    op.execute("DROP TYPE IF EXISTS resource_status_enum")
