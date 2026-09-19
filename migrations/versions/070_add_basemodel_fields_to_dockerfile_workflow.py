"""Add BaseModel fields to service_config_dockerfile_workflows

Revision ID: 070_add_basemodel_fields_to_dockerfile_workflow
Revises: 069_add_infrastructure_mst_code_to_service_configs
Create Date: 2025-12-11

Adds code, name, description, is_deleted, is_active columns to service_config_dockerfile_workflows
table to match BaseModel pattern. This enables proper polymorphic reference tracking.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '070_add_basemodel_fields_to_dockerfile_workflow'
down_revision: Union[str, None] = '069_add_infrastructure_mst_code_to_service_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add code column (required, unique)
    op.add_column(
        'service_config_dockerfile_workflows',
        sa.Column(
            'code',
            sa.String(100),
            nullable=True,  # Temporarily nullable for existing records
            comment='Unique workflow code'
        )
    )

    # Add name column (required)
    op.add_column(
        'service_config_dockerfile_workflows',
        sa.Column(
            'name',
            sa.String(255),
            nullable=True,  # Temporarily nullable for existing records
            comment='Workflow name'
        )
    )

    # Add description column (optional)
    op.add_column(
        'service_config_dockerfile_workflows',
        sa.Column(
            'description',
            sa.String(500),
            nullable=True,
            comment='Workflow description'
        )
    )

    # Add is_deleted column
    op.add_column(
        'service_config_dockerfile_workflows',
        sa.Column(
            'is_deleted',
            sa.Boolean(),
            server_default='false',
            nullable=False,
            comment='Soft delete flag'
        )
    )

    # Add is_active column
    op.add_column(
        'service_config_dockerfile_workflows',
        sa.Column(
            'is_active',
            sa.Boolean(),
            server_default='true',
            nullable=False,
            comment='Active status flag'
        )
    )

    # Backfill existing records with generated codes and names
    connection = op.get_bind()
    connection.execute(text("""
        UPDATE service_config_dockerfile_workflows
        SET
            code = 'SCDF_' || id || '_' || substring(md5(random()::text) from 1 for 8),
            name = 'Dockerfile Workflow #' || id
        WHERE code IS NULL
    """))

    # Now make code NOT NULL and add unique constraint
    op.alter_column(
        'service_config_dockerfile_workflows',
        'code',
        nullable=False
    )
    op.alter_column(
        'service_config_dockerfile_workflows',
        'name',
        nullable=False
    )

    # Create unique index on code
    op.create_index(
        'idx_scdw_code_unique',
        'service_config_dockerfile_workflows',
        ['code'],
        unique=True
    )


def downgrade() -> None:
    # Drop unique index
    op.drop_index('idx_scdw_code_unique', table_name='service_config_dockerfile_workflows')

    # Drop columns
    op.drop_column('service_config_dockerfile_workflows', 'is_active')
    op.drop_column('service_config_dockerfile_workflows', 'is_deleted')
    op.drop_column('service_config_dockerfile_workflows', 'description')
    op.drop_column('service_config_dockerfile_workflows', 'name')
    op.drop_column('service_config_dockerfile_workflows', 'code')
