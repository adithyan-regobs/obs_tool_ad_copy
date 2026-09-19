"""Remove app_info column from service_configs table

Revision ID: 044_remove_app_info_column
Revises: 043_add_region_to_service_configs
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '044_remove_app_info_column'
down_revision: Union[str, None] = '043_add_region_to_service_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove redundant app_info column - language/version resolved from language_ref_code FK."""
    op.drop_column('service_configs', 'app_info')


def downgrade() -> None:
    """Re-add app_info JSONB column."""
    op.add_column('service_configs', sa.Column(
        'app_info',
        JSONB,
        nullable=True,
        comment="App info copied from language_ref: {language: 'Java', version: '17'}"
    ))
