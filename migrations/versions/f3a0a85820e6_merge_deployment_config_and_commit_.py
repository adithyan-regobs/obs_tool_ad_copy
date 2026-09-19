"""merge deployment config and commit tracking migrations

Revision ID: f3a0a85820e6
Revises: 019_add_pipeline_deployment_config, 9b8a9dca40f8
Create Date: 2025-11-06 17:18:19.206329

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a0a85820e6'
down_revision: Union[str, Sequence[str], None] = ('a4f2e8d1c9b3', '9b8a9dca40f8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
