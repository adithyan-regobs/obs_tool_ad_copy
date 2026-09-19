"""Add hierarchy columns to pipeline_vendor_mst

Revision ID: 009_pipeline_vendor_hierarchy
Revises: 008_language_ref_columns
Create Date: 2025-11-05 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '009_pipeline_vendor_hierarchy'
down_revision: Union[str, None] = '008_language_ref_columns'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add hierarchy columns to pipeline_vendor_mst for hierarchical configuration"""

    # Add hierarchy foreign key columns
    op.add_column('pipeline_vendor_mst',
        sa.Column('tenants_mst_code', sa.String(length=100), nullable=True)
    )
    op.add_column('pipeline_vendor_mst',
        sa.Column('applications_mst_code', sa.String(length=100), nullable=True)
    )
    op.add_column('pipeline_vendor_mst',
        sa.Column('resource_group_mst_code', sa.String(length=100), nullable=True)
    )
    op.add_column('pipeline_vendor_mst',
        sa.Column('service_mst_code', sa.String(length=100), nullable=True)
    )

    # Add configuration columns
    op.add_column('pipeline_vendor_mst',
        sa.Column('environment',
                  postgresql.ENUM('dev', 'staging', 'prod', name='environment_enum', create_type=False),
                  nullable=True)
    )
    op.add_column('pipeline_vendor_mst',
        sa.Column('auth_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column('pipeline_vendor_mst',
        sa.Column('runner_info_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column('pipeline_vendor_mst',
        sa.Column('pipeline_agent_enum',
                  postgresql.ENUM('aws_codepipeline', 'azure_devops', 'github_actions', 'gitlab_ci',
                                  'bitbucket_pipelines', 'circleci', 'travisci', 'drone', 'wercker', 'buildkite',
                                  name='pipeline_agent_enum', create_type=False),
                  nullable=True)
    )

    # Create foreign key constraints
    op.create_foreign_key(
        'fk_pipeline_vendor_mst_tenants_mst_code',
        'pipeline_vendor_mst', 'tenants_mst',
        ['tenants_mst_code'], ['code'],
        ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_pipeline_vendor_mst_applications_mst_code',
        'pipeline_vendor_mst', 'applications_mst',
        ['applications_mst_code'], ['code'],
        ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_pipeline_vendor_mst_resource_group_mst_code',
        'pipeline_vendor_mst', 'resource_group_mst',
        ['resource_group_mst_code'], ['code'],
        ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_pipeline_vendor_mst_service_mst_code',
        'pipeline_vendor_mst', 'services_mst',
        ['service_mst_code'], ['code'],
        ondelete='CASCADE'
    )

    # Make tenants_mst_code and environment NOT NULL after adding columns
    # (we'll update existing rows first if needed)
    op.alter_column('pipeline_vendor_mst', 'tenants_mst_code', nullable=False)
    op.alter_column('pipeline_vendor_mst', 'environment', nullable=False)
    op.alter_column('pipeline_vendor_mst', 'pipeline_agent_enum', nullable=False)


def downgrade() -> None:
    """Remove hierarchy columns from pipeline_vendor_mst"""

    # Drop foreign key constraints
    op.drop_constraint('fk_pipeline_vendor_mst_service_mst_code', 'pipeline_vendor_mst', type_='foreignkey')
    op.drop_constraint('fk_pipeline_vendor_mst_resource_group_mst_code', 'pipeline_vendor_mst', type_='foreignkey')
    op.drop_constraint('fk_pipeline_vendor_mst_applications_mst_code', 'pipeline_vendor_mst', type_='foreignkey')
    op.drop_constraint('fk_pipeline_vendor_mst_tenants_mst_code', 'pipeline_vendor_mst', type_='foreignkey')

    # Drop columns
    op.drop_column('pipeline_vendor_mst', 'pipeline_agent_enum')
    op.drop_column('pipeline_vendor_mst', 'runner_info_config')
    op.drop_column('pipeline_vendor_mst', 'auth_config')
    op.drop_column('pipeline_vendor_mst', 'environment')
    op.drop_column('pipeline_vendor_mst', 'service_mst_code')
    op.drop_column('pipeline_vendor_mst', 'resource_group_mst_code')
    op.drop_column('pipeline_vendor_mst', 'applications_mst_code')
    op.drop_column('pipeline_vendor_mst', 'tenants_mst_code')
