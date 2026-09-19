"""Remove folder column from pipeline_mst

Revision ID: 053_remove_folder_from_pipeline_mst
Revises: 052_rename_region_mst_to_geo_loc_mst
Create Date: 2025-11-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '053_remove_folder_from_pipeline_mst'
down_revision: Union[str, None] = '052_rename_region_mst_to_geo_loc_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('pipeline_mst', 'folder')


def downgrade() -> None:
    op.add_column(
        'pipeline_mst',
        sa.Column('folder', sa.String(500), nullable=True)
    )
