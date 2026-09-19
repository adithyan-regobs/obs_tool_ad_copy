from enum import Enum


class WorkspaceStatusEnum(str, Enum):
    active = "active"
    archived = "archived"


class WorkspaceRoleEnum(str, Enum):
    owner = "owner"
    admin = "admin"
    edit = "edit"
    read_only = "read_only"

class ComparatorEnum(str, Enum):
    gt = "gt"
    lt = "lt"
    eq = "eq"
    gte = "gte"
    lte = "lte"

class SeverityEnum(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"

class StatusEnum(str, Enum):
    active="active"
    inactive="inactive"

class HttpMethodEnum(str, Enum):
    """HTTP methods supported by Kong Gateway"""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    OPTIONS = "OPTIONS"
    HEAD = "HEAD"

class ThresholdUnitEnum(str, Enum):
    percent = "percent"
    ms = "ms"
    count = "count"
    seconds = "seconds"
    count_per_min = "count/min"
    minutes = "minutes"
    mbps = "mbps"
    event = "event"

class ObsVendorEnum(str, Enum):
    datadog = "datadog"
    grafana_prom = "grafana_prom"
    loki = "loki"
    cloudwatch = "cloudwatch"

class SignalKindEnum(str, Enum):
    metric = "metric"
    log = "log"
    trace = "trace"
    event = "event"

class InfraFamilyEnum(str, Enum):
    workload = "workload" #EC2, Lambda, ECS, etc.
    database = "database" #RDS, DynamoDB, etc.
    infra = "infra" #General infrastructure
    network = "network" #VPC, VPN, etc.
    storage = "storage" #S3, EBS, etc.

class InfraVendorEnum(str, Enum):
    on_prem = "on_prem"
    aws = "aws"
    gcp = "gcp"
    azure = "azure"
    devlift_k8s = "devlift_k8s"

class EnvironmentEnum(str, Enum):
    dev = "dev"
    stage = "stage"
    qa = "qa"
    prod = "prod"

class RegionEnum(str, Enum):
    us_east_1 = "us-east-1"
    us_west_2 = "us-west-2"
    eu_west_1 = "eu-west-1"
    ap_southeast_1 = "ap-southeast-1"

class MysqlPrivilegeEnum(str, Enum):
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    CREATE = "CREATE"
    ALTER = "ALTER"
    INDEX = "INDEX"
    DELETE = "DELETE"
    DROP = "DROP"

class PgDatabasePrivilegeEnum(str, Enum):
    CONNECT = "CONNECT"
    CREATE = "CREATE"

class PgSchemaPrivilegeEnum(str, Enum):
    USAGE = "USAGE"
    CREATE = "CREATE"

class PgTablePrivilegeEnum(str, Enum):
    SELECT = "SELECT"
    UPDATE = "UPDATE"
    INSERT = "INSERT"
    DELETE = "DELETE"

class IntegrationStatusEnum(str, Enum):
    INITIATED = "INITIATED"          # Request initiated, waiting for processing
    IN_PROGRESS = "IN_PROGRESS"      # Request is being processed
    SUCCESS = "SUCCESS"              # Response received successfully
    FAILED = "FAILED"                # Call completed but failed (HTTP error, etc.)
    TIMEOUT = "TIMEOUT"              # Request timed out
    RETRYING = "RETRYING"            # Retrying due to transient failure
    CANCELLED = "CANCELLED"          # Request cancelled manually or by logic
    INVALID_RESPONSE = "INVALID_RESPONSE"  # Response received but invalid schema/format
    UNAUTHORIZED = "UNAUTHORIZED"    # Auth or API key error
    RATE_LIMITED = "RATE_LIMITED"    # Hit rate limit or quota
    NETWORK_ERROR = "NETWORK_ERROR"  # Network issue (DNS, connection refused, etc.)

class DeploymentStatusEnum(str, Enum):
    """Deployment workflow status tracking for GitOps pipeline"""
    INITIATED = "INITIATED"                     # Deployment request initiated
    TERRAFORM_GENERATED = "TERRAFORM_GENERATED" # Terragrunt files generated
    GIT_COMMITTED = "GIT_COMMITTED"             # Changes committed to Git
    PR_CREATED = "PR_CREATED"                   # Pull request created
    PR_OPEN = "PR_OPEN"                         # Pull request is open
    PR_APPROVED = "PR_APPROVED"                 # Pull request approved
    PR_MERGED = "PR_MERGED"                     # Pull request merged
    GITOPS_TRIGGERED = "GITOPS_TRIGGERED"       # GitHub Actions workflow triggered
    TERRAFORM_APPLYING = "TERRAFORM_APPLYING"   # Terraform apply in progress
    TERRAFORM_APPLIED = "TERRAFORM_APPLIED"     # Terraform apply completed
    VENDOR_CREATED = "VENDOR_CREATED"           # Resource created in vendor (Datadog/AWS)
    VERIFYING_PERMISSIONS = "VERIFYING_PERMISSIONS"  # Infra-apply pipeline verifying IAM via simulate-principal-policy
    PERMISSIONS_MISSING = "PERMISSIONS_MISSING" # Resource exists but tenant role lacks required actions (IAM apply likely failed)
    ACTIVE = "ACTIVE"                           # Resource is active and verified
    FAILED = "FAILED"                           # Deployment failed at any stage

# new 
class ResourceDeploymentStatusEnum(str, Enum):
    """Granular Temporal GitOps deployment workflow states written to infra_mst / service_config."""
    STARTING_PR_CREATION = "starting_pr_creation"
    CREATING_PR = "creating_pr"
    PR_CREATED_SUCCESSFULLY = "pr_created_successfully"
    PR_CREATION_FAILED = "pr_creation_failed"
    STARTING_PLANNING = "starting_planning"
    PLANNING = "planning"
    PLANNED_SUCCESSFULLY = "planned_successfully"
    PLAN_FAILED = "plan_failed"
    STARTING_PLAN_VERIFICATION = "starting_plan_verification"
    PLAN_VERIFIED_SUCCESSFULLY = "plan_verified_successfully"
    DESTRUCTIVE_PLAN_DETECTED = "destructive_plan_detected"
    STARTING_APPLYING = "starting_applying"
    APPLYING = "applying"
    APPLIED_SUCCESSFULLY = "applied_successfully"
    APPLY_FAILED = "apply_failed"
    STARTING_APPROVAL = "starting_approval"
    APPROVED_SUCCESSFULLY = "approved_successfully"
    APPROVAL_FAILED = "approval_failed"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    ERROR = "error"
    # Variable (secrets/configs) deployment stage — written by the multi-deployment
    # orchestrator's variable stage, in actual execution order (secrets are pushed
    # to Secrets Manager before configs go to SSM). Coarse milestones only; granular
    # steps (audit trail saved, etc.) live in pipeline_run_track.build_stages.
    STARTING_SECRETS_DEPLOYMENT = "starting_secrets_deployment"
    DEPLOYING_SECRETS = "deploying_secrets"
    SECRETS_DEPLOYED = "secrets_deployed"
    SECRETS_DEPLOYMENT_FAILED = "secrets_deployment_failed"
    STARTING_CONFIG_DEPLOYMENT = "starting_config_deployment"
    DEPLOYING_CONFIGS = "deploying_configs"
    CONFIGS_DEPLOYED = "configs_deployed"
    CONFIG_DEPLOYMENT_FAILED = "config_deployment_failed"


class ResourceStatusEnum(str, Enum):
    """UI-ready resource deployment status — displayed directly on canvas node badges.
    Granular workflow details (stages, logs) live in pipeline_run_track."""
    DRAFT = "DRAFT"               # Never deployed / just created
    PR_RAISED = "PR_RAISED"       # PR exists, waiting on review/merge — nothing running
    INITIALISING = "INITIALISING" # Deploy triggered, waiting for pipeline
    PROVISIONING = "PROVISIONING" # Pipeline picked up, early stages
    BUILDING = "BUILDING"         # Building image/artifact
    DEPLOYING = "DEPLOYING"       # Deploying to cluster/applying infra
    VERIFYING = "VERIFYING"       # Post-deploy verification
    ONLINE = "ONLINE"             # Successfully deployed and active
    FAILED = "FAILED"             # Deploy failed at any stage
    # Deletion lifecycle.
    # Soft = DB-only cleanup (variable_mst.is_deleted = true, references cascaded).
    # Hard = actual infra teardown (terragrunt destroy / kubectl delete / etc).
    SOFT_DELETING = "SOFT_DELETING"
    SOFT_DELETED = "SOFT_DELETED"
    HARD_DELETING = "HARD_DELETING"
    HARD_DELETED = "HARD_DELETED"

class PRStatusEnum(str, Enum):
    """Pull request status for GitOps workflow tracking"""
    PR_OPEN = "PR_OPEN"
    PR_MERGED = "PR_MERGED"
    PR_CLOSED = "PR_CLOSED"

class WorkflowSourceTableEnum(str, Enum):
    """Source table for polymorphic reference in gitops_workflow_detail"""
    SERVICE_CONFIG = "SERVICE_CONFIG"
    SERVICE_CONFIG_DOCKERFILE = "SERVICE_CONFIG_DOCKERFILE"
    ALERT_CONFIG = "ALERT_CONFIG"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    KONG_ROUTE = "KONG_ROUTE"
    PIPELINE = "PIPELINE"

class VariableTypeEnum(str, Enum):
    """Discriminator for variable_mst: plaintext variable vs external secret"""
    VARIABLE = "VARIABLE"
    SECRET = "SECRET"

class VariableScopeTypeEnum(str, Enum):
    """Scope type for variable_mst: global (shared) vs infra (resource-specific)"""
    GLOBAL = "GLOBAL"
    INFRA = "INFRA"

class VariableDataTypeEnum(str, Enum):
    """Data type hint for variable_mst values"""
    string = "string"
    integer = "integer"
    boolean = "boolean"
    json = "json"

class SecretProviderEnum(str, Enum):
    """External secret provider for SECRET-type variables"""
    AWS_SECRETS_MANAGER = "aws_secrets_manager"
    AWS_SSM = "aws_ssm"
    HASHICORP_VAULT = "hashicorp_vault"
    AZURE_KEY_VAULT = "azure_key_vault"
    GCP_SECRET_MANAGER = "gcp_secret_manager"
    K8S_SECRET = "k8s_secret"
    LOCAL = "local"

class AuthProviderEnum(str, Enum):
    clerk = "clerk"
    auth0 = "auth0"
    cognito = "cognito"
    firebase = "firebase"
    okta = "okta"
    manual = "manual"  # For manually created users

class ServiceTypeEnum(str, Enum):
    API = "API"
    BACKGROUND_SERVICE = "BACKGROUND_SERVICE"
    OPS_TOOLS = "OPS_TOOLS"  # Internal ops services (no Datadog, no Kong)
    MODEL_SERVING = "MODEL_SERVING"  # vLLM model hosting on EKS

class LogProviderEnum(str, Enum):
    CLOUDWATCH = "CLOUDWATCH"
    DATADOG = "DATADOG"

class AWSResourceTypeEnum(str, Enum):
    secret = "secret"
    parameter = "parameter"

class ResourceGroupKindEnum(str, Enum):
    service = "service"
    infra = "infra"

class PipelineAgentEnum(str, Enum):
    aws_codepipeline = "aws_codepipeline"
    azure_devops = "azure_devops"
    github_actions = "github_actions"
    gitlab_ci = "gitlab_ci"
    bitbucket_pipelines = "bitbucket_pipelines"
    circleci = "circleci"
    travisci = "travisci"
    drone = "drone"
    wercker = "wercker"
    buildkite = "buildkite"
    jenkins = "jenkins"
    temporal = "temporal"

class PipelineRunStatusEnum(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"

class AuditActionEnum(str, Enum):
    """Audit trail action types"""
    # CRUD Operations
    CREATE = "CREATE"
    READ = "READ"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

    # Authentication & Authorization
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    LOGIN_FAILED = "LOGIN_FAILED"
    PASSWORD_CHANGE = "PASSWORD_CHANGE"
    PASSWORD_RESET = "PASSWORD_RESET"

    # Access Control
    PERMISSION_GRANT = "PERMISSION_GRANT"
    PERMISSION_REVOKE = "PERMISSION_REVOKE"
    ROLE_ASSIGN = "ROLE_ASSIGN"
    ROLE_REMOVE = "ROLE_REMOVE"

    # Data Export & Reporting
    EXPORT = "EXPORT"
    REPORT_GENERATE = "REPORT_GENERATE"

    # Configuration Changes
    CONFIG_UPDATE = "CONFIG_UPDATE"

    # Integration Actions
    INTEGRATION_CONNECT = "INTEGRATION_CONNECT"
    INTEGRATION_DISCONNECT = "INTEGRATION_DISCONNECT"

    # Pipeline Actions
    PIPELINE_TRIGGER = "PIPELINE_TRIGGER"
    PIPELINE_CANCEL = "PIPELINE_CANCEL"

    # Alert Actions
    ALERT_CREATE = "ALERT_CREATE"
    ALERT_UPDATE = "ALERT_UPDATE"
    ALERT_DELETE = "ALERT_DELETE"
    ALERT_ACKNOWLEDGE = "ALERT_ACKNOWLEDGE"

    # System Events
    SYSTEM_STARTUP = "SYSTEM_STARTUP"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"


class ProductionDeploymentStatusEnum(str, Enum):
    """Where a prod dir's content stands between merge-to-stage and
    merge-to-main (prod_promotion_record.status). NOT the resource's
    deployment status (that stays ResourceDeploymentStatusEnum) — this
    drives the promotion PR's merge gate."""
    IN_PROGRESS = "IN_PROGRESS"   # merged to stage, not applied yet
    APPLIED = "APPLIED"           # live in AWS — safe to merge into main
    RESUMING = "RESUMING"         # parked deploy re-queued for apply
    FAILED = "FAILED"             # deploy died with content unapplied on stage
