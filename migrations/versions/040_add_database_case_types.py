"""add mysql and postgresql database case types

Revision ID: 040_add_database_case_types
Revises: 039_fix_case_tables_null_values
Create Date: 2025-11-26

This migration adds mysql_database and postgresql_database case types
and their related case_ref entries for database management operations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '040_add_database_case_types'
down_revision: Union[str, Sequence[str], None] = '039_fix_case_tables_null_values'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Insert case_type_ref entries first (parent table)
    op.execute(
        """
        INSERT INTO case_type_ref (vendor_provider, code, name, description, created_at, updated_at, is_deleted, is_active)
        VALUES ('aws', 'mysql_database', 'MySQL Database', 'MySQL relational database', now(), now(), false, true)
        ON CONFLICT (code) DO UPDATE SET
          vendor_provider = EXCLUDED.vendor_provider,
          name = EXCLUDED.name,
          description = EXCLUDED.description,
          updated_at = now(),
          is_deleted = EXCLUDED.is_deleted,
          is_active = EXCLUDED.is_active;
        """
    )

    op.execute(
        """
        INSERT INTO case_type_ref (vendor_provider, code, name, description, created_at, updated_at, is_deleted, is_active)
        VALUES ('aws', 'postgresql_database', 'PostgreSQL Database', 'PostgreSQL relational database', now(), now(), false, true)
        ON CONFLICT (code) DO UPDATE SET
          vendor_provider = EXCLUDED.vendor_provider,
          name = EXCLUDED.name,
          description = EXCLUDED.description,
          updated_at = now(),
          is_deleted = EXCLUDED.is_deleted,
          is_active = EXCLUDED.is_active;
        """
    )

    # Step 2: Insert case_ref entries (child table - depends on case_type_ref)
    op.execute(
        """
        INSERT INTO case_ref (case_type_ref_code, code, name, description, created_at, updated_at, is_deleted, is_active, prompt_file_path)
        VALUES ('mysql_database', 'mysql_user_management', 'MySQL User Management', 'Manage MySQL users, roles, and permissions', now(), now(), false, true, 'prompts/cases/aws/db_user_management.txt')
        ON CONFLICT (code) DO UPDATE SET
          case_type_ref_code = EXCLUDED.case_type_ref_code,
          name = EXCLUDED.name,
          description = EXCLUDED.description,
          updated_at = now(),
          is_deleted = EXCLUDED.is_deleted,
          is_active = EXCLUDED.is_active,
          prompt_file_path = EXCLUDED.prompt_file_path;
        """
    )

    op.execute(
        """
        INSERT INTO case_ref (case_type_ref_code, code, name, description, created_at, updated_at, is_deleted, is_active, prompt_file_path)
        VALUES ('postgresql_database', 'postgresql_user_management', 'PostgreSQL User Management', 'Manage PostgreSQL users, roles, and access privileges', now(), now(), false, true, 'prompts/cases/aws/db_user_management.txt')
        ON CONFLICT (code) DO UPDATE SET
          case_type_ref_code = EXCLUDED.case_type_ref_code,
          name = EXCLUDED.name,
          description = EXCLUDED.description,
          updated_at = now(),
          is_deleted = EXCLUDED.is_deleted,
          is_active = EXCLUDED.is_active,
          prompt_file_path = EXCLUDED.prompt_file_path;
        """
    )


def downgrade() -> None:
    # Delete case_ref entries first (due to foreign key)
    op.execute(
        """
        DELETE FROM case_ref WHERE code IN (
          'mysql_user_management', 'postgresql_user_management'
        );
        """
    )

    # Delete case_type_ref entries
    op.execute(
        """
        DELETE FROM case_type_ref WHERE code IN ('mysql_database', 'postgresql_database');
        """
    )
