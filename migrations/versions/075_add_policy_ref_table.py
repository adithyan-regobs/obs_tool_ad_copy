"""add policy_ref table

Revision ID: 075_add_policy_ref_table
Revises: 074_add_infrastructure_mst_code_to_pipeline_mst
Create Date: 2025-01-17

Adds policy reference table for RBAC system:
- New table: policy_ref (stores available policy types)
- Seed data: service_admin, service_manager, service_support
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

# revision identifiers
revision = '075_add_policy_ref_table'
down_revision = '074_add_infrastructure_mst_code_to_pipeline_mst'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # CREATE TABLE: policy_ref
    # ============================================================
    op.create_table(
        'policy_ref',

        # BaseModel columns
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', TIMESTAMP(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=True),

        # Policy category
        sa.Column('policy_category', sa.String(50), nullable=False,
                  server_default='service',
                  comment='Category: service, infrastructure, alert'),

        # Capabilities
        sa.Column('can_read', sa.Boolean(), nullable=False,
                  server_default=sa.text('true'),
                  comment='Can view resources'),
        sa.Column('can_write', sa.Boolean(), nullable=False,
                  server_default=sa.text('false'),
                  comment='Can edit resources'),
        sa.Column('can_manage', sa.Boolean(), nullable=False,
                  server_default=sa.text('false'),
                  comment='Can grant/revoke permissions'),

        # Constraints
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_policy_ref_code'),
    )

    # Create index for category lookups
    op.create_index('idx_policy_ref_category', 'policy_ref', ['policy_category'])


def downgrade():
    # Drop in reverse order
    op.drop_index('idx_policy_ref_category', 'policy_ref')
    op.drop_table('policy_ref')