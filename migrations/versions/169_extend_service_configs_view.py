"""Extend mcp_ro.service_configs: the settings blob and the labels a copy needs.

Creating a service from an existing one starts by FINDING the existing one —
"a Python service in stage", "the one like payments-api" — which is a question
the client LLM answers with a SELECT over these views. That needs four labels
168 left out (service type, resource group, language version) and the settings
themselves, so the user can be shown what will be copied before anything is
created.

`sc.config` is the one column here that 168's docstring rules out ("config
blobs ... are simply not selected"), so it is worth saying why this one is
admitted: a service_config's `config` holds deployment settings — repository,
branches, cpu/memory, port, health path, replica counts, feature flags and the
cluster context. Secrets and environment variables are NOT in it; they live in
variable_mst and the secret-config-manager, neither of which any view here
touches. The blob is the same content the Settings tab shows every user who can
open the service page.

The view is re-created rather than replaced: CREATE OR REPLACE VIEW can only
append columns at the end and cannot restate `security_barrier`. A dropped view
also loses its grants, so the grant block from 168 is repeated below.

Keep app/mcp_servers/devlift_mcp/db_query/catalog.py in step with the column
list here.

Revision ID: 169_extend_service_configs_view
Revises: 168_add_mcp_ro_views
"""
from typing import Sequence, Union

from alembic import op

revision: str = "169_extend_service_configs_view"
down_revision: Union[str, None] = "168_add_mcp_ro_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "mcp_ro"
ROLE = "devlift_mcp_ro"

# Empty string and unset both become NULL, and `= NULL` matches nothing.
TENANT = "NULLIF(current_setting('app.tenant_code', true), '')"

# The 168 definition plus service_type, resource_group_code / _name,
# language_version and config.
VIEWS: list[tuple[str, str]] = [
    (
        "service_configs",
        f"""
        SELECT sc.code, sc.name, sc.description,
               sc.services_mst_code AS service_code, s.name AS service_name,
               s.service_type::text AS service_type,
               s.applications_mst_code AS application_code, a.name AS application_name,
               s.resource_group_mst_code AS resource_group_code, rg.name AS resource_group_name,
               sc.environment::text AS environment,
               sc.infra_vendor_enum::text AS vendor,
               sc.infrastructuretype_ref_code AS infrastructure_type_code, it.name AS infrastructure_type,
               sc.infrastructure_mst_code AS infrastructure_code, i.name AS infrastructure_name,
               sc.geo_loc_mst_code AS geo_location_code, g.name AS geo_location,
               sc.language_ref_code AS language_code, l.name AS language, l.version AS language_version,
               sc.status::text AS status, sc.status_updated_at, sc.sync_status,
               sc.deployment_status::text AS deployment_status, sc.deployment_status_updated_at,
               sc.deployment_error_message, sc.iac_locked_at,
               sc.log_provider::text AS log_provider,
               sc.deployment_strategy ->> 'strategy' AS deployment_strategy,
               sc.config,
               gw.pr_url, gw.pr_status::text AS pr_status,
               sc.created_at, sc.updated_at, sc.is_active
        FROM public.service_configs sc
        LEFT JOIN public.services_mst s ON s.code = sc.services_mst_code
        LEFT JOIN public.applications_mst a ON a.code = s.applications_mst_code
        LEFT JOIN public.resource_group_mst rg ON rg.code = s.resource_group_mst_code
        LEFT JOIN public.infrastructuretype_ref it ON it.code = sc.infrastructuretype_ref_code
        LEFT JOIN public.infrastructure_mst i ON i.code = sc.infrastructure_mst_code
        LEFT JOIN public.geo_loc_mst g ON g.code = sc.geo_loc_mst_code
        LEFT JOIN public.language_ref l ON l.code = sc.language_ref_code
        LEFT JOIN public.gitops_workflow_detail gw ON gw.id = sc.gitops_workflow_id
        WHERE sc.tenant_mst_code = {TENANT} AND sc.is_deleted IS NOT TRUE
        """,
    ),
]

# What 168 created, restored on downgrade.
PREVIOUS_VIEWS: list[tuple[str, str]] = [
    (
        "service_configs",
        f"""
        SELECT sc.code, sc.name, sc.description,
               sc.services_mst_code AS service_code, s.name AS service_name,
               s.applications_mst_code AS application_code, a.name AS application_name,
               sc.environment::text AS environment,
               sc.infra_vendor_enum::text AS vendor,
               sc.infrastructuretype_ref_code AS infrastructure_type_code, it.name AS infrastructure_type,
               sc.infrastructure_mst_code AS infrastructure_code, i.name AS infrastructure_name,
               sc.geo_loc_mst_code AS geo_location_code, g.name AS geo_location,
               sc.language_ref_code AS language_code, l.name AS language,
               sc.status::text AS status, sc.status_updated_at, sc.sync_status,
               sc.deployment_status::text AS deployment_status, sc.deployment_status_updated_at,
               sc.deployment_error_message, sc.iac_locked_at,
               sc.log_provider::text AS log_provider,
               sc.deployment_strategy ->> 'strategy' AS deployment_strategy,
               gw.pr_url, gw.pr_status::text AS pr_status,
               sc.created_at, sc.updated_at, sc.is_active
        FROM public.service_configs sc
        LEFT JOIN public.services_mst s ON s.code = sc.services_mst_code
        LEFT JOIN public.applications_mst a ON a.code = s.applications_mst_code
        LEFT JOIN public.infrastructuretype_ref it ON it.code = sc.infrastructuretype_ref_code
        LEFT JOIN public.infrastructure_mst i ON i.code = sc.infrastructure_mst_code
        LEFT JOIN public.geo_loc_mst g ON g.code = sc.geo_loc_mst_code
        LEFT JOIN public.language_ref l ON l.code = sc.language_ref_code
        LEFT JOIN public.gitops_workflow_detail gw ON gw.id = sc.gitops_workflow_id
        WHERE sc.tenant_mst_code = {TENANT} AND sc.is_deleted IS NOT TRUE
        """,
    ),
]

_GRANTS = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {ROLE}';
        EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {ROLE}';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {ROLE}';
    END IF;
END
$$;
"""


def _recreate(views: list[tuple[str, str]]) -> None:
    for name, select_sql in views:
        op.execute(f"DROP VIEW IF EXISTS {SCHEMA}.{name}")
        op.execute(
            f"CREATE VIEW {SCHEMA}.{name} WITH (security_barrier = true) AS {select_sql}"
        )
    # A dropped view takes its grants with it.
    op.execute(_GRANTS)


def upgrade() -> None:
    _recreate(VIEWS)


def downgrade() -> None:
    # Only this view goes back; the other views from 168 are untouched.
    _recreate(PREVIOUS_VIEWS)
