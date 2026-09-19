"""create cicd_template_ref table and drop cicd_template_mst

Revision ID: 088_create_cicd_template_ref_table
Revises: 087_make_tenant_nullable_in_cicd_template
Create Date: 2024-12-25

Creates new cicd_template_ref table with simplified structure and drops the old cicd_template_mst table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '088_create_cicd_template_ref_table'
down_revision: Union[str, None] = '086_add_step_columns_to_cicd_template'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # Drop old cicd_template_mst table first (to avoid conflicts)
    op.drop_table('cicd_template_mst')

    # Create new cicd_template_ref table
    op.create_table(
        'cicd_template_ref',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('config', postgresql.JSONB(), nullable=True),
        sa.Column('applications_mst_code', sa.String(100), nullable=True),
        sa.Column('tenant_mst_code', sa.String(100), nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create indexes
    op.create_index('ix_cicd_template_ref_code', 'cicd_template_ref', ['code'])
    op.create_index('ix_cicd_template_ref_applications_mst_code', 'cicd_template_ref', ['applications_mst_code'])
    op.create_index('ix_cicd_template_ref_tenant_mst_code', 'cicd_template_ref', ['tenant_mst_code'])


def downgrade():
    # Recreate cicd_template_mst table (if needed for rollback)
    op.create_table(
        'cicd_template_mst',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('config', postgresql.JSONB(), nullable=True),
        sa.Column('template_type', sa.String(50), nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('is_system_template', sa.Boolean(), default=False, nullable=False),
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=False),
        sa.Column('applications_mst_code', sa.String(100), nullable=True),
        sa.Column('tenant_mst_code', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # Individual step columns (from migration 086)
        sa.Column('step_order', sa.Integer(), nullable=True),
        sa.Column('step_category', sa.String(50), nullable=True),
        sa.Column('step_enabled', sa.Boolean(), default=True, nullable=False),
        sa.Column('step_mandatory', sa.Boolean(), default=False, nullable=False),
        sa.Column('step_dependencies', postgresql.JSONB(), nullable=True),
        sa.Column('language', sa.String(50), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Drop cicd_template_ref table
    op.drop_table('cicd_template_ref')
