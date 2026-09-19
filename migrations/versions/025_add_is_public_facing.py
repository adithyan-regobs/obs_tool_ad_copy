"""add is_public_facing to services_mst

Revision ID: 025_add_is_public_facing
Revises: 024_add_gitops_workflow_tracking
Create Date: 2025-01-18

Adds is_public_facing column to services_mst table to track whether
a service is publicly accessible (has public endpoints).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '025_add_is_public_facing'
down_revision = '024_add_gitops_workflow_tracking'
branch_labels = None
depends_on = None


def upgrade():
    """Add is_public_facing column to services_mst table."""
    op.add_column('services_mst',
        sa.Column('is_public_facing', sa.Boolean(),
                  nullable=False,
                  server_default='false',
                  comment="Whether service is publicly accessible")
    )


def downgrade():
    """Remove is_public_facing column from services_mst table."""
    op.drop_column('services_mst', 'is_public_facing')
