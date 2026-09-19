"""Add geo_loc_mst_code to chat_info

Revision ID: 066_add_geo_loc_to_chat_info
Revises: 065_create_dockerfile_workflows_junction
Create Date: 2025-12-10

Adds geographic location reference to chat_info table.
Used by service config chat to form unique context hash (geo_loc + env + service).
Nullable for backward compatibility - langchat doesn't use this field.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '066_add_geo_loc_to_chat_info'
down_revision: Union[str, None] = '065_create_dockerfile_workflows_junction'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add geo_loc_mst_code column to chat_info
    op.add_column(
        'chat_info',
        sa.Column(
            'geo_loc_mst_code',
            sa.String(100),
            nullable=True,
            comment='Geographic location for service config chat context'
        )
    )

    # Create foreign key constraint with SET NULL on delete
    op.create_foreign_key(
        'fk_chat_info_geo_loc_mst',
        'chat_info',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='SET NULL'
    )

    # Create index for better query performance
    op.create_index(
        'ix_chat_info_geo_loc_mst_code',
        'chat_info',
        ['geo_loc_mst_code']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('ix_chat_info_geo_loc_mst_code', table_name='chat_info')

    # Drop foreign key constraint
    op.drop_constraint(
        'fk_chat_info_geo_loc_mst',
        'chat_info',
        type_='foreignkey'
    )

    # Drop column
    op.drop_column('chat_info', 'geo_loc_mst_code')
