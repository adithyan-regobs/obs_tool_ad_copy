"""Add db_object_mst and db_permission_mst tables

Revision ID: 115_add_db_object_and_permission_mst
Revises: 114_refactor_aws_secrets_parameters_mst_drop_services_fk_add_resource_code
Create Date: 2026-03-13

db_object_mst     : Hierarchical tree of database objects (database → schema → table/view/etc.)
                    per K8s-hosted server. Self-referential parent_id enables arbitrary depth.
db_permission_mst : Access control per object level. db_object_mst_id=null means server-level
                    admin; otherwise scoped to a specific database, schema, or table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = '115_add_db_object_and_permission_mst'
down_revision: Union[str, None] = '114_refactor_aws_secrets_parameters_mst_drop_services_fk_add_resource_code'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── db_object_mst ──────────────────────────────────────────────────────────
    op.create_table(
        'db_object_mst',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True, default=False),
        sa.Column('is_active', sa.Boolean(), nullable=True, default=True),
        sa.Column('infrastructure_mst_code', sa.String(100), nullable=False,
                  comment='FK to the server (infrastructure_mst) that owns this object'),
        sa.Column('type', sa.String(50), nullable=False,
                  comment='Object type: database, schema, table, view, sequence, function, etc.'),
        sa.Column('parent_id', sa.BigInteger(), nullable=True,
                  comment='Self-referential FK — null for top-level databases'),
        sa.Column('metadata', JSONB(), nullable=True,
                  comment='Type-specific properties: encoding, owner, collation, etc.'),
        sa.Column('tenant_code', sa.String(100), nullable=False),
        sa.Column('environment', sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.ForeignKeyConstraint(['infrastructure_mst_code'], ['infrastructure_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_code'], ['tenants_mst.code'], ondelete='CASCADE'),
    )
    # Self-referential FK added after table creation to avoid forward-reference issues
    op.create_foreign_key(
        'fk_db_object_mst_parent_id',
        'db_object_mst', 'db_object_mst',
        ['parent_id'], ['id'],
        ondelete='CASCADE',
    )
    op.create_index('ix_db_object_mst_infrastructure_mst_code', 'db_object_mst', ['infrastructure_mst_code'])
    op.create_index('ix_db_object_mst_parent_id', 'db_object_mst', ['parent_id'])
    op.create_index('ix_db_object_mst_type', 'db_object_mst', ['type'])

    # ── db_permission_mst ──────────────────────────────────────────────────────
    op.create_table(
        'db_permission_mst',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True, default=False),
        sa.Column('is_active', sa.Boolean(), nullable=True, default=True),
        sa.Column('infrastructure_mst_code', sa.String(100), nullable=False,
                  comment='FK to the server (infrastructure_mst) this permission belongs to'),
        sa.Column('db_object_mst_id', sa.BigInteger(), nullable=True,
                  comment='FK to db_object_mst — null means server-level permission'),
        sa.Column('username', sa.String(255), nullable=False,
                  comment='PostgreSQL username this permission applies to'),
        sa.Column('permissions', JSONB(), nullable=False,
                  comment='Permission value — e.g. "admin" or ["read", "write"]'),
        sa.Column('tenant_code', sa.String(100), nullable=False),
        sa.Column('environment', sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.ForeignKeyConstraint(['infrastructure_mst_code'], ['infrastructure_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['db_object_mst_id'], ['db_object_mst.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_code'], ['tenants_mst.code'], ondelete='CASCADE'),
    )
    op.create_index('ix_db_permission_mst_infrastructure_mst_code', 'db_permission_mst', ['infrastructure_mst_code'])
    op.create_index('ix_db_permission_mst_db_object_mst_id', 'db_permission_mst', ['db_object_mst_id'])
    op.create_index('ix_db_permission_mst_username', 'db_permission_mst', ['username'])


def downgrade() -> None:
    op.drop_table('db_permission_mst')
    op.drop_constraint('fk_db_object_mst_parent_id', 'db_object_mst', type_='foreignkey')
    op.drop_table('db_object_mst')
