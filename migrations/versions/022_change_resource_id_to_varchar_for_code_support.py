"""022_change_resource_id_to_varchar_for_code_support

Revision ID: 1d403db88b8d
Revises: c7d4e9f2a5b8
Create Date: 2025-11-13 18:50:17.216162

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1d403db88b8d'
down_revision: Union[str, Sequence[str], None] = 'c7d4e9f2a5b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Change audit_event.resource_id from BIGINT to VARCHAR(100) to support code-based identifiers.

    Background:
    - All tables in the system have both 'id' (BIGINT) and 'code' (VARCHAR 100) columns
    - All foreign keys use 'code', not 'id'
    - All URLs use 'code' values as identifiers (e.g., /update-policy-override/global_alb_unhealthy_fast_01)
    - All repository lookups use get_by_code(code: str)
    - Therefore, resource_id should store 'code' values, not numeric IDs
    """
    # Change resource_id from BIGINT to VARCHAR(100) in audit_event table
    op.alter_column(
        'audit_event',
        'resource_id',
        existing_type=sa.BigInteger(),
        type_=sa.String(100),
        existing_nullable=True,
        postgresql_using='resource_id::varchar(100)'
    )

    # Update the trigger function to read event_id from PostgreSQL session variable
    # Middleware sets this variable BEFORE the UPDATE using the same database connection
    op.execute('''
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
            -- Read event_id from PostgreSQL session variable
            -- Middleware sets this using: set_config('app.audit_event_id', '12345', false)
            -- false = session-level (not transaction-level), persists across transactions in same connection
            BEGIN
                event_id_var := current_setting('app.audit_event_id', true)::bigint;
            EXCEPTION
                WHEN OTHERS THEN
                    event_id_var := NULL;
            END;

            -- If no event_id in session, skip silently (non-audited operation)
            IF event_id_var IS NULL THEN
                RETURN NEW;
            END IF;

            -- Convert OLD and NEW records to JSONB for easy comparison
            old_json := to_jsonb(OLD);
            new_json := to_jsonb(NEW);

            -- Loop through all fields in the new record
            FOR field_key IN SELECT jsonb_object_keys(new_json)
            LOOP
                -- Skip system/meta fields that change automatically
                IF field_key IN ('created_at', 'updated_at', 'id') THEN
                    CONTINUE;
                END IF;

                -- Get old and new values as text for comparison
                old_val := old_json->>field_key;
                new_val := new_json->>field_key;

                -- Only log if values actually changed
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
    ''')


def downgrade() -> None:
    """Revert resource_id back to BIGINT and restore original trigger"""
    # Change resource_id from VARCHAR(100) back to BIGINT
    op.alter_column(
        'audit_event',
        'resource_id',
        existing_type=sa.String(100),
        type_=sa.BigInteger(),
        existing_nullable=True,
        postgresql_using='CASE WHEN resource_id ~ \'^[0-9]+$\' THEN resource_id::bigint ELSE NULL END'
    )

    # Restore original trigger function (without the ::varchar cast)
    op.execute('''
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
            -- Convert OLD and NEW records to JSONB for easy comparison
            old_json := to_jsonb(OLD);
            new_json := to_jsonb(NEW);

            -- Find the most recent audit_event for this resource
            SELECT ae.event_id INTO event_id_var
            FROM audit_event ae
            WHERE ae.resource_type = TG_TABLE_NAME
              AND ae.resource_id = NEW.id
              AND ae.event_name IN ('UPDATE')
            ORDER BY ae.event_time DESC
            LIMIT 1;

            -- If no audit_event found, skip silently
            IF event_id_var IS NULL THEN
                RETURN NEW;
            END IF;

            -- Loop through all fields in the new record
            FOR field_key IN SELECT jsonb_object_keys(new_json)
            LOOP
                -- Skip system/meta fields that change automatically
                IF field_key IN ('created_at', 'updated_at', 'id') THEN
                    CONTINUE;
                END IF;

                -- Get old and new values as text for comparison
                old_val := old_json->>field_key;
                new_val := new_json->>field_key;

                -- Only log if values actually changed
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
    ''')
