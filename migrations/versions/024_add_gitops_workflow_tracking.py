"""add gitops workflow tracking

Revision ID: 024_add_gitops_workflow_tracking
Revises: 023_add_invitations_mst
Create Date: 2025-01-17

Adds GitOps workflow tracking for deployment management:
- New table: gitops_workflow_detail
- New columns in alert_configs: creation_status, creation_status_updated_by,
  creation_status_updated_at, gitops_workflow_id, resource_identifier
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

# revision identifiers
revision = '024_add_gitops_workflow_tracking'
down_revision = '023_add_invitations_mst'
branch_labels = None
depends_on = None


def upgrade():
    # Create enum type for deployment status
    op.execute("""
        CREATE TYPE deployment_status_enum AS ENUM (
            'INITIATED',
            'TERRAFORM_GENERATED',
            'GIT_COMMITTED',
            'PR_CREATED',
            'PR_OPEN',
            'PR_APPROVED',
            'PR_MERGED',
            'GITOPS_TRIGGERED',
            'TERRAFORM_APPLYING',
            'TERRAFORM_APPLIED',
            'VENDOR_CREATED',
            'ACTIVE',
            'FAILED'
        )
    """)

    # Create gitops_workflow_detail table
    op.create_table(
        'gitops_workflow_detail',
        # Columns from BaseModel
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', TIMESTAMP(timezone=True), onupdate=sa.text('now()'), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=True),

        # Custom columns for GitOps workflow
        sa.Column('git_repository', sa.String(200), nullable=True, comment="GitHub repository"),
        sa.Column('git_branch', sa.String(100), nullable=True, comment="Feature branch name"),
        sa.Column('git_commit_sha', sa.String(40), nullable=True, comment="Git commit SHA"),
        sa.Column('pr_number', sa.Integer(), nullable=True, comment="GitHub pull request number"),
        sa.Column('pr_url', sa.String(500), nullable=True, comment="Direct URL to pull request"),
        sa.Column('workflow_run_id', sa.String(100), nullable=True, comment="GitHub Actions workflow run ID"),
        sa.Column('workflow_run_url', sa.String(500), nullable=True, comment="Direct URL to workflow run logs"),
        sa.Column('workflow_run_outputs', JSONB, nullable=True, comment="Terraform outputs (monitor IDs, ARNs)"),
        sa.Column('run_initiated_at', TIMESTAMP(timezone=True), nullable=True, comment="Workflow start time"),
        sa.Column('run_completed_at', TIMESTAMP(timezone=True), nullable=True, comment="Workflow completion time"),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create indexes on gitops_workflow_detail
    op.create_index('idx_gwd_pr_number', 'gitops_workflow_detail', ['pr_number'])
    op.create_index('idx_gwd_workflow_run_id', 'gitops_workflow_detail', ['workflow_run_id'])
    op.create_index('idx_gwd_git_commit_sha', 'gitops_workflow_detail', ['git_commit_sha'])
    op.execute('CREATE INDEX idx_gwd_workflow_outputs_gin ON gitops_workflow_detail USING gin(workflow_run_outputs)')

    # Add columns to alert_configs table
    op.add_column('alert_configs',
        sa.Column('creation_status',
                  sa.Enum('INITIATED', 'TERRAFORM_GENERATED', 'GIT_COMMITTED', 'PR_CREATED', 'PR_OPEN',
                          'PR_APPROVED', 'PR_MERGED', 'GITOPS_TRIGGERED', 'TERRAFORM_APPLYING',
                          'TERRAFORM_APPLIED', 'VENDOR_CREATED', 'ACTIVE', 'FAILED',
                          name='deployment_status_enum'),
                  nullable=True,
                  server_default='INITIATED',
                  comment="Deployment workflow status")
    )
    op.add_column('alert_configs',
        sa.Column('creation_status_updated_by', sa.String(255), nullable=True,
                  comment="Email or GitHub username of user/system that updated status")
    )
    op.add_column('alert_configs',
        sa.Column('creation_status_updated_at', TIMESTAMP(timezone=True), nullable=True,
                  comment="Timestamp of last status update")
    )
    op.add_column('alert_configs',
        sa.Column('gitops_workflow_id', sa.BigInteger(), nullable=True,
                  comment="Foreign key to gitops_workflow_detail (MANY alerts → ONE workflow)")
    )
    op.add_column('alert_configs',
        sa.Column('resource_identifier', sa.String(500), nullable=True,
                  comment="Monitor ID or AWS ARN (populated after vendor creation)")
    )

    # Create foreign key constraint
    op.create_foreign_key(
        'fk_alert_gitops_workflow',
        'alert_configs', 'gitops_workflow_detail',
        ['gitops_workflow_id'], ['id'],
        ondelete='SET NULL'
    )

    # Create indexes on alert_configs
    op.create_index('idx_alert_creation_status', 'alert_configs', ['creation_status'])
    op.create_index('idx_alert_gitops_workflow', 'alert_configs', ['gitops_workflow_id'])


def downgrade():
    # Drop indexes on alert_configs
    op.drop_index('idx_alert_gitops_workflow', 'alert_configs')
    op.drop_index('idx_alert_creation_status', 'alert_configs')

    # Drop foreign key
    op.drop_constraint('fk_alert_gitops_workflow', 'alert_configs', type_='foreignkey')

    # Drop columns from alert_configs
    op.drop_column('alert_configs', 'resource_identifier')
    op.drop_column('alert_configs', 'gitops_workflow_id')
    op.drop_column('alert_configs', 'creation_status_updated_at')
    op.drop_column('alert_configs', 'creation_status_updated_by')
    op.drop_column('alert_configs', 'creation_status')

    # Drop indexes on gitops_workflow_detail
    op.execute('DROP INDEX IF EXISTS idx_gwd_workflow_outputs_gin')
    op.drop_index('idx_gwd_git_commit_sha', 'gitops_workflow_detail')
    op.drop_index('idx_gwd_workflow_run_id', 'gitops_workflow_detail')
    op.drop_index('idx_gwd_pr_number', 'gitops_workflow_detail')

    # Drop table
    op.drop_table('gitops_workflow_detail')

    # Drop enum type
    op.execute('DROP TYPE deployment_status_enum')
