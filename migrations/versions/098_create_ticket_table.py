"""create ticket table

Revision ID: 098_create_ticket_table
Revises: 097_add_chat_history_context_and_fk_fields
Create Date: 2026-01-08

Adds ticket table for ticket management system:
- New table: ticket (stores tickets with multi-tenancy support)
- Foreign keys to tenants_mst and user_mst
- Globally unique ticket_number field
- Used for tracking and managing support/work tickets
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '098_create_ticket_table'
down_revision: Union[str, None] = '097_add_chat_history_context_and_fk_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ============================================================
    # CREATE TABLE: ticket
    # ============================================================
    op.create_table(
        'ticket',

        # BaseModel columns
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), onupdate=sa.text('now()'), nullable=False),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),

        # Ticket-specific columns
        sa.Column('tenants_mst_code', sa.String(100), nullable=False,
                  comment='Tenant code (FK to tenants_mst.code)'),
        sa.Column('user_mst_code', sa.String(100), nullable=False,
                  comment='User code (FK to user_mst.code) - assigned user for this ticket'),
        sa.Column('ticket_number', sa.String(100), nullable=False,
                  comment='Globally unique ticket number (e.g., ticket-001)'),

        # Constraints
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_ticket_code'),
        sa.UniqueConstraint('ticket_number', name='uq_ticket_ticket_number'),
        sa.ForeignKeyConstraint(
            ['tenants_mst_code'],
            ['tenants_mst.code'],
            ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['user_mst_code'],
            ['user_mst.code'],
            ondelete='CASCADE'
        ),
    )

    # Create indexes for faster lookups
    op.create_index(
        'ix_ticket_tenants_mst_code',
        'ticket',
        ['tenants_mst_code']
    )

    op.create_index(
        'ix_ticket_user_mst_code',
        'ticket',
        ['user_mst_code']
    )

    op.create_index(
        'ix_ticket_ticket_number',
        'ticket',
        ['ticket_number']
    )


def downgrade() -> None:
    op.drop_index('ix_ticket_ticket_number', 'ticket')
    op.drop_index('ix_ticket_user_mst_code', 'ticket')
    op.drop_index('ix_ticket_tenants_mst_code', 'ticket')
    op.drop_table('ticket')