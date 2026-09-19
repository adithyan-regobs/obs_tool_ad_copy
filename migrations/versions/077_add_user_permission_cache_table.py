"""add user_permission_cache table

Revision ID: 077_add_user_permission_cache_table
Revises: 076_add_service_user_permission_table
Create Date: 2025-01-17

Adds cache table for fast permission lookups:
- Stores pre-computed permissions as JSONB array
- One row per user + tenant + environment
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, ENUM

# revision identifiers
revision = '077_add_user_permission_cache_table'
down_revision = '076_add_service_user_permission_table'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # CREATE TABLE: user_permission_cache
    # ============================================================
    op.create_table(
        'user_permission_cache',

        # Primary key
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),

        # User and tenant
        sa.Column('user_mst_code', sa.String(100), nullable=False,
                  comment='User this cache belongs to'),
        sa.Column('tenants_mst_code', sa.String(100), nullable=False,
                  comment='Tenant for data isolation'),

        # Environment
        sa.Column('environment',
                  ENUM('dev', 'staging', 'prod', name='environment_enum', create_type=False),
                  nullable=True,
                  comment='Environment scope (NULL = all environments)'),

        # Permissions JSONB
        sa.Column('permissions', JSONB, nullable=False, server_default='[]',
                  comment='Array of {service_mst_code, policy_ref_code}'),

        # Timestamps
        sa.Column('created_at', TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', TIMESTAMP(timezone=True), nullable=True),

        # Constraints
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_mst_code', 'tenants_mst_code', 'environment',
                          name='uq_permission_cache_user_tenant_env'),
    )

    # Foreign keys
    op.create_foreign_key(
        'fk_perm_cache_user', 'user_permission_cache', 'user_mst',
        ['user_mst_code'], ['code'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_perm_cache_tenant', 'user_permission_cache', 'tenants_mst',
        ['tenants_mst_code'], ['code'], ondelete='CASCADE'
    )

    # Indexes
    op.create_index('idx_perm_cache_user', 'user_permission_cache', ['user_mst_code'])
    op.create_index('idx_perm_cache_tenant', 'user_permission_cache', ['tenants_mst_code'])
    op.create_index('idx_perm_cache_env', 'user_permission_cache', ['environment'])


def downgrade():
    # Drop indexes
    op.drop_index('idx_perm_cache_env', 'user_permission_cache')
    op.drop_index('idx_perm_cache_tenant', 'user_permission_cache')
    op.drop_index('idx_perm_cache_user', 'user_permission_cache')

    # Drop foreign keys
    op.drop_constraint('fk_perm_cache_tenant', 'user_permission_cache', type_='foreignkey')
    op.drop_constraint('fk_perm_cache_user', 'user_permission_cache', type_='foreignkey')

    # Drop table
    op.drop_table('user_permission_cache')
