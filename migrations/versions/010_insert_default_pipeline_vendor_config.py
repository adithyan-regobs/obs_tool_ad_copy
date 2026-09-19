"""Insert default pipeline vendor configurations

Revision ID: 010_default_pipeline_vendor
Revises: 009_pipeline_vendor_hierarchy
Create Date: 2025-11-05 17:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from sqlalchemy import String, Text
import json
import os

# revision identifiers, used by Alembic.
revision: str = '010_default_pipeline_vendor'
down_revision: Union[str, None] = '009_pipeline_vendor_hierarchy'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Insert default pipeline vendor configurations for GitHub Actions"""

    # Get GitHub token from environment
    github_token = os.getenv('GITHUB_TOKEN', '')

    # Prepare auth config with GitHub PAT
    auth_config_json = json.dumps({'github_pat': github_token})

    # Insert default configurations for all environments using raw SQL
    environments = [
        ('dev', 'Dev'),
        ('staging', 'Staging'),
        ('prod', 'Prod')
    ]

    for env_code, env_name in environments:
        op.execute(f"""
            INSERT INTO pipeline_vendor_mst (
                code, name, description, tenants_mst_code, environment,
                auth_config, pipeline_agent_enum, is_active
            ) VALUES (
                'github_actions_techstart_{env_code}',
                'GitHub Actions for TechStart Inc ({env_name})',
                'Default GitHub Actions configuration for {env_code} environment',
                'techstart_inc',
                '{env_code}',
                '{auth_config_json}'::jsonb,
                'github_actions',
                true
            )
            ON CONFLICT (code) DO NOTHING;
        """)


def downgrade() -> None:
    """Remove default pipeline vendor configurations"""

    # Delete the inserted configurations
    op.execute(
        """
        DELETE FROM pipeline_vendor_mst
        WHERE code IN (
            'github_actions_techstart_dev',
            'github_actions_techstart_staging',
            'github_actions_techstart_prod'
        )
        """
    )
