"""Add transaction_code and table_name to gitops_workflow_detail

Revision ID: 067_add_transaction_code_table_name_to_gitops_workflow
Revises: 066_add_geo_loc_to_chat_info
Create Date: 2025-12-11

Adds transaction_code and table_name columns for polymorphic reference.
These fields allow reverse lookup from gitops_workflow_detail to the source entity
(service_config, alert_config, infrastructure, kong_route, pipeline, etc.)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '067_add_transaction_code_table_name_to_gitops_workflow'
down_revision: Union[str, None] = '066_add_geo_loc_to_chat_info'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create enum type for table_name
    workflow_source_table_enum = sa.Enum(
        'SERVICE_CONFIG',
        'SERVICE_CONFIG_DOCKERFILE',
        'ALERT_CONFIG',
        'INFRASTRUCTURE',
        'KONG_ROUTE',
        'PIPELINE',
        name='workflow_source_table_enum'
    )
    workflow_source_table_enum.create(op.get_bind(), checkfirst=True)

    # Add transaction_code column
    op.add_column(
        'gitops_workflow_detail',
        sa.Column(
            'transaction_code',
            sa.String(100),
            nullable=True,
            comment='Code of the entity that created this workflow (e.g., service_config.code)'
        )
    )

    # Add table_name column with enum type
    op.add_column(
        'gitops_workflow_detail',
        sa.Column(
            'table_name',
            workflow_source_table_enum,
            nullable=True,
            comment='Source table name: SERVICE_CONFIG, ALERT_CONFIG, INFRASTRUCTURE, KONG_ROUTE, PIPELINE, SERVICE_CONFIG_DOCKERFILE'
        )
    )

    # Create indexes for performance
    op.create_index(
        'idx_gitops_workflow_detail_transaction_code',
        'gitops_workflow_detail',
        ['transaction_code']
    )
    op.create_index(
        'idx_gitops_workflow_detail_table_name',
        'gitops_workflow_detail',
        ['table_name']
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_gitops_workflow_detail_table_name', table_name='gitops_workflow_detail')
    op.drop_index('idx_gitops_workflow_detail_transaction_code', table_name='gitops_workflow_detail')

    # Drop columns
    op.drop_column('gitops_workflow_detail', 'table_name')
    op.drop_column('gitops_workflow_detail', 'transaction_code')

    # Drop enum type
    sa.Enum(name='workflow_source_table_enum').drop(op.get_bind(), checkfirst=True)
