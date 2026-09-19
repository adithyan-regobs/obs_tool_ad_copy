"""Rename variable_mst.secret_cloud_identifier to variable_cloud_identifier

Revision ID: 147_rename_secret_cloud_identifier_to_variable_cloud_identifier
Revises: 146_add_referenced_transaction_to_variable_mst
Create Date: 2026-07-04

The column holds the cloud identifier (Secrets Manager ARN or SSM parameter
name) for both secrets AND plain variables now that variables are deployed to
SSM Parameter Store — 'secret_' in the name no longer fits.
"""

from alembic import op

revision = "147_rename_secret_cloud_identifier_to_variable_cloud_identifier"
down_revision = "146_add_referenced_transaction_to_variable_mst"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "variable_mst",
        "secret_cloud_identifier",
        new_column_name="variable_cloud_identifier",
    )


def downgrade() -> None:
    op.alter_column(
        "variable_mst",
        "variable_cloud_identifier",
        new_column_name="secret_cloud_identifier",
    )
