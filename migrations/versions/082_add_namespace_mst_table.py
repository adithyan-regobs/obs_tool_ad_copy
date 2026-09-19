"""add namespace_mst table

Revision ID: 082_add_namespace_mst_table
Revises: 081_add_deployment_strategy_to_service_configs
Create Date: 2025-01-19

Adds namespace master table for EKS namespace management:
- New table: namespace_mst (stores K8s namespaces per infrastructure/cluster)
- Foreign key to infrastructure_mst
- Used for dropdown selection in EKS deployment configuration
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

# revision identifiers
revision = '082_add_namespace_mst_table'
down_revision = '081_add_deployment_strategy_to_service_configs'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # CREATE TABLE: namespace_mst
    # ============================================================
    op.create_table(
        'namespace_mst',

        # BaseModel columns
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', TIMESTAMP(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=True),

        # Namespace-specific columns
        sa.Column('infrastructure_mst_code', sa.String(100), nullable=False),
        sa.Column('namespace', sa.String(255), nullable=False),

        # Constraints
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_namespace_mst_code'),
        sa.ForeignKeyConstraint(
            ['infrastructure_mst_code'],
            ['infrastructure_mst.code'],
            ondelete='CASCADE'
        ),
    )

    # Create index for faster lookups by infrastructure
    op.create_index(
        'ix_namespace_mst_infrastructure_mst_code',
        'namespace_mst',
        ['infrastructure_mst_code']
    )

    # Create unique constraint for namespace per infrastructure (prevent duplicates)
    op.create_unique_constraint(
        'uq_namespace_mst_infra_namespace',
        'namespace_mst',
        ['infrastructure_mst_code', 'namespace']
    )


def downgrade():
    op.drop_constraint('uq_namespace_mst_infra_namespace', 'namespace_mst', type_='unique')
    op.drop_index('ix_namespace_mst_infrastructure_mst_code', 'namespace_mst')
    op.drop_table('namespace_mst')
