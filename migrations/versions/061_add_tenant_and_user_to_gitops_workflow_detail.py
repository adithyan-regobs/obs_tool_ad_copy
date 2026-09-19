"""Add tenant_mst_code and user_mst_code to gitops_workflow_detail

Revision ID: 061_add_tenant_and_user_to_gitops_workflow_detail
Revises: 060_add_pr_status_to_gitops_workflow_detail
Create Date: 2025-12-02

Adds tenant_mst_code and user_mst_code columns to track which tenant and user
created the workflow. Existing records get 'aspora' as default tenant.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '061_add_tenant_and_user_to_gitops_workflow_detail'
down_revision: Union[str, None] = '060_add_pr_status_to_gitops_workflow_detail'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add tenant_mst_code column (nullable initially)
    op.add_column(
        'gitops_workflow_detail',
        sa.Column(
            'tenant_mst_code',
            sa.String(100),
            nullable=True,
            comment='Tenant code this workflow belongs to'
        )
    )

    # Add user_mst_code column (nullable)
    op.add_column(
        'gitops_workflow_detail',
        sa.Column(
            'user_mst_code',
            sa.String(100),
            nullable=True,
            comment='User code who created this workflow'
        )
    )

    # Update existing records with default tenant 'aspora'
    op.execute("UPDATE gitops_workflow_detail SET tenant_mst_code = 'aspora' WHERE tenant_mst_code IS NULL")

    # Create foreign key for tenant_mst_code
    op.create_foreign_key(
        'fk_gitops_workflow_detail_tenant_mst',
        'gitops_workflow_detail',
        'tenants_mst',
        ['tenant_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # Create foreign key for user_mst_code
    op.create_foreign_key(
        'fk_gitops_workflow_detail_user_mst',
        'gitops_workflow_detail',
        'user_mst',
        ['user_mst_code'],
        ['code'],
        ondelete='SET NULL'
    )

    # Create indexes for performance
    op.create_index(
        'idx_gitops_workflow_detail_tenant',
        'gitops_workflow_detail',
        ['tenant_mst_code']
    )
    op.create_index(
        'idx_gitops_workflow_detail_user',
        'gitops_workflow_detail',
        ['user_mst_code']
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_gitops_workflow_detail_user', table_name='gitops_workflow_detail')
    op.drop_index('idx_gitops_workflow_detail_tenant', table_name='gitops_workflow_detail')

    # Drop foreign keys
    op.drop_constraint('fk_gitops_workflow_detail_user_mst', 'gitops_workflow_detail', type_='foreignkey')
    op.drop_constraint('fk_gitops_workflow_detail_tenant_mst', 'gitops_workflow_detail', type_='foreignkey')

    # Drop columns
    op.drop_column('gitops_workflow_detail', 'user_mst_code')
    op.drop_column('gitops_workflow_detail', 'tenant_mst_code')
