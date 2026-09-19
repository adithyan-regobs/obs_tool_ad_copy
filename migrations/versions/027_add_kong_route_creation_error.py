"""add creation_error to kong_route_configs

Revision ID: 027_add_kong_route_creation_error
Revises: 026_add_kong_route_configs, b5e3f9c2d8a4
Create Date: 2025-01-18

Merges two migration branches and adds error message field to kong_route_configs:
- Merges 026_add_kong_route_configs and b5e3f9c2d8a4 (monitor_vendor_reference_identifier)
- Aligns with alert_configs.vendor_error pattern
- Stores error messages when route creation/deployment fails
- Enables querying FAILED routes with error details
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = 'c8f5a7b3d2e1'  # 027: Add creation_error to kong_route_configs
down_revision = ('026_add_kong_route_configs', 'b5e3f9c2d8a4')  # Merge migration
branch_labels = None
depends_on = None


def upgrade():
    # Add creation_error column to kong_route_configs table
    op.add_column(
        'kong_route_configs',
        sa.Column(
            'creation_error',
            sa.String(500),
            nullable=True,
            comment="Error message if route creation/deployment failed"
        )
    )


def downgrade():
    # Drop creation_error column
    op.drop_column('kong_route_configs', 'creation_error')
