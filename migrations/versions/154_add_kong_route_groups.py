"""add kong_route_groups and link kong_route_configs to it

Revision ID: 154_add_kong_route_groups
Revises: 153_add_unique_service_name_per_app
Create Date: 2026-07-31

A Kong route OBJECT is (service, env, region, route_group_key, http_method) — the
grain plugins are actually applied at. Terragrunt renders
`kong_configs["<group>"].routes["<METHOD>"] = [paths...]` and targets plugins with
`target_keys = ["<group>-<method>"]`, so every path sharing a (group, method)
NECESSARILY shares one plugin set. Storing plugins per path let two paths in the
same group carry different values and generation silently unioned them.

So: kong_route_groups holds the group-level config, kong_route_configs holds one
row per path, and every reader goes through kong_route_group_id.

Supersedes an earlier three-step version (add per-route plugin columns → build
groups from them → drop the columns). That path existed to migrate a database
which had accumulated per-route plugin data. Collapsed to one step because the
intermediate columns were added empty and never populated by a migration — the
group data comes from scripts/import_kong_routes_from_terragrunt.py, which reads
the gateway terragrunt file itself. Adding three columns only to drop them again
bought nothing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '154_add_kong_route_groups'
down_revision: Union[str, None] = '153_add_unique_service_name_per_app'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'kong_route_groups',
        # BaseModel columns
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('is_deleted', sa.Boolean(), server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true')),

        # Scope — mirrors kong_route_configs so a group is per service/env/region.
        # This is also the ONLY place a gateway change can be scoped: services_mst
        # has no environment or region, so a stage gateway and a prod one are
        # distinguishable only by these columns.
        sa.Column('services_mst_code', sa.String(100), nullable=True),
        sa.Column('geo_loc_mst_code', sa.String(100), nullable=True),
        # Reference the EXISTING enum type by name. Do not pass a value list:
        # 106 added stage/qa, and create_type=False keeps alembic from
        # re-issuing CREATE TYPE (which would fail — the type already exists).
        sa.Column(
            'environments_enum',
            postgresql.ENUM(name='environment_enum', create_type=False),
            nullable=True,
        ),

        # Group identity
        sa.Column(
            'route_group_key', sa.String(200), nullable=False,
            comment='Terragrunt kong_configs group key',
        ),
        sa.Column(
            'http_method', sa.String(10), nullable=False,
            comment='HTTP method — part of the group identity ("<group>-<method>")',
        ),
        sa.Column(
            'api_name', sa.String(100), nullable=True,
            comment='API identifier in kong_configs',
        ),

        # Group-level Kong config
        sa.Column(
            'plugins', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"),
            comment='Name-only Kong plugins applied to every path in this group',
        ),
        sa.Column(
            'regex_priority', sa.Integer(), nullable=False, server_default=sa.text('0'),
            comment='Kong regex_priority for this route-group',
        ),

        # Which group carries the terragrunt `service { }` block; every other group
        # for the same service emits `existing_service = "<owner label>"` instead.
        # Stored rather than derived: the generator used to guess the owner by name
        # (the group labelled like the service, else alphabetically first), so a
        # rename, a delete, or a service whose first route was public could silently
        # move ownership and emit two service blocks.
        #
        # Set on EVERY row sharing the owner's label (a kong_configs entry is keyed
        # by label and holds all methods), so removing one method does not lose it.
        sa.Column(
            'is_service_owner', sa.Boolean(), nullable=False, server_default=sa.text('false'),
            comment='True if this group carries the service{} block for its service',
        ),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.ForeignKeyConstraint(['services_mst_code'], ['services_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['geo_loc_mst_code'], ['geo_loc_mst.code'], ondelete='CASCADE'),
    )

    # One group per (service, env, region, key, method) among ACTIVE rows.
    #
    # NULLS NOT DISTINCT is required, not cosmetic: services/env/geo are all
    # nullable and Postgres would otherwise treat NULL != NULL, so the constraint
    # would skip exactly the rows that need it — chat-created routes carry NULL
    # env AND region today.
    #
    # This is the PG15+ form. A COALESCE expression index is NOT an option here:
    # the enum->text cast is only STABLE, and index expressions must be
    # IMMUTABLE. Requires PostgreSQL >= 15 (dev/stage run 15.17).
    op.execute("""
        CREATE UNIQUE INDEX uq_kong_route_groups_identity
        ON kong_route_groups (
            services_mst_code,
            environments_enum,
            geo_loc_mst_code,
            route_group_key,
            http_method
        )
        NULLS NOT DISTINCT
        WHERE is_deleted = false
    """)

    op.create_index('idx_kong_route_groups_service', 'kong_route_groups', ['services_mst_code'])

    # Scoping index for the Gateway tab's read, which always filters on all three.
    op.execute("""
        CREATE INDEX idx_kong_route_groups_scope
        ON kong_route_groups (services_mst_code, environments_enum, geo_loc_mst_code)
        WHERE is_deleted = false
    """)

    # Link paths to groups. Nullable: rows predating the import have no group until
    # scripts/import_kong_routes_from_terragrunt.py runs, and a route with no group
    # is deliberately invisible to plugin reconciliation rather than treated as
    # "no plugins" — see the generator's [KONG-PLUGINS] guard.
    op.add_column(
        'kong_route_configs',
        sa.Column(
            'kong_route_group_id', sa.BigInteger(), nullable=True,
            comment='FK to kong_route_groups (MANY paths -> ONE group)',
        )
    )
    op.create_foreign_key(
        'kong_route_configs_kong_route_group_id_fkey',
        'kong_route_configs', 'kong_route_groups',
        ['kong_route_group_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index('idx_kong_route_configs_group', 'kong_route_configs', ['kong_route_group_id'])


def downgrade() -> None:
    op.drop_index('idx_kong_route_configs_group', table_name='kong_route_configs')
    op.drop_constraint(
        'kong_route_configs_kong_route_group_id_fkey',
        'kong_route_configs',
        type_='foreignkey',
    )
    op.drop_column('kong_route_configs', 'kong_route_group_id')
    op.execute("DROP INDEX IF EXISTS idx_kong_route_groups_scope")
    op.drop_index('idx_kong_route_groups_service', table_name='kong_route_groups')
    op.execute("DROP INDEX IF EXISTS uq_kong_route_groups_identity")
    op.drop_table('kong_route_groups')
