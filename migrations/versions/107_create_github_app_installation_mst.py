"""Create github_app_installation_mst table

Revision ID: 107_create_github_app_installation_mst
Revises: 106_add_stage_qa_to_environment_enum
Create Date: 2026-03-06

Stores per-tenant GitHub App installations so we can generate
scoped installation tokens for each GitHub org without a PAT.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '107_create_github_app_installation_mst'
down_revision: Union[str, None] = '106_add_stage_qa_to_environment_enum'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'github_app_installation_mst',
        # BaseModel columns
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False, unique=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('is_deleted', sa.Boolean(), server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true')),
        # GitHubAppInstallationMst-specific columns
        sa.Column('tenant_code', sa.String(100), nullable=False, index=True),
        sa.Column('github_org', sa.String(255), nullable=False, index=True),
        sa.Column('installation_id', sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('github_org', name='uq_github_app_installation_mst_org'),
    )


def downgrade() -> None:
    op.drop_table('github_app_installation_mst')
