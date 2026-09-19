"""
Helper functions for generating EKS onboarding files.
Extracted from eks_onboarding_server.py for better organization.
"""
import os
import re
from typing import Optional

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    LANGUAGE_DEFAULT_PORTS,
    AWS_ACCOUNT_ID,
    AWS_ROLE_ARN,
    DEFAULT_AWS_REGION,
    DEFAULT_ALB_GROUP_ORDER_SERVICE,
    SHARED_ACM_CERT_ARN,
)


# ─── Template Paths ───

BASE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
JAVA_DOCKERFILE_TEMPLATE = os.path.join(BASE_DIR, "templates", "dockerfiles", "java-standard.Dockerfile")
PYTHON_DOCKERFILE_TEMPLATE = os.path.join(BASE_DIR, "templates", "dockerfiles", "python-standard.Dockerfile")
GO_DOCKERFILE_TEMPLATE = os.path.join(BASE_DIR, "templates", "dockerfiles", "go-standard.Dockerfile")
NODEJS_DOCKERFILE_TEMPLATE = os.path.join(BASE_DIR, "templates", "dockerfiles", "nodejs-standard.Dockerfile")
JAVA_GRADLE_WORKFLOW_TEMPLATE = os.path.join(BASE_DIR, "templates", "eks", "workflow", "workflow-java-gradle.yml")
JAVA_MAVEN_WORKFLOW_TEMPLATE = os.path.join(BASE_DIR, "templates", "eks", "workflow", "workflow-java-maven.yml")
GO_WORKFLOW_TEMPLATE = os.path.join(BASE_DIR, "templates", "eks", "workflow", "workflow-golang.yml")
PYTHON_WORKFLOW_TEMPLATE = os.path.join(BASE_DIR, "templates", "eks", "workflow", "workflow-python.yml")
NODEJS_WORKFLOW_TEMPLATE = os.path.join(BASE_DIR, "templates", "eks", "workflow", "workflow-nodejs.yml")

# Kustomize template paths
KUSTOMIZE_BASE = os.path.join(BASE_DIR, "templates", "eks", "kustomize", "base")
KUSTOMIZATION_TEMPLATE = os.path.join(KUSTOMIZE_BASE, "kustomization.yaml")
NAMESPACE_TEMPLATE = os.path.join(KUSTOMIZE_BASE, "namespace.yaml")
SERVICEACCOUNT_TEMPLATE = os.path.join(KUSTOMIZE_BASE, "serviceaccount.yaml")
DEPLOYMENT_TEMPLATE = os.path.join(KUSTOMIZE_BASE, "deployment.yaml")
SERVICE_TEMPLATE = os.path.join(KUSTOMIZE_BASE, "service.yaml")
INGRESS_TEMPLATE = os.path.join(KUSTOMIZE_BASE, "ingress.yaml")


# ─── Helper Functions ───

def sanitize_name(name: str) -> str:
    """Sanitize service name for Kubernetes."""
    name = name.replace(" ", "-").replace("_", "-")
    name = re.sub(r'[^a-zA-Z0-9\-]', '', name)
    name = re.sub(r'-+', '-', name)
    return name.strip('-').lower()


def parse_language_name(language_name: str) -> tuple:
    """Parse language string -> (base_language, normalized_language, version)."""
    lang_lower = language_name.lower().strip()

    if lang_lower.startswith("go") or lang_lower.startswith("golang"):
        version_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', lang_lower)
        version = version_match.group(1) if version_match else "1.24.0"
        return ("golang", "golang", version)

    if lang_lower.startswith("java"):
        if "maven" in lang_lower:
            version_match = re.search(r'(\d+)', lang_lower)
            version = version_match.group(1) if version_match else "17"
            return ("java", "java", version)
        else:
            version_match = re.search(r'(\d+)', lang_lower)
            version = version_match.group(1) if version_match else "17"
            return ("java", "java", version)

    if lang_lower.startswith("python") or lang_lower.startswith("py"):
        version_match = re.search(r'(\d+\.\d+)', lang_lower)
        version = version_match.group(1) if version_match else "3.11"
        return ("python", "python", version)

    if lang_lower.startswith("node") or lang_lower.startswith("nodejs"):
        version_match = re.search(r'(\d+)', lang_lower)
        version = version_match.group(1) if version_match else "20"
        return ("nodejs", "nodejs", version)

    return (lang_lower, lang_lower, None)


def get_default_build_type(base_language: str) -> str:
    """Get default build type for language."""
    if base_language == "java":
        return "gradle"
    elif base_language == "golang":
        return "docker-only"
    elif base_language == "python":
        return "docker-only"
    return "docker-only"


def get_default_port(language: str) -> int:
    """Get default port based on language."""
    _, normalized_language, _ = parse_language_name(language)
    return LANGUAGE_DEFAULT_PORTS.get(normalized_language, 8080)


def _read_template(template_path: str) -> str:
    """Read template file content."""
    with open(template_path, 'r') as f:
        return f.read()


# ─── Dockerfile Generator ───

def generate_dockerfile(language: str, port: int = 8080) -> str:
    """Generate Dockerfile from template."""
    _, normalized_language, version = parse_language_name(language)

    if normalized_language == "java":
        content = _read_template(JAVA_DOCKERFILE_TEMPLATE)
        content = content.replace("{{JDK_VERSION}}", version or "17")
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", "")
        content = content.replace("{{DATADOG_BLOCK}}\n", 'ENV JAVA_TOOL_OPTIONS="-Dlogging.level.root=info"\n\n')
        return content

    if normalized_language == "golang":
        content = _read_template(GO_DOCKERFILE_TEMPLATE)
        content = content.replace("{{AWS_SECRETS_BLOCK}}\n", "")
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", "")
        content = content.replace("{{CONFIG_PATH}}", "/app/configs/config.json")
        content = content.replace("{{CONFIG_TYPE}}", "json")
        content = content.replace("{{CONFIG_COPY_LINE}}", "")
        content = content.replace("{{PORT}}", str(port))
        return content

    if normalized_language == "python":
        content = _read_template(PYTHON_DOCKERFILE_TEMPLATE)
        content = content.replace("{{PYTHON_VERSION}}", version or "3.11")
        content = content.replace("{{PORT}}", str(port))
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", "")
        return content

    if normalized_language == "nodejs":
        content = _read_template(NODEJS_DOCKERFILE_TEMPLATE)
        content = content.replace("{{NODE_VERSION}}", version or "20")
        content = content.replace("{{PORT}}", str(port))
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", "")
        return content

    raise ValueError(f"Unsupported language: {language}")


# ─── Workflow Generator ───

def generate_workflow(service_name: str, language: str, version: str, environment: str, branch: str, eks_cluster_name: str = "devlift-dev-cluster", dockerfile_path: str = "Dockerfile") -> str:
    """Generate workflow YAML from template based on language."""
    lang_lower = language.lower().strip()

    if lang_lower == "java-maven":
        content = _read_template(JAVA_MAVEN_WORKFLOW_TEMPLATE)
    elif lang_lower in ("java-gradle", "java"):
        content = _read_template(JAVA_GRADLE_WORKFLOW_TEMPLATE)
    elif lang_lower in ("golang", "go"):
        content = _read_template(GO_WORKFLOW_TEMPLATE)
    elif lang_lower == "python":
        content = _read_template(PYTHON_WORKFLOW_TEMPLATE)
    elif lang_lower in ("nodejs", "node"):
        content = _read_template(NODEJS_WORKFLOW_TEMPLATE)
    else:
        raise ValueError(f"Unsupported language for workflow: {language}")

    env_display = environment.capitalize()

    # Common placeholders
    content = content.replace("{{SERVICE_NAME}}", service_name)
    content = content.replace("{{ENVIRONMENT_NAME}}", env_display)
    content = content.replace("{{ENVIRONMENT}}", environment)
    content = content.replace("{{BRANCH}}", branch)
    content = content.replace("{{FOLDER_PATH_FILTER}}", "")
    content = content.replace("{{AWS_REGION}}", DEFAULT_AWS_REGION)
    content = content.replace("{{ECR_REPOSITORY}}", f"{AWS_ACCOUNT_ID}.dkr.ecr.{DEFAULT_AWS_REGION}.amazonaws.com/{service_name}")
    content = content.replace("{{AWS_ROLE_ARN}}", AWS_ROLE_ARN)
    content = content.replace("{{EKS_CLUSTER_NAME}}", eks_cluster_name)
    content = content.replace("{{GITHUB_ENVIRONMENT}}", "")
    content = content.replace("{{CUSTOM_BUILD_ARGS}}", "")

    # Handle Dockerfile path - add -f flag if custom path is provided
    if dockerfile_path and dockerfile_path != "Dockerfile":
        content = content.replace("{{DOCKERFILE_FLAG}}", f"-f {dockerfile_path} ")
    else:
        content = content.replace("{{DOCKERFILE_FLAG}}", "")

    # Language-specific placeholders
    if lang_lower in ("java-gradle", "java-maven", "java"):
        content = content.replace("{{JAVA_VERSION}}", version)
        content = content.replace("{{JAR_PATH}}", "build/libs/*.jar")
    elif lang_lower in ("golang", "go"):
        content = content.replace("{{GO_VERSION}}", version)
        content = content.replace("{{BUILD_PATH}}", "./cmd/...")
        content = content.replace("{{WIRE_STEP}}", "")
        content = content.replace("{{GO_DOCKER_BUILD_ARGS}}", "")

    return content


# ─── Kustomize Generators ───

def generate_kustomization_yaml(
    service_name: str,
    environment: str,
    namespace: str,
    tenant_code: str = "",
    include_namespace: bool = True,
) -> str:
    """Generate kustomization.yaml file.

    *include_namespace* should be False when deploying into a shared namespace
    that was already created at signup, so the namespace.yaml resource is omitted.
    """
    content = _read_template(KUSTOMIZATION_TEMPLATE)

    resources = []
    if include_namespace:
        resources.append("  - namespace.yaml")
    resources += [
        "  - serviceaccount.yaml",
        "  - deployment.yaml",
        "  - service.yaml",
        "  - ingress.yaml",
    ]

    content = content.replace("{{KUSTOMIZE_RESOURCES}}", "\n".join(resources))
    content = content.replace("{{SERVICE_NAME}}", service_name)
    content = content.replace("{{ENVIRONMENT}}", environment)
    content = content.replace("{{NAMESPACE}}", namespace)
    content = content.replace("{{TENANT_CODE}}", tenant_code)

    return content


def generate_namespace_yaml(
    namespace: str,
    environment: str,
    tenant_code: str = "",
) -> str:
    """Generate namespace.yaml file."""
    content = _read_template(NAMESPACE_TEMPLATE)

    content = content.replace("{{NAMESPACE}}", namespace)
    content = content.replace("{{ENVIRONMENT}}", environment)
    content = content.replace("{{TENANT_CODE}}", tenant_code)

    return content


def generate_serviceaccount_yaml(
    service_name: str,
    namespace: str,
    environment: str
) -> str:
    """Generate serviceaccount.yaml file."""
    content = _read_template(SERVICEACCOUNT_TEMPLATE)

    content = content.replace("{{SERVICE_NAME}}", service_name)
    content = content.replace("{{NAMESPACE}}", namespace)
    content = content.replace("{{ENVIRONMENT}}", environment)

    return content


def generate_deployment_yaml(
    service_name: str,
    namespace: str,
    port: int,
    cpu_requested: str = "500m",
    cpu_limit: str = "1000m",
    memory_requested: str = "512Mi",
    memory_limit: str = "1024Mi",
    replica_count: int = 1,
    health_endpoint: str = "/health",
    ecr_registry: str = "597189966628.dkr.ecr.ap-south-1.amazonaws.com",
    image_tag: str = "latest",
    tenant_code: str = "",
    environment: str = "",
) -> str:
    """Generate deployment.yaml file."""
    content = _read_template(DEPLOYMENT_TEMPLATE)

    strategy = """    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0"""

    env_vars = """          env:
            - name: K8S_POD_UID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.uid
            - name: K8S_POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name"""

    content = content.replace("{{SERVICE_NAME}}", service_name)
    content = content.replace("{{NAMESPACE}}", namespace)
    content = content.replace("{{REPLICA_COUNT}}", str(replica_count))
    content = content.replace("{{DEPLOYMENT_STRATEGY}}", strategy)
    content = content.replace("{{ECR_REGISTRY}}", ecr_registry)
    content = content.replace("{{ECR_REPO_NAME}}", service_name)
    content = content.replace("{{IMAGE_TAG}}", image_tag)
    content = content.replace("{{CONTAINER_PORT}}", str(port))
    content = content.replace("{{ENV_VARS}}", env_vars)
    content = content.replace("{{VOLUME_MOUNTS}}\n", "")
    content = content.replace("{{CPU_REQUESTED}}", cpu_requested)
    content = content.replace("{{CPU_LIMIT}}", cpu_limit)
    content = content.replace("{{MEMORY_REQUESTED}}", memory_requested)
    content = content.replace("{{MEMORY_LIMIT}}", memory_limit)
    content = content.replace("{{HEALTH_ENDPOINT}}", health_endpoint)
    content = content.replace("{{VOLUMES}}\n", "")
    content = content.replace("{{TENANT_CODE}}", tenant_code)
    content = content.replace("{{ENVIRONMENT}}", environment)

    return content


def generate_service_yaml(service_name: str, namespace: str, port: int) -> str:
    """Generate service.yaml file."""
    content = _read_template(SERVICE_TEMPLATE)

    content = content.replace("{{SERVICE_NAME}}", service_name)
    content = content.replace("{{NAMESPACE}}", namespace)
    content = content.replace("{{CONTAINER_PORT}}", str(port))

    return content


def generate_ingress_yaml(
    service_name: str,
    namespace: str,
    port: int,
    service_path: str = None,
    health_endpoint: str = "/health",
    alb_schema: str = "internet-facing",
    alb_group_name: Optional[str] = None,
    ingress_hostname: Optional[str] = None,
    ingress_class_name: Optional[str] = None,
) -> str:
    """Generate ingress.yaml file.

    When *alb_group_name* is provided the Ingress joins the tenant's IngressGroup
    so all services in the same tenant+env share one ALB. *ingress_hostname*
    sets the host-based routing rule, e.g. ``my-api.aslam-stage.devlift.ai``.
    *ingress_class_name* defaults to ``alb-public-{alb_group_name}`` so each
    tenant-env binds to its own per-tenant IngressClass.
    """
    if service_path is None:
        service_path = "/"  # Default to root path unless explicitly provided

    content = _read_template(INGRESS_TEMPLATE)

    # Build ALB IngressGroup annotation block (4-space indent to align under `annotations:`).
    if alb_group_name:
        alb_group_annotations = (
            f"    alb.ingress.kubernetes.io/group.name: {alb_group_name}\n"
            f"    alb.ingress.kubernetes.io/group.order: '{DEFAULT_ALB_GROUP_ORDER_SERVICE}'"
        )
        certificate_annotations = (
            f"    alb.ingress.kubernetes.io/certificate-arn: {SHARED_ACM_CERT_ARN}\n"
            f"    alb.ingress.kubernetes.io/listen-ports: '[{{\"HTTP\":80}},{{\"HTTPS\":443}}]'\n"
            f"    alb.ingress.kubernetes.io/ssl-redirect: '443'"
        )
    else:
        alb_group_annotations = ""
        certificate_annotations = ""

    # Build host-rule block (2-space indent under `rules:`).
    if ingress_hostname:
        ingress_host_rule = f"  - host: {ingress_hostname}\n    http:"
    else:
        ingress_host_rule = "  - http:"

    content = content.replace("{{SERVICE_NAME}}", service_name)
    content = content.replace("{{NAMESPACE}}", namespace)
    content = content.replace("{{ALB_SCHEME}}", alb_schema)
    content = content.replace("{{HEALTH_ENDPOINT}}", health_endpoint)
    content = content.replace("{{CERTIFICATE_ANNOTATIONS}}", certificate_annotations)
    content = content.replace("{{ALB_GROUP_ANNOTATIONS}}", alb_group_annotations)
    content = content.replace("{{INGRESS_HOST_RULE}}", ingress_host_rule)
    if not ingress_class_name:
        ingress_class_name = f"alb-public-{alb_group_name}" if alb_group_name else "alb-public"
    content = content.replace("{{INGRESS_CLASS_NAME}}", ingress_class_name)
    content = content.replace("{{SERVICE_PATH}}", service_path)
    content = content.replace("{{CONTAINER_PORT}}", str(port))

    return content


