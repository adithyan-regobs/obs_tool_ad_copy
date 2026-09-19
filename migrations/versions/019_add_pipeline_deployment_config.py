"""Add deployment_config to pipeline_mst

Revision ID: 019_add_pipeline_deployment_config
Revises: 018_replace_infra_vendor_accounts_with_infrastructure_mst
Create Date: 2025-11-06

This migration:
1. Adds deployment_config JSONB column to pipeline_mst table
2. Migrates existing data from authentication_config to deployment_config
3. Cleans up authentication_config to only keep github_pat
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a4f2e8d1c9b3'  # 019: Add deployment_config
down_revision: Union[str, None] = '018_infra_mst_fk'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Add deployment_config column and migrate data.

    Before:
    authentication_config = {
        "aws_role_arn": "...",
        "ecr_repo": "...",
        "github_pat": "...",
        "github_commit_sha": "...",
        "workflow_file_path": "..."
    }

    After:
    authentication_config = {"github_pat": "..."}
    deployment_config = {
        "ecr_repo_url": "...",
        "iam_role_arn": "...",
        "github_commit_sha": "...",
        "workflow_file_path": "..."
    }
    """
    # Step 1: Add deployment_config column
    op.add_column(
        'pipeline_mst',
        sa.Column('deployment_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )

    # Step 2: Migrate existing data
    connection = op.get_bind()
    connection.execute(sa.text("""
        UPDATE pipeline_mst
        SET deployment_config = jsonb_build_object(
            'ecr_repo_url', authentication_config->>'ecr_repo',
            'iam_role_arn', authentication_config->>'aws_role_arn',
            'github_commit_sha', authentication_config->>'github_commit_sha',
            'workflow_file_path', authentication_config->>'workflow_file_path'
        ),
        authentication_config = jsonb_build_object(
            'github_pat', authentication_config->>'github_pat'
        )
        WHERE authentication_config IS NOT NULL
    """))


def downgrade() -> None:
    """
    Reverse the migration by merging deployment_config back into authentication_config.
    """
    # Step 1: Reverse data migration
    connection = op.get_bind()
    connection.execute(sa.text("""
        UPDATE pipeline_mst
        SET authentication_config = jsonb_build_object(
            'github_pat', authentication_config->>'github_pat',
            'ecr_repo', deployment_config->>'ecr_repo_url',
            'aws_role_arn', deployment_config->>'iam_role_arn',
            'github_commit_sha', deployment_config->>'github_commit_sha',
            'workflow_file_path', deployment_config->>'workflow_file_path'
        )
        WHERE deployment_config IS NOT NULL
    """))

    # Step 2: Drop deployment_config column
    op.drop_column('pipeline_mst', 'deployment_config')
