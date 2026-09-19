"""Add case_type_ref_code to chat_info

Revision ID: 073_add_case_type_to_chat_info
Revises: 072_update_service_config_unique_constraint
Create Date: 2025-12-15

Adds case type reference to chat_info table.
Used to group chat sessions by case type for history display.
Nullable for backward compatibility with existing chat sessions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '073_add_case_type_to_chat_info'
down_revision: Union[str, None] = '072_update_service_config_unique_constraint'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add case_type_ref_code column to chat_info
    op.add_column(
        'chat_info',
        sa.Column(
            'case_type_ref_code',
            sa.String(100),
            nullable=True,
            comment='Case type for chat history grouping'
        )
    )

    # Create foreign key constraint with SET NULL on delete
    op.create_foreign_key(
        'fk_chat_info_case_type_ref',
        'chat_info',
        'case_type_ref',
        ['case_type_ref_code'],
        ['code'],
        ondelete='SET NULL'
    )

    # Create index for better query performance
    op.create_index(
        'ix_chat_info_case_type_ref_code',
        'chat_info',
        ['case_type_ref_code']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('ix_chat_info_case_type_ref_code', table_name='chat_info')

    # Drop foreign key constraint
    op.drop_constraint(
        'fk_chat_info_case_type_ref',
        'chat_info',
        type_='foreignkey'
    )

    # Drop column
    op.drop_column('chat_info', 'case_type_ref_code')
