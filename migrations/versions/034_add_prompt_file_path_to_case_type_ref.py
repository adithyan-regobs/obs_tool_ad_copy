"""add prompt_file_path to case_type_ref

Revision ID: 034_add_prompt_file_path
Revises: 033_add_case_code_summary
Create Date: 2025-11-25

This migration adds a prompt_file_path column to the case_type_ref table
to store the file path for service-specific prompts.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '034_add_prompt_file_path'
down_revision = '033_add_case_code_summary'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add prompt_file_path column (if not exists)
    conn = op.get_bind()
    result = conn.execute(sa.text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'case_type_ref' AND column_name = 'prompt_file_path'
    """))
    column_exists = result.fetchone() is not None

    if not column_exists:
        op.add_column(
            'case_type_ref',
            sa.Column('prompt_file_path', sa.String(length=500), nullable=True)
        )

    # Update existing records with prompt file paths
    op.execute(
        """
        UPDATE case_type_ref
        SET prompt_file_path = 
            CASE code
                WHEN 's3' THEN 'prompts/service_types/s3.txt'
                WHEN 'sqs' THEN 'prompts/service_types/sqs.txt'
                WHEN 'kong_gateway' THEN 'prompts/service_types/kong_gateway.txt'
                WHEN 'dynamodb' THEN 'prompts/service_types/dynamodb.txt'
                ELSE NULL
            END
        WHERE code IN ('s3', 'sqs', 'kong_gateway', 'dynamodb');
        """
    )


def downgrade() -> None:
    # Drop prompt_file_path column
    op.drop_column('case_type_ref', 'prompt_file_path')
