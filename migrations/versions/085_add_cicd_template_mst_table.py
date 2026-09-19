"""add cicd_template_mst table

Revision ID: 085_add_cicd_template_mst_table
Revises: 084_add_dockerfile_column_to_service_configs
Create Date: 2025-12-25

Adds CI/CD template management support:
- New table: cicd_template_mst (stores predefined CI/CD workflow templates)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP, JSONB

# revision identifiers
revision = '085_add_cicd_template_mst_table'
down_revision = '084_add_dockerfile_column_to_service_configs'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # CREATE TABLE: cicd_template_mst
    # ============================================================
    op.create_table(
        'cicd_template_mst',

        # Primary columns
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('tenant_mst_code', sa.String(100), nullable=False),
        sa.Column('applications_mst_code', sa.String(100), nullable=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('config', JSONB, nullable=False),
        sa.Column('template_type', sa.String(50), server_default='custom', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('is_system_template', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),

        # Constraints
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_cicd_template_code'),
        sa.ForeignKeyConstraint(
            ['tenant_mst_code'],
            ['tenants_mst.code'],
            name='fk_cicd_template_tenant',
            ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['applications_mst_code'],
            ['services_mst.code'],
            name='fk_cicd_template_service',
            ondelete='CASCADE'
        )
    )

    # Indexes for cicd_template_mst
    op.create_index(
        'idx_cicd_template_tenant',
        'cicd_template_mst',
        ['tenant_mst_code']
    )
    op.create_index(
        'idx_cicd_template_service',
        'cicd_template_mst',
        ['applications_mst_code']
    )
    op.create_index(
        'idx_cicd_template_type',
        'cicd_template_mst',
        ['template_type']
    )
    op.create_index(
        'idx_cicd_template_active',
        'cicd_template_mst',
        ['is_active']
    )


def downgrade():
    # ============================================================
    # DOWNGRADE: cicd_template_mst
    # ============================================================
    op.drop_index('idx_cicd_template_active', table_name='cicd_template_mst')
    op.drop_index('idx_cicd_template_type', table_name='cicd_template_mst')
    op.drop_index('idx_cicd_template_service', table_name='cicd_template_mst')
    op.drop_index('idx_cicd_template_tenant', table_name='cicd_template_mst')
    op.drop_table('cicd_template_mst')
