"""
Parameter definitions and aliases for Service Config Assistant.

Single source of truth for parameter one-liners and name variations.
Based on terragrunt templates: api-common-alb.hcl, api-new-alb.hcl, worker-no-alb.hcl
"""
from typing import Optional

# One-liner definitions for config parameters
PARAMETER_DEFINITIONS = {
    # Container Config (ECS)
    "cpu": "ECS CPU units (256, 512, 1024, 2048, 4096). Determines compute capacity for your container.",
    "memory": "ECS memory in MB. Must be compatible with CPU tier (e.g., 512 CPU supports 1024-4096 MB).",
    "container_port": "Port your application listens on inside the container. ALB routes traffic to this port.",
    "enable_ulimits": "Increase file descriptor limits for high-connection services like databases or message queues.",

    # EKS Container Resource Config (separate from ECS cpu/memory)
    "cpu_requested": "EKS CPU request in millicores ('500m', '1000m') or cores ('1', '2'). Minimum guaranteed CPU.",
    "cpu_limit": "EKS CPU limit - maximum CPU your pod can use. Container throttled if exceeded.",
    "memory_requested": "EKS memory request (e.g., '512Mi', '1Gi', '2Gi'). Minimum guaranteed memory.",
    "memory_limit": "EKS memory limit - maximum memory your pod can use. Pod killed (OOMKilled) if exceeded.",

    # EKS HPA (Horizontal Pod Autoscaler) Config
    "replica_count": "Number of pod replicas to run. Start with 2 for high availability.",
    "min_replicas": "Minimum pod replicas during low traffic. HPA won't scale below this.",
    "max_replicas": "Maximum pod replicas during peak load. HPA won't scale above this.",
    "cpu_threshold": "CPU utilization percentage that triggers HPA scaling (e.g., 80 means scale when CPU > 80%).",
    "memory_threshold": "Memory utilization percentage that triggers HPA scaling (e.g., 80 means scale when memory > 80%).",

    # Health & Routing
    "health_check_path": "Endpoint ALB calls to verify your service is healthy. Should return 200 OK quickly (e.g., /health).",
    "service_path": "URL path pattern that routes to this service. Use /* for root or /api/v1/* for specific paths.",

    # Scaling
    "enable_autoscaling": "Automatically adjust task count based on CPU/memory usage. Recommended for variable workloads.",
    "desired_count": "Number of tasks to run normally. Start with 2 for high availability.",
    "min_task_count": "Minimum tasks during low traffic. Set to 1-2 to save costs while maintaining availability.",
    "max_task_count": "Maximum tasks during peak load. Set based on your scaling budget and expected traffic spikes.",

    # HTTP Scaling
    "http_scaling_enabled": "Scale based on incoming HTTP requests instead of CPU/memory. Better for request-heavy APIs.",
    "http_scaling_target_value": "Target requests per task before scaling. Lower values (1000-2000) scale faster, higher (5000+) scale slower.",

    # ALB
    "alb_selection": "Load balancer setup: no_alb for background workers, existing_alb to share tenant ALB, create_new_alb for dedicated ALB.",
    "listener_rule_priority": "Order in which ALB evaluates routing rules. Lower numbers = higher priority. Must be unique per tenant.",

    # Datadog Sidecar
    "enable_datadog_sidecar": "Add Datadog agent container for APM tracing, metrics, and log collection.",
    "datadog_sidecar_cpu": "CPU for Datadog sidecar. 256 is usually sufficient unless high log volume.",
    "datadog_sidecar_memory": "Memory for Datadog sidecar. 512 MB default, increase if buffering issues.",
    "datadog_log_source": "Identifies log format in Datadog (e.g., java, python, nodejs). Enables proper parsing.",
    "datadog_logs_enabled": "Enable Datadog log collection from container stdout/stderr.",

    # EBS storage
    "ebs_enabled": "Attach persistent EBS volume for data that survives task restarts.",
    "ebs_volume": "Mount path inside container where EBS volume appears (e.g., /data, /var/lib/mysql).",
    "ebs_size": "EBS volume size in GB. Cannot be decreased after creation.",
    "ebs_type": "EBS volume type: gp3 (balanced), io2 (high IOPS), st1 (throughput). gp3 is default.",

    # JVM Configuration
    "xms": "JVM initial heap size in MB. Set equal to xmx for predictable memory usage.",
    "xmx": "JVM maximum heap size in MB. Set to ~75% of container memory to leave room for non-heap.",
    "java_version": "Java version for the application runtime. Default is 17. Supported: 8, 11, 17, 21.",

    # EKS Namespace & Secrets
    "namespace": "Kubernetes namespace where the service will be deployed. Usually matches environment or tenant.",
    "secrets_enabled": "Enable injection of secrets from AWS Secrets Manager into the container.",
    "secret_keys": "Comma-separated list of secret keys to inject from AWS Secrets Manager.",
    "alb_schema": "ALB visibility: 'internal' for private access, 'internet-facing' for public access.",

    # EKS Build Config
    "eks_build_type": "Build type for EKS: gradle, maven, or docker-only.",
    "skip_tests": "Skip running tests during build. Use for faster deployments (not recommended for prod).",
    "skip_checks": "Skip code quality checks during build.",
    "skip_code_quality": "Skip SonarQube or similar code quality analysis.",
    "skip_snyk_code": "Skip Snyk code vulnerability scanning.",
    "skip_snyk_oss": "Skip Snyk open-source dependency scanning.",
    "skip_snyk_container": "Skip Snyk container image scanning.",
    "gradle_jar_file": "Path to the output JAR file for Gradle builds.",
    "gradle_tasks": "Gradle tasks to execute (e.g., 'clean build').",
    "gradle_jvm_args": "JVM arguments for Gradle build process.",
    "gradle_workers_max": "Maximum number of Gradle worker processes.",
    "maven_jar_file": "Path to the output JAR file for Maven builds.",
    "maven_goals": "Maven goals to execute (e.g., 'clean package').",
    "maven_profile": "Maven profile to activate during build.",
    "maven_xms": "Initial heap size for Maven build JVM.",
    "maven_xmx": "Maximum heap size for Maven build JVM.",

    # Deployment Strategy
    "deployment_strategy": "Deployment strategy: canary, bluegreen, rolling, or recreate.",
    "canary_steps": "Number of steps for canary rollout (e.g., 5%, 25%, 50%, 100%).",
    "canary_analysis": "Enable automated canary analysis for rollback decisions.",
    "bluegreen_active_service": "Active service selector for blue-green deployments.",
    "bluegreen_preview_service": "Preview service selector for blue-green deployments.",
    "rolling_max_surge": "Maximum number of pods that can be created above desired during rolling update.",
    "rolling_max_unavailable": "Maximum number of pods that can be unavailable during rolling update.",

    # Go Configuration
    "go_config_path": "Path to Go application config file within the repository.",
    "go_use_aws_secrets": "Enable AWS Secrets Manager integration for Go applications.",

    # Docker
    "generate_dockerfile": "Auto-generate Dockerfile based on language detection. Disable for custom Dockerfiles.",

    # Notifications
    "slack_channel_id": "Slack channel ID for deployment and alert notifications.",

    # Build Configuration
    "build_path": "Directory containing build output (e.g., target for Maven, dist for Node).",
    "dockerfile_path": "Path to Dockerfile relative to repo root. Default is Dockerfile in root.",
    "wire_enabled": "Enable Wire for compile-time dependency injection in Go services.",
    "wire_path": "Path to Wire package for Go dependency injection (e.g., ./cmd/wire).",

    # Repository
    "repository": "GitHub repository URL for this service's source code.",
    "branches": "Branches allowed to trigger deployments (e.g., main, staging, release/*).",

    # CI/CD Triggers
    "other_paths": "Additional repo paths that trigger CI/CD pipeline. Use for shared code dependencies (e.g., shared-util, common/lib).",

    # Language
    "language": "Programming language/framework for your service (e.g., Java Maven, Go, Node.js, Python). Determines build configuration and runtime.",
}


# Aliases map variations to canonical parameter names
PARAMETER_ALIASES = {
    # CPU variations (generic only - EKS-specific are separate params)
    "vcpu": "cpu",

    # Memory variations (generic only - EKS-specific are separate params)
    "ram": "memory",
    "mem": "memory",

    # EKS CPU/Memory aliases (map to specific EKS params, NOT generic)
    "cpu request": "cpu_requested",
    "cpu requested": "cpu_requested",
    "memory request": "memory_requested",
    "memory requested": "memory_requested",

    # EKS HPA aliases
    "replica count": "replica_count",
    "replicas count": "replica_count",
    "pod replicas": "replica_count",
    "min replicas": "min_replicas",
    "minimum replicas": "min_replicas",
    "max replicas": "max_replicas",
    "maximum replicas": "max_replicas",
    "cpu threshold": "cpu_threshold",
    "cpu utilization": "cpu_threshold",
    "memory threshold": "memory_threshold",
    "memory utilization": "memory_threshold",
    # Note: "hpa" is ambiguous - handled as AMBIGUOUS_HPA in extraction prompt

    # Port variations
    "port": "container_port",

    # Health check variations
    "health": "health_check_path",
    "healthcheck": "health_check_path",
    "health_path": "health_check_path",

    # Service path variations
    "path": "service_path",
    "route": "service_path",
    "routing": "service_path",

    # Scaling variations (shared: ECS task-based scaling, EKS HPA)
    "scaling": "enable_autoscaling",
    "autoscaling": "enable_autoscaling",
    "auto_scaling": "enable_autoscaling",
    "enable hpa": "enable_autoscaling",
    "hpa enabled": "enable_autoscaling",
    "autoscale hpa": "enable_autoscaling",
    "replicas": "desired_count",
    "instances": "desired_count",
    "desired": "desired_count",
    "min_tasks": "min_task_count",
    "max_tasks": "max_task_count",
    "min": "min_task_count",
    "max": "max_task_count",
    # Note: replica_count, min_replicas, max_replicas are now EKS HPA params (first-class, not aliases)

    # HTTP Scaling variations
    "http scaling": "http_scaling_enabled",
    "http_scaling": "http_scaling_enabled",
    "request scaling": "http_scaling_enabled",
    "http autoscaling": "http_scaling_enabled",
    "target value": "http_scaling_target_value",
    "http target": "http_scaling_target_value",
    "scaling target": "http_scaling_target_value",

    # ALB variations
    "alb": "alb_selection",
    "load_balancer": "alb_selection",
    "lb": "alb_selection",
    "priority": "listener_rule_priority",
    "rule_priority": "listener_rule_priority",
    "listener": "listener_rule_priority",
    "rule": "listener_rule_priority",
    "lister priority": "listener_rule_priority",
    "lister rule": "listener_rule_priority",

    # Sidecar variations (only Datadog supported)
    "sidecar": "enable_datadog_sidecar",
    "datadog": "enable_datadog_sidecar",
    "dd": "enable_datadog_sidecar",
    "datadog cpu": "datadog_sidecar_cpu",
    "datadog memory": "datadog_sidecar_memory",
    "datadog mem": "datadog_sidecar_memory",
    "datadog ram": "datadog_sidecar_memory",
    "datadog sidecar ram": "datadog_sidecar_memory",
    "dd ram": "datadog_sidecar_memory",
    "log source": "datadog_log_source",
    "datadog source": "datadog_log_source",
    "datadog logs": "datadog_logs_enabled",
    "dd logs": "datadog_logs_enabled",
    "logs enabled": "datadog_logs_enabled",

    # EBS variations
    "ebs": "ebs_enabled",
    "volume": "ebs_volume",
    "storage": "ebs_enabled",
    "disk": "ebs_enabled",
    "disk_size": "ebs_size",
    "volume_size": "ebs_size",
    "volume_type": "ebs_type",

    # JVM variations
    "heap": "xmx",
    "heap_size": "xmx",
    "jvm": "xmx",
    "initial_heap": "xms",
    "max_heap": "xmx",

    # Build variations
    "build": "build_path",
    "build path": "build_path",
    "dockerfile": "dockerfile_path",
    "dockerfile path": "dockerfile_path",
    "wire": "wire_enabled",
    "wire path": "wire_path",

    # Repo variations
    "repo": "repository",
    "github": "repository",
    "git hub": "repository",
    "github repo": "repository",
    "github url": "repository",
    "github config": "repository",
    "branch": "branches",

    # Trigger paths variations
    "trigger paths": "other_paths",
    "trigger path": "other_paths",
    "additional trigger paths": "other_paths",
    "additional paths": "other_paths",
    "other paths": "other_paths",
    "trigger": "other_paths",
    "shared paths": "other_paths",

    # Ulimits
    "ulimits": "enable_ulimits",

    # Multi-word aliases (spaces) - handle LLM returning spaced versions
    "health check": "health_check_path",
    "health check path": "health_check_path",
    "service path": "service_path",
    "desired count": "desired_count",
    "min tasks": "min_task_count",
    "max tasks": "max_task_count",
    "min task count": "min_task_count",
    "max task count": "max_task_count",
    "http scaling": "http_scaling_enabled",
    "request scaling": "http_scaling_enabled",
    "http scaling enabled": "http_scaling_enabled",
    "http scaling target value": "http_scaling_target_value",
    "alb selection": "alb_selection",
    "listener priority": "listener_rule_priority",
    "listener rule": "listener_rule_priority",
    "listener rule priority": "listener_rule_priority",
    "ebs size": "ebs_size",
    "ebs type": "ebs_type",
    "ebs volume": "ebs_volume",
    "ebs enabled": "ebs_enabled",
    "volume size": "ebs_size",
    "volume type": "ebs_type",
    # Enable flags with spaces
    "enable autoscaling": "enable_autoscaling",
    "enable ulimits": "enable_ulimits",
    "enable datadog sidecar": "enable_datadog_sidecar",
    "enable datadog": "enable_datadog_sidecar",
    # Other spaced variations
    "container port": "container_port",
    "docker file": "dockerfile_path",
    "dockerfile path": "dockerfile_path",
    "wire enabled": "wire_enabled",
    "datadog sidecar cpu": "datadog_sidecar_cpu",
    "datadog sidecar memory": "datadog_sidecar_memory",

    # Language variations
    "lang": "language",
    "programming language": "language",
    "framework": "language",
    "runtime": "language",
    "tech stack": "language",

    # EKS-specific resource aliases
    "cpu limit": "cpu_limit",
    "memory limit": "memory_limit",
    "k8s cpu": "cpu_requested",
    "k8s memory": "memory_requested",
    "kubernetes cpu": "cpu_requested",
    "kubernetes memory": "memory_requested",

    # Java version
    "java": "java_version",
    "jdk": "java_version",
    "jdk version": "java_version",

    # Namespace
    "k8s namespace": "namespace",
    "kubernetes namespace": "namespace",
    "ns": "namespace",

    # Secrets
    "secrets": "secrets_enabled",
    "enable secrets": "secrets_enabled",
    "aws secrets": "secrets_enabled",

    # Slack
    "slack": "slack_channel_id",
    "slack channel": "slack_channel_id",
    "notifications": "slack_channel_id",

    # Build config
    "build type": "eks_build_type",
    "skip test": "skip_tests",
    "skip tests": "skip_tests",
    "skip check": "skip_checks",
    "skip checks": "skip_checks",
    "snyk": "skip_snyk_code",

    # Gradle
    "gradle jar": "gradle_jar_file",
    "gradle task": "gradle_tasks",
    "gradle tasks": "gradle_tasks",

    # Maven
    "maven jar": "maven_jar_file",
    "maven goal": "maven_goals",
    "maven goals": "maven_goals",

    # Deployment strategy
    "strategy": "deployment_strategy",
    "deploy strategy": "deployment_strategy",
    "canary": "deployment_strategy",
    "blue green": "deployment_strategy",
    "bluegreen": "deployment_strategy",
    "rolling": "deployment_strategy",

    # Go config
    "go config": "go_config_path",
    "go secrets": "go_use_aws_secrets",
}


def get_canonical_parameter(name: str) -> str:
    """
    Get canonical parameter name from alias.

    Args:
        name: Parameter name or alias

    Returns:
        Canonical parameter name
    """
    lower_name = name.lower().strip()
    return PARAMETER_ALIASES.get(lower_name, lower_name)


def get_parameter_definition(name: str) -> Optional[str]:
    """
    Get one-liner definition for parameter.

    Args:
        name: Parameter name (will be canonicalized)

    Returns:
        Definition string or None if not found
    """
    canonical = get_canonical_parameter(name)
    return PARAMETER_DEFINITIONS.get(canonical)


# Parameter categories for organized display
PARAMETER_CATEGORIES = {
    # Existing categories (preserved for backward compatibility)
    "Container": ["cpu", "memory", "container_port", "enable_ulimits"],
    "EKS Container Resources": ["cpu_requested", "cpu_limit", "memory_requested", "memory_limit"],
    "EKS HPA": ["replica_count", "min_replicas", "max_replicas", "cpu_threshold", "memory_threshold"],
    "Health & Routing": ["health_check_path", "service_path"],
    "Scaling": ["enable_autoscaling", "desired_count", "min_task_count", "max_task_count"],
    "HTTP Scaling": ["http_scaling_enabled", "http_scaling_target_value"],
    "ALB": ["alb_selection", "listener_rule_priority"],
    "Datadog Sidecar": ["enable_datadog_sidecar", "datadog_sidecar_cpu", "datadog_sidecar_memory", "datadog_log_source", "datadog_logs_enabled"],
    "EBS Storage": ["ebs_enabled", "ebs_volume", "ebs_size", "ebs_type"],
    "JVM": ["xms", "xmx", "java_version"],
    "Build": ["build_path", "dockerfile_path", "generate_dockerfile", "wire_enabled", "wire_path", "language"],
    "Repository": ["repository", "branches", "other_paths"],
    # New EKS categories
    "EKS Namespace": ["namespace", "secrets_enabled", "secret_keys", "alb_schema"],
    "EKS Build Config": [
        "eks_build_type", "skip_tests", "skip_checks", "skip_code_quality",
        "skip_snyk_code", "skip_snyk_oss", "skip_snyk_container",
        "gradle_jar_file", "gradle_tasks", "gradle_jvm_args", "gradle_workers_max",
        "maven_jar_file", "maven_goals", "maven_profile", "maven_xms", "maven_xmx",
    ],
    "Deployment Strategy": [
        "deployment_strategy", "canary_steps", "canary_analysis",
        "bluegreen_active_service", "bluegreen_preview_service",
        "rolling_max_surge", "rolling_max_unavailable",
    ],
    "Go Config": ["go_config_path", "go_use_aws_secrets"],
    "Notifications": ["slack_channel_id"],
}


# Parameters that should NOT offer autofill (service-specific configs)
# These still show help/definition/stats, but no "Would you like me to fill..." prompt
AUTOFILL_BLOCKED_PARAMETERS = {
    "service_path",
    "language",
    "repository",
    "branches",
    "build_path",
    "dockerfile_path",
    "other_paths",
    "health_check_path",
    "listener_rule_priority",
    "ebs_volume",
    "wire_enabled",
    "wire_path",
}


# ECS-only parameters (not applicable to EKS)
ECS_ONLY_PARAMETERS = {
    # Container resources (ECS uses task definition units, not Kubernetes resources)
    "cpu", "memory",
    # ECS task-based scaling
    "desired_count", "min_task_count", "max_task_count",
    # ECS-specific features
    "enable_ulimits",
    # HTTP scaling (ECS ALB target tracking, not available in EKS)
    "http_scaling_enabled", "http_scaling_target_value",
}
# Note: enable_autoscaling is shared - ECS uses it for task scaling, EKS uses it for HPA

# EKS-only parameters (not applicable to ECS)
EKS_ONLY_PARAMETERS = {
    # Container resources (Kubernetes format)
    "cpu_requested", "cpu_limit", "memory_requested", "memory_limit",
    # HPA configuration
    "replica_count", "min_replicas", "max_replicas",
    "cpu_threshold", "memory_threshold",
    # Namespace & Secrets
    "namespace", "secrets_enabled", "secret_keys", "alb_schema",
    # Build configuration
    "java_version", "slack_channel_id",
    "eks_build_type", "skip_tests", "skip_checks", "skip_code_quality",
    "skip_snyk_code", "skip_snyk_oss", "skip_snyk_container",
    "gradle_jar_file", "gradle_tasks", "gradle_jvm_args", "gradle_workers_max",
    "maven_jar_file", "maven_goals", "maven_profile", "maven_xms", "maven_xmx",
    # Deployment strategy
    "deployment_strategy", "canary_steps", "canary_analysis",
    "bluegreen_active_service", "bluegreen_preview_service",
    "rolling_max_surge", "rolling_max_unavailable",
}


def is_autofill_allowed(parameter: str, infrastructure_type: str = None) -> bool:
    """
    Check if autofill is allowed for a parameter in given infrastructure context.

    Returns False if:
    1. Parameter is in AUTOFILL_BLOCKED_PARAMETERS (service-specific)
    2. Parameter is EKS-only but context is ECS
    3. Parameter is ECS-only but context is EKS
    """
    # Rule 1: Service-specific blocklist
    if parameter in AUTOFILL_BLOCKED_PARAMETERS:
        return False

    # Rule 2 & 3: Infrastructure context check
    # Note: infrastructure_type can be "eks_infrastructuretype_ref" or "ecs_infrastructuretype_ref"
    if infrastructure_type:
        infra_upper = infrastructure_type.upper()
        if "ECS" in infra_upper and parameter in EKS_ONLY_PARAMETERS:
            return False
        if "EKS" in infra_upper and parameter in ECS_ONLY_PARAMETERS:
            return False

    return True


def get_parameters_by_category() -> dict[str, list[str]]:
    """Get parameters organized by category."""
    return PARAMETER_CATEGORIES


def get_all_parameter_names() -> list[str]:
    """Get all canonical parameter names."""
    return list(PARAMETER_DEFINITIONS.keys())


def format_extraction_parameters() -> str:
    """Format all parameters by category for extraction prompt."""
    lines = []
    for category, params in PARAMETER_CATEGORIES.items():
        lines.append(f"- {category}: {', '.join(params)}")
    return "\n".join(lines)


def format_extraction_aliases() -> str:
    """Format key aliases for extraction prompt."""
    key_aliases = [
        "dockerfile, docker, dockerfile path → dockerfile_path",
        "build, build path → build_path",
        "wire, wire path → wire_enabled/wire_path",
        "ram, mem → memory",
        "cpu request, cpu requested → cpu_requested",
        "cpu limit → cpu_limit",
        "memory request, memory requested → memory_requested",
        "memory limit → memory_limit",
        "replica count, pod replicas, hpa → replica_count",
        "min replicas, minimum replicas → min_replicas",
        "max replicas, maximum replicas → max_replicas",
        "cpu threshold, cpu utilization → cpu_threshold",
        "memory threshold, memory utilization → memory_threshold",
        "instances, desired, desired count → desired_count",
        "health, healthcheck, health check → health_check_path",
        "port → container_port",
        "path, route, service path → service_path",
        "scaling, autoscaling → enable_autoscaling",
        "http scaling, request scaling → http_scaling_enabled",
        "target value, http target, scaling target → http_scaling_target_value",
        "ebs, storage, disk → ebs_enabled",
        "ebs size, volume size → ebs_size",
        "ebs type, volume type → ebs_type",
        "volume → ebs_volume",
        "heap, jvm → xmx",
        "repo, github, git hub → repository",
        "branch → branches",
        "trigger paths, trigger path, additional trigger paths, other paths → other_paths",
        "datadog, dd → enable_datadog_sidecar",
        "datadog cpu → datadog_sidecar_cpu",
        "datadog memory, datadog mem, datadog ram, dd ram → datadog_sidecar_memory",
        "log source, datadog source → datadog_log_source",
        "datadog logs, dd logs, logs enabled → datadog_logs_enabled",
        "priority, listener, listener priority, listener rule, lister priority, rule → listener_rule_priority",
        "alb, lb, alb selection → alb_selection",
        "min tasks → min_task_count",
        "max tasks → max_task_count",
        "ulimits → enable_ulimits",
        # Java/JDK
        "java, jdk, jdk version → java_version",
        # Namespace & Secrets
        "k8s namespace, kubernetes namespace, ns → namespace",
        "secrets, enable secrets, aws secrets → secrets_enabled",
        # Notifications
        "slack, slack channel, notifications → slack_channel_id",
        # Build config
        "build type → eks_build_type",
        "skip test, skip tests → skip_tests",
        "skip check, skip checks → skip_checks",
        "snyk → skip_snyk_code",
        # Gradle
        "gradle jar → gradle_jar_file",
        "gradle task, gradle tasks → gradle_tasks",
        # Maven
        "maven jar → maven_jar_file",
        "maven goal, maven goals → maven_goals",
        # Deployment strategy
        "strategy, deploy strategy, canary, bluegreen, rolling → deployment_strategy",
        # Go config
        "go config → go_config_path",
        "go secrets → go_use_aws_secrets",
    ]
    return "\n".join(f"- {a}" for a in key_aliases)
