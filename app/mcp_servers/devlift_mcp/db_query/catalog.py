"""The view catalog the LLM is allowed to query.

Mirrors the views created by migration 168_add_mcp_ro_views. Keep the two in
step: a column listed here but missing from the view produces an SQL error the
LLM then has to work around, and a view column not listed here is one the LLM
never learns about.

Every view is filtered to `current_setting('app.tenant_code')`, which the
executor sets per transaction, so nothing here mentions tenants. `*_code`
columns exist for JOINs; the instructions tell the LLM never to show them.

This module is pure data: no settings, no DB, importable from unit tests.
"""

from dataclasses import dataclass


SCHEMA_NAME = "mcp_ro"


@dataclass(frozen=True)
class ColumnDoc:
    name: str
    type: str
    description: str = ""


@dataclass(frozen=True)
class ViewDoc:
    name: str
    description: str
    columns: tuple[ColumnDoc, ...]


def _c(name: str, type_: str, description: str = "") -> ColumnDoc:
    return ColumnDoc(name=name, type=type_, description=description)


_AUDIT = (
    _c("created_at", "timestamptz"),
    _c("updated_at", "timestamptz"),
)
_AUDIT_ACTIVE = _AUDIT + (_c("is_active", "boolean"),)


VIEWS: dict[str, ViewDoc] = {
    v.name: v
    for v in (
        ViewDoc(
            "workspaces",
            "Top-level grouping of applications inside the user's account.",
            (
                _c("code", "text", "Join key."),
                _c("name", "text"),
                _c("description", "text"),
                _c("status", "text", "active / archived style status."),
            )
            + _AUDIT_ACTIVE,
        ),
        ViewDoc(
            "applications",
            "An application owns services, resource groups and infrastructure.",
            (
                _c("code", "text", "Join key (services.application_code, infrastructure.application_code)."),
                _c("name", "text"),
                _c("description", "text"),
                _c("workspace_code", "text", "Join key to workspaces.code."),
                _c("workspace_name", "text"),
            )
            + _AUDIT_ACTIVE,
        ),
        ViewDoc(
            "resource_groups",
            "Logical group inside an application; kind is 'service' or 'infra'.",
            (
                _c("code", "text", "Join key."),
                _c("name", "text"),
                _c("description", "text"),
                _c("kind", "text", "'service' or 'infra'."),
                _c("application_code", "text", "Join key to applications.code."),
                _c("application_name", "text"),
            )
            + _AUDIT_ACTIVE,
        ),
        ViewDoc(
            "services",
            "A deployable service (API or background worker). Per-environment "
            "settings live in service_configs.",
            (
                _c("code", "text", "Join key (service_configs.service_code, service_dependencies.service_code)."),
                _c("name", "text"),
                _c("description", "text"),
                _c("service_type", "text", "e.g. api, background."),
                _c("is_public_facing", "boolean"),
                _c("application_code", "text", "Join key to applications.code."),
                _c("application_name", "text"),
                _c("resource_group_code", "text", "Join key to resource_groups.code."),
                _c("resource_group_name", "text"),
                _c("infrastructure_code", "text", "Cluster/host it is deployed on; join to infrastructure.code."),
                _c("infrastructure_name", "text"),
                _c("owner_email", "text", "Point of contact for the service; may be null."),
                _c("owner_name", "text"),
            )
            + _AUDIT_ACTIVE,
        ),
        ViewDoc(
            "infrastructure",
            "Provisioned infrastructure resources: databases, buckets, queues, "
            "clusters, caches. One row per resource per environment.",
            (
                _c("code", "text", "Join key (services.infrastructure_code, service_dependencies.infrastructure_code, deployment_requests.resource_code)."),
                _c("name", "text"),
                _c("description", "text"),
                _c("infrastructure_type_code", "text", "Join key to infrastructure_types.code."),
                _c("infrastructure_type", "text", "Human name of the resource kind, e.g. 'PostgreSQL', 'S3 Bucket', 'EKS Cluster'."),
                _c("vendor", "text", "aws / gcp / azure / on_prem."),
                _c("family", "text", "Broad family of the type, e.g. database, storage, compute."),
                _c("environment", "text", "dev / stage / qa / prod."),
                _c("geo_location_code", "text", "Join key to geo_locations.code."),
                _c("geo_location", "text", "Business region name, e.g. 'Mumbai'. Not the cloud region id."),
                _c("application_code", "text", "Join key to applications.code."),
                _c("application_name", "text"),
                _c("resource_group_code", "text", "Join key to resource_groups.code."),
                _c("resource_group_name", "text"),
                _c("status", "text", "UI status: DRAFT, PR_RAISED, DEPLOYING, DEPLOYED, FAILED, ..."),
                _c("status_updated_at", "timestamptz"),
                _c("deployment_status", "text", "Fine-grained GitOps deployment state; null when never deployed."),
                _c("deployment_status_updated_at", "timestamptz"),
                _c("deployment_error_message", "text", "Last deployment error, if any."),
                _c("workflow_status", "text", "Legacy deployment workflow status."),
                _c("workflow_status_updated_by", "text"),
                _c("workflow_status_updated_at", "timestamptz"),
                _c("iac_locked_at", "timestamptz", "Non-null while an IaC state lock blocks deployment."),
                _c("resource_identifier", "text", "Cloud identifier / ARN once created."),
                _c("locator", "jsonb", "Vendor identity, keys depend on kind: instance_id, db_instance_id, cluster, region, ..."),
                _c("pr_url", "text", "Pull request that provisions it, when one exists."),
                _c("pr_status", "text"),
            )
            + _AUDIT_ACTIVE,
        ),
        ViewDoc(
            "service_configs",
            "Per-environment deployment configuration of a service: where it "
            "runs, on what, and its current deployment state.",
            (
                _c("code", "text", "Join key (deployment_requests.resource_code, pipelines.resource_code when resource_kind = 'SERVICE_CONFIG')."),
                _c("name", "text"),
                _c("description", "text"),
                _c("service_code", "text", "Join key to services.code."),
                _c("service_name", "text"),
                _c("service_type", "text", "e.g. api, background."),
                _c("application_code", "text"),
                _c("application_name", "text"),
                _c("resource_group_code", "text", "Join key to resource_groups.code."),
                _c("resource_group_name", "text"),
                _c("environment", "text", "dev / stage / qa / prod."),
                _c("vendor", "text", "aws / gcp / azure / on_prem."),
                _c("infrastructure_type_code", "text"),
                _c("infrastructure_type", "text", "e.g. 'EKS', 'ECS', 'Lambda'."),
                _c("infrastructure_code", "text", "Cluster/host; join to infrastructure.code."),
                _c("infrastructure_name", "text"),
                _c("geo_location_code", "text"),
                _c("geo_location", "text", "Business region name."),
                _c("language_code", "text"),
                _c("language", "text", "Runtime language WITH its version, e.g. 'Go 1.24', 'Python 3.10'."),
                _c("language_version", "text", "Just the version, e.g. '1.24'."),
                _c("status", "text", "UI status: DRAFT, PR_RAISED, DEPLOYING, DEPLOYED, FAILED, ..."),
                _c("status_updated_at", "timestamptz"),
                _c("sync_status", "text", "NEVER_SYNCED / PENDING_SYNC / SYNCED."),
                _c("deployment_status", "text", "Fine-grained GitOps deployment state."),
                _c("deployment_status_updated_at", "timestamptz"),
                _c("deployment_error_message", "text"),
                _c("iac_locked_at", "timestamptz"),
                _c("log_provider", "text", "Override log provider; null means tenant default."),
                _c("deployment_strategy", "text", "rolling / canary / bluegreen / recreate; null means default."),
                _c(
                    "config",
                    "jsonb",
                    "The deployment settings themselves (the Settings tab). EKS keys: "
                    "repository, branches[], cpu_requested, cpu_limit, memory_requested, "
                    "memory_limit, port, health, service_path, alb_schema, compute, "
                    "replica_count, hpa{enabled,min_replicas,max_replicas}, "
                    "custom_iam_policies[], build_args, generate_dockerfile, "
                    "dockerfile_path, create_ecr/create_secrets/create_ssm/create_argo, "
                    "auth_mode, plus cluster context (cluster_name, subnet_ids, region). "
                    "No secrets or environment variables. Keys vary by infrastructure type "
                    "and unused ones are null.",
                ),
                _c("pr_url", "text"),
                _c("pr_status", "text"),
            )
            + _AUDIT_ACTIVE,
        ),
        ViewDoc(
            "namespaces",
            "Kubernetes namespaces known on each cluster.",
            (
                _c("code", "text"),
                _c("namespace", "text", "Namespace name."),
                _c("infrastructure_code", "text", "Cluster; join to infrastructure.code."),
                _c("infrastructure_name", "text"),
                _c("environment", "text"),
                _c("created_at", "timestamptz"),
            ),
        ),
        ViewDoc(
            "geo_locations",
            "Business deployment regions configured for the account (e.g. Mumbai, Singapore).",
            (
                _c("code", "text", "Join key."),
                _c("name", "text"),
                _c("description", "text"),
                _c("created_at", "timestamptz"),
            ),
        ),
        ViewDoc(
            "infrastructure_types",
            "Catalog of resource kinds DevLift can provision (shared, not account-specific).",
            (
                _c("code", "text", "Join key to infrastructure.infrastructure_type_code."),
                _c("name", "text"),
                _c("description", "text"),
                _c("vendor", "text"),
                _c("family", "text"),
                _c("has_log", "boolean"),
                _c("has_metrics", "boolean"),
                _c("has_traces", "boolean"),
            ),
        ),
        ViewDoc(
            "regions",
            "Cloud vendor regions (shared reference list).",
            (
                _c("code", "text"),
                _c("name", "text"),
                _c("vendor", "text"),
                _c("region_identifier", "text", "Vendor region id, e.g. ap-south-1."),
                _c("display_order", "integer"),
            ),
        ),
        ViewDoc(
            "deployment_requests",
            "Change requests / approval queue items. One row per requested "
            "change to a service config or infrastructure resource.",
            (
                _c("code", "text", "Join key (pipeline_runs.deployment_request_code)."),
                _c("display_name", "text"),
                _c("resource_code", "text", "Join to service_configs.code or infrastructure.code depending on resource_kind."),
                _c("resource_kind", "text", "SERVICE_CONFIG, INFRASTRUCTURE, ..."),
                _c("case_ref_code", "text"),
                _c("case_name", "text", "Kind of change, e.g. settings, variables, gateway."),
                _c("status", "text", "DRAFT, PENDING_APPROVAL, APPROVED, REJECTED, DEPLOYED, FAILED, ..."),
                _c("status_last_updated_at", "timestamptz"),
                _c("requested_by_code", "text"),
                _c("requested_by_email", "text"),
                _c("decided_by_code", "text"),
                _c("decided_by_email", "text", "Approver / rejecter."),
                _c("decided_at", "timestamptz"),
                _c("decision_comment", "text"),
                _c("deploy_started_at", "timestamptz", "Non-null once a deploy is in flight."),
                _c("ticket_code", "text", "Join key to tickets.code."),
            )
            + _AUDIT,
        ),
        ViewDoc(
            "pipelines",
            "CI/CD pipeline definitions attached to a service config or infrastructure resource.",
            (
                _c("code", "text", "Join key (pipeline_runs.pipeline_code)."),
                _c("name", "text"),
                _c("resource_code", "text", "Join to service_configs.code or infrastructure.code depending on resource_kind."),
                _c("resource_kind", "text", "SERVICE_CONFIG or INFRASTRUCTURE."),
                _c("repo_url", "text"),
                _c("repo_branch", "text"),
                _c("pipeline_vendor", "text", "e.g. GitHub Actions, Jenkins."),
                _c("language", "text"),
                _c("geo_location_code", "text"),
            )
            + _AUDIT_ACTIVE,
        ),
        ViewDoc(
            "pipeline_runs",
            "Individual pipeline executions (builds / deploys) with their outcome.",
            (
                _c("code", "text"),
                _c("pipeline_code", "text", "Join key to pipelines.code."),
                _c("pipeline_name", "text"),
                _c("resource_code", "text"),
                _c("resource_kind", "text"),
                _c("status", "text", "PENDING / RUNNING / COMPLETED / FAILED / ..."),
                _c("build_number", "text"),
                _c("commit_sha", "text"),
                _c("log_url", "text"),
                _c("deployment_request_code", "text", "Join key to deployment_requests.code."),
                _c("error_message", "text"),
            )
            + _AUDIT,
        ),
        ViewDoc(
            "pull_requests",
            "GitOps pull requests and workflow runs raised by DevLift in the infra repository.",
            (
                _c("id", "bigint"),
                _c("code", "text"),
                _c("git_repository", "text"),
                _c("git_branch", "text"),
                _c("git_commit_sha", "text"),
                _c("pr_number", "integer"),
                _c("pr_url", "text"),
                _c("pr_status", "text", "OPEN / MERGED / CLOSED / ..."),
                _c("workflow_run_url", "text"),
                _c("run_initiated_at", "timestamptz"),
                _c("run_completed_at", "timestamptz"),
                _c("resource_code", "text", "Join to service_configs.code or infrastructure.code depending on resource_kind."),
                _c("resource_kind", "text"),
                _c("initiated_by_email", "text"),
            )
            + _AUDIT,
        ),
        ViewDoc(
            "service_dependencies",
            "Which infrastructure resource a service depends on (service -> infrastructure edges).",
            (
                _c("code", "text"),
                _c("service_code", "text", "Join key to services.code."),
                _c("service_name", "text"),
                _c("infrastructure_code", "text", "Join key to infrastructure.code."),
                _c("infrastructure_name", "text"),
                _c("application_code", "text"),
                _c("application_name", "text"),
                _c("created_at", "timestamptz"),
            ),
        ),
        ViewDoc(
            "resource_connections",
            "Connections between two resources in one environment, with the "
            "access and network policy granted.",
            (
                _c("code", "text"),
                _c("source_code", "text", "Consuming resource; join to service_configs.code or infrastructure.code by source_kind."),
                _c("source_kind", "text", "SERVICE_CONFIG or INFRASTRUCTURE."),
                _c("target_code", "text", "Provider resource; join by target_kind."),
                _c("target_kind", "text"),
                _c("environment", "text"),
                _c("permission_type", "text", "e.g. s3, db."),
                _c("permission", "jsonb", "Access grant detail (actions, db grants)."),
                _c("network_policy", "jsonb", "Ports, CIDRs, security groups."),
            )
            + _AUDIT,
        ),
        ViewDoc(
            "tickets",
            "Tickets that group related change requests (the MCP creates one per provisioning conversation).",
            (
                _c("code", "text", "Join key (deployment_requests.ticket_code)."),
                _c("ticket_number", "text"),
                _c("name", "text"),
                _c("description", "text"),
                _c("source", "text", "portal / api / mcp / ..."),
                _c("source_ref_id", "text"),
                _c("assigned_to_email", "text"),
            )
            + _AUDIT,
        ),
        ViewDoc(
            "production_deployments",
            "Tracked production deploys per repository directory.",
            (
                _c("code", "text"),
                _c("name", "text"),
                _c("dir", "text", "Repository directory being deployed."),
                _c("repo_full_name", "text", "owner/repo."),
                _c("workflow_id", "text"),
                _c("status", "text", "IN_PROGRESS / COMPLETED / FAILED / ..."),
            )
            + _AUDIT,
        ),
    )
}


JOIN_HINTS: tuple[str, ...] = (
    "services.application_code = applications.code",
    "services.infrastructure_code = infrastructure.code  (cluster the service runs on)",
    "service_configs.service_code = services.code  (one row per environment)",
    "service_configs.infrastructure_code = infrastructure.code",
    "service_configs.resource_group_code = resource_groups.code",
    "infrastructure.infrastructure_type_code = infrastructure_types.code",
    "infrastructure.geo_location_code = geo_locations.code",
    "service_dependencies.service_code = services.code AND service_dependencies.infrastructure_code = infrastructure.code",
    "deployment_requests.resource_code = service_configs.code WHERE resource_kind = 'SERVICE_CONFIG'",
    "deployment_requests.resource_code = infrastructure.code WHERE resource_kind = 'INFRASTRUCTURE'",
    "pipeline_runs.pipeline_code = pipelines.code",
    "pipeline_runs.deployment_request_code = deployment_requests.code",
    "pull_requests.resource_code = infrastructure.code WHERE resource_kind = 'INFRASTRUCTURE'",
    "deployment_requests.ticket_code = tickets.code",
)


QUERY_RULES: tuple[str, ...] = (
    "Only the views listed here may appear in FROM / JOIN. Base tables, other "
    "schemas, pg_* and information_schema are rejected.",
    "Every view is already filtered to the signed-in user's account. Do not add "
    "tenant filters and do not ask the user which tenant.",
    "One SELECT (CTEs allowed) per call. No DML, DDL, SET, EXPLAIN, or multiple statements.",
    "Rows are capped server-side; use WHERE, ORDER BY, LIMIT and aggregates "
    "(COUNT, GROUP BY) instead of pulling everything.",
    "Soft-deleted rows are already excluded. is_active = false means disabled, not deleted.",
    "Use name / email / status / date columns in answers. *_code and id columns "
    "are join keys only and must never be shown to the user.",
    "Environment values are lower-case: dev, stage, qa, prod. Status values are "
    "upper-case as listed. Compare with ILIKE when unsure of casing.",
    "jsonb columns (config, locator, permission, network_policy) are read with "
    "-> and ->> : config ->> 'port'. Everything ->> returns is text, so cast "
    "before comparing numerically. Select the whole column when you want to "
    "show the settings; select single keys when you want to filter on them.",
)


def allowed_view_names() -> frozenset[str]:
    return frozenset(VIEWS)


def describe_schema() -> dict:
    """JSON-ready catalog for the describe_data_schema tool."""
    return {
        "schema": SCHEMA_NAME,
        "views": [
            {
                "name": v.name,
                "description": v.description,
                "columns": [
                    {"name": c.name, "type": c.type, **({"description": c.description} if c.description else {})}
                    for c in v.columns
                ],
            }
            for v in VIEWS.values()
        ],
        "joins": list(JOIN_HINTS),
        "rules": list(QUERY_RULES),
    }
