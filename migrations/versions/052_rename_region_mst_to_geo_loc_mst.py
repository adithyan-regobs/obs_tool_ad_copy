"""Rename region_mst to geo_loc_mst

Revision ID: 052_rename_region_mst_to_geo_loc_mst
Revises: 051_remove_region_from_services_mst
Create Date: 2025-11-30

Renames:
- Table: region_mst -> geo_loc_mst
- Columns: region_mst_code -> geo_loc_mst_code (in service_configs, sidecar_configs, infrastructure_mst)
- Foreign keys, indexes, and unique constraints updated accordingly
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '052_rename_region_mst_to_geo_loc_mst'
down_revision = '051_remove_region_from_services_mst'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Rename region_mst table and all references to geo_loc_mst."""

    # =========================================================================
    # Step 1: Rename the main table
    # =========================================================================
    op.rename_table('region_mst', 'geo_loc_mst')

    # =========================================================================
    # Step 2: Rename columns in referencing tables
    # =========================================================================

    # service_configs
    op.alter_column(
        'service_configs',
        'region_mst_code',
        new_column_name='geo_loc_mst_code'
    )

    # sidecar_configs
    op.alter_column(
        'sidecar_configs',
        'region_mst_code',
        new_column_name='geo_loc_mst_code'
    )

    # infrastructure_mst
    op.alter_column(
        'infrastructure_mst',
        'region_mst_code',
        new_column_name='geo_loc_mst_code'
    )

    # =========================================================================
    # Step 3: Drop ALL old foreign key constraints FIRST
    # (Must drop before renaming the unique constraint they depend on)
    # =========================================================================

    op.drop_constraint(
        'fk_service_configs_region_mst',
        'service_configs',
        type_='foreignkey'
    )

    op.drop_constraint(
        'fk_sidecar_config_region',
        'sidecar_configs',
        type_='foreignkey'
    )

    op.drop_constraint(
        'fk_infrastructure_mst_region_mst_code',
        'infrastructure_mst',
        type_='foreignkey'
    )

    # =========================================================================
    # Step 4: Rename the unique constraint on geo_loc_mst
    # (Now safe since no FKs depend on it)
    # =========================================================================

    op.drop_constraint('uq_region_mst_code', 'geo_loc_mst', type_='unique')
    op.create_unique_constraint(
        'uq_geo_loc_mst_code',
        'geo_loc_mst',
        ['code']
    )

    # =========================================================================
    # Step 5: Create NEW foreign key constraints
    # (They will now depend on the new unique constraint)
    # =========================================================================

    op.create_foreign_key(
        'fk_service_configs_geo_loc_mst',
        'service_configs',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    op.create_foreign_key(
        'fk_sidecar_config_geo_loc',
        'sidecar_configs',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    op.create_foreign_key(
        'fk_infrastructure_mst_geo_loc_mst_code',
        'infrastructure_mst',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # =========================================================================
    # Step 6: Rename indexes
    # =========================================================================

    # geo_loc_mst (formerly region_mst) tenant index
    op.drop_index('ix_region_mst_tenants_mst_code', table_name='geo_loc_mst')
    op.create_index(
        'ix_geo_loc_mst_tenants_mst_code',
        'geo_loc_mst',
        ['tenants_mst_code']
    )

    # sidecar_configs region index
    op.drop_index('idx_sidecar_config_region', table_name='sidecar_configs')
    op.create_index(
        'idx_sidecar_config_geo_loc',
        'sidecar_configs',
        ['geo_loc_mst_code']
    )

    # infrastructure_mst region index
    op.drop_index('ix_infrastructure_mst_region_mst_code', table_name='infrastructure_mst')
    op.create_index(
        'ix_infrastructure_mst_geo_loc_mst_code',
        'infrastructure_mst',
        ['geo_loc_mst_code']
    )

    # =========================================================================
    # Step 7: Rename other unique constraints
    # =========================================================================

    # service_configs composite unique constraint
    op.drop_constraint(
        'uq_service_config_tenant_service_env_region_alb',
        'service_configs',
        type_='unique'
    )
    op.create_unique_constraint(
        'uq_service_config_tenant_service_env_geo_loc_alb',
        'service_configs',
        ['tenant_mst_code', 'services_mst_code', 'environment', 'geo_loc_mst_code', 'alb_selection']
    )

    # sidecar_configs composite unique constraint
    op.drop_constraint(
        'uq_sidecar_config_app_rg_env_region_alb_name',
        'sidecar_configs',
        type_='unique'
    )
    op.create_unique_constraint(
        'uq_sidecar_config_app_rg_env_geo_loc_alb_name',
        'sidecar_configs',
        ['applications_mst_code', 'resource_group_mst_code', 'environment', 'geo_loc_mst_code', 'alb_selection', 'name']
    )


def downgrade() -> None:
    """Revert geo_loc_mst back to region_mst."""

    # =========================================================================
    # Step 1: Rename unique constraints back
    # =========================================================================

    # sidecar_configs
    op.drop_constraint(
        'uq_sidecar_config_app_rg_env_geo_loc_alb_name',
        'sidecar_configs',
        type_='unique'
    )
    op.create_unique_constraint(
        'uq_sidecar_config_app_rg_env_region_alb_name',
        'sidecar_configs',
        ['applications_mst_code', 'resource_group_mst_code', 'environment', 'geo_loc_mst_code', 'alb_selection', 'name']
    )

    # service_configs
    op.drop_constraint(
        'uq_service_config_tenant_service_env_geo_loc_alb',
        'service_configs',
        type_='unique'
    )
    op.create_unique_constraint(
        'uq_service_config_tenant_service_env_region_alb',
        'service_configs',
        ['tenant_mst_code', 'services_mst_code', 'environment', 'geo_loc_mst_code', 'alb_selection']
    )

    # =========================================================================
    # Step 2: Rename indexes back
    # =========================================================================

    # infrastructure_mst
    op.drop_index('ix_infrastructure_mst_geo_loc_mst_code', table_name='infrastructure_mst')
    op.create_index(
        'ix_infrastructure_mst_region_mst_code',
        'infrastructure_mst',
        ['geo_loc_mst_code']
    )

    # sidecar_configs
    op.drop_index('idx_sidecar_config_geo_loc', table_name='sidecar_configs')
    op.create_index(
        'idx_sidecar_config_region',
        'sidecar_configs',
        ['geo_loc_mst_code']
    )

    # geo_loc_mst tenant index
    op.drop_index('ix_geo_loc_mst_tenants_mst_code', table_name='geo_loc_mst')
    op.create_index(
        'ix_region_mst_tenants_mst_code',
        'geo_loc_mst',
        ['tenants_mst_code']
    )

    # =========================================================================
    # Step 3: Drop new FKs
    # =========================================================================

    op.drop_constraint(
        'fk_infrastructure_mst_geo_loc_mst_code',
        'infrastructure_mst',
        type_='foreignkey'
    )

    op.drop_constraint(
        'fk_sidecar_config_geo_loc',
        'sidecar_configs',
        type_='foreignkey'
    )

    op.drop_constraint(
        'fk_service_configs_geo_loc_mst',
        'service_configs',
        type_='foreignkey'
    )

    # =========================================================================
    # Step 4: Rename unique constraint on geo_loc_mst back
    # =========================================================================

    op.drop_constraint('uq_geo_loc_mst_code', 'geo_loc_mst', type_='unique')
    op.create_unique_constraint(
        'uq_region_mst_code',
        'geo_loc_mst',
        ['code']
    )

    # =========================================================================
    # Step 5: Recreate old FKs
    # =========================================================================

    op.create_foreign_key(
        'fk_infrastructure_mst_region_mst_code',
        'infrastructure_mst',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    op.create_foreign_key(
        'fk_sidecar_config_region',
        'sidecar_configs',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    op.create_foreign_key(
        'fk_service_configs_region_mst',
        'service_configs',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # =========================================================================
    # Step 6: Rename columns back
    # =========================================================================

    op.alter_column(
        'infrastructure_mst',
        'geo_loc_mst_code',
        new_column_name='region_mst_code'
    )

    op.alter_column(
        'sidecar_configs',
        'geo_loc_mst_code',
        new_column_name='region_mst_code'
    )

    op.alter_column(
        'service_configs',
        'geo_loc_mst_code',
        new_column_name='region_mst_code'
    )

    # =========================================================================
    # Step 7: Rename table back
    # =========================================================================
    op.rename_table('geo_loc_mst', 'region_mst')
