"""replace_audit_log_with_3_table_design

Revision ID: 021_replace_audit_log
Revises: b5c3d2e1f4a6
Create Date: 2025-11-12 17:43:25.763662

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c7d4e9f2a5b8'  # 021: Replace audit_log with 3-table design
down_revision: Union[str, Sequence[str], None] = 'b5c3d2e1f4a6'  # Previous: 020 Add audit_log
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Replace single audit_log table with 3-table normalized design:
    - audit_actor: stores who performed the action
    - audit_event: stores what happened, when, and where
    - audit_field_change: stores field-level changes for UPDATE operations
    """

    # Drop old audit_log table (we're rebuilding from scratch)
    op.drop_table('audit_log')

    # Create audit_actor table
    op.create_table(
        'audit_actor',
        sa.Column('actor_id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True, comment='Foreign key to user_mst table'),
        sa.Column('username', sa.String(length=100), nullable=True, comment='Username or email for quick reference'),
        sa.Column('role', sa.String(length=100), nullable=True, comment='User role at the time of action (admin, user, api_client, system)'),
        sa.Column('ip_address', postgresql.INET(), nullable=True, comment='IP address of the actor'),
        sa.Column('user_agent', sa.String(length=500), nullable=True, comment='User agent string (browser, API client, etc.)'),
        sa.ForeignKeyConstraint(['user_id'], ['user_mst.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('actor_id')
    )
    op.create_index('idx_audit_actor_user_id', 'audit_actor', ['user_id'])
    op.create_index('idx_audit_actor_username', 'audit_actor', ['username'])
    op.create_index('idx_audit_actor_ip', 'audit_actor', ['ip_address'])

    # Create audit_event table
    # Note: Using postgresql.ENUM with create_type=False to reference existing enum from migration 020
    audit_action_enum_type = postgresql.ENUM('CREATE', 'READ', 'UPDATE', 'DELETE', 'LOGIN', 'LOGOUT', 'LOGIN_FAILED', 'PASSWORD_CHANGE', 'PASSWORD_RESET', 'PERMISSION_GRANT', 'PERMISSION_REVOKE', 'ROLE_ASSIGN', 'ROLE_REMOVE', 'EXPORT', 'REPORT_GENERATE', 'CONFIG_CHANGE', 'CONFIG_RESET', 'INTEGRATION_ENABLE', 'INTEGRATION_DISABLE', 'INTEGRATION_SYNC', 'PIPELINE_START', 'PIPELINE_SUCCESS', 'PIPELINE_FAILED', 'PIPELINE_CANCELLED', 'ALERT_TRIGGERED', 'ALERT_RESOLVED', 'ALERT_ACKNOWLEDGED', 'ALERT_SILENCED', 'SYSTEM_START', 'SYSTEM_STOP', 'SYSTEM_ERROR', 'BULK_IMPORT', 'BULK_EXPORT', 'BULK_DELETE', name='audit_action_enum', create_type=False)

    op.create_table(
        'audit_event',
        sa.Column('event_id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('event_time', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the event occurred'),
        sa.Column('event_name', audit_action_enum_type, nullable=False, comment='Type of action: CREATE, UPDATE, DELETE, LOGIN, etc.'),
        sa.Column('event_source', sa.String(length=200), nullable=True, comment='Source of the event: API endpoint, background job, system, etc.'),
        sa.Column('actor_id', sa.BigInteger(), nullable=False, comment='Foreign key to audit_actor table'),
        sa.Column('resource_type', sa.String(length=100), nullable=False, comment='Type of resource: service, application, user, alert_policy, etc.'),
        sa.Column('resource_id', sa.BigInteger(), nullable=True, comment='ID of the affected resource'),
        sa.Column('status', sa.String(length=50), nullable=True, comment='Status of the event: success, failure, pending, etc.'),
        sa.Column('correlation_id', sa.String(length=100), nullable=True, comment='Correlation ID for distributed tracing'),
        sa.Column('tenants_mst_code', sa.String(length=100), nullable=False, comment='Tenant code for data isolation'),
        sa.Column('request_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Request payload (sanitized - no passwords/secrets)'),
        sa.Column('response_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Response payload (sanitized)'),
        sa.ForeignKeyConstraint(['actor_id'], ['audit_actor.actor_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenants_mst_code'], ['tenants_mst.code'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('event_id')
    )
    op.create_index('idx_audit_event_time', 'audit_event', ['event_time'])
    op.create_index('idx_audit_event_name', 'audit_event', ['event_name'])
    op.create_index('idx_audit_event_actor', 'audit_event', ['actor_id'])
    op.create_index('idx_audit_event_resource', 'audit_event', ['resource_type', 'resource_id'])
    op.create_index('idx_audit_event_tenant', 'audit_event', ['tenants_mst_code'])
    op.create_index('idx_audit_event_tenant_time', 'audit_event', ['tenants_mst_code', 'event_time'])
    op.create_index('idx_audit_event_correlation', 'audit_event', ['correlation_id'])
    op.create_index('idx_audit_event_status', 'audit_event', ['status'])

    # Create audit_field_change table
    op.create_table(
        'audit_field_change',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('event_id', sa.BigInteger(), nullable=False, comment='Foreign key to audit_event table'),
        sa.Column('field_name', sa.String(length=200), nullable=False, comment='Name of the field that changed'),
        sa.Column('old_value', sa.Text(), nullable=True, comment='Previous value of the field (as string)'),
        sa.Column('new_value', sa.Text(), nullable=True, comment='New value of the field (as string)'),
        sa.ForeignKeyConstraint(['event_id'], ['audit_event.event_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_audit_field_change_event', 'audit_field_change', ['event_id'])
    op.create_index('idx_audit_field_change_field', 'audit_field_change', ['field_name'])


def downgrade() -> None:
    """
    Downgrade by dropping the 3 new tables and recreating the old audit_log table.
    Note: This will lose all audit data in the new schema.
    """

    # Drop new tables
    op.drop_table('audit_field_change')
    op.drop_table('audit_event')
    op.drop_table('audit_actor')

    # Recreate old audit_log table (simplified version - adjust if needed)
    op.create_table(
        'audit_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_mst_id', sa.BigInteger(), nullable=True),
        sa.Column('user_email', sa.String(length=100), nullable=True),
        sa.Column('tenants_mst_code', sa.String(length=100), nullable=False),
        sa.Column('action_type', sa.Enum('CREATE', 'READ', 'UPDATE', 'DELETE', 'LOGIN', 'LOGOUT', 'LOGIN_FAILED', 'PASSWORD_CHANGE', 'PASSWORD_RESET', 'PERMISSION_GRANT', 'PERMISSION_REVOKE', 'ROLE_ASSIGN', 'ROLE_REMOVE', 'EXPORT', 'REPORT_GENERATE', 'CONFIG_CHANGE', 'CONFIG_RESET', 'INTEGRATION_ENABLE', 'INTEGRATION_DISABLE', 'INTEGRATION_SYNC', 'PIPELINE_START', 'PIPELINE_SUCCESS', 'PIPELINE_FAILED', 'PIPELINE_CANCELLED', 'ALERT_TRIGGERED', 'ALERT_RESOLVED', 'ALERT_ACKNOWLEDGED', 'ALERT_SILENCED', 'SYSTEM_START', 'SYSTEM_STOP', 'SYSTEM_ERROR', 'BULK_IMPORT', 'BULK_EXPORT', 'BULK_DELETE', name='audit_action_enum', create_type=False), nullable=False),
        sa.Column('entity_type', sa.String(length=100), nullable=False),
        sa.Column('entity_id', sa.BigInteger(), nullable=True),
        sa.Column('entity_code', sa.String(length=100), nullable=True),
        sa.Column('old_values', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('new_values', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('changes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('ip_address', postgresql.INET(), nullable=True),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('request_id', sa.String(length=100), nullable=True),
        sa.Column('request_method', sa.String(length=10), nullable=True),
        sa.Column('request_path', sa.String(length=500), nullable=True),
        sa.Column('additional_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_mst_id'], ['user_mst.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenants_mst_code'], ['tenants_mst.code'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_audit_log_user', 'audit_log', ['user_mst_id'])
    op.create_index('idx_audit_log_tenant', 'audit_log', ['tenants_mst_code'])
    op.create_index('idx_audit_log_entity', 'audit_log', ['entity_type', 'entity_id'])
    op.create_index('idx_audit_log_action', 'audit_log', ['action_type'])
    op.create_index('idx_audit_log_timestamp', 'audit_log', ['timestamp'])
    op.create_index('idx_audit_log_tenant_timestamp', 'audit_log', ['tenants_mst_code', 'timestamp'])
    op.create_index('idx_audit_log_entity_code', 'audit_log', ['entity_code'])
