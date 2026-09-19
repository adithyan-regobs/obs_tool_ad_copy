"""create case_type_ref and case_ref tables with seeds

Revision ID: 030_create_case_tables
Revises: 029_make_rg_optional
Create Date: 2025-11-22

This migration creates two new tables: case_type_ref and case_ref.
It also seeds initial values and adds an index for frequent joins.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM

# revision identifiers
revision = '030_create_case_tables'
down_revision = '029_make_rg_optional'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create case_type_ref
    op.create_table(
        'case_type_ref',
        sa.Column('vendor_provider', PG_ENUM(name='infra_vendor_enum', create_type=False), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create case_ref
    op.create_table(
        'case_ref',
        sa.Column('case_type_ref_code', sa.String(length=100), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('prompt_file_path', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(['case_type_ref_code'], ['case_type_ref.code'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Optional index for joins/filters
    op.create_index('idx_case_ref_case_type_code', 'case_ref', ['case_type_ref_code'], unique=False)

    # Seed data for case_type_ref
    op.execute(
        """
        INSERT INTO case_type_ref (code, name, description, vendor_provider, is_active, is_deleted)
        VALUES
          ('sqs', 'SQS', 'Amazon Simple Queue Service', 'aws', TRUE, FALSE),
          ('s3', 'S3', 'Amazon Simple Storage Service', 'aws', TRUE, FALSE),
          ('kong_gateway', 'Kong Gateway', 'Kong API Gateway', 'aws', TRUE, FALSE),
          ('dynamodb', 'DynamoDB', 'Amazon DynamoDB NoSQL database', 'aws', TRUE, FALSE),
          ('database', 'Database', 'Relational/NoSQL database', 'aws', TRUE, FALSE)
        ON CONFLICT (code) DO NOTHING;
        """
    )

    # Note: case_ref seed data is added via migration 032_add_service_seed_data.py


def downgrade() -> None:
    # Drop index then tables in reverse order
    op.drop_index('idx_case_ref_case_type_code', table_name='case_ref')
    op.drop_table('case_ref')
    op.drop_table('case_type_ref')
