"""Add audit_log table for audit trail

Revision ID: 020_add_audit_log
Revises: a4f2e8d1c9b3
Create Date: 2025-11-12

This migration:
1. Creates audit_action_enum type in PostgreSQL
2. Creates audit_log table with all necessary columns and indexes
3. Sets up foreign keys to user_mst and tenants_mst
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b5c3d2e1f4a6'  # 020: Add audit_log table
down_revision: Union[str, None] = 'f3a0a85820e6'  # Previous head (merge deployment config)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Create audit_log table and audit_action_enum type.
    """

    # Create the audit_action_enum type
    # Note: checkfirst=True to avoid errors if it already exists
    audit_action_enum = postgresql.ENUM(
        'CREATE', 'READ', 'UPDATE', 'DELETE',
        'LOGIN', 'LOGOUT', 'LOGIN_FAILED', 'PASSWORD_CHANGE', 'PASSWORD_RESET',
        'PERMISSION_GRANT', 'PERMISSION_REVOKE', 'ROLE_ASSIGN', 'ROLE_REMOVE',
        'EXPORT', 'REPORT_GENERATE',
        'CONFIG_UPDATE',
        'INTEGRATION_CONNECT', 'INTEGRATION_DISCONNECT',
        'PIPELINE_TRIGGER', 'PIPELINE_CANCEL',
        'ALERT_CREATE', 'ALERT_UPDATE', 'ALERT_DELETE', 'ALERT_ACKNOWLEDGE',
        'SYSTEM_STARTUP', 'SYSTEM_SHUTDOWN',
        name='audit_action_enum',
        create_type=False  # Don't auto-create, we'll handle it manually
    )

    # Check if enum exists and create if it doesn't
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT 1 FROM pg_type WHERE typname = 'audit_action_enum'"
    ))
    if not result.fetchone():
        audit_action_enum.create(conn)

    # Create audit_log table
    op.create_table(
        'audit_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_mst_id', sa.BigInteger(), nullable=True),
        sa.Column('user_email', sa.String(length=100), nullable=True),
        sa.Column('tenants_mst_code', sa.String(length=100), nullable=False),
        sa.Column('action_type', audit_action_enum, nullable=False),
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
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_mst_id'], ['user_mst.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenants_mst_code'], ['tenants_mst.code'], ondelete='CASCADE'),
        comment='Audit log table for tracking all user actions and system events'
    )

    # Create indexes for efficient querying
    op.create_index('idx_audit_log_user', 'audit_log', ['user_mst_id'])
    op.create_index('idx_audit_log_tenant', 'audit_log', ['tenants_mst_code'])
    op.create_index('idx_audit_log_entity', 'audit_log', ['entity_type', 'entity_id'])
    op.create_index('idx_audit_log_action', 'audit_log', ['action_type'])
    op.create_index('idx_audit_log_timestamp', 'audit_log', ['timestamp'])
    op.create_index('idx_audit_log_tenant_timestamp', 'audit_log', ['tenants_mst_code', 'timestamp'])
    op.create_index('idx_audit_log_entity_code', 'audit_log', ['entity_code'])


def downgrade() -> None:
    """
    Drop audit_log table and audit_action_enum type.
    """

    # Drop indexes first
    op.drop_index('idx_audit_log_entity_code', table_name='audit_log')
    op.drop_index('idx_audit_log_tenant_timestamp', table_name='audit_log')
    op.drop_index('idx_audit_log_timestamp', table_name='audit_log')
    op.drop_index('idx_audit_log_action', table_name='audit_log')
    op.drop_index('idx_audit_log_entity', table_name='audit_log')
    op.drop_index('idx_audit_log_tenant', table_name='audit_log')
    op.drop_index('idx_audit_log_user', table_name='audit_log')

    # Drop the table
    op.drop_table('audit_log')

    # Drop the enum type
    audit_action_enum = postgresql.ENUM(name='audit_action_enum')
    audit_action_enum.drop(op.get_bind(), checkfirst=True)
