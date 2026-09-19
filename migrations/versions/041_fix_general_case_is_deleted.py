"""fix is_deleted for general case_type and case_ref

Revision ID: 041_fix_general_case_is_deleted
Revises: 040_add_database_case_types
Create Date: 2025-11-27

This migration fixes the is_deleted column for 'general' case_type_ref
and 'general_chat' case_ref entries that were seeded in migration 031
without specifying the is_deleted value.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '041_fix_general_case_is_deleted'
down_revision: Union[str, Sequence[str], None] = '040_add_database_case_types'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fix is_deleted for 'general' case_type_ref
    op.execute(
        """
        UPDATE case_type_ref
        SET is_deleted = FALSE
        WHERE code = 'general' AND is_deleted IS NULL;
        """
    )

    # Fix is_deleted for 'general_chat' case_ref
    op.execute(
        """
        UPDATE case_ref
        SET is_deleted = FALSE
        WHERE code = 'general_chat' AND is_deleted IS NULL;
        """
    )


def downgrade() -> None:
    # Revert to NULL (original state from migration 031)
    op.execute(
        """
        UPDATE case_type_ref
        SET is_deleted = NULL
        WHERE code = 'general';
        """
    )

    op.execute(
        """
        UPDATE case_ref
        SET is_deleted = NULL
        WHERE code = 'general_chat';
        """
    )
