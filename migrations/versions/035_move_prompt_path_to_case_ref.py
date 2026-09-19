"""move prompt_file_path from case_type_ref to case_ref

Revision ID: 035_move_prompt_path
Revises: 034_add_prompt_file_path
Create Date: 2025-11-25

This migration moves the prompt_file_path column from case_type_ref to case_ref
to allow more granular prompt configuration per case instead of per service type.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '035_move_prompt_path'
down_revision = '034_add_prompt_file_path'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Add prompt_file_path column to case_ref (if not exists)
    # Note: Migration 030 already creates case_ref with prompt_file_path,
    # so we check if column exists before adding to make this idempotent
    conn = op.get_bind()
    result = conn.execute(sa.text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'case_ref' AND column_name = 'prompt_file_path'
    """))
    column_exists = result.fetchone() is not None

    if not column_exists:
        op.add_column(
            'case_ref',
            sa.Column('prompt_file_path', sa.String(length=500), nullable=True)
        )
    
    # Step 2: Populate case_ref.prompt_file_path based on case code
    # Map each case to its specific prompt file
    op.execute(
        """
        UPDATE case_ref
        SET prompt_file_path =
            CASE code
                WHEN 'create_bucket' THEN 'prompts/cases/aws/create_bucket.txt'
                WHEN 'create_queue' THEN 'prompts/cases/aws/create_queue.txt'
                WHEN 'add_route' THEN 'prompts/cases/aws/add_route.txt'
                WHEN 'table_management' THEN 'prompts/cases/aws/dynamo_table_management.txt'
                ELSE NULL
            END
        WHERE code IN ('create_bucket', 'create_queue', 'add_route', 'table_management');
        """
    )
    
    # Step 3: Drop prompt_file_path column from case_type_ref (if exists)
    result = conn.execute(sa.text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'case_type_ref' AND column_name = 'prompt_file_path'
    """))
    column_exists_in_type_ref = result.fetchone() is not None

    if column_exists_in_type_ref:
        op.drop_column('case_type_ref', 'prompt_file_path')


def downgrade() -> None:
    # Step 1: Add prompt_file_path back to case_type_ref
    op.add_column(
        'case_type_ref',
        sa.Column('prompt_file_path', sa.String(length=500), nullable=True)
    )
    
    # Step 2: Populate case_type_ref from case_ref (take any value since they should be the same)
    op.execute(
        """
        UPDATE case_type_ref ct
        SET prompt_file_path = (
            SELECT cr.prompt_file_path 
            FROM case_ref cr 
            WHERE cr.case_type_ref_code = ct.code 
            AND cr.prompt_file_path IS NOT NULL
            LIMIT 1
        )
        WHERE ct.code IN ('s3', 'sqs', 'kong_gateway', 'dynamodb');
        """
    )
    
    # Step 3: Drop prompt_file_path from case_ref
    op.drop_column('case_ref', 'prompt_file_path')
