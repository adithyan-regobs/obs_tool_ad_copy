"""Add infra_vendor_enum to service_configs

Revision ID: 068_add_infra_vendor_enum_to_service_configs
Revises: 067_add_transaction_code_table_name_to_gitops_workflow
Create Date: 2025-12-11

Adds infra_vendor_enum column to service_configs table.
This is part of the refactoring to move infrastructure vendor from services_mst to service_config,
allowing a single service to have multiple configurations with different vendors.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '068_add_infra_vendor_enum_to_service_configs'
down_revision: Union[str, None] = '067_add_transaction_code_table_name_to_gitops_workflow'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The enum type 'infra_vendor_enum' already exists in the database (from services_mst),
    # so we don't need to create it.

    # Add infra_vendor_enum column to service_configs
    # Use the existing enum type
    infra_vendor_enum = postgresql.ENUM(
        'on_prem', 'aws', 'gcp', 'azure',
        name='infra_vendor_enum',
        create_type=False  # Don't create - already exists
    )

    op.add_column(
        'service_configs',
        sa.Column(
            'infra_vendor_enum',
            infra_vendor_enum,
            nullable=True,  # Initially nullable for migration
            server_default='aws',
            comment='Infrastructure vendor (AWS, GCP, Azure, On-Prem)'
        )
    )

    # Populate from services_mst via JOIN for existing records
    # Cast to text first then to enum to handle potential type mismatches
    op.execute("""
        UPDATE service_configs sc
        SET infra_vendor_enum = sm.infra_vendor_enum::text::infra_vendor_enum
        FROM services_mst sm
        WHERE sc.services_mst_code = sm.code
        AND sc.infra_vendor_enum IS NULL
    """)

    # Set default for any remaining nulls (orphaned configs or edge cases)
    op.execute("""
        UPDATE service_configs
        SET infra_vendor_enum = 'aws'
        WHERE infra_vendor_enum IS NULL
    """)

    # Now make the column non-nullable
    op.alter_column(
        'service_configs',
        'infra_vendor_enum',
        nullable=False
    )

    # Create index for better query performance
    op.create_index(
        'idx_service_configs_infra_vendor',
        'service_configs',
        ['infra_vendor_enum']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('idx_service_configs_infra_vendor', table_name='service_configs')

    # Drop column
    op.drop_column('service_configs', 'infra_vendor_enum')
