"""Add metadata_json column to db_permission_mst

Revision ID: 120_add_metadata_json_to_db_permission_mst
Revises: 119_pipeline_mst_language_ref_nullable
Create Date: 2026-03-26

Adds a JSONB metadata_json column to db_permission_mst for storing
additional data such as encrypted passwords for user management.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '120_add_metadata_json_to_db_permission_mst'
down_revision: Union[str, None] = '119_pipeline_mst_language_ref_nullable'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'db_permission_mst',
        sa.Column(
            'metadata_json',
            JSONB,
            nullable=True,
            comment='Additional metadata (e.g. encrypted password for user management)',
        )
    )


def downgrade() -> None:
    op.drop_column('db_permission_mst', 'metadata_json')
