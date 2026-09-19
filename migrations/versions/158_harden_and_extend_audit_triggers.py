"""Harden audit_capture_field_changes() and attach it to every remaining table

Revision ID: 158_harden_and_extend_audit_triggers
Revises: 157_add_approval_rule_mst
Create Date: 2026-08-12

Two changes, both aimed at the same gap: the field-level audit trail covered
35 of 73 tables, because migration 063 looped over information_schema at the
moment it ran (Dec 2025) and the 94 migrations since never attached a trigger
to the tables they created. transaction_queue, approval_rule_mst and
variable_mst — three actively-developed surfaces — recorded no old->new
history at all.

1. HARDEN the trigger function. It copied every changed column into
   audit_field_change.old_value/new_value as plaintext, skipping only
   created_at/updated_at/id. Attaching it to tables that hold credentials
   (mcp_oauth_token.token_hash, mcp_auth_session.session_token,
   transaction_queue.script_access_key, invitations_mst.token, the
   *_vendor_accounts_mst.auth_config blobs) would have written that material
   into the audit tables. A regex skip on the column NAME now drops those
   before the INSERT, so the protection is a property of the function rather
   than a per-table judgement call — and it applies to the 35 tables that
   were already triggered, not just the new ones.

   `value` is deliberately NOT skipped: on variable_mst it holds config data
   (AWS_REGION, S3_BUCKET_NAME, SQS_QUEUE_URL — verified against the live
   table), values for AWS-backed secrets are NULL by design, and "bucket name
   changed from X to Y" is exactly what the audit trail exists to show.
   `threshold_value` is likewise preserved (real monitoring config).

2. ATTACH the trigger to every public table except the exclusions below.
   Idempotent (DROP IF EXISTS first), so it also re-attaches cleanly to the
   35 tables 063 already covered.

Exclusions, and why each one is not an oversight:
  - alembic_version                    Alembic's own bookkeeping
  - audit_actor/event/field_change     the audit system itself; a trigger here
                                       would recurse on its own writes
  - checkpoints, checkpoint_writes,    LangGraph agent checkpointer: ~212 MB /
    checkpoint_blobs,                  190k rows of serialised binary state,
    checkpoint_migrations              rewritten on every agent step. Not
                                       business data, and it would dominate
                                       audit_field_change.
  - user_permission_cache              derived cache, rebuilt from the tables
                                       that are themselves audited

NOTE for future migrations: this is again a point-in-time sweep. A table
created after this migration gets NO trigger unless its own migration adds
one (see the snippet in 063) — or unless a DDL event trigger is introduced to
automate it.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "158_harden_and_extend_audit_triggers"
down_revision: Union[str, None] = "157_add_approval_rule_mst"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EXCLUDED_TABLES = [
    "alembic_version",
    "audit_actor",
    "audit_event",
    "audit_field_change",
    "checkpoints",
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoint_migrations",
    "user_permission_cache",
]

# Column names whose VALUES must never be copied into audit_field_change.
# Matched case-insensitively as a substring, so token_hash, session_token,
# client_secret_hash, script_access_key and auth_config are all caught.
SENSITIVE_COLUMN_PATTERN = (
    "(password|passwd|secret|token|api_key|apikey|access_key"
    "|private_key|credential|auth_config)"
)

HARDENED_FUNCTION = f"""
CREATE OR REPLACE FUNCTION audit_capture_field_changes()
RETURNS TRIGGER AS $$
DECLARE
    event_id_var BIGINT;
    old_json JSONB;
    new_json JSONB;
    field_key TEXT;
    old_val TEXT;
    new_val TEXT;
BEGIN
    -- event_id comes from the PostgreSQL session variable the audit
    -- middleware sets on this connection before the UPDATE.
    BEGIN
        event_id_var := current_setting('app.audit_event_id', true)::bigint;
    EXCEPTION
        WHEN OTHERS THEN
            event_id_var := NULL;
    END;

    -- No event_id => the write did not come through an audited HTTP request
    -- (background job, Temporal activity, manual SQL). Skip silently.
    IF event_id_var IS NULL THEN
        RETURN NEW;
    END IF;

    old_json := to_jsonb(OLD);
    new_json := to_jsonb(NEW);

    FOR field_key IN SELECT jsonb_object_keys(new_json)
    LOOP
        -- System/meta fields that change on every write.
        IF field_key IN ('created_at', 'updated_at', 'id') THEN
            CONTINUE;
        END IF;

        -- Credential-bearing columns: never copy their values into the
        -- audit trail. The change is simply not recorded field-level; the
        -- audit_event row still shows that the row was updated, by whom.
        IF field_key ~* '{SENSITIVE_COLUMN_PATTERN}' THEN
            CONTINUE;
        END IF;

        old_val := old_json->>field_key;
        new_val := new_json->>field_key;

        IF old_val IS DISTINCT FROM new_val THEN
            INSERT INTO audit_field_change (
                event_id,
                field_name,
                old_value,
                new_value
            ) VALUES (
                event_id_var,
                field_key,
                old_val,
                new_val
            );
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
"""

# Restores the pre-158 behaviour (no sensitive-column skip).
ORIGINAL_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_capture_field_changes()
RETURNS TRIGGER AS $$
DECLARE
    event_id_var BIGINT;
    old_json JSONB;
    new_json JSONB;
    field_key TEXT;
    old_val TEXT;
    new_val TEXT;
BEGIN
    BEGIN
        event_id_var := current_setting('app.audit_event_id', true)::bigint;
    EXCEPTION
        WHEN OTHERS THEN
            event_id_var := NULL;
    END;

    IF event_id_var IS NULL THEN
        RETURN NEW;
    END IF;

    old_json := to_jsonb(OLD);
    new_json := to_jsonb(NEW);

    FOR field_key IN SELECT jsonb_object_keys(new_json)
    LOOP
        IF field_key IN ('created_at', 'updated_at', 'id') THEN
            CONTINUE;
        END IF;

        old_val := old_json->>field_key;
        new_val := new_json->>field_key;

        IF old_val IS DISTINCT FROM new_val THEN
            INSERT INTO audit_field_change (
                event_id,
                field_name,
                old_value,
                new_value
            ) VALUES (
                event_id_var,
                field_key,
                old_val,
                new_val
            );
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
"""


def _target_tables(connection) -> list:
    """Every public base table that should carry the audit trigger."""
    result = connection.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
    )
    return [row[0] for row in result if row[0] not in EXCLUDED_TABLES]


def upgrade() -> None:
    connection = op.get_bind()

    # 1. Harden the shared trigger function (covers already-triggered tables).
    op.execute(text(HARDENED_FUNCTION))

    # 2. Attach the trigger to every non-excluded table, idempotently.
    tables = _target_tables(connection)
    for table_name in tables:
        op.execute(
            text(
                f"""
                DROP TRIGGER IF EXISTS trg_audit_{table_name} ON {table_name};
                CREATE TRIGGER trg_audit_{table_name}
                    AFTER UPDATE ON {table_name}
                    FOR EACH ROW
                    EXECUTE FUNCTION audit_capture_field_changes();
                """
            )
        )

    print(
        f"Audit triggers present on {len(tables)} tables "
        f"({len(EXCLUDED_TABLES)} excluded); trigger function hardened"
    )


def downgrade() -> None:
    connection = op.get_bind()

    # Restore the un-hardened function.
    op.execute(text(ORIGINAL_FUNCTION))

    # Drop triggers from the tables that did NOT have one before 158, leaving
    # the 35 attached by migration 063 in place.
    kept_by_063 = {
        "alert_configs", "alerttype_ref", "applications_mst",
        "aws_secrets_parameters_mst", "case_ref", "case_type_ref", "chat_info",
        "chat_message", "chat_summary", "datadog_alert_query_ref",
        "geo_loc_mst", "gitops_workflow_detail", "infra_vendor_accounts_mst",
        "infrastructure_mst", "infrastructuretype_ref", "invitations_mst",
        "kong_route_configs", "language_ref", "monitoring_policy_defaults_ref",
        "monitoring_policy_overrides_mst", "obs_vendor_accounts_mst",
        "pipeline_mst", "pipeline_run_track", "pipeline_vendor_mst",
        "region_ref", "resource_group_mst", "role_mst", "service_configs",
        "service_dependency_map", "services_mst", "sidecar_configs",
        "team_mst", "tenants_mst", "user_mst", "zone_mst",
    }

    dropped = 0
    for table_name in _target_tables(connection):
        if table_name in kept_by_063:
            continue
        op.execute(
            text(f"DROP TRIGGER IF EXISTS trg_audit_{table_name} ON {table_name};")
        )
        dropped += 1

    print(f"Removed {dropped} audit triggers added by 158; function restored")
