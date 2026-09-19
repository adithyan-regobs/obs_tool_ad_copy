"""Add version, pipeline_agent_enum, and yaml_url columns to language_ref

Revision ID: 008_language_ref_columns
Revises: 007_infra_vendor_fk
Create Date: 2025-01-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '008_language_ref_columns'
down_revision: Union[str, None] = '007_infra_vendor_accounts_fk'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add missing columns to language_ref table"""

    # Create pipeline_agent_enum type if it doesn't exist
    pipeline_agent_enum = postgresql.ENUM(
        'aws_codepipeline',
        'azure_devops',
        'github_actions',
        'gitlab_ci',
        'bitbucket_pipelines',
        'circleci',
        'travisci',
        'drone',
        'wercker',
        'buildkite',
        name='pipeline_agent_enum',
        create_type=True
    )
    pipeline_agent_enum.create(op.get_bind(), checkfirst=True)

    # Add version column
    op.add_column('language_ref',
        sa.Column('version', sa.String(length=50), nullable=False, server_default='1.0')
    )

    # Add pipeline_agent_enum column
    op.add_column('language_ref',
        sa.Column('pipeline_agent_enum',
                  postgresql.ENUM(
                      'aws_codepipeline',
                      'azure_devops',
                      'github_actions',
                      'gitlab_ci',
                      'bitbucket_pipelines',
                      'circleci',
                      'travisci',
                      'drone',
                      'wercker',
                      'buildkite',
                      name='pipeline_agent_enum',
                      create_type=False
                  ),
                  nullable=False,
                  server_default='github_actions'
        )
    )

    # Add yaml_url column
    op.add_column('language_ref',
        sa.Column('yaml_url', sa.String(length=500), nullable=True)
    )

    # Remove server defaults after adding columns
    op.alter_column('language_ref', 'version', server_default=None)
    op.alter_column('language_ref', 'pipeline_agent_enum', server_default=None)


def downgrade() -> None:
    """Remove added columns from language_ref table"""
    op.drop_column('language_ref', 'yaml_url')
    op.drop_column('language_ref', 'pipeline_agent_enum')
    op.drop_column('language_ref', 'version')

    # Drop the enum type
    pipeline_agent_enum = postgresql.ENUM(name='pipeline_agent_enum')
    pipeline_agent_enum.drop(op.get_bind(), checkfirst=True)
