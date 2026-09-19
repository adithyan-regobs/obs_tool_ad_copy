"""Add is_write_only to variable_mst

Revision ID: 155_add_is_write_only_to_variable_mst
Revises: 154_add_kong_route_groups
Create Date: 2026-08-05

Changes:
  - Add variable_mst.is_write_only (boolean, NOT NULL, default false). A
    write-only variable can be written and overwritten but its value is never
    returned by any read API — the listing sends the key with the value masked.
    Applies to SECRET rows only (plain variables live in plaintext in SSM and
    the S3 buckets, so masking them in the UI would be misleading).
  - No backfill: every existing row is readable, so false is correct.
"""

from alembic import op
import sqlalchemy as sa

revision = "155_add_is_write_only_to_variable_mst"
down_revision = "154_add_kong_route_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "variable_mst",
        sa.Column(
            "is_write_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "SECRET only: value is write-once-and-overwrite but never readable "
                "back through DevLift APIs"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("variable_mst", "is_write_only")
