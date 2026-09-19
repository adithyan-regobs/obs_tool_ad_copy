"""add kong route configs table

Revision ID: 026_add_kong_route_configs
Revises: 025_add_is_public_facing
Create Date: 2025-01-18

Adds Kong route configuration tracking for PR workflow:
- New table: kong_route_configs
- Tracks Kong Gateway routes deployed via GitOps workflow
- Links to gitops_workflow_detail (MANY routes → ONE workflow)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP, ENUM

# revision identifiers
revision = '026_add_kong_route_configs'
down_revision = '025_add_is_public_facing'
branch_labels = None
depends_on = None


def upgrade():
    # Note: deployment_status_enum already exists from migration 024_add_gitops_workflow_tracking
    # We just reference it, not create it

    # Create kong_route_configs table
    op.create_table(
        'kong_route_configs',
        # Columns from BaseModel
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', TIMESTAMP(timezone=True), onupdate=sa.text('now()'), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), default=False, nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=True),

        # Foreign Keys
        sa.Column('services_mst_code', sa.String(100), nullable=True,
                  comment="Service this route belongs to (NULL for plugin/global routes)"),

        # Kong Route Configuration
        sa.Column('api_name', sa.String(100), nullable=False,
                  comment="API identifier in kong_configs"),
        sa.Column('http_method', sa.String(10), nullable=False,
                  comment="HTTP method (GET, POST, PUT, DELETE, PATCH, OPTIONS)"),
        sa.Column('route_path', sa.String(500), nullable=False,
                  comment="Kong route pattern (regex with $ suffix)"),

        # PR Workflow Tracking (reusing existing deployment_status_enum from migration 024)
        sa.Column('creation_status',
                  ENUM('INITIATED', 'TERRAFORM_GENERATED', 'GIT_COMMITTED', 'PR_CREATED', 'PR_OPEN',
                       'PR_APPROVED', 'PR_MERGED', 'GITOPS_TRIGGERED', 'TERRAFORM_APPLYING',
                       'TERRAFORM_APPLIED', 'VENDOR_CREATED', 'ACTIVE', 'FAILED',
                       name='deployment_status_enum', create_type=False),
                  nullable=True,
                  server_default='INITIATED',
                  comment="Deployment workflow status"),
        sa.Column('creation_status_updated_by', sa.String(255), nullable=True,
                  comment="Email or GitHub username of user/system that updated status"),
        sa.Column('creation_status_updated_at', TIMESTAMP(timezone=True), nullable=True,
                  comment="Timestamp of last status update"),
        sa.Column('gitops_workflow_id', sa.BigInteger(), nullable=True,
                  comment="Foreign key to gitops_workflow_detail (MANY routes → ONE workflow)"),
        sa.Column('resource_identifier', sa.String(500), nullable=True,
                  comment="Kong route ID (populated after deployment)"),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create foreign key constraints
    op.create_foreign_key(
        'fk_kong_route_service',
        'kong_route_configs', 'services_mst',
        ['services_mst_code'], ['code'],
        ondelete='CASCADE'
    )
    op.create_foreign_key(
        'fk_kong_route_gitops_workflow',
        'kong_route_configs', 'gitops_workflow_detail',
        ['gitops_workflow_id'], ['id'],
        ondelete='SET NULL'
    )

    # Create indexes
    op.create_index('idx_kong_route_creation_status', 'kong_route_configs', ['creation_status'])
    op.create_index('idx_kong_route_gitops_workflow', 'kong_route_configs', ['gitops_workflow_id'])
    op.create_index('idx_kong_route_service', 'kong_route_configs', ['services_mst_code'])


def downgrade():
    # Drop indexes
    op.drop_index('idx_kong_route_service', 'kong_route_configs')
    op.drop_index('idx_kong_route_gitops_workflow', 'kong_route_configs')
    op.drop_index('idx_kong_route_creation_status', 'kong_route_configs')

    # Drop foreign keys
    op.drop_constraint('fk_kong_route_gitops_workflow', 'kong_route_configs', type_='foreignkey')
    op.drop_constraint('fk_kong_route_service', 'kong_route_configs', type_='foreignkey')

    # Drop table
    op.drop_table('kong_route_configs')
