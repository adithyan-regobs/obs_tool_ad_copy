"""
EKS Dry Run Service

Generates preview YAML files (values.yaml and deployment manifests) from form data
without requiring a saved configuration. Used for real-time preview in the UI.
"""

import yaml
import logging
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.service_config_schemas import EKSDryRunRequest
from app.services.kustomize_generator_service import KustomizeGeneratorService
from app.repository.language_ref_repository import LanguageRefRepository

logger = logging.getLogger(__name__)


class EKSDryRunService:
    """Service for generating EKS dry-run previews"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.language_repo = LanguageRefRepository(session)

    async def generate_dry_run(
        self,
        request: EKSDryRunRequest,
        tenant_code: str
    ) -> Dict[str, Any]:
        """
        Generate dry-run preview from form data.

        Args:
            request: EKS configuration from the form
            tenant_code: Tenant code

        Returns:
            Dictionary with values_yaml, deployment_yaml, and metadata
        """
        # Get language info if language_ref_code provided
        language_name = request.language or "unknown"
        if request.language_ref_code:
            language_ref = await self.language_repo.get_by_code(request.language_ref_code)
            if language_ref:
                language_name = language_ref.name

        # Determine namespace
        namespace = request.namespace or f"{request.service_name}-{request.environment.value}"

        # Build config dict from request
        config = self._build_config_dict(request)

        # Build deployment strategy dict
        deployment_strategy = None
        if request.deployment_strategy:
            deployment_strategy = request.deployment_strategy.model_dump()

        # Generate values.yaml content
        values_yaml = self._generate_values_yaml(request, language_name, namespace)

        # Generate deployment manifests using KustomizeGeneratorService
        kustomize_service = KustomizeGeneratorService()
        deployment_yaml = kustomize_service.generate_deployment_yaml(
            service_name=request.service_name,
            namespace=namespace,
            environment=request.environment.value,
            config=config,
            deployment_strategy=deployment_strategy
        )

        return {
            "service_name": request.service_name,
            "environment": request.environment.value,
            "language": language_name,
            "values_yaml": values_yaml,
            "deployment_yaml": deployment_yaml,
            "config_yaml": None,  # Not generated for dry-run
            "workflow_yaml": None  # Not generated for dry-run
        }

    def _build_config_dict(self, request: EKSDryRunRequest) -> Dict[str, Any]:
        """Build config dict from request for KustomizeGeneratorService"""
        config = {
            "cpu_requested": request.cpu_requested,
            "cpu_limit": request.cpu_limit,
            "memory_requested": request.memory_requested,
            "memory_limit": request.memory_limit,
            "container_port": request.port if request.port else None,
            "health_endpoint": request.health if request.health else None,
            "alb_schema": request.alb_schema if request.alb_schema else None,
            "service_path": request.service_path if request.service_path else None,
            "secrets_enabled": request.secrets_enabled,
            "secret_keys": request.secret_keys,
            "ebs_enabled": request.ebs_enabled,
        }

        # Add EBS config if enabled
        if request.ebs_enabled:
            config["ebs"] = {
                "volume": request.ebs_volume,
                "size": request.ebs_size,
                "type": request.ebs_type or "gp3"
            }

        # Add HPA config
        if request.hpa_enabled:
            config["hpa"] = {
                "enabled": True,
                "min_replicas": request.min_replicas,
                "max_replicas": request.max_replicas,
                "cpu_threshold": request.cpu_threshold,
                "memory_threshold": request.memory_threshold
            }
        else:
            config["hpa"] = {"enabled": False}
            config["replica_count"] = request.replica_count or "1"

        return config

    def _generate_values_yaml(
        self,
        request: EKSDryRunRequest,
        language_name: str,
        namespace: str
    ) -> str:
        """
        Generate values.yaml content in Helm-style format.
        This is displayed in the left panel of the dry-run UI.
        """
        # Only include container port if specified
        container_ports = []
        if request.port:
            container_ports = [int(request.port)]

        values = {
            "namespace": namespace,
            "ContainerPort": container_ports,
            "GracePeriod": 30,
            "LivenessProbe": {
                "Path": request.health if request.health else None,
                "failureThreshold": 3,
                "initialDelaySeconds": 20,
                "periodSeconds": 10,
                "port": int(request.port) if request.port else None,
                "successThreshold": 1,
                "timeoutSeconds": 5
            } if request.health or request.port else None,
            "ReadinessProbe": {
                "Path": request.health if request.health else None,
                "failureThreshold": 3,
                "initialDelaySeconds": 20,
                "periodSeconds": 10,
                "port": int(request.port) if request.port else None,
                "successThreshold": 1,
                "timeoutSeconds": 5
            } if request.health or request.port else None,
            "ServicePath": request.service_path or "/",
            "autoscaling": {
                "enabled": request.hpa_enabled,
                "MinReplicas": int(request.min_replicas or 0) if request.hpa_enabled else 0,
                "MaxReplicas": int(request.max_replicas or 0) if request.hpa_enabled else 0,
                "TargetCPUUtilizationPercentage": int(request.cpu_threshold or 0) if request.hpa_enabled else 0,
                "TargetMemoryUtilizationPercentage": int(request.memory_threshold or 0) if request.hpa_enabled else 0
            },
            "imagePullPolicy": "IfNotPresent",
            "replicaCount": int(request.replica_count or 1) if not request.hpa_enabled else int(request.min_replicas or 1),
            "resources": {
                "limits": {
                    "cpu": request.cpu_limit or "",
                    "memory": request.memory_limit or ""
                },
                "requests": {
                    "cpu": request.cpu_requested or "",
                    "memory": request.memory_requested or ""
                }
            },
            "secret": {
                "enabled": request.secrets_enabled,
                "data": {}
            }
        }

        # Add secrets if enabled
        if request.secrets_enabled and request.secret_keys:
            secret_keys = [k.strip() for k in request.secret_keys.split(",") if k.strip()]
            values["secret"]["data"] = {key: "" for key in secret_keys}

        # Add deployment strategy info based on selected strategy
        if request.deployment_strategy:
            strategy = request.deployment_strategy.strategy
            if strategy == "canary" and request.deployment_strategy.canary:
                values["deploymentStrategy"] = {
                    "type": "canary",
                    "canary": request.deployment_strategy.canary.model_dump()
                }
            elif strategy == "bluegreen" and request.deployment_strategy.blueGreen:
                values["deploymentStrategy"] = {
                    "type": "bluegreen",
                    "blueGreen": request.deployment_strategy.blueGreen.model_dump()
                }
            elif strategy == "rolling":
                # For rolling, only add MaxSurge/MaxUnavailable at top level (not deploymentStrategy)
                if request.deployment_strategy.rolling:
                    values["MaxSurge"] = request.deployment_strategy.rolling.maxSurge
                    values["MaxUnavailable"] = request.deployment_strategy.rolling.maxUnavailable
                else:
                    values["MaxSurge"] = 1
                    values["MaxUnavailable"] = 0
        else:
            # Default to rolling with default values if no strategy specified
            # Only add MaxSurge/MaxUnavailable at root level, not deploymentStrategy
            values["MaxSurge"] = 1
            values["MaxUnavailable"] = 0

        # Convert to YAML
        return yaml.dump(values, default_flow_style=False, sort_keys=False, allow_unicode=True)

    async def generate_deployment_from_values(
        self,
        service_name: str,
        namespace: str,
        environment: str,
        values_yaml: str,
        deployment_strategy: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Generate deployment.yaml from edited values.yaml content.

        This enables live preview - when user edits values.yaml, regenerate deployment.yaml.

        Args:
            service_name: Service name
            namespace: Kubernetes namespace
            environment: Environment (dev/staging/prod)
            values_yaml: Edited values.yaml content (YAML string)
            deployment_strategy: Optional deployment strategy override

        Returns:
            Regenerated deployment.yaml content
        """
        # Parse the values.yaml
        try:
            values = yaml.safe_load(values_yaml)
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse values.yaml: {e}")
            raise ValueError(f"Invalid YAML format: {e}")

        if not values:
            raise ValueError("Empty values.yaml content")

        # Extract namespace from values.yaml (user may have edited it)
        extracted_namespace = values.get("namespace", namespace)

        # Extract config from values.yaml structure
        resources = values.get("resources", {})
        autoscaling = values.get("autoscaling", {})
        liveness_probe = values.get("LivenessProbe", {})
        readiness_probe = values.get("ReadinessProbe", {})
        container_ports = values.get("ContainerPort", [])
        # Handle both old format (list of dicts) and new format (list of ints)
        if container_ports and isinstance(container_ports[0], dict):
            container_port = container_ports[0].get("port", 8080)
        elif container_ports and isinstance(container_ports[0], int):
            container_port = container_ports[0]
        else:
            container_port = 8080
        secret_config = values.get("secret", {})
        image_pull_policy = values.get("imagePullPolicy", "Always")
        grace_period = values.get("GracePeriod", 30)

        config = {
            "cpu_requested": resources.get("requests", {}).get("cpu", "500m"),
            "cpu_limit": resources.get("limits", {}).get("cpu", "1000m"),
            "memory_requested": resources.get("requests", {}).get("memory", "512Mi"),
            "memory_limit": resources.get("limits", {}).get("memory", "1024Mi"),
            "container_port": str(container_port),
            "grace_period": grace_period,
            "health_endpoint": liveness_probe.get("Path", "/health"),
            "alb_schema": "internal",
            "service_path": values.get("ServicePath", "/"),
            "secrets_enabled": secret_config.get("enabled", False),
            "secret_keys": ",".join(secret_config.get("data", {}).keys()) if secret_config.get("data") else "",
            "replica_count": str(values.get("replicaCount", 1)),
            # Image pull policy
            "image_pull_policy": image_pull_policy,
            # Liveness probe configuration
            "liveness_probe": {
                "path": liveness_probe.get("Path", "/health"),
                "port": liveness_probe.get("port", container_port),
                "initial_delay_seconds": liveness_probe.get("initialDelaySeconds", 30),
                "period_seconds": liveness_probe.get("periodSeconds", 30),
                "timeout_seconds": liveness_probe.get("timeoutSeconds", 5),
                "failure_threshold": liveness_probe.get("failureThreshold", 3),
                "success_threshold": liveness_probe.get("successThreshold", 1),
            },
            # Readiness probe configuration
            "readiness_probe": {
                "path": readiness_probe.get("Path", liveness_probe.get("Path", "/health")),
                "port": readiness_probe.get("port", container_port),
                "initial_delay_seconds": readiness_probe.get("initialDelaySeconds", 20),
                "period_seconds": readiness_probe.get("periodSeconds", 5),
                "timeout_seconds": readiness_probe.get("timeoutSeconds", 5),
                "failure_threshold": readiness_probe.get("failureThreshold", 3),
                "success_threshold": readiness_probe.get("successThreshold", 1),
            },
        }

        # Add HPA config if enabled
        if autoscaling.get("enabled"):
            config["hpa"] = {
                "enabled": True,
                "min_replicas": str(autoscaling.get("MinReplicas", 1)),
                "max_replicas": str(autoscaling.get("MaxReplicas", 10)),
                "cpu_threshold": str(autoscaling.get("TargetCPUUtilizationPercentage", 80)),
                "memory_threshold": str(autoscaling.get("TargetMemoryUtilizationPercentage", 80))
            }
        else:
            config["hpa"] = {"enabled": False}

        # ALWAYS extract deployment strategy from values.yaml (user may have edited it)
        # This overrides any passed deployment_strategy to ensure we use the edited values
        values_strategy = values.get("deploymentStrategy", {})
        if values_strategy:
            deployment_strategy = {
                "strategy": values_strategy.get("type", "rolling"),
                "canary": values_strategy.get("canary"),
                "blueGreen": values_strategy.get("blueGreen"),
                "rolling": {
                    "maxSurge": values.get("MaxSurge", 1),
                    "maxUnavailable": values.get("MaxUnavailable", 0)
                }
            }
        elif not deployment_strategy:
            # Fallback: if no deploymentStrategy in values and none passed, use rolling
            deployment_strategy = {
                "strategy": "rolling",
                "rolling": {
                    "maxSurge": values.get("MaxSurge", 1),
                    "maxUnavailable": values.get("MaxUnavailable", 0)
                }
            }

        # Generate deployment using KustomizeGeneratorService
        kustomize_service = KustomizeGeneratorService()
        return kustomize_service.generate_deployment_yaml(
            service_name=service_name,
            namespace=extracted_namespace,
            environment=environment,
            config=config,
            deployment_strategy=deployment_strategy
        )
