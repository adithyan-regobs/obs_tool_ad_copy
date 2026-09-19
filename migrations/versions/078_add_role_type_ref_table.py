"""add role_type_ref table

Revision ID: 078_add_role_type_ref_table
Revises: 077_add_user_permission_cache_table
Create Date: 2025-01-18

Adds role type reference table for RBAC system:
- New table: role_type_ref (stores available role types)
- Used by service_user_permission for role-based permissions
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

# revision identifiers
revision = '078_add_role_type_ref_table'
down_revision = '077_add_user_permission_cache_table'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # CREATE TABLE: role_type_ref
    # ============================================================
    op.create_table(
        'role_type_ref',

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

        # Constraints
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_role_type_ref_code'),
    )


def downgrade():
    op.drop_table('role_type_ref')
