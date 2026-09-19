"""add role_type_ref_code to role_mst

Revision ID: 080_add_role_type_ref_code_to_role_mst
Revises: 079_change_role_mst_to_role_type_ref
Create Date: 2025-01-18

Adds role_type_ref_code column to role_mst table:
- Links each user's role assignment to a role type
- Enables finding all users with a specific role type for permission lookups
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '080_add_role_type_ref_code_to_role_mst'
down_revision = '079_change_role_mst_to_role_type_ref'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # ADD COLUMN: role_type_ref_code
    # ============================================================
    op.add_column(
        'role_mst',
        sa.Column(
            'role_type_ref_code',
            sa.String(100),
            nullable=True,
            comment='Role type (links to role_type_ref for permission lookups)'
        )
    )

    # ============================================================
    # CREATE FOREIGN KEY
    # ============================================================
    op.create_foreign_key(
        'fk_role_mst_role_type',
        'role_mst',
        'role_type_ref',
        ['role_type_ref_code'],
        ['code'],
        ondelete='SET NULL'
    )

    # ============================================================
    # CREATE INDEX for fast lookups
    # ============================================================
    op.create_index('idx_role_mst_role_type', 'role_mst', ['role_type_ref_code'])


def downgrade():
    # Drop index
    op.drop_index('idx_role_mst_role_type', 'role_mst')

    # Drop foreign key
    op.drop_constraint('fk_role_mst_role_type', 'role_mst', type_='foreignkey')

    # Drop column
    op.drop_column('role_mst', 'role_type_ref_code')
