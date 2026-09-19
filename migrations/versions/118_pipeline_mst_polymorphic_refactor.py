"""Refactor pipeline_mst to polymorphic transaction_code + table_name

Revision ID: 118_pipeline_mst_polymorphic_refactor
Revises: 117_add_variable_mst_scope_reference_datatype
Create Date: 2026-03-13

Replaces service_mst_code, environment, infrastructure_mst_code with:
  - transaction_code: polymorphic reference (service_config.code or infrastructure_mst.code)
  - table_name: discriminator (SERVICE_CONFIG, INFRASTRUCTURE, etc.)
  - tenant_code: direct tenant reference (replaces join through services_mst)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '118_pipeline_mst_polymorphic_refactor'
down_revision: Union[str, None] = '117_add_variable_mst_scope_reference_datatype'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Step 1: Add new columns as NULLABLE first ─────────────────────
    op.add_column(
        'pipeline_mst',
        sa.Column('transaction_code', sa.String(100), nullable=True,
                  comment='Polymorphic reference: service_config.code or infrastructure_mst.code')
    )
    # Reuse existing workflow_source_table_enum type (from transaction_queue)
    workflow_source_table_enum = sa.Enum(
        'SERVICE_CONFIG', 'SERVICE_CONFIG_DOCKERFILE', 'ALERT_CONFIG',
        'INFRASTRUCTURE', 'KONG_ROUTE', 'PIPELINE',
        name='workflow_source_table_enum', create_type=False
    )
    op.add_column(
        'pipeline_mst',
        sa.Column('table_name', workflow_source_table_enum, nullable=True,
                  comment='Discriminator: SERVICE_CONFIG, INFRASTRUCTURE, etc.')
    )
    op.add_column(
        'pipeline_mst',
        sa.Column('tenant_code', sa.String(100), nullable=True,
                  comment='Tenant code for direct tenant scoping')
    )

    # ── Step 2: Backfill existing rows ────────────────────────────────
    # Join pipeline_mst → service_configs (via service_mst_code + environment)
    # to resolve transaction_code = service_configs.code
    op.execute("""
        UPDATE pipeline_mst p
        SET transaction_code = sc.code,
            table_name = 'SERVICE_CONFIG',
            tenant_code = sm.tenants_mst_code
        FROM service_configs sc
        JOIN services_mst sm ON sm.code = sc.services_mst_code
        WHERE p.service_mst_code = sc.services_mst_code
          AND p.environment = sc.environment
          AND p.transaction_code IS NULL
    """)

    # Fallback for rows that didn't match a service_configs row
    # (use service_mst_code as transaction_code, resolve tenant from services_mst)
    op.execute("""
        UPDATE pipeline_mst p
        SET transaction_code = COALESCE(p.service_mst_code, 'UNKNOWN'),
            table_name = 'SERVICE_CONFIG',
            tenant_code = COALESCE(sm.tenants_mst_code, 'UNKNOWN')
        FROM services_mst sm
        WHERE p.service_mst_code = sm.code
          AND p.transaction_code IS NULL
    """)

    # Last resort: any remaining NULL rows (orphans with no services_mst match)
    op.execute("""
        UPDATE pipeline_mst
        SET transaction_code = COALESCE(service_mst_code, 'UNKNOWN'),
            table_name = 'SERVICE_CONFIG',
            tenant_code = 'UNKNOWN'
        WHERE transaction_code IS NULL
    """)

    # ── Step 3: Set NOT NULL after backfill ───────────────────────────
    op.alter_column('pipeline_mst', 'transaction_code', nullable=False)
    op.alter_column('pipeline_mst', 'table_name', nullable=False)
    op.alter_column('pipeline_mst', 'tenant_code', nullable=False)

    # ── Step 4: Drop old columns and constraints ─────────────────────
    # Drop infrastructure_mst_code FK + index
    op.drop_index('idx_pipeline_mst_infrastructure_mst_code', table_name='pipeline_mst')
    op.drop_constraint('fk_pipeline_mst_infrastructure_mst_code', 'pipeline_mst', type_='foreignkey')
    op.drop_column('pipeline_mst', 'infrastructure_mst_code')

    # Drop service_mst_code FK + column
    op.drop_constraint('pipeline_mst_service_mst_code_fkey', 'pipeline_mst', type_='foreignkey')
    op.drop_column('pipeline_mst', 'service_mst_code')

    # Drop environment column
    op.drop_column('pipeline_mst', 'environment')

    # ── Step 5: Add indexes ──────────────────────────────────────────
    op.create_index(
        'idx_pipeline_mst_transaction_code_table_name',
        'pipeline_mst',
        ['transaction_code', 'table_name']
    )
    op.create_index(
        'idx_pipeline_mst_tenant_code',
        'pipeline_mst',
        ['tenant_code']
    )


def downgrade() -> None:
    # Drop new indexes
    op.drop_index('idx_pipeline_mst_tenant_code', table_name='pipeline_mst')
    op.drop_index('idx_pipeline_mst_transaction_code_table_name', table_name='pipeline_mst')

    # Drop new columns first
    op.drop_column('pipeline_mst', 'tenant_code')
    op.drop_column('pipeline_mst', 'table_name')
    op.drop_column('pipeline_mst', 'transaction_code')

    # Re-add old columns
    op.add_column(
        'pipeline_mst',
        sa.Column('service_mst_code', sa.String(100), nullable=True)
    )
    op.add_column(
        'pipeline_mst',
        sa.Column('environment',
                  sa.Enum('dev', 'staging', 'prod', name='environment_enum', create_type=False),
                  nullable=True)
    )
    op.add_column(
        'pipeline_mst',
        sa.Column('infrastructure_mst_code', sa.String(100), nullable=True)
    )

    # Re-add FKs
    op.create_foreign_key(
        'pipeline_mst_service_mst_code_fkey', 'pipeline_mst', 'services_mst',
        ['service_mst_code'], ['code'], ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_pipeline_mst_infrastructure_mst_code',
        'pipeline_mst', 'infrastructure_mst',
        ['infrastructure_mst_code'], ['code'], ondelete='SET NULL'
    )
    op.create_index(
        'idx_pipeline_mst_infrastructure_mst_code',
        'pipeline_mst', ['infrastructure_mst_code']
    )
