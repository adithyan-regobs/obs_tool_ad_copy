"""Add user_mst_code and infra_vendor_enum to chat_info

Revision ID: 004_user_vendor
Revises: 003_chat_summary
Create Date: 2025-11-04 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '004_user_vendor'
down_revision: Union[str, None] = '003_chat_summary'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add user_mst_code and infra_vendor_enum columns to chat_info"""

    # Use existing infra_vendor_enum type (don't create it)
    infra_vendor_enum = postgresql.ENUM('on_prem', 'aws', 'gcp', 'azure', name='infra_vendor_enum', create_type=False)

    # Add user_mst_code column
    op.add_column('chat_info',
        sa.Column('user_mst_code', sa.String(length=100), nullable=True)
    )

    # Add infra_vendor_enum column
    op.add_column('chat_info',
        sa.Column('infra_vendor_enum', infra_vendor_enum, nullable=True, server_default='aws')
    )

    # Create foreign key constraint for user_mst_code
    op.create_foreign_key(
        'fk_chat_info_user_mst',
        'chat_info', 'user_mst',
        ['user_mst_code'], ['code'],
        ondelete='CASCADE'
    )

    # Create indexes
    op.create_index('idx_chat_info_user', 'chat_info', ['user_mst_code'])
    op.create_index('idx_chat_info_vendor', 'chat_info', ['infra_vendor_enum'])

    # Make columns NOT NULL after adding them (optional: update existing rows first if needed)
    op.alter_column('chat_info', 'user_mst_code', nullable=False)
    op.alter_column('chat_info', 'infra_vendor_enum', nullable=False)


def downgrade() -> None:
    """Remove user_mst_code and infra_vendor_enum columns from chat_info"""
    op.drop_index('idx_chat_info_vendor', table_name='chat_info')
    op.drop_index('idx_chat_info_user', table_name='chat_info')
    op.drop_constraint('fk_chat_info_user_mst', 'chat_info', type_='foreignkey')
    op.drop_column('chat_info', 'infra_vendor_enum')
    op.drop_column('chat_info', 'user_mst_code')