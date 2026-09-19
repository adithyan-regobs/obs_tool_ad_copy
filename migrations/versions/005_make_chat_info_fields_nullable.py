"""Make optional chat_info fields nullable

Revision ID: 005_nullable_fields
Revises: 004_user_vendor
Create Date: 2025-11-04 19:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '005_nullable_fields'
down_revision: Union[str, None] = '004_user_vendor'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Make applications_mst_code, resource_group_mst_code, and environment_enum nullable"""

    # Make applications_mst_code nullable
    op.alter_column('chat_info', 'applications_mst_code',
                    existing_type=sa.String(length=100),
                    nullable=True)

    # Make resource_group_mst_code nullable
    op.alter_column('chat_info', 'resource_group_mst_code',
                    existing_type=sa.String(length=100),
                    nullable=True)

    # Make environment_enum nullable and remove default
    op.alter_column('chat_info', 'environment_enum',
                    existing_type=sa.Enum('dev', 'staging', 'prod', name='environment_enum'),
                    nullable=True,
                    server_default=None)


def downgrade() -> None:
    """Revert fields back to NOT NULL"""

    # Revert environment_enum to NOT NULL with default
    op.alter_column('chat_info', 'environment_enum',
                    existing_type=sa.Enum('dev', 'staging', 'prod', name='environment_enum'),
                    nullable=False,
                    server_default='dev')

    # Revert resource_group_mst_code to NOT NULL
    op.alter_column('chat_info', 'resource_group_mst_code',
                    existing_type=sa.String(length=100),
                    nullable=False)

    # Revert applications_mst_code to NOT NULL
    op.alter_column('chat_info', 'applications_mst_code',
                    existing_type=sa.String(length=100),
                    nullable=False)
