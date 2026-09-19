"""Update transaction_queue to new structure with polymorphic reference and workflow status

Revision ID: 093_update_transaction_queue_to_new_structure
Revises: 092_add_s3_key_to_transaction_queue
Create Date: 2026-01-07

Changes:
1. Create new enum type: transaction_queue_status_enum (DRAFT, SUBMIT, APPROVED, REJECTED, COMMIT, PR_RAISED, PR_DRAFT, PR_APPROVED, PR_REJECTED)
2. Create new enum type: workflow_source_table_enum (SERVICE_CONFIG, SERVICE_CONFIG_DOCKERFILE, ALERT_CONFIG, INFRASTRUCTURE, KONG_ROUTE, PIPELINE)
3. Add new columns: transaction_code, table_name, status_last_updated_at
4. Drop old columns: service_config_code, environment, infra_type, hcl_file_path, atlantis_project_name
5. Rename artifact_s3_key to script_access_key
6. Update status column to use new enum
7. Update FK constraint for user_code to reference user_mst.code with tenant composite key
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '093_update_transaction_queue_to_new_structure'
down_revision: Union[str, None] = '092_add_s3_key_to_transaction_queue'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Create new enum types (with IF NOT EXISTS to handle partial migrations)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE transaction_queue_status_enum AS ENUM ('draft', 'submit', 'approved', 'rejected', 'commit', 'pr_raised', 'pr_draft', 'pr_approved', 'pr_rejected');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE workflow_source_table_enum AS ENUM ('SERVICE_CONFIG', 'SERVICE_CONFIG_DOCKERFILE', 'ALERT_CONFIG', 'INFRASTRUCTURE', 'KONG_ROUTE', 'PIPELINE');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Step 2: Add new columns (nullable first)
    op.add_column(
        'transaction_queue',
        sa.Column('transaction_code', sa.String(100), nullable=True, comment='Code of the entity that created this workflow (e.g., service_config.code)')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('table_name', postgresql.ENUM(name='workflow_source_table_enum', create_type=False), nullable=True, comment='Source table name: SERVICE_CONFIG, ALERT_CONFIG, INFRASTRUCTURE, KONG_ROUTE, PIPELINE, SERVICE_CONFIG_DOCKERFILE')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('status_last_updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='When item status was last updated')
    )

    # Step 2b: Add display_name and case_ref_code columns
    op.add_column(
        'transaction_queue',
        sa.Column('display_name', sa.String(255), nullable=True, comment='Human-readable display name for UI')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('case_ref_code', sa.String(100), nullable=True, comment='Foreign key reference to case_ref table')
    )

    # Step 3: Create index on transaction_code
    op.create_index('idx_transaction_queue_transaction_code', 'transaction_queue', ['transaction_code'])
    op.create_index('idx_transaction_queue_table_name', 'transaction_queue', ['table_name'])

    # Create index on case_ref_code for queries
    op.create_index('idx_transaction_queue_case_ref_code', 'transaction_queue', ['case_ref_code'])

    # Add foreign key constraint to case_ref table
    op.create_foreign_key(
        'transaction_queue_case_ref_fkey',
        'transaction_queue',
        'case_ref',
        ['case_ref_code'],
        ['code'],
        ondelete='SET NULL'
    )

    # Step 4: Drop old composite FK constraint
    op.drop_constraint('gitops_queue_user_code_fkey', 'transaction_queue', type_='foreignkey')

    # Step 5: Populate new columns from old data (migration logic)
    # For now, set table_name to SERVICE_CONFIG as default
    op.execute("""
        UPDATE transaction_queue
        SET transaction_code = service_config_code,
            table_name = 'SERVICE_CONFIG'::workflow_source_table_enum
        WHERE service_config_code IS NOT NULL
    """)

    # Step 6: Drop old columns
    op.drop_column('transaction_queue', 'service_config_code')
    op.drop_column('transaction_queue', 'environment')
    op.drop_column('transaction_queue', 'infra_type')
    op.drop_column('transaction_queue', 'hcl_file_path')
    op.drop_column('transaction_queue', 'atlantis_project_name')

    # Step 7: Rename artifact_s3_key to script_access_key
    op.alter_column('transaction_queue', 'artifact_s3_key', new_column_name='script_access_key')

    # Step 8: Update status column to new enum
    # First drop the default constraint (it uses old enum type)
    op.execute("ALTER TABLE transaction_queue ALTER COLUMN status DROP DEFAULT")

    # Alter the column type from old enum to text (intermediate step)
    op.execute("ALTER TABLE transaction_queue ALTER COLUMN status TYPE TEXT USING status::text")

    # Then alter from text to new enum type, mapping old values to new ones
    op.execute("""
        ALTER TABLE transaction_queue
        ALTER COLUMN status TYPE transaction_queue_status_enum
        USING CASE
            WHEN status = 'pending' THEN 'draft'
            WHEN status = 'deleted' THEN 'draft'
            WHEN status = 'pr_raised' THEN 'pr_raised'
            WHEN status = 'pr_merged' THEN 'pr_raised'
            WHEN status = 'pr_closed' THEN 'pr_raised'
            ELSE 'draft'
        END::transaction_queue_status_enum
    """)

    # Add back the default with new enum type
    op.execute("ALTER TABLE transaction_queue ALTER COLUMN status SET DEFAULT 'draft'::transaction_queue_status_enum")

    # Step 9: Drop old enum type
    op.execute("DROP TYPE gitops_queue_status_enum")

    # Step 10: Add simple FK constraint (user_code -> user_mst.code)
    # Note: Composite FK on (user_code, tenant_code) is not possible without
    # a unique constraint on user_mst(code, tenants_mst_code)
    op.create_foreign_key(
        'gitops_queue_user_code_fkey',
        'transaction_queue',
        'user_mst',
        ['user_code'],
        ['code'],
        ondelete='CASCADE'
    )


def downgrade() -> None:
    # Reverse all changes

    # Step 1: Drop composite FK constraint
    op.drop_constraint('transaction_queue_user_tenant_fkey', 'transaction_queue', type_='foreignkey')

    # Step 2: Restore old FK constraint (user_code -> user_mst.code)
    op.create_foreign_key(
        'gitops_queue_user_code_fkey',
        'transaction_queue',
        'user_mst',
        ['user_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # Step 3: Restore old enum type
    op.execute("CREATE TYPE gitops_queue_status_enum AS ENUM ('pending', 'deleted', 'pr_raised', 'pr_merged', 'pr_closed')")

    # Step 4: Revert status column to old enum
    op.execute("""
        UPDATE transaction_queue
        SET status = CASE
            WHEN status::text = 'draft' THEN 'pending'::gitops_queue_status_enum
            WHEN status::text = 'pr_raised' THEN 'pr_raised'::gitops_queue_status_enum
            ELSE 'pending'::gitops_queue_status_enum
        END::gitops_queue_status_enum
    """)
    op.execute("ALTER TABLE transaction_queue ALTER COLUMN status TYPE gitops_queue_status_enum USING status::text::gitops_queue_status_enum")

    # Step 5: Rename script_access_key back to artifact_s3_key
    op.alter_column('transaction_queue', 'script_access_key', new_column_name='artifact_s3_key')

    # Step 6: Add back old columns
    op.add_column(
        'transaction_queue',
        sa.Column('service_config_code', sa.String(100), nullable=True, comment='Service config code (nullable for standalone infra)')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('environment', sa.String(50), nullable=False, comment='Environment: dev, staging, prod')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('infra_type', sa.String(50), nullable=False, comment='Infrastructure type: ecs, s3, sqs, dynamodb, etc.')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('hcl_file_path', sa.String(500), nullable=False, comment='HCL file path in repo')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('atlantis_project_name', sa.String(200), nullable=False, comment='Atlantis project name')
    )

    # Step 7: Restore old data from new columns
    op.execute("""
        UPDATE transaction_queue
        SET service_config_code = transaction_code
        WHERE transaction_code IS NOT NULL
    """)

    # Step 8: Drop new columns and indexes
    op.drop_constraint('transaction_queue_case_ref_fkey', 'transaction_queue', type_='foreignkey')
    op.drop_index('idx_transaction_queue_case_ref_code', table_name='transaction_queue')
    op.drop_column('transaction_queue', 'case_ref_code')
    op.drop_column('transaction_queue', 'display_name')
    op.drop_index('idx_transaction_queue_table_name', table_name='transaction_queue')
    op.drop_index('idx_transaction_queue_transaction_code', table_name='transaction_queue')
    op.drop_column('transaction_queue', 'status_last_updated_at')
    op.drop_column('transaction_queue', 'table_name')
    op.drop_column('transaction_queue', 'transaction_code')

    # Step 9: Drop new enum types
    op.execute("DROP TYPE workflow_source_table_enum")
    op.execute("DROP TYPE transaction_queue_status_enum")
