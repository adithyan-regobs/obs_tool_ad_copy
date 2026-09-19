"""Alter audit_event event_source column to VARCHAR(255)

Revision ID: 056_alter_audit_event_source_varchar
Revises: 055_add_gitops_workflow_to_service_configs
Create Date: 2025-11-30

Increases the event_source column size to accommodate longer source identifiers.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '056_alter_audit_event_source_varchar'
down_revision: Union[str, None] = '055_add_gitops_workflow_to_service_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Alter event_source column to VARCHAR(255)
    op.alter_column(
        'audit_event',
        'event_source',
        existing_type=sa.String(50),
        type_=sa.String(255),
        existing_nullable=True
    )


def downgrade() -> None:
    # Revert event_source column back to original size
    op.alter_column(
        'audit_event',
        'event_source',
        existing_type=sa.String(255),
        type_=sa.String(50),
        existing_nullable=True
    )
