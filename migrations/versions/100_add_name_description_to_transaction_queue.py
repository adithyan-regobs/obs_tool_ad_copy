"""add name and description to transaction_queue

Revision ID: 100_add_name_description_to_transaction_queue
Revises: 099_add_source_fields_to_ticket
Create Date: 2026-01-09

Changes:
1. Add name column (VARCHAR 255, nullable)
2. Add description column (VARCHAR 500, nullable)
3. Add is_deleted column (BOOLEAN, default False)
4. Add is_active column (BOOLEAN, default True)
5. These fields are part of BaseModel and were missing from transaction_queue table
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '100_add_name_description_to_transaction_queue'
down_revision: Union[str, None] = '099_add_source_fields_to_ticket'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add BaseModel fields that were missing from transaction_queue
    op.add_column(
        'transaction_queue',
        sa.Column('name', sa.String(255), nullable=True, comment='Name of the queue item')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('description', sa.String(500), nullable=True, comment='Description of the queue item')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=True, comment='Soft delete flag')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('is_active', sa.Boolean(), default=True, nullable=True, comment='Active status flag')
    )


def downgrade() -> None:
    # Remove the added columns
    op.drop_column('transaction_queue', 'is_active')
    op.drop_column('transaction_queue', 'is_deleted')
    op.drop_column('transaction_queue', 'description')
    op.drop_column('transaction_queue', 'name')
