"""Add deployment_strategy column to service_configs

Revision ID: 081_add_deployment_strategy_to_service_configs
Revises: 080_add_role_type_ref_code_to_role_mst
Create Date: 2025-12-19

Adds deployment_strategy JSONB column to service_configs table.
This separates deployment strategy configuration from the main config JSONB column.
Stores: {strategy: 'rolling'|'canary'|'bluegreen'|'recreate', rolling: {...}, canary: {...}, blueGreen: {...}}
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '081_add_deployment_strategy_to_service_configs'
down_revision: Union[str, None] = '080_add_role_type_ref_code_to_role_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add deployment_strategy JSONB column to service_configs
    op.add_column(
        'service_configs',
        sa.Column(
            'deployment_strategy',
            JSONB,
            nullable=True,
            comment="Deployment strategy configuration: {strategy: 'rolling'|'canary'|'bluegreen'|'recreate', rolling: {...}, canary: {...}, blueGreen: {...}}"
        )
    )

    # Migrate existing deployment_strategy data from config JSONB to new column
    # This extracts deployment_strategy from config and moves it to the new column
    op.execute("""
        UPDATE service_configs
        SET deployment_strategy = config->'deployment_strategy'
        WHERE config->'deployment_strategy' IS NOT NULL
          AND config->'deployment_strategy' != 'null'::jsonb
    """)

    # Remove deployment_strategy from config JSONB (optional - keeps config clean)
    op.execute("""
        UPDATE service_configs
        SET config = config - 'deployment_strategy'
        WHERE config ? 'deployment_strategy'
    """)


def downgrade() -> None:
    # Move deployment_strategy back into config JSONB
    op.execute("""
        UPDATE service_configs
        SET config = COALESCE(config, '{}'::jsonb) || jsonb_build_object('deployment_strategy', deployment_strategy)
        WHERE deployment_strategy IS NOT NULL
    """)

    # Drop the deployment_strategy column
    op.drop_column('service_configs', 'deployment_strategy')
