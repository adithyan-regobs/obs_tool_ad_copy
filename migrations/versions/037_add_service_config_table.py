"""add service_config table

Revision ID: 037_add_service_config_table
Revises: 036_add_sidecar_config_table
Create Date: 2025-11-25

Adds service configuration table:
- New table: service_configs (service-specific configurations per environment)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, ENUM

# revision identifiers
revision = '037_add_service_config_table'
down_revision = '036_add_sidecar_config_table'
branch_labels = None
depends_on = None


def upgrade():
    # Note: environment_enum already exists in the database
    # We just reference it, not create it

    # ============================================================
    # CREATE TABLE: service_configs
    # ============================================================
    op.create_table(
        'service_configs',
        # Columns from BaseModel
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', TIMESTAMP(timezone=True), onupdate=sa.text('now()'), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=True),

        # Foreign Keys
        sa.Column('services_mst_code', sa.String(100), nullable=False,
                  comment="Service this configuration belongs to"),
        sa.Column('infrastructuretype_ref_code', sa.String(100), nullable=False,
                  comment="Infrastructure type (ECS, Lambda, EC2, etc.)"),
        sa.Column('environment',
                  ENUM('dev', 'staging', 'prod', name='environment_enum', create_type=False),
                  nullable=False,
                  comment="Environment (dev/staging/prod)"),
        sa.Column('language_ref_code', sa.String(100), nullable=True,
                  comment="Language reference FK - for relationship to language_ref table"),

        # Configuration fields (JSONB)
        sa.Column('app_info', JSONB, nullable=True,
                  comment="App info copied from language_ref: {language: 'Java', version: '17'}"),
        sa.Column('config', JSONB, nullable=True,
                  comment="Main service configuration: {cpu, ram, port, health, min, max, desired, ebs: {volume, size, type}}"),
        sa.Column('sidecar_config', JSONB, nullable=True, server_default='[]',
                  comment="Sidecar overrides: [{sidecar_config_code, cpu, ram}] - name comes from JOIN with sidecar_configs table"),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.UniqueConstraint('services_mst_code', 'environment',
                          name='uq_service_config_service_env')
    )

    # Create foreign key constraints for service_configs
    op.create_foreign_key(
        'fk_service_config_service',
        'service_configs', 'services_mst',
        ['services_mst_code'], ['code'],
        ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_service_config_infrastructure_type',
        'service_configs', 'infrastructuretype_ref',
        ['infrastructuretype_ref_code'], ['code']
    )
    op.create_foreign_key(
        'fk_service_config_language_ref',
        'service_configs', 'language_ref',
        ['language_ref_code'], ['code']
    )

    # Create indexes for service_configs
    op.create_index('idx_service_config_service', 'service_configs', ['services_mst_code'])
    op.create_index('idx_service_config_environment', 'service_configs', ['environment'])
    op.create_index('idx_service_config_infrastructure_type', 'service_configs', ['infrastructuretype_ref_code'])
    op.create_index('idx_service_config_language_ref', 'service_configs', ['language_ref_code'])


def downgrade():
    # Drop service_configs
    op.drop_index('idx_service_config_language_ref', 'service_configs')
    op.drop_index('idx_service_config_infrastructure_type', 'service_configs')
    op.drop_index('idx_service_config_environment', 'service_configs')
    op.drop_index('idx_service_config_service', 'service_configs')
    op.drop_constraint('fk_service_config_language_ref', 'service_configs', type_='foreignkey')
    op.drop_constraint('fk_service_config_infrastructure_type', 'service_configs', type_='foreignkey')
    op.drop_constraint('fk_service_config_service', 'service_configs', type_='foreignkey')
    op.drop_table('service_configs')
