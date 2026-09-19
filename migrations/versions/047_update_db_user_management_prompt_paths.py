"""Update prompt_file_path for database user management cases

Revision ID: 047_update_db_user_management_prompt_paths
Revises: 046_add_region_to_infrastructure_mst
Create Date: 2025-11-28

This migration updates the prompt_file_path column for postgresql_user_management
and mysql_user_management cases in the case_ref table.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '047_update_db_user_management_prompt_paths'
down_revision: Union[str, None] = '046_add_region_to_infrastructure_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Update prompt_file_path for postgresql_user_management
    op.execute(
        """
        UPDATE case_ref
        SET prompt_file_path = 'prompts/cases/aws/postgresql_user_management.txt'
        WHERE code = 'postgresql_user_management';
        """
    )

    # Update prompt_file_path for mysql_user_management
    op.execute(
        """
        UPDATE case_ref
        SET prompt_file_path = 'prompts/cases/aws/mysql_user_management.txt'
        WHERE code = 'mysql_user_management';
        """
    )


def downgrade() -> None:
    # Revert prompt_file_path to NULL
    op.execute(
        """
        UPDATE case_ref
        SET prompt_file_path = NULL
        WHERE code = 'postgresql_user_management';
        """
    )

    op.execute(
        """
        UPDATE case_ref
        SET prompt_file_path = NULL
        WHERE code = 'mysql_user_management';
        """
    )
