"""add S3, SQS, Kong, and DynamoDB case seed data

Revision ID: 032_add_service_seed_data
Revises: 031_add_case_code
Create Date: 2025-11-24

This migration adds case_ref seed data for S3, SQS, Kong Gateway, and DynamoDB operations:
- create_bucket (s3)
- create_queue (sqs)
- add_route (kong_gateway)
- table_management (dynamodb)
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '032_add_service_seed_data'
down_revision = '031_add_case_code'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # First ensure case_type_ref entries exist (in case older 030 migration was applied)
    op.execute(
        """
        INSERT INTO case_type_ref (code, name, description, vendor_provider, is_active, is_deleted)
        VALUES ('dynamodb', 'DynamoDB', 'Amazon DynamoDB NoSQL database', 'aws', TRUE, FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )

    # Add cases to case_ref (assuming case types already exist)
    # Note: prompt_file_path is added later in migration 035

    # S3: create_bucket
    op.execute(
        """
        INSERT INTO case_ref (code, name, description, case_type_ref_code, is_active, is_deleted)
        VALUES ('create_bucket', 'Create S3 Bucket',
                'Create a new AWS S3 bucket for object storage', 's3', TRUE, FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )

    # SQS: create_queue
    op.execute(
        """
        INSERT INTO case_ref (code, name, description, case_type_ref_code, is_active, is_deleted)
        VALUES ('create_queue', 'Create SQS Queue',
                'Create a new AWS SQS queue (Standard or FIFO)', 'sqs', TRUE, FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )

    # Kong: add_route
    op.execute(
        """
        INSERT INTO case_ref (code, name, description, case_type_ref_code, is_active, is_deleted)
        VALUES ('add_route', 'Add Kong Gateway Route',
                'Add a new route to Kong Gateway API', 'kong_gateway', TRUE, FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )

    # DynamoDB: table_management
    op.execute(
        """
        INSERT INTO case_ref (code, name, description, case_type_ref_code, is_active, is_deleted)
        VALUES ('table_management', 'Create DynamoDB Table',
                'Create a new DynamoDB table with partition key', 'dynamodb', TRUE, FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )


def downgrade() -> None:
    # Remove case seed data

    op.execute("DELETE FROM case_ref WHERE code = 'create_bucket';")
    op.execute("DELETE FROM case_ref WHERE code = 'create_queue';")
    op.execute("DELETE FROM case_ref WHERE code = 'add_route';")
    op.execute("DELETE FROM case_ref WHERE code = 'table_management';")
