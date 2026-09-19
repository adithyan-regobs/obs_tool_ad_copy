"""add invitations_mst table

Revision ID: 023_add_invitations_mst
Revises: 1d403db88b8d
Create Date: 2025-01-17

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '023_add_invitations_mst'
down_revision = '1d403db88b8d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create invitations_mst table for user invitation management
    """
    op.create_table(
        'invitations_mst',
        # BaseModel columns (inherited)
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('is_active', sa.Boolean(), nullable=True, server_default='true'),

        # InvitationMstModel specific columns
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('token', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=50), nullable=False),
        sa.Column('is_org_owner', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('tenant_code', sa.String(length=100), nullable=False),
        sa.Column('invited_by_user_code', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='pending'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),

        # Primary key
        sa.PrimaryKeyConstraint('id'),

        # Unique constraints
        sa.UniqueConstraint('code', name='uq_invitations_mst_code'),
        sa.UniqueConstraint('token', name='uq_invitations_mst_token'),

        # Foreign keys
        sa.ForeignKeyConstraint(
            ['tenant_code'],
            ['tenants_mst.code'],
            name='fk_invitations_mst_tenant_code',
            ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['invited_by_user_code'],
            ['user_mst.code'],
            name='fk_invitations_mst_invited_by_user_code',
            ondelete='SET NULL'
        ),
    )

    # Create indexes for better query performance
    op.create_index('ix_invitations_mst_email', 'invitations_mst', ['email'])
    op.create_index('ix_invitations_mst_token', 'invitations_mst', ['token'])
    op.create_index('ix_invitations_mst_tenant_code', 'invitations_mst', ['tenant_code'])
    op.create_index('ix_invitations_mst_status', 'invitations_mst', ['status'])


def downgrade() -> None:
    """
    Drop invitations_mst table
    """
    op.drop_index('ix_invitations_mst_status', table_name='invitations_mst')
    op.drop_index('ix_invitations_mst_tenant_code', table_name='invitations_mst')
    op.drop_index('ix_invitations_mst_token', table_name='invitations_mst')
    op.drop_index('ix_invitations_mst_email', table_name='invitations_mst')
    op.drop_table('invitations_mst')
