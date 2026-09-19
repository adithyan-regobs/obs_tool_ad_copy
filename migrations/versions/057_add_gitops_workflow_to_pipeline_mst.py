"""Add gitops_workflow_id to pipeline_mst

Revision ID: 057_add_gitops_workflow_to_pipeline_mst
Revises: 056_alter_audit_event_source_varchar
Create Date: 2025-11-30

Links pipeline_mst to gitops_workflow_detail for PR tracking.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '057_add_gitops_workflow_to_pipeline_mst'
down_revision: Union[str, None] = '056_alter_audit_event_source_varchar'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add gitops_workflow_id column to pipeline_mst
    op.add_column(
        'pipeline_mst',
        sa.Column(
            'gitops_workflow_id',
            sa.BigInteger(),
            nullable=True,
            comment='Foreign key to gitops_workflow_detail for PR tracking'
        )
    )

    # Create foreign key constraint
    op.create_foreign_key(
        'fk_pipeline_mst_gitops_workflow',
        'pipeline_mst',
        'gitops_workflow_detail',
        ['gitops_workflow_id'],
        ['id'],
        ondelete='SET NULL'
    )

    # Create index for better query performance
    op.create_index(
        'idx_pipeline_mst_gitops_workflow',
        'pipeline_mst',
        ['gitops_workflow_id']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('idx_pipeline_mst_gitops_workflow', table_name='pipeline_mst')

    # Drop foreign key constraint
    op.drop_constraint(
        'fk_pipeline_mst_gitops_workflow',
        'pipeline_mst',
        type_='foreignkey'
    )

    # Drop column
    op.drop_column('pipeline_mst', 'gitops_workflow_id')
