from fastapi import APIRouter
from app.api.v1.endpoints.webhook import aspora_ticket as aspora_ticket_webhook
from app.api.v1.endpoints import (
    auth,
    ext_auth,
    mcp_auth,
    user_signup,
    user_management,
    invitations,
    team_members,
    obs_alerts_mgmt,
    services,
    applications,
    clerk_webhook,
    datadog_mgmt,
    monitoring_policies,
    alert_types,
    infrastructure_types,
    infrastructure_transactions,
    infrastructure_mst,
    infrastructures,
    kong_route_configs,
    resource_groups,
    regions,
    geo_loc_mst,
    pipeline,
    pipeline_mgmt,
    environment_variable,
    # secrets_parameters,  # TODO: Commented out - SecretsMgmtService disabled during infra_vendor_enum refactoring
    # project_variables,  # DISABLED: served by devlift-secret-config-manager (variable-mst-isolation-spec); delete after cutover soak
    # variable,           # DISABLED: served by devlift-secret-config-manager (variable-mst-isolation-spec); delete after cutover soak
    github_mgmt,
    terragrunt_mgmt,
    terragrunt_sync,
    chat,
    service_config_chat,
    infra_chat,
    test_infra_chat,
    service_reference_tools,
    infra_chat_with_tools,
    language_ref,
    audit_trail,
    case_reference,
    service_config,
    ui_dynamic_form_config,
    gitops_workflows,
    transaction_queue,
    approvals,
    cli_context,
    file_manager,
    service_permissions,
    namespace_mst,
    db_object_mst,
    db_permission_mst,
    vpc_discovery,
    videos,
    documents,
    dockerfiles,
    cicd_templates,
    cicd_template_ref,
    ticket,
    github_app_webhook,
    github_app_installation,
    pipeline_webhook,
    jenkins_webhook,
    resource_status,
    gpu_config,
    model_registry,
    observability_logs,
    log_provider_config,
    new_chat,
    placement_parameter,
    workspaces,
    temporal_admin,
    temporal_dashboard,
    deployments,
    deployment_history,
    admin_deploy_tracker,
    cicd_deployments,
    db_console,
)
from app.api.v1.endpoints.aws_ops import (
    database_ops_endpoint,
    dynamodb_ops_endpoint,
    elasticache_ops_endpoint,
    k8s_postgres_ops_endpoint,
    s3_ops_endpoint,
    sqs_ops_endpoint,
)
from app.api.v1.endpoints.kong_ops import (
    kong_route_ops_endpoint,
)


# Create main API v1 router
api_router = APIRouter()

# Include all endpoint routers
api_router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Authentication"]
)

# External client auth endpoints (VSCode, MCP, etc.)
api_router.include_router(
    ext_auth.router,
    tags=["Authentication"]
)

# MCP OAuth 2.1 authorization completion
api_router.include_router(
    mcp_auth.router,
    tags=["MCP Authentication"]
)

# User signup endpoint
api_router.include_router(
    user_signup.router,
    tags=["User Signup"]
)

# User management endpoint
api_router.include_router(
    user_management.router,
    prefix="/user-management",
    tags=["User Management"]
)

# Invitations endpoint
api_router.include_router(
    invitations.router,
    tags=["Invitations"]
)

# Team Members endpoint
api_router.include_router(
    team_members.router,
    tags=["Team Members"]
)

api_router.include_router(
    obs_alerts_mgmt.router,
    prefix="/alerts",
    tags=["Alerts Management"]
)

api_router.include_router(
    observability_logs.router,
    prefix="/observability-logs",
    tags=["Observability Logs"]
)

api_router.include_router(
    log_provider_config.router,
    prefix="/log-provider-config",
    tags=["Log Provider Config"]
)

api_router.include_router(
    services.router,
    prefix="/services",
    tags=["Services"]
)

api_router.include_router(
    applications.router,
    prefix="/applications",
    tags=["Applications"]
)

api_router.include_router(
    monitoring_policies.router,
    prefix="/monitoring-policies",
    tags=["Monitoring Policies"]
)

api_router.include_router(
    clerk_webhook.router,
    prefix="/webhooks",
    tags=["Webhooks"]
)

api_router.include_router(
    datadog_mgmt.router,
    prefix="/datadog-mgmt",
    tags=["Datadog Management"]
)

api_router.include_router(
    alert_types.router,
    prefix="/alert-types",
    tags=["Reference Data"]
)

api_router.include_router(
    gpu_config.router,
    prefix="/gpu-configs",
    tags=["Reference Data"]
)

api_router.include_router(
    model_registry.router,
    prefix="/model-registry",
    tags=["Model Registry"]
)

api_router.include_router(
    infrastructure_types.router,
    prefix="/infrastructure-types",
    tags=["Reference Data"]
)

api_router.include_router(
    infrastructure_mst.router,
    prefix="/infrastructure-mst",
    tags=["Infrastructure Master"]
)

api_router.include_router(
    infrastructures.router,
    prefix="/infrastructures",
    tags=["Infrastructure Creation"]
)

api_router.include_router(
    kong_route_configs.router,
    prefix="/kong-route-configs",
    tags=["Kong Route Configs"]
)

# FGA-carded gateway fetch (/gateway/by-config/{code}) — separate SecureRouter
# so the legacy kong routes don't have to declare cards yet. Same prefix; the
# path doesn't collide with the legacy /gateway.
api_router.include_router(
    kong_route_configs.secure_router,
    prefix="/kong-route-configs",
    tags=["Kong Route Configs"]
)

api_router.include_router(
    resource_groups.router,
    prefix="/resource-groups",
    tags=["Reference Data"]
)

api_router.include_router(
    regions.router,
    prefix="/regions",
    tags=["Reference Data"]
)

api_router.include_router(
    pipeline.router,
    prefix="/pipelines",
    tags=["Pipelines"]
)

api_router.include_router(
    pipeline_mgmt.router,
    prefix="/pipeline-management",
    tags=["Pipeline Management"]
)

api_router.include_router(
    environment_variable.router,
    prefix="/environment-variables",
    tags=["Environment Variables"]
)

# TODO: Commented out - SecretsMgmtService disabled during infra_vendor_enum refactoring
# api_router.include_router(
#     secrets_parameters.router,
#     prefix="/secrets-parameters",
#     tags=["Secrets & Parameters Management"]
# )

# DISABLED: /project-variables and /resource-variable are served by
# devlift-secret-config-manager (variable-mst-isolation-spec). The endpoint/
# service/repository files are unreachable dead code from here on; delete them
# (plus handlers/environment_variable_handler.py and plugin/environment_variable/)
# after the cutover has soaked.
# api_router.include_router(
#     project_variables.router,
#     prefix="/project-variables",
#     tags=["Project Variables"]
# )
#
# api_router.include_router(
#     variable.router,
#     prefix="/resource-variable",
#     tags=["Resource Variables"]
# )

api_router.include_router(
    github_mgmt.router,
    prefix="/github",
    tags=["GitHub Integration"]
)

api_router.include_router(
    terragrunt_mgmt.router,
    prefix="/terragrunt",
    tags=["Terragrunt Management"]
)

api_router.include_router(
    terragrunt_sync.router,
    prefix="/terragrunt-sync",
    tags=["Terragrunt Sync"]
)

api_router.include_router(
    chat.router,
    prefix="",
    tags=["Chat"]
)

api_router.include_router(
    service_config_chat.router,
    prefix="",
    tags=["Service Config Chat"]
)

api_router.include_router(
    infra_chat.router,
    prefix="",
    tags=["Infrastructure Chat Agent"]
)

# Test endpoint for infra_chat (no auth required)
api_router.include_router(
    test_infra_chat.router,
    prefix="",
    tags=["Test - Infrastructure Chat"]
)

# Service Reference Tools (MCP server consumption)
api_router.include_router(
    service_reference_tools.router,
    prefix="/service-reference-tools",
    tags=["Service Reference Tools (MCP)"]
)

# Infra Chat with Tools POC
api_router.include_router(
    infra_chat_with_tools.router,
    prefix="",
    tags=["Infrastructure Chat with Tools (POC)"]
)

api_router.include_router(
    language_ref.router,
    prefix="/languages",
    tags=["Language Reference"]
)

api_router.include_router(
    audit_trail.router,
    prefix="/audit-trail",
    tags=["Audit Trail"]
)

api_router.include_router(
    infrastructure_transactions.router,
    prefix="/infrastructure-transactions",
    tags=["Infrastructure Transactions"]
)

api_router.include_router(
    case_reference.router,
    prefix="/case-reference",
    tags=["Reference Data"]
)

api_router.include_router(
    service_config.router,
    prefix="/service-configs",
    tags=["Service Configuration"]
)

# FGA-carded split of the upsert (create-service-config / update-service-config)
# — separate SecureRouter so the rest of service_config.py's routes don't have
# to declare cards yet. Same prefix; paths don't collide with the legacy ones.
api_router.include_router(
    service_config.secure_router,
    prefix="/service-configs",
    tags=["Service Configuration"]
)

api_router.include_router(
    ui_dynamic_form_config.router,
    prefix="/ui-dynamic-form-config",
    tags=["UI Form Configuration"]
)

api_router.include_router(
    geo_loc_mst.router,
    tags=["Geographic Location Master"]
)

api_router.include_router(
    placement_parameter.router,
    prefix="/placement-parameters",
    tags=["Placement Parameters"]
)

api_router.include_router(
    gitops_workflows.router,
    prefix="/gitops-workflows",
    tags=["GitOps Workflows"]
)

api_router.include_router(
    transaction_queue.router,
    prefix="/transaction-queue",
    tags=["Transaction Queue"]
)

# Carded create-pr entry points (/by-config/{code}/create-pr FGA-checked,
# /by-infra/{code}/create-pr auth-only for now) — separate SecureRouter so the
# legacy queue routes don't have to declare cards yet. Same prefix; no path
# collisions.
api_router.include_router(
    transaction_queue.secure_router,
    prefix="/transaction-queue",
    tags=["Transaction Queue"]
)

# Change approval. A new surface — the transaction-queue routes above are
# unchanged, and the approvals page talks to these instead.
api_router.include_router(
    approvals.router,
    prefix="/approvals",
    tags=["Approvals"]
)

# The maker's draft saves, one endpoint per half (settings / kong gateway).
# Same ApprovalService.save_draft underneath as /approvals/draft — same
# can_update check, same lane lock — split so each half is its own request.
api_router.include_router(
    approvals.transaction_router,
    prefix="/transaction",
    tags=["Transaction Save"]
)

api_router.include_router(
    file_manager.router,
    prefix="/file-manager",
    tags=["File Manager"]
)

api_router.include_router(
    service_permissions.router,
    prefix="/permissions",
    tags=["Service Permissions"]
)

api_router.include_router(
    namespace_mst.router,
    tags=["Namespace Master"]
)

api_router.include_router(
    db_object_mst.router,
    prefix="/db-objects",
    tags=["Database Objects"]
)

api_router.include_router(
    db_permission_mst.router,
    prefix="/db-permissions",
    tags=["Database Permissions"]
)

api_router.include_router(
    vpc_discovery.router,
    prefix="/vpc-discovery",
    tags=["VPC Discovery"]
)

api_router.include_router(
    videos.router,
    prefix="/videos",
    tags=["Documentation Videos"]
)

api_router.include_router(
    documents.router,
    prefix="/documents",
    tags=["Documentation Files"]
)

api_router.include_router(
    dockerfiles.router,
    prefix="/dockerfiles",
    tags=["Dockerfile Operations"]
)

api_router.include_router(
    cicd_templates.router,
    prefix="/cicd-templates",
    tags=["CI/CD Templates"]
)

api_router.include_router(
    cicd_template_ref.router,
    prefix="/cicd-template-refs",
    tags=["CI/CD Template References"]
)

api_router.include_router(
    ticket.router,
    prefix="/tickets",
    tags=["Tickets"]
)

api_router.include_router(
    github_app_webhook.router,
    prefix="/webhooks",
    tags=["Webhooks"]
)

api_router.include_router(
    pipeline_webhook.router,
    prefix="/webhooks",
    tags=["Webhooks"]
)

api_router.include_router(
    jenkins_webhook.router,
    prefix="/webhooks",
    tags=["Webhooks"]
)

api_router.include_router(
    aspora_ticket_webhook.router,
    prefix="/webhooks",
    tags=["Webhooks"]
)

api_router.include_router(
    resource_status.router,
    prefix="/resource-status",
    tags=["Resource Status"]
)

api_router.include_router(
    github_app_installation.router,
    prefix="/github-app",
    tags=["GitHub App Integration"]
)

api_router.include_router(
    new_chat.router,
    prefix="/new-chat",
    tags=["New Chat"]
)

api_router.include_router(
    dynamodb_ops_endpoint.router,
    prefix="/aws-ops/dynamodb",
    tags=["AWS Ops - DynamoDB"]
)

api_router.include_router(
    s3_ops_endpoint.router,
    prefix="/aws-ops/s3",
    tags=["AWS Ops - S3"]
)

api_router.include_router(
    sqs_ops_endpoint.router,
    prefix="/aws-ops/sqs",
    tags=["AWS Ops - SQS"]
)

api_router.include_router(
    database_ops_endpoint.router,
    prefix="/aws-ops/database",
    tags=["AWS Ops - Database"]
)

api_router.include_router(
    elasticache_ops_endpoint.router,
    prefix="/aws-ops/redis",
    tags=["AWS Ops - ElastiCache Redis"]
)

api_router.include_router(
    k8s_postgres_ops_endpoint.router,
    prefix="/aws-ops/k8s-postgres",
    tags=["AWS Ops - K8s Postgres"]
)

api_router.include_router(
    kong_route_ops_endpoint.router,
    prefix="/kong-ops/routes",
    tags=["Kong Ops - Routes"]
)

api_router.include_router(
    workspaces.router,
    prefix="/workspaces",
    tags=["Workspaces"]
)

api_router.include_router(
    temporal_admin.router,
    prefix="/temporal",
    tags=["Temporal Admin"]
)

# Admin Dashboard → Temporal view. Own prefix so it never collides with the
# /temporal/workflows/{workflow_id} browser tools above.
api_router.include_router(
    temporal_dashboard.router,
    prefix="/temporal/dashboard",
    tags=["Temporal Dashboard"]
)

api_router.include_router(
    deployments.router,
    prefix="/deployments",
    tags=["Deployment History"]
)

# multiple-deploy carries its own FGA card (can_deploy on service:<config code>),
# so it lives on a SecureRouter rather than the legacy public_surface set.
api_router.include_router(
    deployments.secure_router,
    prefix="/deployments",
    tags=["Deployment History"]
)

# DB-backed history list (pipeline_mst + pipeline_run_track) — kept in its own
# module so the shared deployments.py deploy-path routes stay untouched.
api_router.include_router(
    deployment_history.router,
    prefix="/deployments/history",
    tags=["Deployment History"]
)

# Admin Dashboard aggregates (org owners + DEPLOY_TRACKER_ADMIN_EMAILS).
# The deployment list + detail stay on /deployments/history.
api_router.include_router(
    admin_deploy_tracker.router,
    prefix="/admin/deploy-tracker",
    tags=["Admin Dashboard"]
)

api_router.include_router(
    cicd_deployments.router,
    prefix="/cicd",
    tags=["CI/CD"]
)

# devlift CLI: identity + service URLs + feature flags after login.
api_router.include_router(
    cli_context.router,
    prefix="/cli",
    tags=["CLI"]
)

# TEMPORARY — SQL console, the only DB access path while tunnel access to
# vance-prod is revoked. Delete this block and app/api/v1/endpoints/db_console.py
# once normal DB access is restored.
api_router.include_router(
    db_console.router,
    prefix="/internal/db-console",
    tags=["Internal"],
)
