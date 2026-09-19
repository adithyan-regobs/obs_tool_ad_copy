"""add case_code to chat_summary for case-specific summaries

Revision ID: 033_add_case_code_to_chat_summary
Revises: 032_add_service_seed_data
Create Date: 2025-11-24

This migration:
1. Adds case_code column to chat_summary table with FK to case_ref
2. Changes chat_summary from one-to-one to one-to-many with chat_info
3. Adds unique constraint on (chat_info_code, case_code) - one summary per chat+case
4. Backfills existing summaries with 'general_chat' case_code
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '033_add_case_code_summary'
down_revision = '032_add_service_seed_data'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Add case_code column (nullable temporarily for backfill)
    op.add_column(
        'chat_summary',
        sa.Column('case_code', sa.String(length=100), nullable=True)
    )

    # Step 2: Backfill existing summaries with 'general_chat'
    op.execute(
        """
        UPDATE chat_summary
        SET case_code = 'general_chat'
        WHERE case_code IS NULL;
        """
    )

    # Step 3: Make case_code NOT NULL and add foreign key
    op.alter_column('chat_summary', 'case_code', nullable=False)
    op.create_foreign_key(
        'fk_chat_summary_case_code',
        'chat_summary',
        'case_ref',
        ['case_code'],
        ['code'],
        ondelete='RESTRICT'
    )

    # Step 4: Add unique constraint on (chat_info_code, case_code)
    # This allows multiple summaries per chat (one per case type)
    op.create_unique_constraint(
        'uq_chat_summary_chat_case',
        'chat_summary',
        ['chat_info_code', 'case_code']
    )

    # Step 5: Create index for performance
    op.create_index(
        'idx_chat_summary_case_code',
        'chat_summary',
        ['case_code'],
        unique=False
    )


def downgrade() -> None:
    # Drop in reverse order
    op.drop_index('idx_chat_summary_case_code', table_name='chat_summary')
    op.drop_constraint('uq_chat_summary_chat_case', 'chat_summary', type_='unique')
    op.drop_constraint('fk_chat_summary_case_code', 'chat_summary', type_='foreignkey')
    op.drop_column('chat_summary', 'case_code')
