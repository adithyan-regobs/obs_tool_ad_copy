"""
Service Configuration Schemas

Pydantic schemas for service configuration management.
"""
import re
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
from app.core.enum import EnvironmentEnum, InfraVendorEnum, WorkflowSourceTableEnum
from app.schemas.gitops_workflow_schemas import GitopsWorkflowDetailInfo
from app.core.enum import PRStatusEnum as PRStatus


# ============== Dockerfile PR Info Schema ==============

class DockerfilePRInfo(BaseModel):
    """Schema for individual Dockerfile PR information (one per branch)"""
    branch: str = Field(default="", description="Base branch name (e.g., main, stage)")
    feature_branch: str = Field(default="", description="Feature branch name (e.g., datadog/baa-main-dev-london)")
    pr_number: Optional[int] = Field(None, description="GitHub PR number")
    pr_url: Optional[str] = Field(None, description="GitHub PR URL")
    pr_status: Optional[str] = Field(None, description="PR status: PR_OPEN, PR_MERGED, PR_CLOSED")
    commit_sha: Optional[str] = Field(None, description="Latest commit SHA")
    git_repository: Optional[str] = Field(None, description="GitHub repository (owner/repo)")
    status: str = Field(default="pending", description="Sync status: success, skipped, error, pending")
    error: Optional[str] = Field(None, description="Error message if any")
    created_at: Optional[str] = Field(None, description="When PR was created/updated")
    dockerfile_workflow_code: Optional[str] = Field(None, description="Junction table code (SCDF_xxx) for PR history lookup")


# ============== Path Sanitization Helper Functions ==============

def sanitize_path_base(path: str) -> str:
    """Base sanitization - trim and remove dangerous characters"""
    if not path:
        return path
    path = path.strip()
    # Remove leading ./
    if path.startswith('./'):
        path = path[2:]
    # Remove null bytes and other dangerous chars
    path = re.sub(r'[\x00-\x1f\x7f]', '', path)
    # Remove shell injection chars (but keep / . - _ *)
    path = re.sub(r'[;&|`$]', '', path)
    return path


def sanitize_health_path(path: str) -> str:
    """Health check: must start with /, no trailing /"""
    path = sanitize_path_base(path)
    if not path:
        return path
    # Remove leading / first to normalize
    path = path.lstrip('/')
    # Remove trailing /
    path = path.rstrip('/')
    # Add leading / back
    return f'/{path}' if path else '/'


def sanitize_service_path(path: str) -> str:
    """Service path: ensure leading /, trailing / optional"""
    path = sanitize_path_base(path)
    if not path:
        return path
    # Ensure leading /
    if not path.startswith('/'):
        path = '/' + path
    return path


SERVICE_PATH_ROOT_ERROR = "Service path cannot be just /"

SERVICE_PATH_WILDCARD_ERROR = "Service path cannot end with /*"

MAX_SERVICE_PATH_LENGTH = 255
SERVICE_PATH_ALLOWED = re.compile(r'^/[A-Za-z0-9._~*/-]*$')


def is_eks_infra(infrastructuretype_ref_code: Optional[str]) -> bool:
    """True for an EKS infrastructure type.

    The trailing-/* rule is EKS-only, and the callers that can apply it hold a
    free-form snapshot rather than a typed model — so the test lives here once
    instead of being spelled differently in each of them.
    """
    return "eks" in (infrastructuretype_ref_code or "").lower()


def validate_service_path(path: str, *, reject_slash_wildcard: bool = False) -> str:
    """Sanitize a service path and reject catch-all, traversal and unsafe characters.

    `reject_slash_wildcard` is EKS-only. A trailing `/*` is a real problem for a
    Kubernetes ingress, but it is ordinary — and dominant — on an ECS ALB:
    infrastructure-v2 runs 80 of its 126 ECS service_paths in that form
    (`/goms-service/*`, `/bbps-service/*`, `/*`), alongside 47 of the `/astra*`
    form. Both are valid ALB path patterns.

    Applying the rule to ECS also contradicted the product itself: the ECS
    settings form APPENDS the wildcard (normalizeServicePath), so the app wrote
    values its own schema then refused. Hence the flag, off by default — a
    caller that knows it is EKS opts in.
    """
    path = sanitize_service_path(path)
    # '/', '//' and '///' are all the same catch-all rule
    if not path.strip('/'):
        raise ValueError(SERVICE_PATH_ROOT_ERROR)
    if len(path) > MAX_SERVICE_PATH_LENGTH:
        raise ValueError(
            f"Service path cannot be longer than {MAX_SERVICE_PATH_LENGTH} characters."
        )
    if '..' in path:
        raise ValueError("Service path cannot contain '..'.")
    if reject_slash_wildcard and path.endswith('/*'):
        raise ValueError(SERVICE_PATH_WILDCARD_ERROR)
    # The value is interpolated unquoted into generated HCL and YAML, so anything
    # outside this set can break or inject into the generated manifests.
    if not SERVICE_PATH_ALLOWED.match(path):
        raise ValueError(
            "Service path may only contain letters, digits and . _ ~ - / * "
            "and must start with /"
        )
    return path


def sanitize_build_path(path: str) -> str:
    """Build path: no leading /, trailing / optional"""
    path = sanitize_path_base(path)
    if not path:
        return path
    # Remove leading /
    return path.lstrip('/')


def build_path_for_language(path: Optional[str], is_go: bool) -> Optional[str]:
    """The ONE rule for how build_path is stored, by language.

    Go:     `./cmd`  — `go build cmd` resolves an import path and fails; the
                       directory form is what the user must see and what the
                       workflow gets.
    Others: `cmd`    — trigger filters, jar paths and repo file paths are all
                       repo-relative and never want a ./ prefix.
    Both spellings of the input are accepted; the output is canonical.
    """
    bare = sanitize_build_path(path) if path else path
    if not bare:
        return bare
    if is_go and bare not in (".",) and not bare.startswith("./"):
        return f"./{bare}"
    return bare


def sanitize_dockerfile_path(path: str) -> str:
    """Dockerfile path: no leading /, no trailing /"""
    path = sanitize_path_base(path)
    if not path:
        return path
    # Remove leading and trailing /
    return path.strip('/')


def sanitize_trigger_path(path: str) -> str:
    """Additional trigger paths: no leading /, trailing / optional"""
    path = sanitize_path_base(path)
    if not path:
        return path
    # Remove leading /
    return path.lstrip('/')


# ============== Docker Build Arguments Schema ==============

class BuildArgSchema(BaseModel):
    """Schema for Docker build argument (ARG in Dockerfile)"""
    # Support both 'name' and 'key' for backward compatibility with existing data
    name: Optional[str] = Field(None, description="ARG name (e.g., APP_VERSION)")
    key: Optional[str] = Field(None, description="Alias for 'name' (backward compatibility)")
    value: str = Field(..., description="ARG value (e.g., 1.0.0)")

    @model_validator(mode='before')
    @classmethod
    def normalize_name_field(cls, data: Any) -> Any:
        """Normalize 'key' to 'name' for backward compatibility"""
        if isinstance(data, dict):
            # If 'key' is provided but 'name' is not, use 'key' as 'name'
            if data.get('key') and not data.get('name'):
                data['name'] = data['key']
            # If neither 'name' nor 'key' is provided, raise error
            if not data.get('name') and not data.get('key'):
                raise ValueError("Either 'name' or 'key' must be provided for build argument")
        return data

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        """Validate ARG name follows Docker conventions (uppercase letters, digits, underscores)"""
        if v is None:
            return v
        if not v.strip():
            raise ValueError("ARG name cannot be empty")
        v = v.strip()
        # Docker ARG names: letters, digits, underscores (conventionally uppercase)
        if not re.match(r'^[A-Z][A-Z0-9_]*$', v):
            raise ValueError("ARG name must be uppercase letters, digits, and underscores, starting with a letter (e.g., APP_VERSION, BUILD_NUMBER)")
        return v

    @field_validator("value")
    @classmethod
    def sanitize_value(cls, v: str) -> str:
        """Sanitize ARG value to prevent command injection"""
        if v is None:
            return ""
        v = v.strip()
        # Remove dangerous shell characters
        v = re.sub(r'[;&|`$]', '', v)
        # Remove null bytes and control characters
        v = re.sub(r'[\x00-\x1f\x7f]', '', v)
        return v

    @property
    def arg_name(self) -> str:
        """Get the actual arg name (prioritize 'name' over 'key')"""
        return self.name or self.key or ""


class EbsConfigSchema(BaseModel):
    """EBS configuration schema"""
    volume: str
    size: str
    type: str  # gp3, gp2, io2, io1, st1, sc1


class AutoScalingConfigSchema(BaseModel):
    """Auto scaling configuration schema"""
    enabled: bool = True
    min: Optional[str] = Field(None, description="Min Task Count")
    max: Optional[str] = Field(None, description="Max Task Count")
    desired: Optional[str] = Field(None, description="Desired Count")

    @field_validator("min", "max", "desired")
    @classmethod
    def validate_task_count(cls, v: Optional[str]) -> Optional[str]:
        """Validate task count is a non-negative integer"""
        if v is not None and v.strip():
            try:
                count = int(v)
                if count < 0:
                    raise ValueError("Must be a non-negative integer")
            except ValueError:
                raise ValueError("Must be a valid integer")
        return v

    @model_validator(mode='after')
    def validate_autoscaling_logic(self):
        """Validate min <= desired <= max when autoscaling is enabled.
        Auto-disable autoscaling if required values are missing."""
        if self.enabled:
            # If values are missing, auto-disable autoscaling instead of failing
            if not self.min or not self.max or not self.desired:
                self.enabled = False
                return self

            min_val = int(self.min)
            max_val = int(self.max)
            desired_val = int(self.desired)

            if min_val > max_val:
                raise ValueError("Min cannot be greater than Max")
            if desired_val < min_val:
                raise ValueError("Desired cannot be less than Min")
            if desired_val > max_val:
                raise ValueError("Desired cannot be greater than Max")
        return self


class HPAConfigSchema(BaseModel):
    """HPA (Horizontal Pod Autoscaler) configuration for EKS."""
    enabled: bool = False
    min_replicas: Optional[str] = None
    max_replicas: Optional[str] = None
    cpu_threshold: Optional[str] = None
    memory_threshold: Optional[str] = None


# ============== Deployment Strategy Schemas ==============

class CanaryStepSchema(BaseModel):
    """Schema for a canary deployment traffic shifting step."""
    id: str = Field(description="Unique step identifier")
    weight: int = Field(ge=0, le=100, description="Traffic percentage (0-100)")
    pauseDuration: int = Field(ge=0, description="Pause duration in seconds")
    pauseType: str = Field(default="duration", description="Pause type: 'duration' or 'manual'")

    @field_validator("pauseType")
    @classmethod
    def validate_pause_type(cls, v: str) -> str:
        valid_types = ["duration", "manual"]
        if v.lower() not in valid_types:
            raise ValueError(f"Pause type must be one of: {', '.join(valid_types)}")
        return v.lower()


class CanaryConfigSchema(BaseModel):
    """Schema for canary deployment configuration."""
    canaryService: str = Field(default="service-canary", description="Canary service name")
    stableService: str = Field(default="service-stable", description="Stable service name")
    analysisEnabled: bool = Field(default=False, description="Enable automated analysis")
    steps: List[CanaryStepSchema] = Field(default_factory=list, description="Traffic shifting steps")


class BlueGreenConfigSchema(BaseModel):
    """Schema for blue-green deployment configuration."""
    activeService: str = Field(default="service-active", description="Active service name")
    previewService: str = Field(default="service-preview", description="Preview service name")
    autoPromote: bool = Field(default=False, description="Auto-promote after validation")
    scaleDownDelay: int = Field(default=30, ge=0, description="Delay before scaling down old version (seconds)")
    previewReplicas: int = Field(default=1, ge=1, description="Number of preview replicas")


class RollingConfigSchema(BaseModel):
    """Schema for rolling update deployment configuration."""
    maxSurge: str = Field(default="25%", description="Max surge during rolling update (e.g., '25%' or '1')")
    maxUnavailable: str = Field(default="25%", description="Max unavailable during rolling update (e.g., '25%' or '0')")


class DeploymentStrategySchema(BaseModel):
    """Schema for deployment strategy configuration."""
    strategy: str = Field(default="rolling", description="Deployment strategy: canary, bluegreen, rolling, recreate")
    canary: Optional[CanaryConfigSchema] = Field(None, description="Canary deployment configuration")
    blueGreen: Optional[BlueGreenConfigSchema] = Field(None, description="Blue-green deployment configuration")
    rolling: Optional[RollingConfigSchema] = Field(None, description="Rolling update configuration")

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        valid_strategies = ["canary", "bluegreen", "rolling", "recreate"]
        if v.lower() not in valid_strategies:
            raise ValueError(f"Strategy must be one of: {', '.join(valid_strategies)}")
        return v.lower()


class EKSBuildConfigSchema(BaseModel):
    """EKS build configuration schema for EKS pipelines"""
    # Build type: gradle | maven | docker-only
    type: str = Field(default="maven", description="Build type: gradle, maven, or docker-only")

    # Common options
    skip_tests: bool = Field(default=True, description="Skip tests during build")
    skip_checks: bool = Field(default=True, description="Skip code checks during build")

    # Snyk/Quality options (override environment defaults)
    skip_code_quality: Optional[bool] = Field(None, description="Skip code quality checks (None = use env default)")
    skip_snyk_code: Optional[bool] = Field(None, description="Skip Snyk code scan (None = use env default)")
    skip_snyk_oss: Optional[bool] = Field(None, description="Skip Snyk OSS scan (None = use env default)")
    skip_snyk_container: Optional[bool] = Field(None, description="Skip Snyk container scan (None = use env default)")

    # Gradle-specific options
    gradle_jar_file: Optional[str] = Field(default="build/libs/*.jar", description="Gradle JAR file path")
    gradle_tasks: Optional[str] = Field(default="assemble", description="Gradle build tasks")
    gradle_jvm_args: Optional[str] = Field(default="-Xmx4g -XX:+UseG1GC", description="Gradle JVM arguments")
    gradle_workers_max: Optional[int] = Field(default=4, description="Max Gradle workers")

    # Maven-specific options
    maven_jar_file: Optional[str] = Field(default="target/*.jar", description="Maven JAR file path")
    maven_goals: Optional[str] = Field(default="clean package", description="Maven goals")
    maven_repo_id: Optional[str] = Field(default="github", description="Maven repository ID")
    maven_repo_name: Optional[str] = Field(None, description="Maven repository name")
    maven_repo_url: Optional[str] = Field(None, description="Maven repository URL")
    maven_profile: Optional[str] = Field(None, description="Maven profile")
    maven_xms: Optional[str] = Field(default="2560m", description="Maven -Xms value")
    maven_xmx: Optional[str] = Field(default="5632m", description="Maven -Xmx value")

    @field_validator("type")
    @classmethod
    def validate_build_type(cls, v: str) -> str:
        """Validate build type is one of: gradle, maven, docker-only"""
        valid_types = ["gradle", "maven", "docker-only"]
        if v.lower() not in valid_types:
            raise ValueError(f"Build type must be one of: {', '.join(valid_types)}")
        return v.lower()


class MainConfigSchema(BaseModel):
    """Main service configuration schema for config JSONB field"""
    # ALB Configuration
    alb_selection: Optional[str] = "existing_alb"  # "no_alb" | "existing_alb" | "create_new_alb"

    # Resource Allocation
    cpu: Optional[str] = None
    ram: Optional[str] = None
    port: Optional[str] = None
    health: Optional[str] = None

    # Auto Scaling Configuration (nested structure)
    autoscaling: Optional[AutoScalingConfigSchema] = Field(
        default_factory=lambda: AutoScalingConfigSchema(enabled=True)
    )

    # EBS Storage
    ebs_enabled: bool = False
    ebs: Optional[EbsConfigSchema] = None

    # Process Limits
    enable_ulimits: bool = True

    # Service Configuration
    service_path: Optional[str] = None
    listener_rule_priority: Optional[str] = None
    build_path: Optional[str] = None  # Build path for JAR/Go build location
    other_paths: Optional[List[str]] = None  # Additional paths that trigger the workflow

    # Docker Configuration
    dockerfile_path: Optional[str] = None  # Path to Dockerfile (e.g., ./Dockerfile or services/api/Dockerfile)
    dockerfile_content: Optional[str] = Field(None, description="Dockerfile content for EKS deployments when generate_dockerfile is enabled")

    # Wire Dependency Injection (Go projects only)
    wire_enabled: bool = False  # Enable Wire dependency injection code generation
    wire_path: Optional[str] = None  # Path to wire directory (e.g., "./pkg/wire", "./wire")

    # Repository Configuration
    repository: Optional[str] = None  # Repository name/path
    branches: Optional[List[str]] = None  # All available branch names (for dropdown)
    selected_branches: Optional[List[str]] = None  # Branches selected for workflow triggers (subset of branches)

    # JVM Memory Configuration (for Datadog Dockerfile modification)
    xms: Optional[str] = None  # -Xms value in MB (e.g., "512") - Initial heap size
    xmx: Optional[str] = None  # -Xmx value in MB (e.g., "1024") - Maximum heap size

    # HTTP Scaling Configuration (API only - not for background services)
    http_scaling_enabled: bool = False  # Enable HTTP-based auto scaling
    http_scaling_target_value: Optional[str] = None  # Target value for HTTP scaling (e.g., "3000")

    # Dockerfile Generation Configuration (Java and Go)
    generate_dockerfile: bool = False  # Generate standardized Dockerfile at docker/{service-name}/Dockerfile

    # Go Dockerfile Configuration
    go_config_path: Optional[str] = None  # Config file path for Go (e.g., "configs/config.json")
    go_use_aws_secrets: bool = False  # Enable AWS Secrets Manager in Go Dockerfile

    # Docker Build Arguments (custom ARG_NAME=value pairs)
    build_args: Optional[List[BuildArgSchema]] = Field(
        default=None,
        description="Docker build arguments passed as --build-arg to docker build command"
    )

    # ============== EKS-Specific Configuration ==============
    # EKS build configuration (only used when infrastructuretype_ref_code = eks_infrastructuretype_ref)
    eks_build_config: Optional[EKSBuildConfigSchema] = None  # EKS build config (gradle/maven options)
    java_version: Optional[str] = Field(default="17", description="Java version for EKS builds (e.g., 17, 21)")
    slack_channel_id: Optional[str] = Field(None, description="Slack channel ID for EKS notifications (optional)")

    # ============== EKS Deployment Configuration ==============
    # These fields are stored for shared-lib workflows to use at deployment time
    namespace: Optional[str] = Field(None, description="Kubernetes namespace for EKS deployment")
    cpu_requested: Optional[str] = Field(None, description="CPU request for EKS pod in millicores (e.g., '500m'). Max 5000m.")
    cpu_limit: Optional[str] = Field(None, description="CPU limit for EKS pod in millicores (e.g., '1000m'). Max 5000m.")
    memory_requested: Optional[str] = Field(None, description="Memory request for EKS pod (e.g., '512Mi', '1Gi', '768Gi'). Clamped to instance capacity by backend.")
    memory_limit: Optional[str] = Field(None, description="Memory limit for EKS pod (e.g., '512Mi', '1Gi', '768Gi'). Clamped to instance capacity by backend.")
    alb_schema: Optional[str] = Field(None, description="ALB schema: 'internal' or 'internet-facing'")
    secret_keys: Optional[str] = Field(None, description="Comma-separated secret keys for EKS")
    secrets_enabled: Optional[bool] = Field(False, description="Enable secrets injection for EKS")
    hpa: Optional[HPAConfigSchema] = Field(None, description="HPA configuration for EKS autoscaling")
    replica_count: Optional[str] = Field(None, description="Replica count for EKS deployment")
    compute: Optional[str] = Field("on-demand", description="Compute type for node scheduling: on-demand or spot")

    # ============== EKS Terraform Provisioning ==============
    custom_iam_policies: Optional[List[str]] = Field(None, description="AWS service short-names (s3, sqs, dynamodb, ses) granted region-scoped FullAccess on the pod IAM role")
    create_ecr: Optional[bool] = Field(True, description="Create ECR repository and grant the app role pull access")
    create_secrets: Optional[bool] = Field(True, description="Create Secrets Manager secret and grant the app role read access")
    create_ssm: Optional[bool] = Field(True, description="Create SSM parameters and grant the app role read access")
    create_argo: Optional[bool] = Field(True, description="Create Argo CD Application resource")
    auth_mode: Optional[str] = Field("pod_identity", description="IAM auth mode for the pod: pod_identity, irsa, or none")

    # ============== Environment Variables (K8s Secrets) ==============
    # Stores variable NAMES only — values are NEVER persisted in DB.
    # Values are passed in-memory at deploy time and injected as K8s Opaque Secrets.
    env_variables: Optional[List[Dict[str, str]]] = Field(
        None,
        description="Environment variable definitions. Each entry: {name: 'VAR_NAME', is_secret: 'true'|'false'}. "
                    "Secret values are NOT stored — only key names."
    )

    # ============== EKS Pipeline Configuration ==============
    pipeline_steps: Optional[List[Dict[str, Any]]] = Field(
        None,
        description="Customizable workflow steps for EKS inline pipeline generation. If not provided, default steps will be used. Each step should have: id, name, enabled (bool), dependsOn (array of step IDs)"
    )

    # ============== Model Serving Configuration ==============
    service_name: Optional[str] = Field(None, description="Canvas node label — used to build the KServe ALB URL slug")
    service_type: Optional[str] = Field(None, description="Service type: API, BACKGROUND_SERVICE, OPS_TOOLS, MODEL_SERVING")
    model_name: Optional[str] = Field(None, description="HuggingFace model name (e.g., meta-llama/Llama-3.1-8B-Instruct)")
    gpu_type: Optional[str] = Field(None, description="GPU type: T4, L4, A10G, L40S, A100, H100")
    gpu_count: Optional[int] = Field(None, description="Number of GPUs per pod")
    vllm_image: Optional[str] = Field(None, description="vLLM Docker image (default: vllm/vllm-openai:latest)")
    container_port: Optional[str] = Field(None, description="Container port (default: 8000 for vLLM)")
    runtime: Optional[str] = Field(None, description="Model serving runtime: vllm, tgi, triton")
    additional_args: Optional[str] = Field(None, description="Extra flags passed to the vLLM server")
    quantization: Optional[str] = Field(None, description="vLLM quantization method: awq, gptq, fp8, bitsandbytes, marlin, squeezellm, gguf")
    tensor_parallel_size: Optional[int] = Field(None, description="Tensor parallelism degree (defaults to gpu_count)")
    pipeline_parallel_size: Optional[int] = Field(None, description="Pipeline parallelism degree (defaults to 1)")
    dtype: Optional[str] = Field(None, description="Model data type: auto, float16, bfloat16, float32")
    trust_remote_code: Optional[bool] = Field(None, description="Allow custom model code from HuggingFace repo")
    max_model_len: Optional[int] = Field(None, description="Max token sequence length (overrides default 8192 cap)")
    enable_prefix_caching: Optional[bool] = Field(None, description="Enable vLLM prefix caching")
    enable_chunked_prefill: Optional[bool] = Field(None, description="Enable chunked prefill (interleave prefill and decode)")
    enforce_eager: Optional[bool] = Field(None, description="Disable CUDA graphs (use for debugging)")
    max_num_seqs: Optional[int] = Field(None, description="Max concurrent requests per replica (--max-num-seqs)")
    hf_token_secret_path: Optional[str] = Field(None, description="AWS Secrets Manager path for HuggingFace token")

    # ============== KServe Autoscaling (used by InferenceService CRD) ==============
    min_replicas: Optional[int] = Field(default=0, ge=0, description="0 = scale-to-zero via Knative KPA")
    max_replicas: Optional[int] = Field(default=3, ge=1, description="Max pod replicas for autoscaling")
    scale_target: Optional[int] = Field(default=1, ge=1, description="Target concurrent requests per pod")

    # ============== EFS Model Storage (populated at deploy time by ServiceConfigService) ==============
    model_revision: Optional[str] = Field(None, description="Resolved HuggingFace commit SHA")
    efs_path: Optional[str] = Field(None, description="PVC-root-relative path (no leading slash)")
    download_job_name: Optional[str] = Field(None, description="K8s Job name for this model download")

    # ============== ALB URL (pre-calculated for MODEL_SERVING, set by webhook for regular) ==============
    alb_url: Optional[str] = Field(None, description="Service endpoint URL (pre-calculated for KServe, set by webhook for regular EKS)")

    # ============== Cluster Context (populated on service creation from canvas) ==============
    cluster_name: Optional[str] = Field(None, description="EKS/ECS cluster name")
    cluster_arn: Optional[str] = Field(None, description="EKS/ECS cluster ARN")
    region: Optional[str] = Field(None, description="AWS region (e.g., ap-south-1)")
    cloud_region_id: Optional[str] = Field(None, description="Cloud region master code")
    subnet_ids: Optional[List[str]] = Field(None, description="VPC subnet IDs associated with the cluster")
    launch_type: Optional[str] = Field(None, description="ECS launch type (FARGATE or EC2)")
    vpc_id: Optional[str] = Field(None, description="AWS VPC ID where the cluster runs (e.g., vpc-053303420fb12a6dc)")

    # ============== Validators ==============

    @field_validator("cpu", "ram")
    @classmethod
    def validate_resource(cls, v: Optional[str]) -> Optional[str]:
        """Validate CPU and RAM are positive numbers"""
        if v is not None and v.strip():
            try:
                num = float(v)
                if num <= 0:
                    raise ValueError("Must be a positive number")
            except ValueError:
                raise ValueError("Must be a valid positive number")
        return v

    @field_validator("alb_schema")
    @classmethod
    def validate_alb_schema(cls, v: Optional[str]) -> Optional[str]:
        """
        Validate ALB schema is one of: internal, internet-facing.

        None stays valid — 27 rows have no value, and the field is meaningless
        when alb_selection is no_alb.
        """
        if v is None:
            return v
        valid_schemas = ["internal", "internet-facing"]
        if v.strip().lower() not in valid_schemas:
            raise ValueError(f"ALB schema must be one of: {', '.join(valid_schemas)}")
        return v.strip().lower()

    @field_validator("compute")
    @classmethod
    def validate_compute(cls, v: Optional[str]) -> Optional[str]:
        """
        Validate compute type is one of: on-demand, spot.

        Substituted into the Helm values as `capacityType`
        (templates/k8s-manifests/environments/{service,worker}/values.yaml). The
        generator only falls back when the value is falsy, so any other string
        reaches Karpenter verbatim and the pods never schedule — a failure that
        lands mid-deploy, far from the field that caused it.
        """
        if v is None:
            return v
        valid_types = ["on-demand", "spot"]
        if v.strip().lower() not in valid_types:
            raise ValueError(f"Compute type must be one of: {', '.join(valid_types)}")
        return v.strip().lower()

    @field_validator("custom_iam_policies")
    @classmethod
    def validate_custom_iam_policies(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """
        Validate AWS service short-names against the set the generator knows.

        This one is a privilege boundary, not hygiene. Each entry becomes a
        region-scoped FullAccess statement on the pod IAM role —
        Action = ["<name>:*"], Resource = "*" — and _resolve_service_alias in
        aspora_eks_terragrunt_script_gen_component falls back to using ANY
        unknown string as the action prefix verbatim. Unconstrained, "iam"
        grants iam:* and "ec2" grants ec2:*. Junk like "banana" at least fails
        loudly at apply; a real-but-unintended service prefix does not fail
        at all.

        Kept in step with AWS_SERVICE_ALIASES in that component. Listed here
        rather than imported: the alias map lives in a tenant-specific plugin
        and this schema is tenant-agnostic — a new service belongs in both.
        """
        if v is None:
            return v
        valid_services = [
            "s3", "sqs", "dynamo", "dynamodb", "secrets", "secretsmanager",
            "kms", "lambda", "ses", "sns",
        ]
        cleaned: List[str] = []
        seen: set = set()
        for raw in v:
            name = (raw or "").strip().lower()
            if name not in valid_services:
                raise ValueError(
                    f"Unknown AWS service '{raw}' — must be one of: {', '.join(valid_services)}"
                )
            if name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        return cleaned

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, v: Optional[str]) -> Optional[str]:
        """
        Validate the GitHub repository as "owner/repo".

        None stays allowed — a service can exist before a repository is picked,
        and 18 stored rows have no repository. Only a value that IS supplied is
        checked, so this cannot reject a row on an unrelated save.

        The UI requires a repository before it will save, but only checks that
        it is non-blank; the shape is enforced here because the value is used to
        build clone URLs and workflow paths, where "foo" and "" fail far away
        from where they were entered.
        """
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Repository cannot be blank")
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", v):
            raise ValueError(
                f"Repository must be in 'owner/repo' form (got '{v}')"
            )
        return v

    @field_validator("branches", "selected_branches")
    @classmethod
    def validate_branches(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """
        Validate branch lists: no blank entries, no duplicates.

        None stays allowed (26 stored rows have none), and so does an empty
        list — the UI requires at least one branch, but a config can legitimately
        be saved before branches are loaded, and demanding one here would reject
        those rows on an unrelated edit.
        """
        if v is None:
            return v
        cleaned: List[str] = []
        seen: set = set()
        for b in v:
            name = (b or "").strip()
            if not name:
                raise ValueError("Branch name cannot be blank")
            if name in seen:
                raise ValueError(f"Duplicate branch '{name}'")
            seen.add(name)
            cleaned.append(name)
        return cleaned

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: Optional[str]) -> Optional[str]:
        """Validate port is between 1 and 65535"""
        if v is not None and v.strip():
            try:
                port = int(v)
                if port < 1 or port > 65535:
                    raise ValueError("Port must be between 1 and 65535")
            except ValueError:
                raise ValueError("Port must be a valid integer between 1-65535")
        return v

    @field_validator("listener_rule_priority")
    @classmethod
    def validate_priority(cls, v: Optional[str]) -> Optional[str]:
        """Validate listener rule priority is between 1 and 50000"""
        if v is not None and v.strip():
            try:
                priority = int(v)
                if priority < 1 or priority > 50000:
                    raise ValueError("Priority must be between 1 and 50000")
            except ValueError:
                raise ValueError("Priority must be a valid integer")
        return v

    @field_validator("health")
    @classmethod
    def sanitize_health(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize health path: must start with /, no trailing /"""
        return sanitize_health_path(v) if v else v

    @field_validator("service_path")
    @classmethod
    def sanitize_service(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize service path: ensure leading /, reject the bare '/' catch-all"""
        return validate_service_path(v) if v else v

    @field_validator("build_path")
    @classmethod
    def sanitize_build(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize build path: no leading /, trailing / optional"""
        return sanitize_build_path(v) if v else v

    @field_validator("dockerfile_path")
    @classmethod
    def sanitize_dockerfile(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize dockerfile path: no leading /, no trailing /"""
        return sanitize_dockerfile_path(v) if v else v

    @field_validator("other_paths")
    @classmethod
    def sanitize_trigger_paths(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """Sanitize trigger paths: no leading /, trailing / optional"""
        if v:
            return [sanitize_trigger_path(p) for p in v if p]
        return v

    @field_validator("wire_path")
    @classmethod
    def sanitize_wire_path(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize wire path: normalize to ./path format"""
        if not v:
            return v
        v = sanitize_path_base(v)
        # Ensure path starts with ./ for wire command
        if not v.startswith('./'):
            v = './' + v.lstrip('/')
        return v

    @field_validator("go_config_path")
    @classmethod
    def sanitize_go_config_path(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize Go config path: no leading /, trailing / optional"""
        return sanitize_build_path(v) if v else v

    @field_validator("cpu_requested", "cpu_limit")
    @classmethod
    def normalize_cpu(cls, v: Optional[str]) -> Optional[str]:
        """Store CPU as a plain, suffix-free number; never append a unit here.

        The FE sends raw numbers whose meaning depends on the deploy path
        (EKS=cores, PaaS=millicores) — a distinction this shared validator can't
        see. Each render/deploy path converts (EKS ×1000 -> 'm'; PaaS appends
        its own 'm'), so appending or multiplying here would be correct for one
        path and wrong for the other. We only strip a stray 'm' (healing old
        FE / mangled values) and validate. Max 5000 (cores or millicores).
        """
        if not v or not v.strip():
            return v
        numeric = v.strip()
        if numeric.endswith("m"):
            numeric = numeric[:-1]
        try:
            num = float(numeric)
        except ValueError:
            raise ValueError("CPU must be a valid number (e.g., 0.5, 1, 2)")
        if num <= 0:
            raise ValueError("CPU must be a positive number")
        if num > 5000:
            raise ValueError("CPU value is out of range")
        return numeric

    @field_validator("memory_requested", "memory_limit")
    @classmethod
    def normalize_memory(cls, v: Optional[str]) -> Optional[str]:
        """Store memory as a plain, suffix-free number; never append a unit here.

        Same rationale as normalize_cpu: the FE sends raw numbers whose unit
        depends on the deploy path (EKS=GiB, PaaS=Mi), which this shared
        validator can't see. Each render/deploy path converts (EKS ×1024 ->
        'Mi'; PaaS appends its own 'Mi'). We only strip a stray Mi/Gi/MB/GB
        suffix (healing old FE / mangled values) and validate positivity.
        No hard cap — GPU model-serving pods can request hundreds of GiB;
        per-instance clamping is enforced in clamp_resources().
        """
        if not v or not v.strip():
            return v
        numeric = re.sub(r'(Mi|Gi|MB|GB)$', '', v.strip(), flags=re.IGNORECASE)
        try:
            num = float(numeric)
        except ValueError:
            raise ValueError("Memory must be a valid number (e.g., 0.5, 1, 3)")
        if num <= 0:
            raise ValueError("Memory must be a positive number")
        return numeric

    @model_validator(mode='after')
    def validate_wire_config(self):
        """Validate wire_path is provided when wire_enabled is True"""
        if self.wire_enabled and not self.wire_path:
            raise ValueError("wire_path is required when wire_enabled is True")
        return self


class MainConfigReadSchema(MainConfigSchema):
    """MainConfigSchema for RESPONSES: every path comes back exactly as saved.

    The parent's path validators strip a leading ./ — right for INPUT, wrong
    for output: Pydantic also runs them when a response model is built from
    the DB row, so a Go row holding `./cmd` was served as `cmd`. A read must
    show what the row holds, for every path field. Overriding the validators
    by the same method names makes them pass-through here; everything else on
    the parent (defaults, cpu/memory normalisation) still applies.
    """

    @field_validator("build_path")
    @classmethod
    def sanitize_build(cls, v: Optional[str]) -> Optional[str]:
        return v

    @field_validator("dockerfile_path")
    @classmethod
    def sanitize_dockerfile(cls, v: Optional[str]) -> Optional[str]:
        return v

    @field_validator("other_paths")
    @classmethod
    def sanitize_trigger_paths(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return v

    @field_validator("go_config_path")
    @classmethod
    def sanitize_go_config_path(cls, v: Optional[str]) -> Optional[str]:
        return v

    @field_validator("service_path")
    @classmethod
    def sanitize_service(cls, v: Optional[str]) -> Optional[str]:
        """Served exactly as stored — the same rule as every path above, which
        this one was missing.

        validate_service_path REJECTS rather than rewrites, so on a response it
        did not merely reshape the value: it raised, and the whole
        ServiceConfigResponse failed to build. Every ECS service whose path ends
        `/*` — 80 of the 126 in infrastructure-v2, and 97 rows here — became
        unreadable, so the panel reported "this service has no saved
        configuration yet" and refused the Save that could have corrected it.
        A read must never be able to fail on data the system itself wrote.
        """
        return v


class DatadogAdvancedOption(BaseModel):
    """Single Datadog ENV variable configuration"""
    key: str  # e.g., "DD_LOGS_INJECTION"
    value: str  # e.g., "true"


class SidecarOverrideSchema(BaseModel):
    """Sidecar override schema for sidecar_config JSONB array"""
    sidecar_config_code: str
    enabled: bool = True
    cpu: str
    ram: str
    name: Optional[str] = None  # Populated from sidecar_configs table on GET
    advanced_options: Optional[List[DatadogAdvancedOption]] = None  # Service-specific overrides for Datadog
    default_advanced_options: Optional[List[DatadogAdvancedOption]] = None  # Default values from master (read-only)
    datadog_logs_enabled: Optional[bool] = None  # Enable Datadog logs collection (synced to HCL)

    @field_validator("cpu", "ram")
    @classmethod
    def validate_sidecar_resource(cls, v: str) -> str:
        """Validate sidecar CPU and RAM are positive numbers"""
        if not v or not v.strip():
            raise ValueError("Required field")
        try:
            num = float(v)
            if num <= 0:
                raise ValueError("Must be a positive number")
        except ValueError:
            raise ValueError("Must be a valid positive number")
        return v


class ServiceConfigCreate(BaseModel):
        """Schema for creating a new service configuration"""
        services_mst_code: str = Field(..., description="Service code")
        infrastructuretype_ref_code: str = Field(..., description="Infrastructure type code")
        infra_vendor_enum: InfraVendorEnum = Field(..., description="Infrastructure vendor (aws, azure, gcp, on_prem)")
        infrastructure_mst_code: Optional[str] = Field(None, description="Infrastructure instance (cluster) code")
        environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
        geo_loc_mst_code: str = Field(..., description="Geographic location code")
        language_ref_code: Optional[str] = Field(None, description="Language reference code")
        config: Optional[MainConfigSchema] = Field(None, description="Main service configuration")
        sidecar_config: List[SidecarOverrideSchema] = Field(default_factory=list, description="Sidecar configurations")
        deployment_strategy: Optional[DeploymentStrategySchema] = Field(None, description="Deployment strategy configuration (canary, bluegreen, rolling, recreate)")
        sync_to_github: bool = Field(default=True, description="Whether to sync to GitHub and create/update PR")


class ServiceConfigUpdate(BaseModel):
    """Schema for updating an existing service configuration"""
    infrastructuretype_ref_code: Optional[str] = Field(None, description="Infrastructure type code")
    infra_vendor_enum: Optional[InfraVendorEnum] = Field(None, description="Infrastructure vendor (aws, azure, gcp, on_prem)")
    infrastructure_mst_code: Optional[str] = Field(None, description="Infrastructure instance (cluster) code")
    language_ref_code: Optional[str] = Field(None, description="Language reference code")
    config: Optional[MainConfigSchema] = Field(None, description="Main service configuration")
    sidecar_config: Optional[List[SidecarOverrideSchema]] = Field(None, description="Sidecar configurations")
    deployment_strategy: Optional[DeploymentStrategySchema] = Field(None, description="Deployment strategy configuration (canary, bluegreen, rolling, recreate)")
    sync_to_github: bool = Field(default=True, description="Whether to sync to GitHub and create/update PR")


class SidecarConfigResponse(BaseModel):
    """Schema for available sidecar configurations (for dropdown)"""
    code: str
    name: str
    description: Optional[str] = None
    default_cpu: str
    default_ram: str
    environment: str
    default_advanced_options: Optional[List[DatadogAdvancedOption]] = None  # Default Datadog options from master

    class Config:
        from_attributes = True


class ServiceConfigResponse(BaseModel):
    """Schema for service configuration response"""
    table_name: WorkflowSourceTableEnum = Field(default=WorkflowSourceTableEnum.SERVICE_CONFIG, description="Target table name")
    id: int
    code: str
    name: str
    tenant_mst_code: str
    services_mst_code: str
    infrastructuretype_ref_code: str
    infra_vendor_enum: Optional[str] = None  # Infrastructure vendor (aws, azure, gcp, on_prem)
    infrastructure_mst_code: Optional[str] = None  # Infrastructure instance (cluster) code
    environment: str
    geo_loc_mst_code: Optional[str] = None
    alb_selection: str = "existing_alb"  # ALB selection type column
    language_ref_code: Optional[str] = None
    namespace: Optional[str] = None  # Kubernetes namespace for EKS deployments
    # Read variant: every path served exactly as stored.
    config: Optional[MainConfigReadSchema] = None
    health_url: Optional[str] = Field(
        None,
        description=(
            "Externally reachable health endpoint, derived from config.alb_url "
            "and config.health. Null until the service has been deployed once. "
            "Derived server-side so every client shows the same URL."
        ),
    )
    sidecar_config: List[SidecarOverrideSchema] = Field(default_factory=list)
    deployment_strategy: Optional[DeploymentStrategySchema] = Field(None, description="Deployment strategy configuration")
    gitops_workflow: Optional[GitopsWorkflowDetailInfo] = None
    dockerfile_gitops_workflows: List[DockerfilePRInfo] = Field(default_factory=list, description="Dockerfile PRs for each branch")
    sync_status: str = Field(default="NEVER_SYNCED", description="Sync status: NEVER_SYNCED, PENDING_SYNC, SYNCED")
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ============== Listener Priority Validation Schema ==============

class ListenerPriorityValidationResponse(BaseModel):
    """Response schema for listener priority validation"""
    valid: bool = Field(..., description="Whether the priority is available")
    priority: str = Field(..., description="The priority that was checked")
    message: str = Field(..., description="Human-readable message about availability")
    used_by: Optional[str] = Field(None, description="Service code using this priority (if not available)")


# ============== EKS YAML Preview Schemas ==============

class EKSYamlPreviewRequest(BaseModel):
    """Request schema for previewing generated EKS YAML files"""
    service_code: str = Field(..., description="Service code")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    geo_loc_code: str = Field(..., description="Geographic location code")
    infrastructure_mst_code: Optional[str] = Field(None, description="Infrastructure instance (cluster) code")


class EKSYamlPreviewResponse(BaseModel):
    """Response schema for EKS YAML preview"""
    service_name: str = Field(..., description="Service name")
    environment: str = Field(..., description="Environment")
    language: str = Field(..., description="Language (java/golang/nodejs/python)")

    # File paths where these would be pushed
    config_file_path: str = Field(..., description="Path where config.yaml would be pushed")
    workflow_file_path: str = Field(..., description="Path where workflow.yml would be pushed")
    deployment_file_path: str = Field(..., description="Path where deployment.yaml would be pushed")

    # Generated YAML content
    config_yaml: str = Field(..., description="Generated config.yaml content")
    workflow_yaml: str = Field(..., description="Generated workflow.yml content")
    deployment_yaml: str = Field(..., description="Generated deployment.yaml content (Kubernetes manifests)")


class EKSDryRunRequest(BaseModel):
    """Request schema for EKS dry-run preview from form data (without saved config)"""
    # Service identification
    service_code: str = Field(..., description="Service code")
    service_name: str = Field(..., description="Service name for manifests")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")

    # Resource Allocation
    cpu_requested: Optional[str] = Field(None, description="CPU requested (e.g., '500m')")
    cpu_limit: Optional[str] = Field(None, description="CPU limit (e.g., '1000m')")
    memory_requested: Optional[str] = Field(None, description="Memory requested (e.g., '512Mi')")
    memory_limit: Optional[str] = Field(None, description="Memory limit (e.g., '1024Mi')")
    port: Optional[str] = Field(None, description="Container port")
    health: Optional[str] = Field(None, description="Health check endpoint")

    # HPA Configuration
    hpa_enabled: bool = Field(default=False, description="Enable Horizontal Pod Autoscaler")
    replica_count: Optional[str] = Field(None, description="Static replica count (when HPA disabled)")
    min_replicas: Optional[str] = Field(None, description="HPA min replicas")
    max_replicas: Optional[str] = Field(None, description="HPA max replicas")
    cpu_threshold: Optional[str] = Field(None, description="HPA CPU threshold (%)")
    memory_threshold: Optional[str] = Field(None, description="HPA memory threshold (%)")

    # Namespace
    namespace: Optional[str] = Field(None, description="Kubernetes namespace")

    # Service Configuration
    alb_schema: Optional[str] = Field(None, description="ALB schema: internal or internet-facing")
    service_path: Optional[str] = Field(None, description="Service path pattern")

    # Secrets
    secrets_enabled: bool = Field(default=False, description="Enable AWS Secrets Manager")
    secret_keys: Optional[str] = Field(None, description="Comma-separated secret keys")

    # Language & Build
    language: Optional[str] = Field(None, description="Language name (e.g., 'Java 17 LTS', 'go 1.25')")
    language_ref_code: Optional[str] = Field(None, description="Language reference code")
    build_path: Optional[str] = Field(None, description="Build path")
    dockerfile_path: Optional[str] = Field("Dockerfile", description="Dockerfile path")

    # EBS Storage
    ebs_enabled: bool = Field(default=False, description="Enable EBS storage")
    ebs_volume: Optional[str] = Field(None, description="EBS volume mount path")
    ebs_size: Optional[str] = Field(None, description="EBS size (e.g., '20Gi')")
    ebs_type: Optional[str] = Field("gp3", description="EBS type")

    # Deployment Strategy
    deployment_strategy: Optional[DeploymentStrategySchema] = Field(
        None, description="Deployment strategy configuration"
    )

    compute: Optional[str] = Field("on-demand", description="Compute type: on-demand or spot")

    # GitHub Configuration (for workflow generation)
    repository: Optional[str] = Field(None, description="GitHub repository (owner/repo)")
    branches: Optional[List[str]] = Field(None, description="Branches to deploy")

    # Same rules as MainConfigSchema — kept in step so the dry-run preview cannot
    # accept a value the save would reject.
    @field_validator("alb_schema")
    @classmethod
    def validate_alb_schema(cls, v: Optional[str]) -> Optional[str]:
        """Validate ALB schema is one of: internal, internet-facing"""
        if v is None:
            return v
        valid_schemas = ["internal", "internet-facing"]
        if v.strip().lower() not in valid_schemas:
            raise ValueError(f"ALB schema must be one of: {', '.join(valid_schemas)}")
        return v.strip().lower()

    @field_validator("compute")
    @classmethod
    def validate_compute(cls, v: Optional[str]) -> Optional[str]:
        """Validate compute type is one of: on-demand, spot"""
        if v is None:
            return v
        valid_types = ["on-demand", "spot"]
        if v.strip().lower() not in valid_types:
            raise ValueError(f"Compute type must be one of: {', '.join(valid_types)}")
        return v.strip().lower()

    @field_validator("cpu_requested", "cpu_limit")
    @classmethod
    def normalize_cpu(cls, v: Optional[str]) -> Optional[str]:
        """Store CPU as a plain, suffix-free number; never append a unit here.

        The FE sends raw numbers whose meaning depends on the deploy path
        (EKS=cores, PaaS=millicores) — a distinction this shared validator can't
        see. Each render/deploy path converts (EKS ×1000 -> 'm'; PaaS appends
        its own 'm'), so appending or multiplying here would be correct for one
        path and wrong for the other. We only strip a stray 'm' (healing old
        FE / mangled values) and validate. Max 5000 (cores or millicores).
        """
        if not v or not v.strip():
            return v
        numeric = v.strip()
        if numeric.endswith("m"):
            numeric = numeric[:-1]
        try:
            num = float(numeric)
        except ValueError:
            raise ValueError("CPU must be a valid number (e.g., 0.5, 1, 2)")
        if num <= 0:
            raise ValueError("CPU must be a positive number")
        if num > 5000:
            raise ValueError("CPU value is out of range")
        return numeric

    @field_validator("memory_requested", "memory_limit")
    @classmethod
    def normalize_memory(cls, v: Optional[str]) -> Optional[str]:
        """Store memory as a plain, suffix-free number; never append a unit here.

        Same rationale as normalize_cpu: the FE sends raw numbers whose unit
        depends on the deploy path (EKS=GiB, PaaS=Mi), which this shared
        validator can't see. Each render/deploy path converts (EKS ×1024 ->
        'Mi'; PaaS appends its own 'Mi'). We only strip a stray Mi/Gi/MB/GB
        suffix (healing old FE / mangled values) and validate positivity.
        No hard cap — GPU model-serving pods can request hundreds of GiB;
        per-instance clamping is enforced in clamp_resources().
        """
        if not v or not v.strip():
            return v
        numeric = re.sub(r'(Mi|Gi|MB|GB)$', '', v.strip(), flags=re.IGNORECASE)
        try:
            num = float(numeric)
        except ValueError:
            raise ValueError("Memory must be a valid number (e.g., 0.5, 1, 3)")
        if num <= 0:
            raise ValueError("Memory must be a positive number")
        return numeric


class EKSDryRunResponse(BaseModel):
    """Response schema for EKS dry-run preview with values.yaml"""
    service_name: str = Field(..., description="Service name")
    environment: str = Field(..., description="Environment")
    language: str = Field(..., description="Language")

    # Values.yaml content (left panel in UI)
    values_yaml: str = Field(..., description="Generated values.yaml content for Helm-style display")

    # Generated Kubernetes manifests (right panel in UI)
    deployment_yaml: str = Field(..., description="Generated deployment.yaml content (Kubernetes manifests)")

    # Optional: Additional YAML files
    config_yaml: Optional[str] = Field(None, description="Generated config.yaml content")
    workflow_yaml: Optional[str] = Field(None, description="Generated workflow.yml content")


class EKSValuesPreviewRequest(BaseModel):
    """Request schema for regenerating deployment.yaml from edited values.yaml"""
    service_name: str = Field(..., description="Service name")
    namespace: str = Field(..., description="Kubernetes namespace")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    values_yaml: str = Field(..., description="Edited values.yaml content")
    deployment_strategy: Optional[DeploymentStrategySchema] = Field(None, description="Deployment strategy configuration")


class EKSValuesPreviewResponse(BaseModel):
    """Response schema for deployment.yaml regenerated from values.yaml"""
    deployment_yaml: str = Field(..., description="Regenerated deployment.yaml content")


class EKSSaveFromValuesRequest(BaseModel):
    """Request schema for saving edited values.yaml back to service config DB"""
    code: str = Field(..., description="Service configuration code to update")
    values_yaml: str = Field(..., description="Edited values.yaml content (YAML string)")


class CloneSettingsRequest(BaseModel):
    """Copy portable build/runtime settings from one service_config to another.

    Placement/identity fields (ALB, listener priority, namespace, cluster
    context, ...) are never copied — the target keeps its own.
    """
    source_config_code: str = Field(..., description="service_config code to copy settings FROM")
    target_config_code: str = Field(..., description="service_config code receiving the settings")


# ============== Terragrunt Preview Schemas ==============

class TerragruntPreviewRequest(BaseModel):
    """Request schema for Terragrunt HCL preview generation

    Aligned with ServiceConfigCreate schema for consistency.
    Uses the same field names and types as the create/update endpoints.
    """
    services_mst_code: str = Field(..., description="Service code")
    infrastructuretype_ref_code: str = Field(..., description="Infrastructure type code")
    infra_vendor_enum: InfraVendorEnum = Field(..., description="Infrastructure vendor (aws, azure, gcp, on_prem)")
    infrastructure_mst_code: Optional[str] = Field(None, description="Infrastructure instance (cluster) code")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    geo_loc_mst_code: str = Field(..., description="Geographic location code")
    language_ref_code: Optional[str] = Field(None, description="Language reference code")
    config: Optional['MainConfigSchema'] = Field(None, description="Main service configuration")
    sidecar_config: List['SidecarOverrideSchema'] = Field(default_factory=list, description="Sidecar configurations")
    deployment_strategy: Optional['DeploymentStrategySchema'] = Field(None, description="Deployment strategy configuration")

    # Additional fields for preview (not in create/update)
    alb_selection: Optional[str] = Field("existing_alb", description="ALB selection: existing_alb, new_alb, no_alb")


class TerragruntPreviewResponse(BaseModel):
    """Response schema for Terragrunt HCL preview"""
    status: str = Field(..., description="Status: success or error")
    data: dict = Field(..., description="Contains hcl_content, template_used, environment, service_name, field_mapping")


# ============== Workflow Preview Schemas ==============

class WorkflowPreviewRequest(BaseModel):
    """Request schema for ECS workflow YAML preview generation

    This matches the create/update service config flow:
    - Uses infrastructure_mst_code to fetch AWS values from database
    - Uses service_config fields (build_path, dockerfile_path, etc.)
    - Backend derives all AWS values (ECR URI, IAM role, cluster) from infrastructure_mst.locator

    Supports ECS infrastructure type.
    """
    # Required fields (matching ServiceConfigCreate)
    infrastructure_ref_type: str = Field(..., description="Infrastructure type: 'ecs_ec2_infrastructuretype_ref'")
    infrastructure_mst_code: str = Field(..., description="Infrastructure instance code (e.g., 'infra-ecs-staging-mumbai-vance')")
    services_mst_code: str = Field(..., description="Service code (e.g., 'auth-service')")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    geo_loc_mst_code: str = Field(..., description="Geographic location code (e.g., 'london', 'mumbai')")
    language_ref_code: str = Field(..., description="Language reference code (e.g., 'JAVA_MAVEN_17', 'GO_1_23')")
    branch: str = Field(..., description="Git branch name (e.g., 'main', 'stage')")

    # Optional config fields (matching service_config.config)
    build_path: Optional[str] = Field(None, description="Build path for JAR/Go build location (from service_config.config)")
    dockerfile_path: Optional[str] = Field(None, description="Dockerfile path (from service_config.config)")
    other_paths: Optional[List[str]] = Field(None, description="Additional trigger paths (from service_config.config)")
    wire_enabled: Optional[bool] = Field(False, description="Enable Wire dependency injection (Go only, from service_config.config)")
    wire_path: Optional[str] = Field(None, description="Wire path (Go only, from service_config.config)")
    go_use_aws_secrets: Optional[bool] = Field(False, description="Enable AWS Secrets Manager (Go only, from service_config.config)")
    build_args: Optional[List[Dict[str, str]]] = Field(None, description="Custom Docker build args (from service_config.config)")

    # Repository URL (not used for ECS, kept for compatibility)
    repository: Optional[str] = Field(None, description="GitHub repository URL (owner/repo) - not used for ECS")


class WorkflowPreviewResponse(BaseModel):
    """Response schema for workflow YAML preview"""
    status: str = Field(..., description="Status: success or error")
    data: dict = Field(..., description="Contains yaml_content, template_used, infrastructure_type, language, service_name, environment")


class SettingsDiffItem(BaseModel):
    """One changed settings field between current config and last-deployed snapshot."""
    field: str = Field(..., description="Dotted config key, e.g. 'autoscaling.desired'")
    label: str = Field(..., description="Human-readable label for the field")
    current_value: Optional[str] = Field(None, description="Current DevLift value (to be deployed)")
    deployed_value: Optional[str] = Field(None, description="Last value DevLift deployed")


class SettingsDiffResponse(BaseModel):
    """Settings config drift for the redeploy diff modal.

    Compares service_configs.config against the config_snapshot of the latest
    DEPLOYED transaction_queue row. The AWS live value is not read yet — the UI
    shows a placeholder until AWS access lands. Empty items => nothing pending.
    """
    service_config_code: str
    has_deployed_baseline: bool = Field(
        ..., description="False when the service was never deployed (no baseline to diff)"
    )
    items: List[SettingsDiffItem] = Field(default_factory=list)


class ServiceConfigEnvGeoOption(BaseModel):
    """One (environment, geo loc, cluster) combination a service is configured on."""
    environment: str = Field(..., description="Environment code (dev/stage/qa/prod)")
    geo_loc_code: str = Field(..., description="Geo location code (geo_loc_mst.code)")
    geo_loc_name: Optional[str] = Field(None, description="Geo location display name")
    infra_vendor_enum: Optional[str] = Field(None, description="Infrastructure vendor (aws/gcp/azure)")
    config_code: str = Field(..., description="service_configs.code for this combination")
    infrastructure_mst_code: Optional[str] = Field(None, description="Cluster (infrastructure_mst.code); null when unset")
    infrastructuretype_ref_code: Optional[str] = Field(None, description="Infra type ref code (EKS vs ECS)")
    cluster_name: Optional[str] = Field(None, description="Cluster display name (infrastructure_mst.name)")


class ServiceConfigEnvGeoOptionsResponse(BaseModel):
    """All env/geo/cluster combinations a service has service_config rows for.

    Drives the resource-detail-panel context bar dropdowns: the frontend derives
    environments → geo locs (per env) → clusters (per env+geo) from these combos.
    """
    service_mst_code: str
    options: List[ServiceConfigEnvGeoOption] = Field(default_factory=list)


class ServiceNameResolveRequest(BaseModel):
    """service_configs codes to resolve to their owning service's name.

    Batched deliberately: the authz console holds one store object per
    service_config, so a page can need hundreds of names at once and a
    per-code request would mean hundreds of round trips.
    """
    codes: List[str] = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="service_configs.code values to resolve",
    )


class ServiceNameResolution(BaseModel):
    """One service_config resolved through to its services_mst row."""
    config_code: str = Field(..., description="service_configs.code that was asked for")
    service_mst_code: str = Field(..., description="services_mst.code that owns it")
    service_name: str = Field(..., description="services_mst.name — the label to show")
    resource_group_code: Optional[str] = Field(
        None, description="services_mst.resource_group_mst_code"
    )
    environment: Optional[str] = Field(None, description="Environment of this config")
    geo_loc_code: Optional[str] = Field(None, description="Geo location of this config")
    is_deleted: bool = Field(
        False,
        description=(
            "True when the service_config or its service is soft-deleted. Still "
            "returned: an authz tuple naming a deleted config is exactly what "
            "needs cleaning up, and hiding it makes that object unnameable."
        ),
    )


class ServiceNameResolveResponse(BaseModel):
    """Resolved names plus the codes that could not be resolved at all.

    `unresolved` is not an error. A code lands there when it belongs to another
    tenant or names a row that no longer exists — both legitimate states for an
    authz store, which is written independently of this database.
    """
    resolved: List[ServiceNameResolution] = Field(default_factory=list)
    unresolved: List[str] = Field(default_factory=list)
