"""Alter audit_event resource_type and resource_id columns to VARCHAR(255)

Revision ID: 058_alter_audit_event_resource_columns
Revises: 057_add_gitops_workflow_to_pipeline_mst
Create Date: 2025-12-01

Increases the resource_type and resource_id column sizes to accommodate longer resource identifiers.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '058_alter_audit_event_resource_columns'
down_revision: Union[str, None] = '057_add_gitops_workflow_to_pipeline_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Alter resource_type column to VARCHAR(255)
    op.alter_column(
        'audit_event',
        'resource_type',
        existing_type=sa.String(100),
        type_=sa.String(255),
        existing_nullable=False
    )
    # Alter resource_id column to VARCHAR(255)
    op.alter_column(
        'audit_event',
        'resource_id',
        existing_type=sa.String(100),
        type_=sa.String(255),
        existing_nullable=True
    )


def downgrade() -> None:
    # Revert resource_type column back to original size
    op.alter_column(
        'audit_event',
        'resource_type',
        existing_type=sa.String(255),
        type_=sa.String(100),
        existing_nullable=False
    )
    # Revert resource_id column back to original size
    op.alter_column(
        'audit_event',
        'resource_id',
        existing_type=sa.String(255),
        type_=sa.String(100),
        existing_nullable=True
    )
