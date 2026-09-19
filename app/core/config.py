import re
from typing import List, Optional

from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache

# Static allowed origins (non-subdomain-based: localhost, Vercel previews, bare domain)
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "https://dev.devlift.ai",
    "https://devlift.ai",
    "https://devlift-regobs-dev.vercel.app",
    "https://devlift-regobs.vercel.app",
    "https://vance-devlift.vercel.app",
    "https://devlift-qa.vercel.app",
]

# Regex for any tenant subdomain on devlift.ai, at any depth —
# covers both <tenant>.devlift.ai and <tenant>.<env>.devlift.ai
SUBDOMAIN_ORIGIN_REGEX = r"^https://([a-z0-9-]+\.)+devlift\.ai$"
_SUBDOMAIN_ORIGIN_PATTERN = re.compile(SUBDOMAIN_ORIGIN_REGEX)


def is_origin_allowed(origin: str) -> bool:
    """Check if origin is in static list or matches the tenant subdomain pattern."""
    return origin in ALLOWED_ORIGINS or bool(_SUBDOMAIN_ORIGIN_PATTERN.match(origin))


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database settings
    db_user: str = Field(alias="DB_USER")
    db_password: str = Field(alias="DB_PASSWORD")
    db_host: str = Field(default="localhost", alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_name: str = Field(alias="DB_NAME")
    # Read-only credentials for the MCP data-query tool (query_data). Empty
    # falls back to the main credentials with a startup warning: fine for local
    # dev, but in production the SELECT-only grant on mcp_ro.* is one of the
    # guards that keeps a generated query inside the tenant-scoped views.
    db_ro_user: str = Field(default="", alias="DB_RO_USER")
    db_ro_password: str = Field(default="", alias="DB_RO_PASSWORD")

    # Datadog settings
    datadog_api_key: str = Field(alias="DATADOG_API_KEY")
    datadog_app_key: str = Field(alias="DATADOG_APP_KEY")
    datadog_base_url: str = Field(alias="DATADOG_BASE_URL")

    # Clerk settings
    clerk_signing_secret: str = Field(alias="SIGNING_SECRET")
    clerk_secret_key: str = Field(alias="CLERK_SECRET_KEY")
    clerk_issuer: str = Field(alias="CLERK_ISSUER")
    clerk_jwks_url: str = Field(alias="CLERK_JWKS_URL")

    # Tenant that enterprise-SSO users are auto-provisioned into on first login.
    # Empty disables JIT entirely — only set it on a deployment that serves
    # exactly one tenant behind a SAML connection.
    jit_tenant_code: str = Field(default="", alias="JIT_TENANT_CODE")

    # ─── Per-deployment environment allowlist ───
    #
    # The backend serves one database that holds every environment, so a stage
    # deployment's placement options legitimately include prod: those clusters
    # and services really exist. This declares which environments THIS
    # deployment offers when something is being created — "prod" on the prod
    # backend, "stage,qa" on the other. Unset/empty = no restriction.
    #
    # The mirror of the dashboard's NEXT_PUBLIC_ALLOWED_ENVS (see
    # frontend src/lib/allowedEnvs.ts), same spelling of the value, so the two
    # deployments can be configured from one answer. Without it the chatbot
    # offered prod from a stage tool while the web UI beside it did not.
    #
    # It narrows what is OFFERED, exactly like the frontend's — it is NOT an
    # authorization boundary. Deploying still requires can_deploy on the
    # target, which is what actually stops anyone.
    allowed_environments: str = Field(default="", alias="ALLOWED_ENVIRONMENTS")

    # Admin Dashboard (deploy tracker) — comma-separated emails allowed to call
    # the /admin/deploy-tracker endpoints in addition to org owners. Empty
    # means org owners only. This is the server-side gate; the frontend's
    # NEXT_PUBLIC_DEPLOY_TRACKER_ALLOWED_EMAILS only hides the menu entry.
    deploy_tracker_admin_emails: str = Field(default="", alias="DEPLOY_TRACKER_ADMIN_EMAILS")
    # Slack reporting for the Admin Dashboard — a DEDICATED bot (separate from
    # the main SLACK_BOT_TOKEN app). Both empty ⇒ the report endpoint answers
    # 503 and the UI button explains it isn't configured.
    deploy_tracker_slack_bot_token: str = Field(default="", alias="DEPLOY_TRACKER_SLACK_BOT_TOKEN")
    deploy_tracker_slack_channel: str = Field(default="", alias="DEPLOY_TRACKER_SLACK_CHANNEL")

    # External Client Auth (VSCode, MCP, etc.)
    ext_auth_allowed_client_ids: str = Field(default="", alias="EXT_AUTH_ALLOWED_CLIENT_IDS")
    ext_auth_code_ttl_seconds: int = Field(default=300, alias="EXT_AUTH_CODE_TTL_SECONDS")
    ext_auth_code_cleanup_days: int = Field(default=7, alias="EXT_AUTH_CODE_CLEANUP_DAYS")

    # Secret/variable KMS encryption (staged + audit values are KMS-encrypted).
    # Alias is an identifier, not a credential — safe as a default.
    secret_audit_kms_key_id: str = Field(default="alias/devlift-secret", alias="SECRET_AUDIT_KMS_KEY_ID")

    # GitHub settings
    github_base_url: str = Field(default="https://api.github.com", alias="GITHUB_BASE_URL")

    # GitHub App settings (multi-tenant: installation IDs stored in DB)
    github_app_id: str = Field(default="", alias="GITHUB_APP_ID")
    github_app_slug: str = Field(default="", alias="GITHUB_APP_SLUG")
    github_app_private_key_base64: str = Field(default="", alias="GITHUB_APP_PRIVATE_KEY_BASE64")
    github_app_webhook_secret: str = Field(default="", alias="GITHUB_APP_WEBHOOK_SECRET")
    github_app_platform_installation_id: str = Field(default="", alias="GITHUB_APP_PLATFORM_INSTALLATION_ID")

    # Logins the GitHub App pushes as, comma-separated — one per app, so a new
    # tenant on its own app adds an entry here. Deployments compare pushes and
    # commit authors against this to tell devlift's own force-pushes apart from
    # a human editing the PR; a login missing from this list gets the branch
    # treated as manually committed to and the deployment terminated.
    devlift_git_bots: str = Field(
        default="devlift-regobs[bot],devlift-ai[bot]", alias="DEVLIFT_GIT_BOTS"
    )

    # GitHub Approval App settings (separate app with PR review write permission)
    # Set GITHUB_APPROVAL_ENABLED=true to enable the plan→approve→apply flow.
    # installation_id is hardcoded per-env for now; move to DB table per-tenant later.
    github_approval_enabled: bool = Field(default=False, alias="GITHUB_APPROVAL_ENABLED")
    github_approval_app_id: str = Field(default="", alias="GITHUB_APPROVAL_APP_ID")
    github_approval_app_private_key_base64: str = Field(default="", alias="GITHUB_APPROVAL_APP_PRIVATE_KEY_BASE64")
    github_approval_app_installation_id: str = Field(default="", alias="GITHUB_APPROVAL_APP_INSTALLATION_ID")
    github_codeowner_pat: str = Field(default="", alias="GITHUB_CODEOWNER_PAT")

    # OpenAI settings
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    openai_temperature: float = Field(default=0.7, alias="OPENAI_TEMPERATURE")
    chat_message_threshold: int = Field(default=10, alias="CHAT_MESSAGE_THRESHOLD")

    # Qdrant Vector Database settings
    qdrant_enabled: bool = Field(default=False, alias="QDRANT_ENABLED")
    qdrant_url: str = Field(default="", alias="QDRANT_URL")  # Full URL for ALB (e.g., https://alb.com/qdrant)
    qdrant_host: str = Field(default="localhost", alias="QDRANT_HOST")  # Fallback for direct connection
    qdrant_port: int = Field(default=6333, alias="QDRANT_PORT")
    qdrant_api_key: str = Field(default="", alias="QDRANT_API_KEY")
    qdrant_collection_name: str = Field(default="service_parameters", alias="QDRANT_COLLECTION_NAME")
    qdrant_configs_collection: str = Field(default="service_configs", alias="QDRANT_CONFIGS_COLLECTION")
    qdrant_timeout: int = Field(default=60, alias="QDRANT_TIMEOUT")  # Timeout in seconds for Qdrant operations

    # Embedding settings
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=1536, alias="EMBEDDING_DIMENSIONS")

    # Vector similarity thresholds (OpenAI embeddings typically score 0.3-0.5 for semantic matches)
    vector_match_auto_accept: float = Field(default=0.45, alias="VECTOR_MATCH_AUTO_ACCEPT")
    vector_match_uncertain: float = Field(default=0.30, alias="VECTOR_MATCH_UNCERTAIN")

    # Vector seeding settings
    seed_vectors: bool = Field(default=False, alias="SEED_VECTORS")

    # Service Config Chat settings
    use_agentic_config_chat: bool = Field(default=False, alias="USE_AGENTIC_CONFIG_CHAT")

    # LangFuse settings
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_host: str = Field(default="https://cloud.langfuse.com", alias="LANGFUSE_HOST")

    # LangSmith settings (for LangGraph tracing)
    langchain_api_key: str = Field(default="", alias="LANGCHAIN_API_KEY")
    langchain_tracing_v2: bool = Field(default=False, alias="LANGCHAIN_TRACING_V2")
    langsmith_project: str = Field(default="", alias="LANGSMITH_PROJECT")

    # Infrastructure settings - Git folder structure
    infra_version_index: str = Field(default="01", alias="INFRA_VERSION_INDEX")
    infra_region_dev: str = Field(default="ap-south-1", alias="INFRA_REGION_DEV")
    infra_region_staging: str = Field(default="ap-south-1", alias="INFRA_REGION_STAGING")
    infra_region_qa: str = Field(default="ap-south-1", alias="INFRA_REGION_QA")
    infra_region_prod: str = Field(default="eu-west-2", alias="INFRA_REGION_PROD")
    infra_resource_type: str = Field(default="s3-bucket", alias="INFRA_RESOURCE_TYPE")

    # Org onboarding defaults (infrastructure provisioning for new tenants)
    onboarding_source_org: str = Field(default="Devlift-ai", alias="ONBOARDING_SOURCE_ORG")
    onboarding_source_repo: str = Field(default="infrastructure", alias="ONBOARDING_SOURCE_REPO")
    onboarding_source_branch: str = Field(default="main", alias="ONBOARDING_SOURCE_BRANCH")
    onboarding_default_region: str = Field(default="us-east-1", alias="ONBOARDING_DEFAULT_REGION")
    onboarding_default_region_code: str = Field(default="virginia", alias="ONBOARDING_DEFAULT_REGION_CODE")
    onboarding_default_country_code: str = Field(default="usa", alias="ONBOARDING_DEFAULT_COUNTRY_CODE")
    onboarding_default_index: str = Field(default="01", alias="ONBOARDING_DEFAULT_INDEX")
    onboarding_default_account_id: str = Field(default="443245368846", alias="ONBOARDING_DEFAULT_ACCOUNT_ID")
    onboarding_default_env: str = Field(default="trial", alias="ONBOARDING_DEFAULT_ENV")
    onboarding_default_vm_name: str = Field(default="main", alias="ONBOARDING_DEFAULT_VM_NAME")
    onboarding_default_efs_volume_handle: str = Field(default="", alias="ONBOARDING_DEFAULT_EFS_VOLUME_HANDLE")
    onboarding_default_acm_cert_arn: str = Field(default="arn:aws:acm:us-east-1:443245368846:certificate/fe2b0099-c34a-4586-9999-47887a12742a", alias="ONBOARDING_DEFAULT_ACM_CERT_ARN")
    onboarding_default_vpc_id: str = Field(default="vpc-0e385e35e83218a6a", alias="ONBOARDING_DEFAULT_VPC_ID")
    onboarding_default_subnet_ids: str = Field(default="subnet-02421eba70343423e,subnet-0043f004551e5ecdb", alias="ONBOARDING_DEFAULT_SUBNET_IDS")
    onboarding_default_vpc_cidr: str = Field(default="10.80.0.0/20", alias="ONBOARDING_DEFAULT_VPC_CIDR")

    # Secrets Manager cross-account settings
    secrets_external_id: str = Field(alias="SECRETS_EXTERNAL_ID")

    # Datadog Terraform settings
    datadog_use_terragrunt: bool = Field(default=False, alias="DATADOG_USE_TERRAGRUNT")  # Feature flag to enable Terragrunt mode
    datadog_use_pr_workflow: bool = Field(default=False, alias="DATADOG_USE_PR_WORKFLOW")  # Use PR workflow (GitOps) instead of direct commit

    # Application branding
    app_name: str = Field(default="DevLift", alias="APP_NAME")  # Application name used in PR descriptions and messages

    # AI-generated PR titles/descriptions from actual infra changes (VS Code Copilot style).
    # When enabled (and OPENAI_API_KEY is set), PR title/body are written by the LLM from the
    # redacted diff; on any failure the deterministic template is used, so PR creation never blocks.
    pr_ai_naming_enabled: bool = Field(
        default=True,
        alias="PR_AI_NAMING_ENABLED",
        description="Use the LLM to generate PR titles/descriptions from the changed files. Falls back to the deterministic template on any error."
    )

    # Kong Gateway route validation settings
    kong_enable_duplicate_check: bool = Field(
        default=True,
        alias="KONG_ENABLE_DUPLICATE_CHECK",
        description="Enable duplicate route validation (default: True). Set to False to allow duplicate routes (not recommended for production)."
    )
    kong_enable_enhanced_error_messages: bool = Field(
        default=True,
        alias="KONG_ENABLE_ENHANCED_ERROR_MESSAGES",
        description="Enable enhanced error messages with PR context for duplicate routes (default: True)."
    )

    # Logging settings
    log_file: str = Field(default="", alias="LOG_FILE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_retention_days: int = Field(default=30, alias="LOG_RETENTION_DAYS")

    # AWS S3 Video settings (separate credentials for video processing)
    video_aws_access_key_id: str = Field(default="", alias="VIDEO_AWS_ACCESS_KEY_ID")
    video_aws_secret_access_key: str = Field(default="", alias="VIDEO_AWS_SECRET_ACCESS_KEY")
    aws_region: str = Field(default="ap-south-1", alias="AWS_REGION")
    s3_video_bucket: str = Field(default="", alias="S3_VIDEO_BUCKET")
    allowed_video_tenants: str = Field(default="aspora,vance", alias="ALLOWED_VIDEO_TENANTS")

    # AWS S3 Documentation settings. Files are published by dropping them in the
    # bucket — there is no upload path through the API. The bucket name is an
    # identifier rather than a credential, so it is a default here the same way
    # the audit buckets below are.
    #
    # These credentials deliberately do NOT fall back to the video pair: that
    # IAM user has been deleted, so inheriting it would hand S3 a key AWS no
    # longer recognises and fail with InvalidAccessKeyId. Leave them unset and
    # boto3 uses its normal chain, which on ECS is the task role.
    docs_aws_access_key_id: str = Field(default="", alias="DOCS_AWS_ACCESS_KEY_ID")
    docs_aws_secret_access_key: str = Field(default="", alias="DOCS_AWS_SECRET_ACCESS_KEY")
    #: Region of the documentation bucket. Separate from AWS_REGION because that
    #: one is shared by most of this service, so moving it to follow the docs
    #: bucket would drag everything else along with it. Empty means "wherever
    #: AWS_REGION points", which is the current arrangement — see docs_region.
    docs_aws_region: str = Field(default="", alias="DOCS_AWS_REGION")
    s3_docs_bucket: str = Field(
        default="devlift-documents-bucket",
        alias="S3_DOCS_BUCKET",
    )
    allowed_docs_tenants: str = Field(default="aspora,vance", alias="ALLOWED_DOCS_TENANTS")

    # AWS S3 Terragrunt Upload settings (separate credentials for infrastructure uploads)
    s3_upload_access_key_id: str = Field(default="", alias="S3_UPLOAD_ACCESS_KEY_ID")
    s3_upload_secret_access_key: str = Field(default="", alias="S3_UPLOAD_SECRET_ACCESS_KEY")
    s3_upload_region: str = Field(default="", alias="S3_UPLOAD_REGION")
    s3_upload_bucket: str = Field(default="", alias="S3_UPLOAD_BUCKET")

    # Secret audit trail (variable/secret change history) + temporary staging bucket.
    # Bucket names and KMS aliases are identifiers, not credentials — safe as defaults.
    secret_audit_bucket: str = Field(
        default="vance-core-stage-mumbai-01-devlift-variable-audit-trail",
        alias="SECRET_AUDIT_BUCKET",
    )
    secret_temp_bucket: str = Field(
        default="vance-core-stage-mumbai-01-devlift-variable-drafts",
        alias="SECRET_TEMP_BUCKET",
    )
    secret_state_bucket: str = Field(
        default="vance-core-stage-mumbai-01-devlift-variable-state",
        alias="SECRET_STATE_BUCKET",
    )
    # Slack Integration (Socket Mode)
    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_app_token: str = Field(default="", alias="SLACK_APP_TOKEN")
    slack_app_id: str = Field(default="", alias="SLACK_APP_ID")
    slack_client_id: str = Field(default="", alias="SLACK_CLIENT_ID")
    slack_client_secret: str = Field(default="", alias="SLACK_CLIENT_SECRET")
    slack_signing_secret: str = Field(default="", alias="SLACK_SIGNING_SECRET")  # Not needed for Socket Mode, but kept for compatibility

    # Slack Configuration
    slack_code_block_limit: int = Field(default=2000, alias="SLACK_CODE_BLOCK_LIMIT")
    slack_thread_ttl: int = Field(default=86400, alias="SLACK_THREAD_TTL")  # 24 hours in seconds
    slack_show_processing_indicator: bool = Field(default=False, alias="SLACK_SHOW_PROCESSING_INDICATOR")
    slack_socket_mode_enabled: bool = Field(default=False, alias="SLACK_SOCKET_MODE_ENABLED")
    slack_tenant_code: str = Field(default="", alias="SLACK_TENANT_CODE")  # Tenant for Slack bot (single-tenant deployment)
    slack_auto_apply_alert_channel: str = Field(default="", alias="SLACK_AUTO_APPLY_ALERT_CHANNEL")
    # Frontend origin for links in outbound notifications (e.g. the approval
    # DMs' "review it" link): "https://vance.devlift.ai" or
    # "http://localhost:3000". Empty -> messages carry no link. A setting, not
    # the JWT azp, because notifications fire from background tasks that have
    # no caller origin to borrow.
    frontend_base_url: str = Field(default="", alias="FRONTEND_BASE_URL")
    deploy_alert_slack_bot_token: str = Field(default="", alias="DEPLOY_ALERT_SLACK_BOT_TOKEN")
    # Rendered into generated EKS workflows as build.yaml's skip_slack input so a
    # non-prod DevLift instance (UAT) does not spam the shared build-alert channel.
    deploy_skip_slack_notification: bool = Field(default=False, alias="DEPLOY_SKIP_SLACK_NOTIFICATION")
    # Approval-flow DMs (submitted / approved / sent back) — a DEDICATED bot,
    # deliberately NOT the main SLACK_BOT_TOKEN app (Socket Mode chat) nor the
    # deploy-alert app: the approval audience and scopes are its own, and a
    # rotation of one bot must not silently take the others down. Empty ->
    # approval notifications are a silent no-op (local dev).
    approval_slack_bot_token: str = Field(default="", alias="APPROVAL_SLACK_BOT_TOKEN")

    # MCP OAuth 2.1 (DevLift MCP server authentication)
    # NOTE: both URLs must include the `/devlift-mcp` sub-path — that is where
    # the MCP app (and therefore FastMCP's OAuth endpoints) is mounted in main.py.
    # Issuer without `/devlift-mcp` will advertise OAuth endpoints at paths that
    # don't exist → 404 → Claude Code "Failed to connect".
    mcp_oauth_issuer_url: str = Field(default="https://be.aspora.devlift.ai/devlift-mcp", alias="MCP_OAUTH_ISSUER_URL")
    mcp_oauth_resource_server_url: str = Field(default="https://be.aspora.devlift.ai/devlift-mcp/mcp", alias="MCP_OAUTH_RESOURCE_SERVER_URL")
    # Where the browser is sent for the Clerk login + consent screen. This is a
    # FRONTEND origin, not the backend: the page reads `auth_req_id` from the
    # query string and posts back to /api/v1/auth/mcp/complete. Whichever origin
    # is named here is the one the user must already have a session on, so it
    # has to match the dashboard people actually sign in to.
    mcp_oauth_frontend_authorize_url: str = Field(default="https://aspora.stage.devlift.ai/auth/mcp-authorize", alias="MCP_OAUTH_FRONTEND_AUTHORIZE_URL")
    # Extra Host header values the MCP endpoint will answer to, comma-separated.
    # A full URL is fine — only its host is used. The issuer and resource-server
    # hosts above are always allowed too; this is for anything else that must
    # reach /devlift-mcp/mcp. The default names the UAT backend explicitly so a
    # deployment works without anyone having to set an environment variable.
    mcp_allowed_hosts: str = Field(
        default="https://be.aspora.devlift.ai/devlift-uat", alias="MCP_ALLOWED_HOSTS"
    )
    mcp_oauth_access_token_ttl: int = Field(default=3600, alias="MCP_OAUTH_ACCESS_TOKEN_TTL")
    mcp_oauth_refresh_token_ttl: int = Field(default=7776000, alias="MCP_OAUTH_REFRESH_TOKEN_TTL")
    mcp_oauth_auth_code_ttl: int = Field(default=300, alias="MCP_OAUTH_AUTH_CODE_TTL")

    # MCP data-query fallback (describe_data_schema / query_data tools). The
    # flag drops both tools from the server so a rollout can be reverted
    # without a code change.
    mcp_data_query_enabled: bool = Field(default=True, alias="MCP_DATA_QUERY_ENABLED")
    mcp_data_query_timeout_ms: int = Field(default=15_000, alias="MCP_DATA_QUERY_TIMEOUT_MS")
    mcp_data_query_max_rows: int = Field(default=200, alias="MCP_DATA_QUERY_MAX_ROWS")

    # Infra chat guardrails
    infra_chat_guardrails_enabled: bool = Field(default=False, alias="INFRA_CHAT_GUARDRAILS_ENABLED")
    infra_chat_pii_detection_enabled: bool = Field(default=False, alias="INFRA_CHAT_PII_DETECTION_ENABLED")

    # Redis Cache settings (for multi-worker state management)
    redis_enabled: bool = Field(default=False, alias="REDIS_ENABLED")
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")
    redis_password: str = Field(default="", alias="REDIS_PASSWORD")
    redis_timeout: int = Field(default=5, alias="REDIS_TIMEOUT")  # Connection timeout in seconds

    # Vercel domain provisioning
    vercel_api_token: str = Field(default="", alias="VERCEL_API_TOKEN")
    vercel_project_id: str = Field(default="", alias="VERCEL_PROJECT_ID")
    vercel_team_id: str = Field(default="", alias="VERCEL_TEAM_ID")

    # Domain settings
    base_domain: str = Field(default="devlift.ai", alias="BASE_DOMAIN")

    # Reserved subdomains (comma-separated) that cannot be used for tenant registration
    reserved_subdomains: str = Field(
        default="be,accounts,clerk,clkmail,dev,www,api,mail,admin,ftp,staging,status",
        alias="RESERVED_SUBDOMAINS"
    )

    # Jenkins settings (single shared Jenkins instance for all tenants)
    jenkins_url: str = Field(default="", alias="JENKINS_URL")
    jenkins_user: str = Field(default="", alias="JENKINS_USER")
    jenkins_api_token: str = Field(default="", alias="JENKINS_API_TOKEN")

    # Pipeline Webhook Secret (shared secret for pipeline status callbacks)
    pipeline_webhook_secret: str = Field(default="", alias="PIPELINE_WEBHOOK_SECRET")

    # Aspora service API key (M2M pre-shared key for the aspora-ticket webhook)
    aspora_api_key: str = Field(default="da838aca3f6fafa964a0d152fb05ebbacbbc37e97f9bed2c79f1019b3545068d", alias="ASPORA_API_KEY")

    # Backend URL (used to configure pipeline webhook callbacks in per-org repos)
    devlift_backend_url: str = Field(default="", alias="DEVLIFT_BACKEND_URL")

    # New Chat backend URL
    new_chat_backend_url: str = Field(default="https://be.aspora.devlift.ai/chatbot", alias="NEW_CHAT_BACKEND_URL")

    # MCP -> chatbot internal JWT (HS256). MCP signs short-lived tokens with this
    # secret carrying user_code/tenant_code/user_email, and chatbot relays them on
    # outgoing calls back into obs_tool's API endpoints.
    mcp_internal_jwt_secret: str = Field(default="", alias="MCP_INTERNAL_JWT_SECRET")
    mcp_internal_jwt_ttl: int = Field(default=900, alias="MCP_INTERNAL_JWT_TTL")

    # MCP -> obs_tool's own REST API. The service tools call the same routes the
    # web uses (create-service, service-configs, transaction/service-settings)
    # so every access card and permission check runs unchanged. Points at this
    # process's own port by default; override when the API is served elsewhere.
    mcp_self_api_base_url: str = Field(
        default="http://127.0.0.1:8000/api/v1", alias="MCP_SELF_API_BASE_URL"
    )

    # OpenFGA authorization (app/core/authz). Store/model ids are minted by
    # scripts/fga_backfill.py. Tripwire modes: off | shadow | enforce.
    fga_api_url: str = Field(default="http://localhost:8090", alias="FGA_API_URL")
    fga_store_id: str = Field(default="", alias="FGA_STORE_ID")
    fga_model_id: str = Field(default="", alias="FGA_MODEL_ID")
    authz_tripwire_mode: str = Field(default="shadow", alias="AUTHZ_TRIPWIRE_MODE")

    # Cloudflare DNS (for creating service subdomains after deploy)
    cloudflare_api_token: str = Field(default="", alias="CLOUDFLARE_API_TOKEN")
    cloudflare_zone_id: str = Field(default="", alias="CLOUDFLARE_ZONE_ID")

    # ACM wildcard certificate for *.apps.{base_domain} on ALB HTTPS listeners
    acm_wildcard_cert_arn: str = Field(default="", alias="ACM_WILDCARD_CERT_ARN")

    # Temporal Workflow Engine
    temporal_enabled: bool = Field(default=False, alias="TEMPORAL_ENABLED")
    temporal_host: str = Field(default="localhost", alias="TEMPORAL_HOST")
    temporal_port: int = Field(default=7233, alias="TEMPORAL_PORT")
    temporal_tls_enabled: bool = Field(default=False, alias="TEMPORAL_TLS_ENABLED")
    # Server name the TLS certificate is verified against; differs per
    # environment because each fronts Temporal with its own ALB/cert.
    temporal_tls_domain: str = Field(default="be.aspora.devlift.ai", alias="TEMPORAL_TLS_DOMAIN")
    temporal_namespace: str = Field(default="default", alias="TEMPORAL_NAMESPACE")

    # devlift-secret-config-manager — the deploy_variables_activity now calls
    # its internal variable-deploy endpoint instead of running VariableService
    # locally, so ALL secret handling executes in the auditable service.
    # Includes the service's mount prefix, not just the origin: behind the
    # shared ALB the bare origin resolves to obs_tool via the "/*" catch-all, so
    # the prefix is what selects devlift-secret-config-manager. Only the route
    # is appended at the call site.
    secret_service_url: str = Field(
        default="http://localhost:8001/secret-config-manager", alias="SECRET_SERVICE_URL"
    )
    secret_service_internal_key: str = Field(default="", alias="SECRET_SERVICE_INTERNAL_KEY")
    temporal_task_queue: str = Field(default="deploy-queue", alias="TEMPORAL_TASK_QUEUE")
    # Comma-separated tenant codes that use the Temporal/Atlantis deploy flow.
    # All other tenants fall back to the standard deploy_all path (Jenkins + auto-merge).
    temporal_deploy_tenants: str = Field(default="aspora,vance", alias="TEMPORAL_DEPLOY_TENANTS")
    # If True, the conflict-resolution activity merges the PR directly via API after verifying
    # the plan shows 0 changes (fast path).  If False, the activity only resets + commits and
    # lets DeploymentWorkflow handle the re-plan → apply → Atlantis automerge cycle (safe path).
    temporal_direct_merge_after_conflict: bool = Field(
        default=False, alias="TEMPORAL_DIRECT_MERGE_AFTER_CONFLICT"
    )
    # Production auto-deployment promotion branch pair (stage→main). No
    # enable flag on purpose: nothing reaches the prod infra-deploy paths
    # until the frontend ships its prod gates — the FE release IS the switch.
    # Prod variables-only batches keep today's path (no promotion involved).
    temporal_prod_source_branch: str = Field(default="stage", alias="TEMPORAL_PROD_SOURCE_BRANCH")
    temporal_prod_target_branch: str = Field(default="main", alias="TEMPORAL_PROD_TARGET_BRANCH")

    # "Stuck deploys" strip on Admin Dashboard → Temporal (read-only rules).
    # The Slack alert itself is raised by the workflows (QUEUE_ALERT_AFTER in
    # the deploy workflows) — keep this in step with that constant.
    temporal_stuck_alert_after_min: int = Field(default=2, alias="TEMPORAL_STUCK_ALERT_AFTER_MIN")
    # A deploy whose stage has not changed this long is stuck, whatever the stage.
    temporal_stuck_no_progress_min: int = Field(default=30, alias="TEMPORAL_STUCK_NO_PROGRESS_MIN")

    # Observability — CloudWatch Logs
    cloudwatch_logs_enabled: bool = Field(default=False, alias="CLOUDWATCH_LOGS_ENABLED")
    cloudwatch_logs_region: str = Field(default="us-east-1", alias="CLOUDWATCH_LOGS_REGION")
    cloudwatch_logs_group_prefix: str = Field(default="/devlift/eks", alias="CLOUDWATCH_LOGS_GROUP_PREFIX")
    cloudwatch_logs_query_timeout: int = Field(default=30, alias="CLOUDWATCH_LOGS_QUERY_TIMEOUT")

    @property
    def temporal_deploy_tenants_list(self) -> list:
        """Tenant codes that use the Temporal/Atlantis deploy flow."""
        return [t.strip().lower() for t in self.temporal_deploy_tenants.split(",") if t.strip()]

    @property
    def devlift_git_bots_list(self) -> list:
        """GitHub logins the devlift apps push and commit as."""
        return [b.strip() for b in self.devlift_git_bots.split(",") if b.strip()]

    @property
    def github_repo_owner(self) -> str:
        """Extract owner from infra_github_repository (format: owner/repo)."""
        if self.infra_github_repository and "/" in self.infra_github_repository:
            return self.infra_github_repository.split("/")[0]
        return ""

    @property
    def github_terragrunt_repo(self) -> str:
        """Extract repo name from infra_github_repository (format: owner/repo)."""
        if self.infra_github_repository and "/" in self.infra_github_repository:
            return self.infra_github_repository.split("/")[1]
        return ""

    @property
    def github_base_branch(self) -> str:
        """Get the base branch for infrastructure deployments."""
        return self.infra_github_branch or "main"

    @property
    def allowed_video_tenants_list(self) -> list:
        """Get list of tenants allowed to access videos."""
        return [t.strip() for t in self.allowed_video_tenants.split(",") if t.strip()]

    @property
    def allowed_docs_tenants_list(self) -> list:
        """Get list of tenants allowed to access documentation files."""
        return [t.strip() for t in self.allowed_docs_tenants.split(",") if t.strip()]

    @property
    def docs_aws_credentials(self) -> tuple:
        """
        Static docs S3 credentials, or a pair of empty strings.

        Empty means "no static credentials configured", which the endpoint
        reads as: omit them from the session and let boto3 resolve the ECS task
        role. There is no fallback to the video pair — that IAM user no longer
        exists, and inheriting a deleted key fails harder than having none.
        """
        return self.docs_aws_access_key_id, self.docs_aws_secret_access_key

    @property
    def docs_region(self) -> str:
        """
        Region to talk to the documentation bucket in.

        DOCS_AWS_REGION when set, otherwise AWS_REGION. The fallback is what
        keeps this change invisible to every deployment that has not set the
        new variable, and it is the same shape as S3_UPLOAD_REGION.

        Getting it wrong is not a soft failure: S3 answers a request sent to
        the wrong region with PermanentRedirect, so the Documents tab goes
        empty rather than slow.
        """
        return self.docs_aws_region or self.aws_region

    @property
    def reserved_subdomains_list(self) -> list:
        """Get list of reserved subdomains that cannot be used for tenant registration."""
        return [s.strip().lower() for s in self.reserved_subdomains.split(",") if s.strip()]

    @property
    def ext_auth_allowed_client_ids_list(self) -> list:
        """Get list of allowed external client IDs."""
        return [c.strip() for c in self.ext_auth_allowed_client_ids.split(",") if c.strip()]

    @property
    def sync_database_url(self) -> str:
        """Construct the synchronous database URL (for Alembic)."""
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def async_database_url(self) -> str:
        """Construct the async database URL (for LangGraph checkpointer with psycopg)."""
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def github_app_private_key(self) -> str:
        """Decode base64 private key for GitHub App."""
        if self.github_app_private_key_base64:
            import base64
            return base64.b64decode(self.github_app_private_key_base64).decode('utf-8')
        return ""

    @property
    def github_approval_app_private_key(self) -> str:
        """Decode base64 private key for GitHub Approval App."""
        if self.github_approval_app_private_key_base64:
            import base64
            return base64.b64decode(self.github_approval_app_private_key_base64).decode('utf-8')
        return ""

    @property
    def use_github_app(self) -> bool:
        """Check if GitHub App auth is configured."""
        return bool(
            self.github_app_id and
            self.github_app_private_key_base64
        )

    @property
    def allowed_env_codes(self) -> Optional[List[str]]:
        """The environment allowlist, or None when this deployment is
        unrestricted — the same contract as the dashboard's
        `allowedEnvCodes()`, parsed the same way (comma-separated, trimmed,
        lower-cased, blanks dropped) so one configured value can be pasted
        into both.
        """
        codes = [
            part.strip().lower()
            for part in (self.allowed_environments or "").split(",")
            if part.strip()
        ]
        return codes or None

    def is_env_allowed(self, code: str) -> bool:
        """True when this deployment offers `code`; always true when unset."""
        allowed = self.allowed_env_codes
        return allowed is None or (code or "").strip().lower() in allowed

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Convenience instance for importing
settings = get_settings()
