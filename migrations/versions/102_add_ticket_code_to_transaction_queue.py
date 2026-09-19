"""add ticket_code to transaction_queue

Revision ID: 102_add_ticket_code_to_transaction_queue
Revises: 101_add_environment_and_geo_loc_to_kong_route_configs
Create Date: 2026-01-12

Changes:
1. Add ticket_code column (VARCHAR 100, nullable)
2. Add foreign key constraint to ticket.code
3. Add index on ticket_code for faster lookups
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '102_add_ticket_code_to_transaction_queue'
down_revision: Union[str, None] = '101_add_environment_and_geo_loc_to_kong_route_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add ticket_code column
    op.add_column(
        'transaction_queue',
        sa.Column(
            'ticket_code',
            sa.String(100),
            nullable=True,
            comment='Foreign key reference to ticket table'
        )
    )

    # Add foreign key constraint
    op.create_foreign_key(
        'fk_transaction_queue_ticket_code',
        'transaction_queue',
        'ticket',
        ['ticket_code'],
        ['code'],
        ondelete='SET NULL'
    )

    # Add index for faster lookups
    op.create_index(
        'idx_transaction_queue_ticket_code',
        'transaction_queue',
        ['ticket_code']
    )


def downgrade() -> None:
    # Remove index
    op.drop_index('idx_transaction_queue_ticket_code', table_name='transaction_queue')

    # Remove foreign key constraint
    op.drop_constraint('fk_transaction_queue_ticket_code', 'transaction_queue', type_='foreignkey')

    # Remove column
    op.drop_column('transaction_queue', 'ticket_code')
