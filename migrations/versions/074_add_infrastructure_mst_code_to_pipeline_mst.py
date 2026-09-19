"""Add infrastructure_mst_code to pipeline_mst

Revision ID: 074_add_infrastructure_mst_code_to_pipeline_mst
Revises: 073_add_case_type_to_chat_info
Create Date: 2025-12-15

Adds infrastructure_mst_code column to pipeline_mst table.
This allows filtering pipelines by the same service, geo location, environment, AND infrastructure/cluster.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '074_add_infrastructure_mst_code_to_pipeline_mst'
down_revision: Union[str, None] = '073_add_case_type_to_chat_info'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add infrastructure_mst_code column to pipeline_mst
    op.add_column(
        'pipeline_mst',
        sa.Column(
            'infrastructure_mst_code',
            sa.String(100),
            nullable=True,  # Nullable for backward compatibility with existing pipelines
            comment='Infrastructure instance code (e.g., EKS cluster)'
        )
    )

    # Add foreign key constraint
    op.create_foreign_key(
        'fk_pipeline_mst_infrastructure_mst_code',
        'pipeline_mst',
        'infrastructure_mst',
        ['infrastructure_mst_code'],
        ['code'],
        ondelete='SET NULL'
    )

    # Create index for better query performance
    op.create_index(
        'idx_pipeline_mst_infrastructure_mst_code',
        'pipeline_mst',
        ['infrastructure_mst_code']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('idx_pipeline_mst_infrastructure_mst_code', table_name='pipeline_mst')

    # Drop foreign key
    op.drop_constraint('fk_pipeline_mst_infrastructure_mst_code', 'pipeline_mst', type_='foreignkey')

    # Drop column
    op.drop_column('pipeline_mst', 'infrastructure_mst_code')
