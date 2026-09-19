"""Add monitor_vendor_reference_identifier to alert_configs

Revision ID: 020_add_monitor_vendor_reference_identifier
Revises: 019_add_pipeline_deployment_config
Create Date: 2025-11-12

This migration:
1. Adds monitor_vendor_reference_identifier column to alert_configs table
2. This column stores the internal identifier for terragrunt/IaC organization (e.g., "service-policy-severity")
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b5e3f9c2d8a4'  # 020: Add monitor_vendor_reference_identifier
down_revision: Union[str, None] = 'a4f2e8d1c9b3'  # 019
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Add monitor_vendor_reference_identifier column to alert_configs table.

    This column stores the internal identifier used for organizing monitors
    in terragrunt/IaC files (format: "{service_code}-{infrastructure_type}-{alert_type}").
    """
    op.add_column(
        'alert_configs',
        sa.Column('monitor_vendor_reference_identifier', sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    """
    Remove monitor_vendor_reference_identifier column from alert_configs table.
    """
    op.drop_column('alert_configs', 'monitor_vendor_reference_identifier')
