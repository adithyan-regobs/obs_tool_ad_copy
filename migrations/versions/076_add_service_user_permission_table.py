"""add service_user_permission table

Revision ID: 076_add_service_user_permission_table
Revises: 075_add_policy_ref_table
Create Date: 2025-01-17

Adds service user permission table for RBAC system:
- Maps WHO (user/role) has WHAT (policy) on WHICH (service/RG/app) in WHAT (environment)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP, ENUM

# revision identifiers
revision = '076_add_service_user_permission_table'
down_revision = '075_add_policy_ref_table'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # CREATE TABLE: service_user_permission
    # ============================================================
    op.create_table(
        'service_user_permission',

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

        # Tenant isolation
        sa.Column('tenants_mst_code', sa.String(100), nullable=False,
                  comment='Tenant for data isolation'),

        # WHO: User OR Role (mutually exclusive)
        sa.Column('user_mst_code', sa.String(100), nullable=True,
                  comment='Direct user assignment (NULL if role-based)'),
        sa.Column('role_mst_code', sa.String(100), nullable=True,
                  comment='Role assignment (NULL if user-based)'),

        # WHAT: Policy
        sa.Column('policy_ref_code', sa.String(100), nullable=False,
                  comment='Policy type (service_admin, service_manager, service_support)'),

        # WHERE: Scope (only ONE should be set)
        sa.Column('services_mst_code', sa.String(100), nullable=True,
                  comment='Service-level scope'),
        sa.Column('resource_group_mst_code', sa.String(100), nullable=True,
                  comment='RG-level scope'),
        sa.Column('applications_mst_code', sa.String(100), nullable=True,
                  comment='App-level scope'),

        # Environment
        sa.Column('environment',
                  ENUM('dev', 'staging', 'prod', name='environment_enum', create_type=False),
                  nullable=True,
                  comment='Environment scope (NULL = all environments)'),

        # Primary Key
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_service_user_permission_code'),

        # Check Constraints
        sa.CheckConstraint(
            "(user_mst_code IS NOT NULL AND role_mst_code IS NULL) OR "
            "(user_mst_code IS NULL AND role_mst_code IS NOT NULL)",
            name='chk_user_or_role_exclusive'
        ),
        sa.CheckConstraint(
            "services_mst_code IS NOT NULL OR "
            "resource_group_mst_code IS NOT NULL OR "
            "applications_mst_code IS NOT NULL",
            name='chk_at_least_one_scope'
        ),
    )

    # ============================================================
    # FOREIGN KEYS
    # ============================================================
    op.create_foreign_key(
        'fk_sup_tenant', 'service_user_permission', 'tenants_mst',
        ['tenants_mst_code'], ['code'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_sup_user', 'service_user_permission', 'user_mst',
        ['user_mst_code'], ['code'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_sup_role', 'service_user_permission', 'role_mst',
        ['role_mst_code'], ['code'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_sup_policy', 'service_user_permission', 'policy_ref',
        ['policy_ref_code'], ['code'], ondelete='RESTRICT'
    )
    op.create_foreign_key(
        'fk_sup_service', 'service_user_permission', 'services_mst',
        ['services_mst_code'], ['code'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_sup_resource_group', 'service_user_permission', 'resource_group_mst',
        ['resource_group_mst_code'], ['code'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_sup_application', 'service_user_permission', 'applications_mst',
        ['applications_mst_code'], ['code'], ondelete='CASCADE'
    )

    # ============================================================
    # INDEXES for fast lookups
    # ============================================================
    op.create_index('idx_sup_tenant', 'service_user_permission', ['tenants_mst_code'])
    op.create_index('idx_sup_user', 'service_user_permission', ['user_mst_code'])
    op.create_index('idx_sup_role', 'service_user_permission', ['role_mst_code'])
    op.create_index('idx_sup_service', 'service_user_permission', ['services_mst_code'])
    op.create_index('idx_sup_resource_group', 'service_user_permission', ['resource_group_mst_code'])
    op.create_index('idx_sup_application', 'service_user_permission', ['applications_mst_code'])
    op.create_index('idx_sup_environment', 'service_user_permission', ['environment'])


def downgrade():
    # Drop indexes
    op.drop_index('idx_sup_environment', 'service_user_permission')
    op.drop_index('idx_sup_application', 'service_user_permission')
    op.drop_index('idx_sup_resource_group', 'service_user_permission')
    op.drop_index('idx_sup_service', 'service_user_permission')
    op.drop_index('idx_sup_role', 'service_user_permission')
    op.drop_index('idx_sup_user', 'service_user_permission')
    op.drop_index('idx_sup_tenant', 'service_user_permission')

    # Drop foreign keys
    op.drop_constraint('fk_sup_application', 'service_user_permission', type_='foreignkey')
    op.drop_constraint('fk_sup_resource_group', 'service_user_permission', type_='foreignkey')
    op.drop_constraint('fk_sup_service', 'service_user_permission', type_='foreignkey')
    op.drop_constraint('fk_sup_policy', 'service_user_permission', type_='foreignkey')
    op.drop_constraint('fk_sup_role', 'service_user_permission', type_='foreignkey')
    op.drop_constraint('fk_sup_user', 'service_user_permission', type_='foreignkey')
    op.drop_constraint('fk_sup_tenant', 'service_user_permission', type_='foreignkey')

    # Drop table
    op.drop_table('service_user_permission')
