"""add mysql_user_management case to case_ref

Revision ID: 038_add_mysql_user_management
Revises: 037_add_service_config_table
Create Date: 2025-11-25

This migration adds the mysql_user_management case to case_ref table
for MySQL user management operations.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '038_add_mysql_user_management'
down_revision = '037_add_service_config_table'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add mysql_user_management case
    op.execute(
        """
        INSERT INTO case_ref (code, name, description, case_type_ref_code, is_active, is_deleted, prompt_file_path)
        VALUES ('mysql_user_management', 'MySQL User Management',
                'Create and manage MySQL database users with permissions', 'database', TRUE, FALSE, 'prompts/cases/aws/mysql_user_management.txt')
        ON CONFLICT (code) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM case_ref WHERE code = 'mysql_user_management';")
