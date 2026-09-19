"""Add the mcp_ro schema: tenant-scoped, read-only views for the MCP query_data tool.

The DevLift MCP server lets the client LLM answer long-tail questions ("which
services run in stage in Mumbai?") by writing a SELECT. That SQL never touches
base tables: it runs with search_path pinned to this schema, and every view
here filters on `current_setting('app.tenant_code', true)`, which the server
sets per transaction. An unset or empty setting yields no rows, so a missing
tenant fails closed rather than open.

Views are `security_barrier` so a leaky function in the LLM's WHERE clause
cannot see rows before the tenant predicate applies. Secret-bearing and
PII-heavy columns (auth_config, config blobs, variables, tokens, user tables)
are simply not selected.

The SELECT-only role is created outside migrations (it needs a password):
scripts/sql/create_devlift_mcp_ro_role.sql. The grants below apply only when
that role already exists, so the migration also runs cleanly on a database
that has not been given the role yet.

Keep app/mcp_servers/devlift_mcp/db_query/catalog.py in step with the column
lists here.

Revision ID: 168_add_mcp_ro_views
Revises: 167_add_deploy_started_at
"""
from typing import Sequence, Union

from alembic import op

revision: str = "168_add_mcp_ro_views"
down_revision: Union[str, None] = "167_add_deploy_started_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "mcp_ro"
ROLE = "devlift_mcp_ro"

# Empty string and unset both become NULL, and `= NULL` matches nothing.
TENANT = "NULLIF(current_setting('app.tenant_code', true), '')"

VIEWS: list[tuple[str, str]] = [
    (
        "workspaces",
        f"""
        SELECT w.code, w.name, w.description, w.status::text AS status,
               w.created_at, w.updated_at, w.is_active
        FROM public.workspace_mst w
        WHERE w.tenants_mst_code = {TENANT} AND w.is_deleted IS NOT TRUE
        """,
    ),
    (
        "applications",
        f"""
        SELECT a.code, a.name, a.description,
               a.workspace_code, w.name AS workspace_name,
               a.created_at, a.updated_at, a.is_active
        FROM public.applications_mst a
        LEFT JOIN public.workspace_mst w ON w.code = a.workspace_code
        WHERE a.tenants_mst_code = {TENANT} AND a.is_deleted IS NOT TRUE
        """,
    ),
    (
        "resource_groups",
        f"""
        SELECT rg.code, rg.name, rg.description, rg.kind,
               rg.applications_mst_code AS application_code, a.name AS application_name,
               rg.created_at, rg.updated_at, rg.is_active
        FROM public.resource_group_mst rg
        LEFT JOIN public.applications_mst a ON a.code = rg.applications_mst_code
        WHERE rg.tenants_mst_code = {TENANT} AND rg.is_deleted IS NOT TRUE
        """,
    ),
    (
        "services",
        f"""
        SELECT s.code, s.name, s.description,
               s.service_type::text AS service_type, s.is_public_facing,
               s.applications_mst_code AS application_code, a.name AS application_name,
               s.resource_group_mst_code AS resource_group_code, rg.name AS resource_group_name,
               s.infrastructure_mst_code AS infrastructure_code, i.name AS infrastructure_name,
               u.email_id AS owner_email,
               NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') AS owner_name,
               s.created_at, s.updated_at, s.is_active
        FROM public.services_mst s
        LEFT JOIN public.applications_mst a ON a.code = s.applications_mst_code
        LEFT JOIN public.resource_group_mst rg ON rg.code = s.resource_group_mst_code
        LEFT JOIN public.infrastructure_mst i ON i.code = s.infrastructure_mst_code
        LEFT JOIN public.user_mst u ON u.code = s.owner_user_code
        WHERE s.tenants_mst_code = {TENANT} AND s.is_deleted IS NOT TRUE
        """,
    ),
    (
        "infrastructure",
        f"""
        SELECT i.code, i.name, i.description,
               i.infrastructuretype_ref_code AS infrastructure_type_code,
               it.name AS infrastructure_type,
               it.infra_vendor::text AS vendor, it.infra_family::text AS family,
               i.environments_enum::text AS environment,
               i.geo_loc_mst_code AS geo_location_code, g.name AS geo_location,
               i.applications_mst_code AS application_code, a.name AS application_name,
               i.resource_group_mst_code AS resource_group_code, rg.name AS resource_group_name,
               i.status::text AS status, i.status_updated_at,
               i.deployment_status::text AS deployment_status, i.deployment_status_updated_at,
               i.deployment_error_message,
               i.infra_status::text AS workflow_status,
               i.infra_status_updated_by AS workflow_status_updated_by,
               i.infra_status_updated_at AS workflow_status_updated_at,
               i.iac_locked_at, i.resource_identifier, i.locator,
               gw.pr_url, gw.pr_status::text AS pr_status,
               i.created_at, i.updated_at, i.is_active
        FROM public.infrastructure_mst i
        LEFT JOIN public.infrastructuretype_ref it ON it.code = i.infrastructuretype_ref_code
        LEFT JOIN public.geo_loc_mst g ON g.code = i.geo_loc_mst_code
        LEFT JOIN public.applications_mst a ON a.code = i.applications_mst_code
        LEFT JOIN public.resource_group_mst rg ON rg.code = i.resource_group_mst_code
        LEFT JOIN public.gitops_workflow_detail gw ON gw.id = i.gitops_workflow_id
        WHERE i.tenants_mst_code = {TENANT} AND i.is_deleted IS NOT TRUE
        """,
    ),
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
    (
        "namespaces",
        f"""
        SELECT n.code, n.namespace,
               n.infrastructure_mst_code AS infrastructure_code, i.name AS infrastructure_name,
               i.environments_enum::text AS environment, n.created_at
        FROM public.namespace_mst n
        JOIN public.infrastructure_mst i ON i.code = n.infrastructure_mst_code
        WHERE i.tenants_mst_code = {TENANT} AND n.is_deleted IS NOT TRUE
        """,
    ),
    (
        "geo_locations",
        f"""
        SELECT g.code, g.name, g.description, g.created_at
        FROM public.geo_loc_mst g
        WHERE g.tenants_mst_code = {TENANT} AND g.is_deleted IS NOT TRUE
        """,
    ),
    (
        "infrastructure_types",
        """
        SELECT it.code, it.name, it.description,
               it.infra_vendor::text AS vendor, it.infra_family::text AS family,
               it.has_log, it.has_metrics, it.has_traces
        FROM public.infrastructuretype_ref it
        WHERE it.is_deleted IS NOT TRUE
        """,
    ),
    (
        "regions",
        """
        SELECT r.code, r.name, r.infra_vendor_enum AS vendor, r.region_identifier, r.display_order
        FROM public.region_ref r
        WHERE r.is_deleted IS NOT TRUE
        """,
    ),
    (
        "deployment_requests",
        f"""
        SELECT q.code, q.display_name,
               q.transaction_code AS resource_code, q.table_name::text AS resource_kind,
               q.case_ref_code, cr.name AS case_name,
               q.status::text AS status, q.status_last_updated_at,
               q.user_code AS requested_by_code, ru.email_id AS requested_by_email,
               q.decided_by AS decided_by_code, du.email_id AS decided_by_email,
               q.decided_at, q.decision_comment, q.deploy_started_at,
               q.ticket_code, q.created_at, q.updated_at
        FROM public.transaction_queue q
        LEFT JOIN public.case_ref cr ON cr.code = q.case_ref_code
        LEFT JOIN public.user_mst ru ON ru.code = q.user_code
        LEFT JOIN public.user_mst du ON du.code = q.decided_by
        WHERE q.tenant_code = {TENANT}
          AND q.deleted_at IS NULL AND q.is_deleted IS NOT TRUE
        """,
    ),
    (
        "pipelines",
        f"""
        SELECT p.code, p.name,
               p.transaction_code AS resource_code, p.table_name::text AS resource_kind,
               p.repo_url, p.repo_branch,
               pv.name AS pipeline_vendor, l.name AS language,
               p.deployment_config ->> 'geo_loc_mst_code' AS geo_location_code,
               p.created_at, p.updated_at, p.is_active
        FROM public.pipeline_mst p
        LEFT JOIN public.pipeline_vendor_mst pv ON pv.code = p.pipeline_vendor_mst_code
        LEFT JOIN public.language_ref l ON l.code = p.language_ref_code
        WHERE p.tenant_code = {TENANT} AND p.is_deleted IS NOT TRUE
        """,
    ),
    (
        "pipeline_runs",
        f"""
        SELECT r.code, r.pipeline_mst_code AS pipeline_code, p.name AS pipeline_name,
               p.transaction_code AS resource_code, p.table_name::text AS resource_kind,
               r.status::text AS status, r.build_number::text AS build_number,
               r.commit_sha, r.log_url,
               r.transaction_queue_code AS deployment_request_code,
               r.error_message, r.created_at, r.updated_at
        FROM public.pipeline_run_track r
        JOIN public.pipeline_mst p ON p.code = r.pipeline_mst_code
        WHERE p.tenant_code = {TENANT} AND r.is_deleted IS NOT TRUE
        """,
    ),
    (
        "pull_requests",
        f"""
        SELECT gw.id, gw.code, gw.git_repository, gw.git_branch, gw.git_commit_sha,
               gw.pr_number, gw.pr_url, gw.pr_status::text AS pr_status,
               gw.workflow_run_url, gw.run_initiated_at, gw.run_completed_at,
               gw.transaction_code AS resource_code, gw.table_name::text AS resource_kind,
               u.email_id AS initiated_by_email,
               gw.created_at, gw.updated_at
        FROM public.gitops_workflow_detail gw
        LEFT JOIN public.user_mst u ON u.code = gw.user_mst_code
        WHERE gw.tenant_mst_code = {TENANT} AND gw.is_deleted IS NOT TRUE
        """,
    ),
    (
        "service_dependencies",
        f"""
        SELECT d.code,
               d.services_mst_code AS service_code, s.name AS service_name,
               d.infrastructure_mst_code AS infrastructure_code, i.name AS infrastructure_name,
               d.applications_mst_code AS application_code, a.name AS application_name,
               d.created_at
        FROM public.service_dependency_map d
        JOIN public.services_mst s ON s.code = d.services_mst_code
        LEFT JOIN public.infrastructure_mst i ON i.code = d.infrastructure_mst_code
        LEFT JOIN public.applications_mst a ON a.code = d.applications_mst_code
        WHERE s.tenants_mst_code = {TENANT} AND d.is_deleted IS NOT TRUE
        """,
    ),
    (
        "resource_connections",
        f"""
        SELECT rc.code,
               rc.source_transaction_code AS source_code, rc.source_table_name::text AS source_kind,
               rc.target_transaction_code AS target_code, rc.target_table_name::text AS target_kind,
               rc.environments_enum::text AS environment,
               rc.permission ->> 'type' AS permission_type,
               rc.permission, rc.network_policy,
               rc.created_at, rc.updated_at
        FROM public.resource_connection_mst rc
        WHERE rc.tenants_mst_code = {TENANT} AND rc.is_deleted IS NOT TRUE
        """,
    ),
    (
        "tickets",
        f"""
        SELECT t.code, t.ticket_number, t.name, t.description, t.source, t.source_ref_id,
               u.email_id AS assigned_to_email,
               t.created_at, t.updated_at
        FROM public.ticket t
        LEFT JOIN public.user_mst u ON u.code = t.user_mst_code
        WHERE t.tenants_mst_code = {TENANT} AND t.is_deleted IS NOT TRUE
        """,
    ),
    (
        "production_deployments",
        f"""
        SELECT p.code, p.name, p.dir, p.repo_full_name, p.workflow_id,
               p.status::text AS status, p.created_at, p.updated_at
        FROM public.production_deployment_track p
        WHERE p.tenant_code = {TENANT} AND p.is_deleted IS NOT TRUE
        """,
    ),
]


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    for name, select_sql in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {SCHEMA}.{name}")
        op.execute(
            f"CREATE VIEW {SCHEMA}.{name} WITH (security_barrier = true) AS {select_sql}"
        )

    # Grants only if the role exists; the role itself is created out-of-band.
    op.execute(
        f"""
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
    )


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
