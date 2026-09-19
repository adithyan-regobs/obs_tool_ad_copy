"""Fix NULL values in case_type_ref and case_ref tables

Revision ID: 039_fix_case_tables_null_values
Revises: 038_add_mysql_user_management
Create Date: 2025-11-26 14:23:48.200503

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '039_fix_case_tables_null_values'
down_revision: Union[str, Sequence[str], None] = '038_add_mysql_user_management'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Fix NULL values in is_deleted and is_active columns for case tables."""

    # Add default values to case_type_ref columns
    op.alter_column('case_type_ref', 'is_deleted',
                    existing_type=sa.Boolean(),
                    server_default=sa.text('false'),
                    existing_nullable=True)

    op.alter_column('case_type_ref', 'is_active',
                    existing_type=sa.Boolean(),
                    server_default=sa.text('true'),
                    existing_nullable=True)

    # Add default values to case_ref columns
    op.alter_column('case_ref', 'is_deleted',
                    existing_type=sa.Boolean(),
                    server_default=sa.text('false'),
                    existing_nullable=True)

    op.alter_column('case_ref', 'is_active',
                    existing_type=sa.Boolean(),
                    server_default=sa.text('true'),
                    existing_nullable=True)

    # Update existing NULL values
    op.execute("""
        UPDATE case_type_ref
        SET is_deleted = FALSE
        WHERE is_deleted IS NULL;

        UPDATE case_type_ref
        SET is_active = TRUE
        WHERE is_active IS NULL;

        UPDATE case_ref
        SET is_deleted = FALSE
        WHERE is_deleted IS NULL;

        UPDATE case_ref
        SET is_active = TRUE
        WHERE is_active IS NULL;
    """)


def downgrade() -> None:
    """Remove default values from columns."""

    # Remove defaults from case_type_ref
    op.alter_column('case_type_ref', 'is_deleted',
                    existing_type=sa.Boolean(),
                    server_default=None,
                    existing_nullable=True)

    op.alter_column('case_type_ref', 'is_active',
                    existing_type=sa.Boolean(),
                    server_default=None,
                    existing_nullable=True)

    # Remove defaults from case_ref
    op.alter_column('case_ref', 'is_deleted',
                    existing_type=sa.Boolean(),
                    server_default=None,
                    existing_nullable=True)

    op.alter_column('case_ref', 'is_active',
                    existing_type=sa.Boolean(),
                    server_default=None,
                    existing_nullable=True)
