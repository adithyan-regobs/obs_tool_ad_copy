"""add sidecar_config table

Revision ID: 036_add_sidecar_config_table
Revises: 035_move_prompt_path
Create Date: 2025-11-25

Adds sidecar configuration table:
- New table: sidecar_configs (default sidecar container definitions)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, ENUM

# revision identifiers
revision = '036_add_sidecar_config_table'
down_revision = '035_move_prompt_path'
branch_labels = None
depends_on = None


def upgrade():
    # Note: environment_enum already exists in the database
    # We just reference it, not create it

    # ============================================================
    # CREATE TABLE: sidecar_configs
    # ============================================================
    op.create_table(
        'sidecar_configs',
        # Columns from BaseModel
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', TIMESTAMP(timezone=True), onupdate=sa.text('now()'), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=True),

        # Foreign Keys - REQUIRED for scoping
        sa.Column('applications_mst_code', sa.String(100), nullable=False,
                  comment="Application this sidecar belongs to"),
        sa.Column('resource_group_mst_code', sa.String(100), nullable=False,
                  comment="Resource group this sidecar belongs to"),
        sa.Column('environment',
                  ENUM('dev', 'staging', 'prod', name='environment_enum', create_type=False),
                  nullable=False,
                  comment="Environment (dev/staging/prod)"),

        # Configuration
        sa.Column('config', JSONB, nullable=False,
                  comment="Default configuration JSON: {cpu: '250', ram: '250'}"),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.UniqueConstraint('applications_mst_code', 'resource_group_mst_code', 'environment', 'name',
                          name='uq_sidecar_config_app_rg_env_name'),
        sa.CheckConstraint("config ? 'cpu' AND config ? 'ram'",
                          name='check_sidecar_config_has_cpu_ram')
    )

    # Create foreign key constraints for sidecar_configs
    op.create_foreign_key(
        'fk_sidecar_config_application',
        'sidecar_configs', 'applications_mst',
        ['applications_mst_code'], ['code'],
        ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_sidecar_config_resource_group',
        'sidecar_configs', 'resource_group_mst',
        ['resource_group_mst_code'], ['code'],
        ondelete='CASCADE'
    )

    # Create indexes for sidecar_configs
    op.create_index('idx_sidecar_config_application', 'sidecar_configs', ['applications_mst_code'])
    op.create_index('idx_sidecar_config_resource_group', 'sidecar_configs', ['resource_group_mst_code'])
    op.create_index('idx_sidecar_config_environment', 'sidecar_configs', ['environment'])


def downgrade():
    # Drop sidecar_configs
    op.drop_index('idx_sidecar_config_environment', 'sidecar_configs')
    op.drop_index('idx_sidecar_config_resource_group', 'sidecar_configs')
    op.drop_index('idx_sidecar_config_application', 'sidecar_configs')
    op.drop_constraint('fk_sidecar_config_resource_group', 'sidecar_configs', type_='foreignkey')
    op.drop_constraint('fk_sidecar_config_application', 'sidecar_configs', type_='foreignkey')
    op.drop_table('sidecar_configs')
