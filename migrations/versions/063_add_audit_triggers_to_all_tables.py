"""Add audit triggers to all tables

Revision ID: 063_add_audit_triggers_to_all_tables
Revises: 062_add_sync_status_to_service_configs
Create Date: 2025-12-05

This migration adds AFTER UPDATE triggers to all existing tables to capture
field-level changes for the audit trail system.

The trigger function `audit_capture_field_changes()` was created in migration 022.
This migration attaches that function as a trigger to all tables.

IMPORTANT: When creating NEW tables in future migrations, add the trigger like this:

    op.execute('''
        DROP TRIGGER IF EXISTS trg_audit_your_new_table ON your_new_table;
        CREATE TRIGGER trg_audit_your_new_table
            AFTER UPDATE ON your_new_table
            FOR EACH ROW
            EXECUTE FUNCTION audit_capture_field_changes();
    ''')
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '063_add_audit_triggers_to_all_tables'
down_revision: Union[str, None] = '062_add_sync_status_to_service_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables to exclude from audit triggers
EXCLUDED_TABLES = [
    'alembic_version',    # Alembic system table
    'audit_actor',        # Part of audit system itself
    'audit_event',        # Part of audit system itself
    'audit_field_change', # Part of audit system itself
]


def upgrade() -> None:
    """Add audit triggers to all existing tables."""

    # Get database connection
    connection = op.get_bind()

    # Get all table names from public schema
    result = connection.execute(
        text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
    )
    tables = [row[0] for row in result]

    # Add trigger to each table (except excluded ones)
    for table_name in tables:
        if table_name in EXCLUDED_TABLES:
            continue

        trigger_name = f'trg_audit_{table_name}'

        # Drop existing trigger if exists, then create new one
        # This makes the migration idempotent (safe to run multiple times)
        op.execute(text(f'''
            DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};
            CREATE TRIGGER {trigger_name}
                AFTER UPDATE ON {table_name}
                FOR EACH ROW
                EXECUTE FUNCTION audit_capture_field_changes();
        '''))

    print(f"Added audit triggers to {len(tables) - len(EXCLUDED_TABLES)} tables")


def downgrade() -> None:
    """Remove all audit triggers from tables."""

    # Get database connection
    connection = op.get_bind()

    # Get all table names from public schema
    result = connection.execute(
        text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
    )
    tables = [row[0] for row in result]

    # Remove trigger from each table
    for table_name in tables:
        if table_name in EXCLUDED_TABLES:
            continue

        trigger_name = f'trg_audit_{table_name}'
        op.execute(text(f'DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};'))

    print(f"Removed audit triggers from {len(tables) - len(EXCLUDED_TABLES)} tables")
