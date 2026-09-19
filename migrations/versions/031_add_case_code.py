"""add general case and chat_message case_code

Revision ID: 031_add_case_code
Revises: 030_create_case_tables
Create Date: 2025-11-24

This migration:
1. Adds "general" entry to case_type_ref for non-infrastructure conversations
2. Adds "general_chat" entry to case_ref as the default case
3. Adds case_code column to chat_message table with FK to case_ref
4. Creates index for performance on case_code lookups
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '031_add_case_code'
down_revision = '030_create_case_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Add "general" to case_type_ref
    op.execute(
        """
        INSERT INTO case_type_ref (code, name, description, vendor_provider, is_active)
        VALUES ('general', 'General queries', 'General user queries.', NULL, FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )

    # Step 2: Add "general_chat" to case_ref
    op.execute(
        """
        INSERT INTO case_ref (code, name, description, case_type_ref_code, is_active)
        VALUES ('general_chat', 'General Chat',
                'General conversation not specific to any resource type', 'general', FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )

    # Step 3: Add case_code column to chat_message
    op.add_column(
        'chat_message',
        sa.Column('case_code', sa.String(length=100), nullable=False, server_default='general_chat')
    )

    # Step 4: Add foreign key constraint
    op.create_foreign_key(
        'fk_chat_message_case_code',
        'chat_message',
        'case_ref',
        ['case_code'],
        ['code'],
        ondelete='RESTRICT'
    )

    # Step 5: Create index for performance
    op.create_index(
        'idx_chat_message_case_code',
        'chat_message',
        ['case_code'],
        unique=False
    )


def downgrade() -> None:
    # Drop in reverse order
    op.drop_index('idx_chat_message_case_code', table_name='chat_message')
    op.drop_constraint('fk_chat_message_case_code', 'chat_message', type_='foreignkey')
    op.drop_column('chat_message', 'case_code')

    # Remove seed data (optional, but good for clean rollback)
    op.execute("DELETE FROM case_ref WHERE code = 'general_chat';")
    op.execute("DELETE FROM case_type_ref WHERE code = 'general';")
