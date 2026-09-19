"""Delete all geo_loc_mst entries except mumbai

Revision ID: 059
Revises: 058_alter_audit_event_resource_columns
Create Date: 2025-12-01
"""
from alembic import op


revision = '059_delete_non_mumbai_geo_loc_mst'
down_revision = '058_alter_audit_event_resource_columns'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM geo_loc_mst WHERE code != 'mumbai'")


def downgrade() -> None:
    # Cannot restore deleted data without backup
    pass
