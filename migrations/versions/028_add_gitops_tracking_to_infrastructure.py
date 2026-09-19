"""add gitops tracking to infrastructure_mst

Revision ID: 028_infra_gitops_tracking
Revises: c8f5a7b3d2e1
Create Date: 2025-01-18

Adds GitOps workflow tracking to infrastructure_mst table:
- Adds 5 GitOps tracking columns to existing infrastructure_mst table
- Links to gitops_workflow_detail (MANY infrastructure records → ONE workflow)
- Enables database-first pattern for S3 bucket deployments
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

# revision identifiers
revision = '028_infra_gitops_tracking'
down_revision = 'c8f5a7b3d2e1'  # 027_add_kong_route_creation_error (merge migration)
branch_labels = None
depends_on = None


def upgrade():
    # Note: deployment_status_enum already exists from migration 024_add_gitops_workflow_tracking
    # We just reference it, not create it

    # Add GitOps tracking columns to infrastructure_mst table
    op.add_column('infrastructure_mst',
        sa.Column('infra_status',
                  sa.Enum('INITIATED', 'TERRAFORM_GENERATED', 'GIT_COMMITTED', 'PR_CREATED', 'PR_OPEN',
                          'PR_APPROVED', 'PR_MERGED', 'GITOPS_TRIGGERED', 'TERRAFORM_APPLYING',
                          'TERRAFORM_APPLIED', 'VENDOR_CREATED', 'ACTIVE', 'FAILED',
                          name='deployment_status_enum'),
                  nullable=True,
                  server_default='INITIATED',
                  comment="Deployment workflow status for infrastructure resource")
    )
    op.add_column('infrastructure_mst',
        sa.Column('infra_status_updated_by', sa.String(255), nullable=True,
                  comment="Email or GitHub username of user/system that updated status")
    )
    op.add_column('infrastructure_mst',
        sa.Column('infra_status_updated_at', TIMESTAMP(timezone=True), nullable=True,
                  comment="Timestamp of last status update")
    )
    op.add_column('infrastructure_mst',
        sa.Column('gitops_workflow_id', sa.BigInteger(), nullable=True,
                  comment="Foreign key to gitops_workflow_detail (MANY infrastructure → ONE workflow)")
    )
    op.add_column('infrastructure_mst',
        sa.Column('resource_identifier', sa.String(500), nullable=True,
                  comment="AWS ARN or resource identifier (populated after vendor creation)")
    )

    # Create foreign key constraint
    op.create_foreign_key(
        'fk_infrastructure_gitops_workflow',
        'infrastructure_mst', 'gitops_workflow_detail',
        ['gitops_workflow_id'], ['id'],
        ondelete='SET NULL'
    )

    # Create indexes on infrastructure_mst
    op.create_index('idx_infrastructure_infra_status', 'infrastructure_mst', ['infra_status'])
    op.create_index('idx_infrastructure_gitops_workflow', 'infrastructure_mst', ['gitops_workflow_id'])


def downgrade():
    # Drop indexes
    op.drop_index('idx_infrastructure_gitops_workflow', 'infrastructure_mst')
    op.drop_index('idx_infrastructure_infra_status', 'infrastructure_mst')

    # Drop foreign key
    op.drop_constraint('fk_infrastructure_gitops_workflow', 'infrastructure_mst', type_='foreignkey')

    # Drop columns
    op.drop_column('infrastructure_mst', 'resource_identifier')
    op.drop_column('infrastructure_mst', 'gitops_workflow_id')
    op.drop_column('infrastructure_mst', 'infra_status_updated_at')
    op.drop_column('infrastructure_mst', 'infra_status_updated_by')
    op.drop_column('infrastructure_mst', 'infra_status')
