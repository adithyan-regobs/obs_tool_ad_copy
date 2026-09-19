"""change role_mst_code to role_type_ref_code in service_user_permission

Revision ID: 079_change_role_mst_to_role_type_ref
Revises: 078_add_role_type_ref_table
Create Date: 2025-01-18

Changes service_user_permission to use role_type_ref instead of role_mst:
- Rename column: role_mst_code -> role_type_ref_code
- Change FK: role_mst -> role_type_ref
- Update check constraint name
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '079_change_role_mst_to_role_type_ref'
down_revision = '078_add_role_type_ref_table'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # 1. DROP OLD CONSTRAINTS AND INDEX
    # ============================================================
    # Drop FK to role_mst
    op.drop_constraint('fk_sup_role', 'service_user_permission', type_='foreignkey')

    # Drop index on role_mst_code
    op.drop_index('idx_sup_role', 'service_user_permission')

    # Drop old check constraint
    op.drop_constraint('chk_user_or_role_exclusive', 'service_user_permission', type_='check')

    # ============================================================
    # 2. RENAME COLUMN
    # ============================================================
    op.alter_column(
        'service_user_permission',
        'role_mst_code',
        new_column_name='role_type_ref_code',
        comment='Role type assignment (NULL if user-based). Applies to ALL users with this role type.'
    )

    # ============================================================
    # 3. CREATE NEW CONSTRAINTS AND INDEX
    # ============================================================
    # Create new FK to role_type_ref
    op.create_foreign_key(
        'fk_sup_role_type', 'service_user_permission', 'role_type_ref',
        ['role_type_ref_code'], ['code'], ondelete='CASCADE'
    )

    # Create new index
    op.create_index('idx_sup_role_type', 'service_user_permission', ['role_type_ref_code'])

    # Create new check constraint
    op.create_check_constraint(
        'chk_user_or_role_type_exclusive',
        'service_user_permission',
        "(user_mst_code IS NOT NULL AND role_type_ref_code IS NULL) OR "
        "(user_mst_code IS NULL AND role_type_ref_code IS NOT NULL)"
    )


def downgrade():
    # ============================================================
    # 1. DROP NEW CONSTRAINTS AND INDEX
    # ============================================================
    op.drop_constraint('chk_user_or_role_type_exclusive', 'service_user_permission', type_='check')
    op.drop_index('idx_sup_role_type', 'service_user_permission')
    op.drop_constraint('fk_sup_role_type', 'service_user_permission', type_='foreignkey')

    # ============================================================
    # 2. RENAME COLUMN BACK
    # ============================================================
    op.alter_column(
        'service_user_permission',
        'role_type_ref_code',
        new_column_name='role_mst_code',
        comment='Role assignment (NULL if user-based)'
    )

    # ============================================================
    # 3. RECREATE OLD CONSTRAINTS AND INDEX
    # ============================================================
    op.create_check_constraint(
        'chk_user_or_role_exclusive',
        'service_user_permission',
        "(user_mst_code IS NOT NULL AND role_mst_code IS NULL) OR "
        "(user_mst_code IS NULL AND role_mst_code IS NOT NULL)"
    )
    op.create_index('idx_sup_role', 'service_user_permission', ['role_mst_code'])
    op.create_foreign_key(
        'fk_sup_role', 'service_user_permission', 'role_mst',
        ['role_mst_code'], ['code'], ondelete='CASCADE'
    )
