"""add step columns to cicd_template_mst

Revision ID: 086_add_step_columns_to_cicd_template
Revises: 085_add_cicd_template_mst_table
Create Date: 2024-12-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '086_add_step_columns_to_cicd_template'
down_revision: Union[str, None] = '085_add_cicd_template_mst_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # Add missing is_deleted column from BaseModel
    op.add_column('cicd_template_mst', sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False))

    # Add new columns to support individual workflow step rows
    op.add_column('cicd_template_mst', sa.Column('step_order', sa.Integer(), nullable=True, comment='Execution order of the step'))
    op.add_column('cicd_template_mst', sa.Column('step_category', sa.String(length=50), nullable=True, comment='Step category: setup, quality, build, notification'))
    op.add_column('cicd_template_mst', sa.Column('step_enabled', sa.Boolean(), nullable=True, comment='Whether the step is enabled'))
    op.add_column('cicd_template_mst', sa.Column('step_mandatory', sa.Boolean(), nullable=True, comment='Whether the step is mandatory'))
    op.add_column('cicd_template_mst', sa.Column('step_dependencies', postgresql.JSONB(), nullable=True, comment='Array of step codes this step depends on'))
    op.add_column('cicd_template_mst', sa.Column('language', sa.String(length=50), nullable=True, comment='Programming language for the workflow'))

    # Set default values for existing rows
    op.execute("UPDATE cicd_template_mst SET step_enabled = TRUE WHERE step_enabled IS NULL")
    op.execute("UPDATE cicd_template_mst SET step_mandatory = FALSE WHERE step_mandatory IS NULL")

    # Make columns non-nullable with defaults
    op.alter_column('cicd_template_mst', 'step_enabled', nullable=False, server_default='true')
    op.alter_column('cicd_template_mst', 'step_mandatory', nullable=False, server_default='false')


def downgrade():
    # Remove the columns (use batch_alter_table to check if columns exist)
    with op.batch_alter_table('cicd_template_mst', schema=None) as batch_op:
        batch_op.drop_column('language')
        batch_op.drop_column('step_dependencies')
        batch_op.drop_column('step_category')
        batch_op.drop_column('step_order')
        batch_op.drop_column('is_deleted')
