"""add source fields to ticket

Revision ID: 099_add_source_fields_to_ticket
Revises: 098_create_ticket_table
Create Date: 2025-01-08

Changes:
1. Add source column to track where ticket originated (e.g., portal, api, monitoring)
2. Add source_ref_id column for reference ID from source system
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '099_add_source_fields_to_ticket'
down_revision: Union[str, None] = '098_create_ticket_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add source tracking columns
    op.add_column(
        'ticket',
        sa.Column('source', sa.String(100), nullable=True,
                  comment='Source system where ticket originated (e.g., portal, api, monitoring)')
    )

    op.add_column(
        'ticket',
        sa.Column('source_ref_id', sa.String(255), nullable=True,
                  comment='Reference ID from source system')
    )


def downgrade() -> None:
    # Remove source tracking columns
    op.drop_column('ticket', 'source_ref_id')
    op.drop_column('ticket', 'source')
