"""Add dockerfile column to service_configs

Revision ID: 084_add_dockerfile_column_to_service_configs
Revises: 083_add_slack_integration_tables
Create Date: 2025-12-24

Adds dockerfile TEXT column to service_configs table.
Stores Dockerfile content for EKS deployments when generate_dockerfile is enabled.
This is separate from config JSONB to handle large text content efficiently.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '084_add_dockerfile_column_to_service_configs'
down_revision: Union[str, None] = '083_add_slack_integration_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add dockerfile TEXT column to service_configs
    op.add_column(
        'service_configs',
        sa.Column(
            'dockerfile',
            sa.Text(),
            nullable=True,
            comment="Dockerfile content for EKS deployments when generate_dockerfile is enabled"
        )
    )


def downgrade() -> None:
    # Drop the dockerfile column
    op.drop_column('service_configs', 'dockerfile')
