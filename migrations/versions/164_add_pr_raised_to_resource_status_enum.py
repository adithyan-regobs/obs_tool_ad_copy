"""Add PR_RAISED to resource_status_enum

Revision ID: 164_add_pr_raised_to_resource_status_enum
Revises: 163_widen_branch_columns
Create Date: 2026-08-19

Adds the ``PR_RAISED`` label to ``resource_status_enum`` for the state between
"a PR exists for this resource" and "that PR was merged and applied".

Prod ships infra changes as a reviewed PR, so a resource can sit for hours in a
state that is neither DRAFT (a PR exists) nor any of the in-progress values
(nothing is running — it is waiting on a human). Reusing INITIALISING for it
made every consumer that reads "in progress" — the settings-panel spinner
badge, the deploy button gating — claim work was happening when it was not.

Consumers that care about deployment progress should keep excluding this label;
consumers that only care whether the resource is real enough to reference (the
Add-Reference picker) should include it.

Postgres can't drop a single enum label, so the downgrade is a no-op.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "164_add_pr_raised_to_resource_status_enum"
down_revision: Union[str, None] = "163_widen_branch_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE resource_status_enum ADD VALUE IF NOT EXISTS 'PR_RAISED'")


def downgrade() -> None:
    # Postgres does not support removing a single value from an enum without
    # rebuilding the type. Leaving the label in place is harmless.
    pass
