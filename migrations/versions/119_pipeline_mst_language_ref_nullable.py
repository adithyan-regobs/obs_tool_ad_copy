"""Make pipeline_mst.language_ref_code nullable for infrastructure/Helm pipelines

Revision ID: 119_pipeline_mst_language_ref_nullable
Revises: 118_pipeline_mst_polymorphic_refactor
Create Date: 2026-03-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '119_pipeline_mst_language_ref_nullable'
down_revision: Union[str, None] = '118_pipeline_mst_polymorphic_refactor'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('pipeline_mst', 'language_ref_code', nullable=True)


def downgrade() -> None:
    op.alter_column('pipeline_mst', 'language_ref_code', nullable=False)
