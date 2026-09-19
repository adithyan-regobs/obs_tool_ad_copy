"""
Kustomize Generator Service

Generates Kubernetes manifests (deployment.yaml) using Kustomize templates.
Reads service configuration and produces complete K8s manifests including:
- SecretProviderClass (for AWS Secrets Manager integration)
- Deployment
- Service
- Ingress
- HorizontalPodAutoscaler (HPA)
"""
import os
import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

# Template paths
KUSTOMIZE_TEMPLATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "templates", "eks", "kustomize", "base"
)


class KustomizeGeneratorService:
    """Service for generating Kubernetes manifests from service configuration."""

    def __init__(self):
        self.templates_path = KUSTOMIZE_TEMPLATES_PATH

    def generate_deployment_yaml(
        self,
        service_name: str,
        namespace: str,
        environment: str,
        config: Dict[str, Any],
        deployment_strategy: Optional[Dict[str, Any]] = None,
        aws_region: str = "us-west-2",
        ecr_registry: str = "",
        ecr_repo_name: str = "",
        image_tag: str = "latest",
        alb_subnets: str = "",
        alb_certificate_arn: str = "",
        ingress_host: str = "",
        secrets_manager_path: str = "",
        alb_group_name: str = "",
    ) -> str:
        """
        Generate complete deployment.yaml with all K8s resources.

        Args:
            service_name: Name of the service
            namespace: Kubernetes namespace
            environment: Environment (dev/staging/prod)
            config: Service configuration from DB (config JSONB)
            deployment_strategy: Deployment strategy from DB (separate column)
            aws_region: AWS region for secrets
            ecr_registry: ECR registry URL
            ecr_repo_name: ECR repository name
            image_tag: Docker image tag
            alb_subnets: Comma-separated subnet IDs
            alb_certificate_arn: ACM certificate ARN
            ingress_host: Ingress hostname
            secrets_manager_path: Path to secrets in AWS Secrets Manager

        Returns:
            Complete deployment.yaml content as string
        """
        manifests = []

        # Extract config values with defaults
        container_port = config.get("port") or config.get("container_port") or "8080"
        cpu_requested = config.get("cpu_requested") or config.get("cpu") or "125m"
        cpu_limit = config.get("cpu_limit") or config.get("cpu") or "125m"
        memory_requested = config.get("memory_requested") or config.get("ram") or "256Mi"
        memory_limit = config.get("memory_limit") or config.get("ram") or "256Mi"
        health_endpoint = config.get("health") or config.get("health_endpoint") or "/health"
        service_path = config.get("service_path") or "/"
        replica_count = config.get("replica_count") or "1"
        alb_schema = config.get("alb_schema") or "internet-facing"
        secrets_enabled = config.get("secrets_enabled", False)
        grace_period = config.get("grace_period", 30)
        secret_keys = config.get("secret_keys") or ""
        hpa_config = config.get("hpa") or {}

        # Extract probe configurations
        image_pull_policy = config.get("image_pull_policy", "Always")
        liveness_probe = config.get("liveness_probe") or {}
        readiness_probe = config.get("readiness_probe") or {}

        # Liveness probe values with defaults
        liveness_path = liveness_probe.get("path", health_endpoint)
        liveness_port = liveness_probe.get("port", container_port)
        liveness_initial_delay = liveness_probe.get("initial_delay_seconds", 30)
        liveness_period = liveness_probe.get("period_seconds", 30)
        liveness_timeout = liveness_probe.get("timeout_seconds", 5)
        liveness_failure = liveness_probe.get("failure_threshold", 3)
        liveness_success = liveness_probe.get("success_threshold", 1)

        # Readiness probe values with defaults
        readiness_path = readiness_probe.get("path", health_endpoint)
        readiness_port = readiness_probe.get("port", container_port)
        readiness_initial_delay = readiness_probe.get("initial_delay_seconds", 20)
        readiness_period = readiness_probe.get("period_seconds", 5)
        readiness_timeout = readiness_probe.get("timeout_seconds", 5)
        readiness_failure = readiness_probe.get("failure_threshold", 3)
        readiness_success = readiness_probe.get("success_threshold", 1)

        # Normalize memory values
        if not str(memory_requested).endswith(('Mi', 'Gi', 'MB', 'GB', 'm')):
            memory_requested = f"{memory_requested}Mi"
        if not str(memory_limit).endswith(('Mi', 'Gi', 'MB', 'GB', 'm')):
            memory_limit = f"{memory_limit}Mi"

        # Normalize CPU values
        if not str(cpu_requested).endswith('m'):
            cpu_requested = f"{cpu_requested}m"
        if not str(cpu_limit).endswith('m'):
            cpu_limit = f"{cpu_limit}m"

        # Parse secret keys
        secret_keys_list = []
        if secret_keys:
            if isinstance(secret_keys, str):
                secret_keys_list = [k.strip() for k in secret_keys.split(",") if k.strip()]
            else:
                secret_keys_list = secret_keys

        # 1. Generate Namespace manifest
        namespace_yaml = self._generate_namespace(namespace)
        manifests.append(namespace_yaml)

        # 2. Generate SecretProviderClass (if secrets enabled)
        if secrets_enabled and secret_keys_list:
            secret_provider_yaml = self._generate_secret_provider_class(
                service_name=service_name,
                namespace=namespace,
                aws_region=aws_region,
                secrets_manager_path=secrets_manager_path or f"{service_name}/{environment}",
                secret_keys=secret_keys_list
            )
            manifests.append(secret_provider_yaml)

        # 2. Generate Deployment or Argo Rollout based on strategy
        strategy_type = deployment_strategy.get("strategy", "rolling") if deployment_strategy else "rolling"

        if strategy_type in ["canary", "bluegreen"]:
            # Use Argo Rollouts for canary and blue-green strategies
            rollout_yaml = self._generate_argo_rollout(
                service_name=service_name,
                namespace=namespace,
                replica_count=replica_count,
                deployment_strategy=deployment_strategy,
                ecr_registry=ecr_registry,
                ecr_repo_name=ecr_repo_name or service_name,
                image_tag=image_tag,
                container_port=container_port,
                cpu_requested=cpu_requested,
                cpu_limit=cpu_limit,
                memory_requested=memory_requested,
                memory_limit=memory_limit,
                health_endpoint=health_endpoint,
                secrets_enabled=secrets_enabled,
                secret_keys=secret_keys_list,
                image_pull_policy=image_pull_policy,
                liveness_path=liveness_path,
                liveness_port=liveness_port,
                liveness_initial_delay=liveness_initial_delay,
                liveness_period=liveness_period,
                liveness_timeout=liveness_timeout,
                liveness_failure=liveness_failure,
                liveness_success=liveness_success,
                readiness_path=readiness_path,
                readiness_port=readiness_port,
                readiness_initial_delay=readiness_initial_delay,
                readiness_period=readiness_period,
                readiness_timeout=readiness_timeout,
                readiness_failure=readiness_failure,
                readiness_success=readiness_success,
                grace_period=grace_period
            )
            manifests.append(rollout_yaml)
        else:
            # Use standard Kubernetes Deployment for rolling and recreate strategies
            deployment_yaml = self._generate_deployment(
                service_name=service_name,
                namespace=namespace,
                replica_count=replica_count,
                deployment_strategy=deployment_strategy,
                ecr_registry=ecr_registry,
                ecr_repo_name=ecr_repo_name or service_name,
                image_tag=image_tag,
                container_port=container_port,
                cpu_requested=cpu_requested,
                cpu_limit=cpu_limit,
                memory_requested=memory_requested,
                memory_limit=memory_limit,
                health_endpoint=health_endpoint,
                secrets_enabled=secrets_enabled,
                secret_keys=secret_keys_list,
                image_pull_policy=image_pull_policy,
                liveness_path=liveness_path,
                liveness_port=liveness_port,
                liveness_initial_delay=liveness_initial_delay,
                liveness_period=liveness_period,
                liveness_timeout=liveness_timeout,
                liveness_failure=liveness_failure,
                liveness_success=liveness_success,
                readiness_path=readiness_path,
                readiness_port=readiness_port,
                readiness_initial_delay=readiness_initial_delay,
                readiness_period=readiness_period,
                readiness_timeout=readiness_timeout,
                readiness_failure=readiness_failure,
                readiness_success=readiness_success,
                grace_period=grace_period
            )
            manifests.append(deployment_yaml)

        # 3. Generate Service(s)
        if strategy_type == "canary":
            # For canary: generate canary-service and stable-service
            canary_config = deployment_strategy.get("canary", {}) if deployment_strategy else {}
            canary_service_name = canary_config.get("canaryService", f"{service_name}-canary")
            stable_service_name = canary_config.get("stableService", f"{service_name}-stable")

            canary_svc_yaml = self._generate_service(
                service_name=canary_service_name,
                namespace=namespace,
                container_port=container_port,
                app_label=service_name
            )
            manifests.append(canary_svc_yaml)

            stable_svc_yaml = self._generate_service(
                service_name=stable_service_name,
                namespace=namespace,
                container_port=container_port,
                app_label=service_name
            )
            manifests.append(stable_svc_yaml)

        elif strategy_type == "bluegreen":
            # For blue-green: generate active-service and preview-service
            bluegreen_config = deployment_strategy.get("blueGreen", {}) if deployment_strategy else {}
            active_service_name = bluegreen_config.get("activeService", f"{service_name}-active")
            preview_service_name = bluegreen_config.get("previewService", f"{service_name}-preview")

            active_svc_yaml = self._generate_service(
                service_name=active_service_name,
                namespace=namespace,
                container_port=container_port,
                app_label=service_name
            )
            manifests.append(active_svc_yaml)

            preview_svc_yaml = self._generate_service(
                service_name=preview_service_name,
                namespace=namespace,
                container_port=container_port,
                app_label=service_name
            )
            manifests.append(preview_svc_yaml)

        else:
            # Standard single service for rolling/recreate strategies
            service_yaml = self._generate_service(
                service_name=service_name,
                namespace=namespace,
                container_port=container_port
            )
            manifests.append(service_yaml)

        # 4. Generate Ingress (if ALB schema is set)
        if alb_schema:
            ingress_yaml = self._generate_ingress(
                service_name=service_name,
                namespace=namespace,
                alb_schema=alb_schema,
                alb_subnets=alb_subnets,
                alb_certificate_arn=alb_certificate_arn,
                health_endpoint=health_endpoint,
                ingress_host=ingress_host,
                service_path=service_path,
                container_port=container_port,
                alb_group_name=alb_group_name,
            )
            manifests.append(ingress_yaml)

        # 5. Generate HPA (if enabled)
        # Note: For canary/bluegreen, HPA should target Rollout instead of Deployment
        if hpa_config.get("enabled"):
            is_rollout = strategy_type in ["canary", "bluegreen"]
            hpa_yaml = self._generate_hpa(
                service_name=service_name,
                namespace=namespace,
                min_replicas=hpa_config.get("min_replicas") or "1",
                max_replicas=hpa_config.get("max_replicas") or "10",
                cpu_threshold=hpa_config.get("cpu_threshold") or "60",
                memory_threshold=hpa_config.get("memory_threshold") or "70",
                target_kind="Rollout" if is_rollout else "Deployment",
                target_api_version="argoproj.io/v1alpha1" if is_rollout else "apps/v1"
            )
            manifests.append(hpa_yaml)

        # Join all manifests with YAML document separator
        return "---\n".join(manifests)

    def _generate_secret_provider_class(
        self,
        service_name: str,
        namespace: str,
        aws_region: str,
        secrets_manager_path: str,
        secret_keys: List[str]
    ) -> str:
        """Generate SecretProviderClass manifest."""
        # Generate jmesPath items
        jmes_path_items = []
        for key in secret_keys:
            jmes_path_items.append(f"          - path: {key}")
            jmes_path_items.append(f"            objectAlias: {key}")

        # Generate secretObjects
        secret_objects_lines = [
            f"    - secretName: {service_name}-k8s-secrets",
            "      type: Opaque",
            "      data:"
        ]
        for key in secret_keys:
            secret_objects_lines.append(f"        - key: {key}")
            secret_objects_lines.append(f"          objectName: {key}")

        template = f"""apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: {service_name}-secrets-csi
  namespace: {namespace}
spec:
  provider: aws
  parameters:
    region: "{aws_region}"
    objects: |
      - objectName: "{secrets_manager_path}"
        objectType: "secretsmanager"
        jmesPath:
{chr(10).join(jmes_path_items)}
  secretObjects:
{chr(10).join(secret_objects_lines)}
"""
        return template

    def _generate_deployment_strategy_yaml(self, deployment_strategy: Optional[Dict[str, Any]]) -> str:
        """Generate deployment strategy YAML block."""
        if not deployment_strategy:
            # Default rolling update
            return """    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0"""

        strategy = deployment_strategy.get("strategy", "rolling")

        if strategy == "rolling":
            rolling = deployment_strategy.get("rolling", {})
            max_surge = rolling.get("maxSurge", "25%")
            max_unavailable = rolling.get("maxUnavailable", "0")
            # Convert percentage to integer for K8s if needed
            if str(max_surge).endswith('%'):
                max_surge = f'"{max_surge}"'
            if str(max_unavailable).endswith('%'):
                max_unavailable = f'"{max_unavailable}"'
            return f"""    type: RollingUpdate
    rollingUpdate:
      maxSurge: {max_surge}
      maxUnavailable: {max_unavailable}"""

        elif strategy == "recreate":
            return "    type: Recreate"

        elif strategy == "bluegreen":
            # Blue-green typically uses Argo Rollouts, but basic K8s deployment
            # For standard K8s deployment, we use RollingUpdate
            bluegreen = deployment_strategy.get("blueGreen", {})
            return f"""    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
    # Blue-Green deployment (use with Argo Rollouts for full support)
    # activeService: {bluegreen.get('activeService', 'service-active')}
    # previewService: {bluegreen.get('previewService', 'service-preview')}"""

        elif strategy == "canary":
            # Canary typically uses Argo Rollouts
            canary = deployment_strategy.get("canary", {})
            return f"""    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
    # Canary deployment (use with Argo Rollouts for full support)
    # canaryService: {canary.get('canaryService', 'service-canary')}
    # stableService: {canary.get('stableService', 'service-stable')}"""

        return """    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0"""

    def _generate_deployment(
        self,
        service_name: str,
        namespace: str,
        replica_count: str,
        deployment_strategy: Optional[Dict[str, Any]],
        ecr_registry: str,
        ecr_repo_name: str,
        image_tag: str,
        container_port: str,
        cpu_requested: str,
        cpu_limit: str,
        memory_requested: str,
        memory_limit: str,
        health_endpoint: str,
        secrets_enabled: bool,
        secret_keys: List[str],
        image_pull_policy: str = "Always",
        liveness_path: str = "/health",
        liveness_port: str = "8080",
        liveness_initial_delay: int = 30,
        liveness_period: int = 30,
        liveness_timeout: int = 5,
        liveness_failure: int = 3,
        liveness_success: int = 1,
        readiness_path: str = "/health",
        readiness_port: str = "8080",
        readiness_initial_delay: int = 20,
        readiness_period: int = 5,
        readiness_timeout: int = 5,
        readiness_failure: int = 3,
        readiness_success: int = 1,
        grace_period: int = 30
    ) -> str:
        """Generate Deployment manifest."""
        strategy_yaml = self._generate_deployment_strategy_yaml(deployment_strategy)

        # Generate env vars from secrets
        env_vars_lines = []
        if secrets_enabled and secret_keys:
            for key in secret_keys:
                env_vars_lines.append(f"""            - name: {key}
              valueFrom:
                secretKeyRef:
                  name: {service_name}-k8s-secrets
                  key: {key}""")

        # Add standard env vars
        env_vars_lines.append(f"""            - name: K8S_POD_UID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.uid
            - name: K8S_POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name""")

        env_vars = "\n".join(env_vars_lines) if env_vars_lines else ""
        env_section = f"""          env:
{env_vars}""" if env_vars else ""

        # Generate volume mounts and volumes for secrets
        volume_mounts = ""
        volumes = ""
        if secrets_enabled and secret_keys:
            volume_mounts = f"""          volumeMounts:
            - name: secrets-store
              mountPath: "/var/secrets"
              readOnly: true"""
            volumes = f"""      volumes:
        - name: secrets-store
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: {service_name}-secrets-csi"""

        template = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {service_name}
  namespace: {namespace}
  annotations:
    reloader.stakater.com/auto: 'true'
  labels:
    app: {service_name}
spec:
  replicas: {replica_count}
  strategy:
{strategy_yaml}
  selector:
    matchLabels:
      app: {service_name}
  template:
    metadata:
      labels:
        app: {service_name}
    spec:
      serviceAccountName: {service_name}-sa
      terminationGracePeriodSeconds: {grace_period}
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 2000
      containers:
        - name: {service_name}
          image: {ecr_registry}/{ecr_repo_name}:{image_tag}
          imagePullPolicy: {image_pull_policy}
          ports:
            - name: http
              containerPort: {container_port}
{env_section}
{volume_mounts}
          resources:
            requests:
              cpu: "{cpu_requested}"
              memory: "{memory_requested}"
            limits:
              cpu: "{cpu_limit}"
              memory: "{memory_limit}"
          livenessProbe:
            httpGet:
              path: {liveness_path}
              port: {liveness_port}
            initialDelaySeconds: {liveness_initial_delay}
            periodSeconds: {liveness_period}
            timeoutSeconds: {liveness_timeout}
            failureThreshold: {liveness_failure}
            successThreshold: {liveness_success}
          readinessProbe:
            httpGet:
              path: {readiness_path}
              port: {readiness_port}
            initialDelaySeconds: {readiness_initial_delay}
            periodSeconds: {readiness_period}
            timeoutSeconds: {readiness_timeout}
            failureThreshold: {readiness_failure}
            successThreshold: {readiness_success}
{volumes}
"""
        return template

    def _generate_argo_rollout(
        self,
        service_name: str,
        namespace: str,
        replica_count: str,
        deployment_strategy: Optional[Dict[str, Any]],
        ecr_registry: str,
        ecr_repo_name: str,
        image_tag: str,
        container_port: str,
        cpu_requested: str,
        cpu_limit: str,
        memory_requested: str,
        memory_limit: str,
        health_endpoint: str,
        secrets_enabled: bool,
        secret_keys: List[str],
        image_pull_policy: str = "Always",
        liveness_path: str = "/health",
        liveness_port: str = "8080",
        liveness_initial_delay: int = 30,
        liveness_period: int = 30,
        liveness_timeout: int = 5,
        liveness_failure: int = 3,
        liveness_success: int = 1,
        readiness_path: str = "/health",
        readiness_port: str = "8080",
        readiness_initial_delay: int = 20,
        readiness_period: int = 5,
        readiness_timeout: int = 5,
        readiness_failure: int = 3,
        readiness_success: int = 1,
        grace_period: int = 30
    ) -> str:
        """Generate Argo Rollout manifest for canary or blue-green deployments."""
        strategy_type = deployment_strategy.get("strategy", "canary") if deployment_strategy else "canary"

        # Generate env vars from secrets
        env_vars_lines = []
        if secrets_enabled and secret_keys:
            for key in secret_keys:
                env_vars_lines.append(f"""            - name: {key}
              valueFrom:
                secretKeyRef:
                  name: {service_name}-k8s-secrets
                  key: {key}""")

        # Add standard env vars
        env_vars_lines.append(f"""            - name: K8S_POD_UID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.uid
            - name: K8S_POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name""")

        env_vars = "\n".join(env_vars_lines) if env_vars_lines else ""
        env_section = f"""          env:
{env_vars}""" if env_vars else ""

        # Generate volume mounts and volumes for secrets
        volume_mounts = ""
        volumes = ""
        if secrets_enabled and secret_keys:
            volume_mounts = f"""          volumeMounts:
            - name: secrets-store
              mountPath: "/var/secrets"
              readOnly: true"""
            volumes = f"""      volumes:
        - name: secrets-store
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: {service_name}-secrets-csi"""

        # Generate strategy section based on type
        if strategy_type == "canary":
            strategy_yaml = self._generate_canary_strategy(deployment_strategy, service_name)
        else:  # bluegreen
            strategy_yaml = self._generate_bluegreen_strategy(deployment_strategy, service_name)

        template = f"""apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: {service_name}
  namespace: {namespace}
  labels:
    app: {service_name}
spec:
  replicas: {replica_count}
  revisionHistoryLimit: 2
  selector:
    matchLabels:
      app: {service_name}
  template:
    metadata:
      labels:
        app: {service_name}
    spec:
      serviceAccountName: {service_name}-sa
      terminationGracePeriodSeconds: {grace_period}
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 2000
      containers:
        - name: {service_name}
          image: {ecr_registry}/{ecr_repo_name}:{image_tag}
          imagePullPolicy: {image_pull_policy}
          ports:
            - name: http
              containerPort: {container_port}
              protocol: TCP
{env_section}
{volume_mounts}
          resources:
            requests:
              cpu: "{cpu_requested}"
              memory: "{memory_requested}"
            limits:
              cpu: "{cpu_limit}"
              memory: "{memory_limit}"
          readinessProbe:
            httpGet:
              path: {readiness_path}
              port: {readiness_port}
            initialDelaySeconds: {readiness_initial_delay}
            periodSeconds: {readiness_period}
            timeoutSeconds: {readiness_timeout}
            failureThreshold: {readiness_failure}
            successThreshold: {readiness_success}
          livenessProbe:
            httpGet:
              path: {liveness_path}
              port: {liveness_port}
            initialDelaySeconds: {liveness_initial_delay}
            periodSeconds: {liveness_period}
            timeoutSeconds: {liveness_timeout}
            failureThreshold: {liveness_failure}
            successThreshold: {liveness_success}
{volumes}
  strategy:
{strategy_yaml}
"""
        return template

    def _generate_canary_strategy(self, deployment_strategy: Dict[str, Any], service_name: str) -> str:
        """Generate canary strategy section for Argo Rollout."""
        canary_config = deployment_strategy.get("canary", {})
        canary_service = canary_config.get("canaryService", f"{service_name}-canary")
        stable_service = canary_config.get("stableService", f"{service_name}-stable")

        # Generate steps from config or use defaults
        steps = canary_config.get("steps", [])
        if not steps:
            # Default canary steps
            steps = [
                {"weight": 10, "pauseDuration": 30, "pauseType": "duration"},
                {"weight": 30, "pauseDuration": 30, "pauseType": "duration"},
                {"weight": 50, "pauseDuration": 30, "pauseType": "duration"},
                {"weight": 70, "pauseDuration": 30, "pauseType": "duration"},
                {"weight": 100, "pauseDuration": 0, "pauseType": "duration"}
            ]

        steps_yaml_lines = []
        for step in steps:
            weight = step.get("weight", 10)
            pause_duration = step.get("pauseDuration", 30)
            pause_type = step.get("pauseType", "duration")

            steps_yaml_lines.append(f"        - setWeight: {weight}")
            if pause_type == "manual":
                steps_yaml_lines.append("        - pause: {}")
            elif pause_duration > 0:
                steps_yaml_lines.append(f"        - pause: {{duration: {pause_duration}s}}")

        steps_yaml = "\n".join(steps_yaml_lines)

        return f"""    canary:
      canaryService: {canary_service}
      stableService: {stable_service}
      steps:
{steps_yaml}"""

    def _generate_bluegreen_strategy(self, deployment_strategy: Dict[str, Any], service_name: str) -> str:
        """Generate blue-green strategy section for Argo Rollout."""
        bluegreen_config = deployment_strategy.get("blueGreen", {})
        active_service = bluegreen_config.get("activeService", f"{service_name}-active")
        preview_service = bluegreen_config.get("previewService", f"{service_name}-preview")
        auto_promote = str(bluegreen_config.get("autoPromote", False)).lower()
        scale_down_delay = bluegreen_config.get("scaleDownDelay", 30)
        preview_replicas = bluegreen_config.get("previewReplicas", 1)

        return f"""    blueGreen:
      activeService: {active_service}
      previewService: {preview_service}
      autoPromotionEnabled: {auto_promote}
      scaleDownDelaySeconds: {scale_down_delay}
      previewReplicaCount: {preview_replicas}
      antiAffinity:
        preferredDuringSchedulingIgnoredDuringExecution:
          weight: 100"""

    def _generate_namespace(self, namespace: str) -> str:
        """Generate Namespace manifest.

        Args:
            namespace: Kubernetes namespace name

        Returns:
            Namespace manifest YAML
        """
        return f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
"""

    def _generate_service(
        self,
        service_name: str,
        namespace: str,
        container_port: str,
        app_label: Optional[str] = None
    ) -> str:
        """Generate Service manifest.

        Args:
            service_name: Name of the service (used in metadata.name)
            namespace: Kubernetes namespace
            container_port: Container port
            app_label: App label for selector (defaults to service_name if not provided)
        """
        # Use provided app_label or default to service_name
        selector_app = app_label if app_label else service_name

        return f"""apiVersion: v1
kind: Service
metadata:
  name: {service_name}
  namespace: {namespace}
  labels:
    app: {selector_app}
spec:
  type: ClusterIP
  selector:
    app: {selector_app}
  ports:
  - name: http
    port: {container_port}
    targetPort: {container_port}
    protocol: TCP
"""

    def _generate_ingress(
        self,
        service_name: str,
        namespace: str,
        alb_schema: str,
        alb_subnets: str,
        alb_certificate_arn: str,
        health_endpoint: str,
        ingress_host: str,
        service_path: str,
        container_port: str,
        alb_group_name: str = "",
        ingress_class_name: str = "",
    ) -> str:
        """Generate Ingress manifest.

        When *alb_group_name* is provided the Ingress is placed in an
        IngressGroup so all services sharing the same group.name reuse one ALB.
        *ingress_class_name* defaults to ``alb-public-{alb_group_name}`` so
        each tenant-env binds to its own per-tenant IngressClass.
        """
        host_line = f"  - host: {ingress_host}" if ingress_host else "  - host:"

        group_annotations = ""
        if alb_group_name:
            group_annotations = (
                f"\n    alb.ingress.kubernetes.io/group.name: {alb_group_name}"
                f"\n    alb.ingress.kubernetes.io/group.order: '100'"
            )

        if not ingress_class_name:
            ingress_class_name = f"alb-public-{alb_group_name}" if alb_group_name else "alb-public"

        return f"""apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {service_name}-ingress
  namespace: {namespace}
  annotations:
    alb.ingress.kubernetes.io/scheme: {alb_schema}
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/listen-ports: '[{{"HTTP": 80}}, {{"HTTPS": 443}}]'
    alb.ingress.kubernetes.io/ssl-redirect: '443'
    alb.ingress.kubernetes.io/certificate-arn: {alb_certificate_arn}
    alb.ingress.kubernetes.io/manage-backend-security-group-rules: 'true'
    alb.ingress.kubernetes.io/backend-protocol: HTTP
    alb.ingress.kubernetes.io/healthcheck-path: {health_endpoint}
    alb.ingress.kubernetes.io/healthcheck-protocol: HTTP
    alb.ingress.kubernetes.io/healthcheck-interval-seconds: '30'
    alb.ingress.kubernetes.io/success-codes: '200'{group_annotations}
spec:
  ingressClassName: {ingress_class_name}
  rules:
{host_line}
    http:
      paths:
      - path: {service_path}
        pathType: Prefix
        backend:
          service:
            name: {service_name}-service
            port:
              number: {container_port}
"""

    def _generate_hpa(
        self,
        service_name: str,
        namespace: str,
        min_replicas: str,
        max_replicas: str,
        cpu_threshold: str,
        memory_threshold: str,
        target_kind: str = "Deployment",
        target_api_version: str = "apps/v1"
    ) -> str:
        """Generate HorizontalPodAutoscaler manifest.

        Args:
            service_name: Name of the service
            namespace: Kubernetes namespace
            min_replicas: Minimum number of replicas
            max_replicas: Maximum number of replicas
            cpu_threshold: CPU utilization threshold percentage
            memory_threshold: Memory utilization threshold percentage
            target_kind: Kind of the target resource (Deployment or Rollout)
            target_api_version: API version of the target resource
        """
        return f"""apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {service_name}-hpa
  namespace: {namespace}
spec:
  scaleTargetRef:
    apiVersion: {target_api_version}
    kind: {target_kind}
    name: {service_name}
  minReplicas: {min_replicas}
  maxReplicas: {max_replicas}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {cpu_threshold}
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: {memory_threshold}
"""
