"""add s3_key to transaction_queue as JSONB

Revision ID: 092_add_s3_key_to_transaction_queue
Revises: 091
Create Date: 2025-01-06

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '092_add_s3_key_to_transaction_queue'
down_revision = '091_rename_gitops_queue_to_transaction_queue_and_add_code'
branch_labels = None
depends_on = None


def upgrade():
    """Add artifact_s3_key column to transaction_queue table as JSONB."""
    op.add_column(
        'transaction_queue',
        sa.Column(
            'artifact_s3_key',
            JSONB(astext_type=sa.Text()),
            nullable=True,
            comment='S3 object keys for uploaded HCL artifacts as JSON (e.g., {"original_s3_key": "sqs/payment-queue.hcl", "preview": "preview/sqs/payment-queue.hcl"})'
        )
    )


def downgrade():
    """Remove artifact_s3_key column from transaction_queue table."""
    op.drop_column('transaction_queue', 'artifact_s3_key')
