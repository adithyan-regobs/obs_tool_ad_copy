"""Drop variable_mst — the table now lives in the protected secret_config DB.

variable_mst moved to its own database on the locked-down cluster (reachable
only by devlift-secret-config-manager and OpenFGA); scm is the sole accessor
and obs_tool's code no longer references the table at all
(docs/variable-mst-isolation-spec.md). This removes the stale copy from the
shared app DB, which is tunnel-accessible and therefore the whole reason for
the move.

SAFETY GUARD — this migration runs at every boot on every lane, including
prod BEFORE prod's secret_config cutover. To make an early rollout impossible
to get wrong, the drop refuses to run while the table is still receiving
writes: any row created or updated in the last 24 hours means that lane's
cutover has not happened (post-cutover, all writes go to secret_config and
this table goes permanently quiet). On a not-yet-cut-over lane the migration
FAILS LOUDLY (entrypoint logs it and starts the app anyway); after cutover +
a quiet day it applies cleanly on the next deploy.

Enum types are deliberately NOT dropped: enviornment_enum and
workflow_source_table_enum are shared with other tables, and the rest are
harmless to leave.

Downgrade is a deliberate no-op: the data lives in secret_config (and RDS
snapshots); recreating an empty shell here would only invite something to
write to the wrong store.

Revision ID: 165_drop_variable_mst
Revises: 164_add_pr_raised_to_resource_status_enum
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '165_drop_variable_mst'
down_revision: Union[str, None] = '164_add_pr_raised_to_resource_status_enum'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    exists = conn.execute(sa.text(
        "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'variable_mst'"
    )).scalar()
    if not exists:
        return  # already dropped (or never present) — nothing to do

    recent_writes = conn.execute(sa.text("""
        SELECT count(*) FROM variable_mst
        WHERE greatest(
                  coalesce(created_at, 'epoch'::timestamptz),
                  coalesce(updated_at, 'epoch'::timestamptz)
              ) > now() - interval '24 hours'
    """)).scalar()
    if recent_writes and recent_writes > 0:
        raise RuntimeError(
            f"REFUSING to drop variable_mst: {recent_writes} row(s) written in the "
            "last 24h — this lane is still using the app-DB copy, so the "
            "secret_config cutover has not happened here yet. Complete the "
            "cutover (scm pointed at secret_config + fresh dump restored), wait "
            "for a quiet day, then redeploy."
        )

    # Triggers (audit, migration 158) and indexes go with the table.
    op.execute("DROP TABLE variable_mst")


def downgrade() -> None:
    # Deliberate no-op: the live data is in the secret_config database (plus
    # RDS snapshots). Recreating an empty variable_mst here would only invite
    # something to write to the wrong store.
    pass
