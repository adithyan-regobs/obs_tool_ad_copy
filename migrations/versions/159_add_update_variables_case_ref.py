"""Add update_variables case_ref for Env & Permissions saves

Revision ID: 159_add_update_variables_case_ref
Revises: 158_harden_and_extend_audit_triggers
Create Date: 2026-08-12

Saving on the Env & Permissions tab records a transaction_queue row so the
change goes through the same draft -> submit -> approve gate as service
settings and gateway routes.

Those rows sit in the SERVICE_CONFIG lane, keyed by service_configs.code like
every other service-scoped change. case_ref_code is what separates them from
the service-settings rows already in that lane: without it the variables
upsert would match a pending 'update_service' draft for the same service and
user and overwrite its config_snapshot, silently discarding queued settings
changes. transaction_queue already uses case_ref_code this way — kong tags its
rows 'add_route' — so this is the existing mechanism, not a new one.

'variables' is the umbrella term, not a sibling of secrets: variable_mst holds
both kinds under variable_type (VARIABLE | SECRET), and the tab renders
"Variables" with Config/Secrets as filters beneath it. The display name spells
out both so nobody approves a secret change thinking it was config-only.

case_type_ref 'service_management' matches 'update_service', the closest
sibling: both change an existing service rather than provision one.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '159_add_update_variables_case_ref'
down_revision: Union[str, None] = '158_harden_and_extend_audit_triggers'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO case_ref (code, name, description, case_type_ref_code, is_active, is_deleted)
        VALUES ('update_variables', 'Update Variables & Secrets',
                'Update environment variables and secrets for a service',
                'service_management', TRUE, FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )


def downgrade() -> None:
    # Safe to remove: transaction_queue.case_ref_code is ON DELETE SET NULL, so
    # rows still pointing here lose the tag rather than blocking the delete.
    op.execute("DELETE FROM case_ref WHERE code = 'update_variables';")
