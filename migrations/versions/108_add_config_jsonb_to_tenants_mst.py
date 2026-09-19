"""Add JSONB config column to tenants_mst

Revision ID: 108_add_config_jsonb_to_tenants_mst
Revises: 107_create_github_app_installation_mst
Create Date: 2026-03-06

Stores per-tenant configuration (GitHub repos, branches, etc.)
so each tenant can have its own infra repository and branch settings.

Example config:
{
    "github": {
        "infra_repository": "org/repo",
        "infra_branch": "main"
    }
}
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = '108_add_config_jsonb_to_tenants_mst'
down_revision: Union[str, None] = '107_create_github_app_installation_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tenants_mst',
        sa.Column('config', JSONB, nullable=True, server_default='{}')
    )


def downgrade() -> None:
    op.drop_column('tenants_mst', 'config')
