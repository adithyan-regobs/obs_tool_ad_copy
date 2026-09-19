"""add chat_history context and foreign key fields

Revision ID: 097_add_chat_history_context_and_fk_fields
Revises: 096_create_chat_history_table
Create Date: 2026-01-08

Changes:
1. Add foreign keys: tenants_mst_code, user_mst_code
2. Add workflow tracking: workflow_phase, confidence, reasoning, extraction_method
3. Create foreign key constraints to tenants_mst and user_mst tables
4. Enable better LLM context loading and audit trail
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '097_add_chat_history_context_and_fk_fields'
down_revision: Union[str, None] = '096_create_chat_history_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add foreign keys first (nullable for backwards compatibility)
    op.add_column('chat_history', sa.Column(
        'tenants_mst_code',
        sa.String(100),
        nullable=True,
        comment='Tenant code (FK to tenants_mst.code)'
    ))
    op.add_column('chat_history', sa.Column(
        'user_mst_code',
        sa.String(100),
        nullable=True,
        comment='User code (FK to user_mst.code) - which user created this message'
    ))

    # Add workflow tracking columns
    op.add_column('chat_history', sa.Column(
        'workflow_phase',
        sa.String(50),
        nullable=True,
        comment='Workflow phase: init, intent_detection, parameter_extraction, parameter_collection, validation, confirmation, execution, response_generation'
    ))
    op.add_column('chat_history', sa.Column(
        'confidence',
        sa.Float(),
        nullable=True,
        comment='LLM confidence score (0.0 to 1.0) for intent detection'
    ))
    op.add_column('chat_history', sa.Column(
        'reasoning',
        sa.Text(),
        nullable=True,
        comment='LLM reasoning/explanation for decisions made'
    ))
    op.add_column('chat_history', sa.Column(
        'extraction_method',
        sa.String(20),
        nullable=True,
        comment="Method used: 'llm', 'deterministic', 'manual'"
    ))

    # Create foreign key constraints
    op.create_foreign_key(
        'fk_chat_history_tenants_mst_code',
        'chat_history',
        'tenants_mst',
        ['tenants_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_chat_history_user_mst_code',
        'chat_history',
        'user_mst',
        ['user_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # Create indexes for efficient lookups
    op.create_index('idx_chat_history_tenants_mst_code', 'chat_history', ['tenants_mst_code'])
    op.create_index('idx_chat_history_user_mst_code', 'chat_history', ['user_mst_code'])
    op.create_index('idx_chat_history_workflow_phase', 'chat_history', ['workflow_phase'])


def downgrade() -> None:
    # Drop indexes first
    op.drop_index('idx_chat_history_workflow_phase', table_name='chat_history')
    op.drop_index('idx_chat_history_user_mst_code', table_name='chat_history')
    op.drop_index('idx_chat_history_tenants_mst_code', table_name='chat_history')

    # Drop foreign key constraints
    op.drop_constraint('fk_chat_history_user_mst_code', 'chat_history', type_='foreignkey')
    op.drop_constraint('fk_chat_history_tenants_mst_code', 'chat_history', type_='foreignkey')

    # Drop columns
    op.drop_column('chat_history', 'extraction_method')
    op.drop_column('chat_history', 'reasoning')
    op.drop_column('chat_history', 'confidence')
    op.drop_column('chat_history', 'workflow_phase')
    op.drop_column('chat_history', 'user_mst_code')
    op.drop_column('chat_history', 'tenants_mst_code')
