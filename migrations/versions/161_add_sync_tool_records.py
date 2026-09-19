"""add sync_tool_records — durable per-resource sync provenance for devlift-sync-tool

Revision ID: 161_add_sync_tool_records
Revises: 160_audit_archival_partitioning
Create Date: 2026-08-14

Devlift-sync-tool needs to answer "did THIS TOOL write this record, when, and
did it create it or adopt one that already existed" for every row it manages.
Until now that came from heuristics, each with a hole:

  infrastructure   infra_status_updated_by — overwritten the moment anything
                   else touches the row
  service_configs  a 'Synced from terragrunt' description stamped on create
                   only — a config the tool merely UPDATED carries no mark
  kong tables      nothing at all

The audit trail used to be the fallback answer, but 160 gave audit_event a
14-day retention — provenance questions live for years, audit partitions for
two weeks. So: one row per (tenant, resource_type, resource_code), upserted by
the tool on every apply. Deliberately state, not events — the events are
audit_event's job; this holds only the current answer.

resource_code is what the tool keys the resource on, matching what it writes
to audit_event.resource_id:

  infrastructure   infrastructure_mst.code
  service          service_configs.code
  gateway          services_mst.code (kong rows hang off the service)

No FK on resource_code — it spans three tables (same reason audit_event's
resource_id is a bare varchar). No ORM model in obs_tool either: the table is
owned and written by devlift-sync-tool, which is not this codebase. NB: env.py
has no include_object filter, so an `alembic revision --autogenerate` will
propose dropping this table — hand-prune that, as with any model-less table.

The backfill below recovers what the still-attached audit partitions know
(≤14 days). Older rows are re-adopted lazily: the sync tool falls back to the
per-table heuristics above for rows this table does not know, and the next
apply writes them here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '161_add_sync_tool_records'
down_revision: Union[str, None] = '160_audit_archival_partitioning'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sync_tool_records',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('tenants_mst_code', sa.String(100), nullable=False),
        sa.Column(
            'resource_type', sa.String(50), nullable=False,
            comment='infrastructure | service | gateway — the store the resource lives in',
        ),
        sa.Column(
            'resource_code', sa.String(255), nullable=False,
            comment='infrastructure_mst.code / service_configs.code / services_mst.code',
        ),
        # Whether the tool CREATED the devlift record, as opposed to adopting
        # and updating one made in the devlift UI. Sticky true: once created by
        # the tool, always created by the tool, whatever later actions say.
        sa.Column(
            'created_by_tool', sa.Boolean(), nullable=False,
            server_default=sa.text('false'),
            comment='True when the tool created the devlift record (not just updated it)',
        ),
        sa.Column('first_synced_by', sa.String(100), nullable=False),
        sa.Column(
            'first_synced_at', sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('last_synced_by', sa.String(100), nullable=False),
        sa.Column(
            'last_synced_at', sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'last_action', sa.String(20), nullable=False,
            comment='CREATE | UPDATE | DELETE — the most recent apply',
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint('id'),
        # The upsert target, and — by its (tenant, type) prefix — the index
        # behind the tool's one read per resource type per page load.
        sa.UniqueConstraint(
            'tenants_mst_code', 'resource_type', 'resource_code',
            name='uq_sync_tool_records_resource',
        ),
        sa.ForeignKeyConstraint(
            ['tenants_mst_code'], ['tenants_mst.code'], ondelete='CASCADE',
        ),
    )

    # Backfill from whatever audit partitions are still attached (14 days —
    # see 160). event_source identifies the tool's writes; the actor username
    # is what the tool wrote as, LEFT-joined because 160 dropped the FK and a
    # detached actor partition must cost the name, not the row.
    op.execute("""
        INSERT INTO sync_tool_records
            (tenants_mst_code, resource_type, resource_code, created_by_tool,
             first_synced_by, first_synced_at, last_synced_by, last_synced_at,
             last_action)
        SELECT e.tenants_mst_code,
               e.resource_type,
               e.resource_id,
               bool_or(e.event_name = 'CREATE'),
               COALESCE((array_agg(a.username ORDER BY e.event_time ASC))[1],
                        'devlift-sync-tool'),
               min(e.event_time),
               COALESCE((array_agg(a.username ORDER BY e.event_time DESC))[1],
                        'devlift-sync-tool'),
               max(e.event_time),
               (array_agg(e.event_name::text ORDER BY e.event_time DESC))[1]
          FROM audit_event e
          LEFT JOIN audit_actor a ON a.actor_id = e.actor_id
         WHERE e.event_source = 'devlift-sync-tool'
           AND e.resource_id IS NOT NULL
         GROUP BY e.tenants_mst_code, e.resource_type, e.resource_id
        ON CONFLICT ON CONSTRAINT uq_sync_tool_records_resource DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table('sync_tool_records')
