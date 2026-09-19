"""refactor_aws_secrets_parameters_mst: drop services_mst_code FK, add resource_code + resource_type_str + applications_mst_code

Revision ID: 114_refactor_aws_secrets_parameters_mst_drop_services_fk_add_resource_code
Revises: 113_add_deploy_result_to_pipeline_run_track
Create Date: 2026-03-13

Table is empty — no data migration needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '114_refactor_aws_secrets_parameters_mst_drop_services_fk_add_resource_code'
down_revision: Union[str, None] = '113_add_deploy_result_to_pipeline_run_track'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop old FK and column
    op.drop_constraint(
        'fk_aws_secrets_parameters_service',
        'aws_secrets_parameters_mst',
        type_='foreignkey'
    )
    op.drop_column('aws_secrets_parameters_mst', 'services_mst_code')

    # Add new columns
    op.add_column(
        'aws_secrets_parameters_mst',
        sa.Column('resource_code', sa.String(100), nullable=False,
                  comment='Generic resource code — services_mst.code or infrastructure_mst.code')
    )
    op.add_column(
        'aws_secrets_parameters_mst',
        sa.Column('resource_type_str', sa.String(50), nullable=False,
                  comment='Canvas resource type: service, database, bucket, queue, function, etc.')
    )
    op.add_column(
        'aws_secrets_parameters_mst',
        sa.Column('applications_mst_code', sa.String(100), nullable=False,
                  comment='Application code for canvas-scoped variables')
    )
    op.create_foreign_key(
        'aws_secrets_parameters_mst_applications_mst_code_fkey',
        'aws_secrets_parameters_mst',
        'applications_mst',
        ['applications_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )


def downgrade() -> None:
    # Remove new FK and columns
    op.drop_constraint(
        'aws_secrets_parameters_mst_applications_mst_code_fkey',
        'aws_secrets_parameters_mst',
        type_='foreignkey'
    )
    op.drop_column('aws_secrets_parameters_mst', 'applications_mst_code')
    op.drop_column('aws_secrets_parameters_mst', 'resource_type_str')
    op.drop_column('aws_secrets_parameters_mst', 'resource_code')

    # Restore old column and FK
    op.add_column(
        'aws_secrets_parameters_mst',
        sa.Column('services_mst_code', sa.String(100), nullable=False)
    )
    op.create_foreign_key(
        'fk_aws_secrets_parameters_service',
        'aws_secrets_parameters_mst',
        'services_mst',
        ['services_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )
