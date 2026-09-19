"""Add production_deployment_track table

Revision ID: 166_add_production_deployment_track
Revises: 165_drop_variable_mst
Create Date: 2026-08-30

Renumbered 165 -> 166 and chained behind 165_drop_variable_mst: both were
authored against 164 on separate lanes and met on vance-prod, giving alembic
two heads. The entrypoints run `alembic upgrade head` (singular), which aborts
with "Multiple head revisions are present", retries twice and starts the app
anyway — leaving the DB at 164 with this table never created, which surfaces
at runtime as UndefinedTableError on production_deployment_track.

The merge gate's source of truth for production auto-deployment via the shared
stage→main promotion PR. One row per prod terragrunt dir that is somewhere
between "merged to stage" and "merged to main":

  IN_PROGRESS — content merged to stage, not applied yet
  APPLIED     — apply succeeded; live in AWS, safe to merge into main
  RESUMING    — parked deploy re-queued for apply after a manual plan
  FAILED      — deploy terminated/orphaned with content unapplied on stage

Rows are written by ProductionDeploymentWorkflow BEFORE the feature PR merges
to stage (so "in the promotion diff with no row" reliably means manual/human
content), flipped at apply/park/terminate, and deleted when the promotion PR
merges. The merge gate merges only when every changed dir is MINE or APPLIED.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "166_add_production_deployment_track"
down_revision: Union[str, None] = "165_drop_variable_mst"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # postgresql.ENUM with create_type=False: the type is created ONCE here,
    # and create_table below must not emit a second CREATE TYPE (generic
    # sa.Enum ignores create_type and would — that duplicate rolled back the
    # first run of this migration).
    promotion_status = postgresql.ENUM(
        "IN_PROGRESS", "APPLIED", "RESUMING", "FAILED",
        name="production_deployment_status_enum",
        create_type=False,
    )
    promotion_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "production_deployment_track",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("dir", sa.String(length=512), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_code", sa.String(length=100), nullable=False),
        sa.Column("repo_full_name", sa.String(length=200), nullable=False),
        sa.Column("status", promotion_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("dir"),
    )
    op.create_index(
        "ix_production_deployment_track_repo", "production_deployment_track", ["repo_full_name"]
    )


def downgrade() -> None:
    op.drop_index("ix_production_deployment_track_repo", table_name="production_deployment_track")
    op.drop_table("production_deployment_track")
    sa.Enum(name="production_deployment_status_enum").drop(op.get_bind(), checkfirst=True)
