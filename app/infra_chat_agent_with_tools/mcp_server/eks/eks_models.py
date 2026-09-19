"""
Pydantic models for EKS service onboarding tools.
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class HostServiceModel(BaseModel):
    """Host a service on EKS. Generates complete EKS onboarding files.

    IMPORTANT REQUIREMENT: The application MUST expose a health check endpoint (default: /health or /actuator/health for Spring Boot).
    Without a health endpoint, Kubernetes health probes will fail and pods will crash. Ensure the application code includes:
    - For Spring Boot: Include spring-boot-starter-actuator dependency and ensure /actuator/health is accessible
    - For Node.js/Express: Add a GET /health route that returns 200 OK
    - For Python/Flask: Add @app.route('/health') endpoint
    - For Go: Add http.HandleFunc("/health", healthHandler)
    """

    service_name: str = Field(
        description="Service name for the application"
    )
    language: Literal["java", "golang", "go", "python", "nodejs", "node"] = Field(
        description="Programming language (e.g., 'java', 'python', 'nodejs', 'golang')"
    )
    build_tool: Optional[Literal["maven", "gradle"]] = Field(
        default=None,
        description="Build tool for Java projects. Required only for Java. Specify 'maven' if pom.xml exists, 'gradle' if build.gradle/build.gradle.kts exists. Infer from user's project context. Defaults to 'gradle' if not specified."
    )
    version: str = Field(
        description="Language/runtime version (e.g., '17' for Java, '1.24' for Go, '3.11' for Python, '20' for Node.js)"
    )
    port: Optional[int] = Field(
        default=None,
        description="Application port (auto-detected from language if not provided: Java=8080, Python=8000, Node=3000, Go=8080)"
    )
    branch: str = Field(
        default="main",
        description="Git branch that triggers the workflow"
    )
    environment: Literal["dev", "stage", "qa", "prod"] = Field(
        default="stage",
        description="Deployment environment"
    )
    generate_dockerfile: bool = Field(
        default=True,
        description="Whether to generate a Dockerfile. Set to False if a Dockerfile already exists in the project"
    )
    dockerfile_path: Optional[str] = Field(
        default="Dockerfile",
        description="Path to existing Dockerfile (e.g., 'Dockerfile', 'docker/Dockerfile', 'Dockerfile.prod'). If not provided and generate_dockerfile=False, defaults to 'Dockerfile'"
    )

    tenant_code: Optional[str] = Field(
        default=None,
        description=(
            "Tenant subdomain. When provided, the service deploys into the pre-created shared "
            "namespace ({tenant_code}-stage) and joins the shared ALB via IngressGroup. "
            "The service hostname will be {service_name}.{tenant_code}-stage.devlift.ai. "
            "Leave unset only for backwards-compatible standalone deployments."
        )
    )

    # Kustomize parameters
    health_endpoint: str = Field(
        description="Health check endpoint that the application exposes (REQUIRED). Common values: '/health', '/actuator/health' (Spring Boot), '/healthz'. The application MUST respond with HTTP 200 OK on this endpoint for Kubernetes health probes to succeed."
    )
    cpu_requested: str = Field(
        default="500m",
        description="CPU request"
    )
    cpu_limit: str = Field(
        default="1000m",
        description="CPU limit"
    )
    memory_requested: str = Field(
        default="512Mi",
        description="Memory request"
    )
    memory_limit: str = Field(
        default="1024Mi",
        description="Memory limit"
    )
    replica_count: int = Field(
        default=1,
        description="Number of replicas"
    )


class GetAlbUrlModel(BaseModel):
    """Get ALB URL for a deployed service."""

    service_name: str = Field(
        description="Service name"
    )
    namespace: Optional[str] = Field(
        default=None,
        description="Kubernetes namespace (defaults to service_name if not provided)"
    )


class MonitorDeploymentModel(BaseModel):
    """Check GitHub Actions deployment status once and return immediately. The LLM should call this repeatedly with delays until status is 'success', 'failure', or timeout."""

    service_name: str = Field(
        description="Service name being deployed"
    )
    github_repo: str = Field(
        description="GitHub repository in format 'owner/repo' (e.g., 'Regobs/my-service')"
    )
    branch: str = Field(
        default="main",
        description="Git branch to monitor"
    )
    environment: Literal["dev", "stage", "qa", "prod"] = Field(
        default="stage",
        description="Deployment environment (must match the environment used during service onboarding)"
    )
    poll_interval: int = Field(
        default=10,
        description="Recommended seconds to wait before checking again (used by LLM for polling loop)"
    )
    max_duration: int = Field(
        default=600,
        description="Maximum total time to keep checking in seconds (default: 600s = 10 minutes)"
    )


class TriggerBuildModel(BaseModel):
    """Trigger a GitHub Actions workflow to rebuild and redeploy a service."""

    service_name: str = Field(
        description="Service name to rebuild/redeploy"
    )
    github_repo: str = Field(
        description="GitHub repository in format 'owner/repo' (e.g., 'Regobs/my-service')"
    )
    branch: str = Field(
        default="main",
        description="Git branch to trigger the workflow on"
    )
    environment: Literal["dev", "stage", "qa", "prod"] = Field(
        default="stage",
        description="Deployment environment to trigger"
    )


# Tool registry
TOOL_MODELS: dict[str, type[BaseModel]] = {
    "HostService": HostServiceModel,
    "GetAlbUrl": GetAlbUrlModel,
    "MonitorDeployment": MonitorDeploymentModel,
    "TriggerBuild": TriggerBuildModel,
}
