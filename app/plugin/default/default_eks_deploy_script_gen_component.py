"""
Default EKS Manifest Script Generation Component

Generates individual Kubernetes manifest files inside deployment/ in the service repo.
Each file is rendered from the corresponding template in:
  templates/eks/kustomize/base/{file_type}.yaml

FileLocationItems created by DefaultFileLocator (one per resource):
  deployment/00-namespace.yaml
  deployment/01-serviceaccount.yaml
  deployment/02-deployment.yaml
  deployment/03-service.yaml
  deployment/06-ingress.yaml
  deployment/07-secret-provider-class.yaml  (when secrets are configured)

The per-tenant IngressClass + IngressClassParams (alb-public-{tenant}-{env})
are cluster-scoped and created once at tenant bootstrap
(eks_bootstrap.build_ingress_class_yaml). Per-service Ingress wires into the
tenant's ALB via ingressClassName: alb-public-{tenant}-{env}.

The file_type is read from file_location.config["file_type"].
IMAGE_TAG_PLACEHOLDER in deployment.yaml is replaced by `sed` in the pipeline before
kubectl apply -f deployment/.
"""

import json
import logging
import os
import re
from typing import Dict, Any

import aiofiles

from app.core.config import settings
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.timing import log_timing
from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    DEFAULT_ECR_REGISTRY,
    DEFAULT_CPU_REQUESTED,
    DEFAULT_CPU_LIMIT,
    DEFAULT_MEMORY_REQUESTED,
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_REPLICA_COUNT,
    DEFAULT_HEALTH_ENDPOINT,
    DEFAULT_ALB_SCHEME,
    EKS_SUBNET_IDS,
    DEVLIFT_BASE_DOMAIN,
    SHARED_ACM_CERT_ARN,
)

from app.core.aws_gpu_config import get_default_resources, get_node_selector, get_best_fit_instance, get_fallback_instances, clamp_resources, GPU_CONFIGS
from app.utils.eks_bootstrap import ingress_class_name_for

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

IMAGE_TAG_PLACEHOLDER = "IMAGE_TAG_PLACEHOLDER"

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "eks", "kustomize", "base"
)
_JOBS_TEMPLATE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "eks", "jobs"
)

_DEFAULT_PORTS = {
    "java": 8080, "java-gradle": 8080, "java-maven": 8080,
    "golang": 8080, "go": 8080,
    "python": 8000,
    "nodejs": 3000, "node": 3000,
}


def _get_default_port(language: str) -> int:
    return _DEFAULT_PORTS.get((language or "").lower(), 8080)


def _find_staged_entry(workflow_context, repo: str, base_branch: str, file_path: str):
    if not workflow_context:
        return None
    for entry in workflow_context.staged_files:
        if (
            entry.get("repo") == repo
            and entry.get("base_branch") == base_branch
            and entry.get("file_path") == file_path
        ):
            return entry
    return None


def _upsert_staged_entry(workflow_context, repo, base_branch, feature_branch, file_path, content, queue_id, script_gen_key):
    if not workflow_context:
        return
    entry = _find_staged_entry(workflow_context, repo, base_branch, file_path)
    if entry:
        entry["content"] = content
        entry["feature_branch"] = feature_branch or entry.get("feature_branch")
        entry["queue_id"] = queue_id
        entry["script_gen_key"] = script_gen_key
        return
    workflow_context.staged_files.append({
        "repo": repo,
        "base_branch": base_branch,
        "feature_branch": feature_branch,
        "file_path": file_path,
        "content": content,
        "queue_id": queue_id,
        "script_gen_key": script_gen_key,
    })


def _append_commit_message(workflow_context, repo: str, base_branch: str, message: str) -> None:
    if not workflow_context or not message:
        return
    key = f"{repo}|||{base_branch}"
    existing = workflow_context.commit_messages.get(key, "")
    workflow_context.commit_messages[key] = f"{existing}\n{message}" if existing else message


async def _fetch_plain_variables_from_db(db, transaction_code: str, environment: str) -> list[dict]:
    """Fetch VARIABLE-type entries for a service and resolve their values.

    Served by devlift-secret-config-manager's internal API — obs_tool no
    longer reads variable_mst directly (docs/variable-mst-isolation-spec.md).
    LOCAL refs are resolved server-side. Returns list of {"name": key,
    "value": value} for direct env injection (value: "..." in the YAML).
    ``db`` is unused, kept so call sites don't churn.
    """
    if not transaction_code:
        return []
    try:
        from app.core.enum import EnvironmentEnum, VariableTypeEnum
        from app.integrations.secret_config_client import SecretConfigClient
        env_enum = EnvironmentEnum(environment) if environment else None
        if not env_enum:
            return []
        items = await SecretConfigClient().get_variables(
            table_name="SERVICE_CONFIG",
            transaction_code=transaction_code,
            environment=env_enum,
            variable_type=VariableTypeEnum.VARIABLE,
        )
        return [
            {"name": i["key"], "value": i.get("value") or ""}
            for i in items if i.get("key")
        ]
    except Exception as exc:
        logger.warning("Failed to fetch plain variables from variable_mst: %s", exc)
        return []


async def _fetch_secret_keys_from_db(db, transaction_code: str, environment: str) -> list[str]:
    """Fetch SECRET-type variable key names for a service.

    Served by devlift-secret-config-manager's internal API — key names only;
    values stay in Secrets Manager and are injected by the CSI driver via
    secretKeyRef. Covers vars added via the project-variables API after the
    initial deploy (not present in config_snapshot).
    ``db`` is unused, kept so call sites don't churn.
    """
    if not transaction_code:
        return []
    try:
        from app.core.enum import EnvironmentEnum, VariableTypeEnum
        from app.integrations.secret_config_client import SecretConfigClient
        env_enum = EnvironmentEnum(environment) if environment else None
        if not env_enum:
            return []
        items = await SecretConfigClient().get_variables(
            table_name="SERVICE_CONFIG",
            transaction_code=transaction_code,
            environment=env_enum,
            variable_type=VariableTypeEnum.SECRET,
        )
        return [i["key"] for i in items if i.get("key")]
    except Exception as exc:
        logger.warning("Failed to fetch secret keys from variable_mst: %s", exc)
        return []


async def _read_template(file_type: str) -> str:
    template_path = os.path.join(_TEMPLATE_DIR, f"{file_type}.yaml")
    async with aiofiles.open(template_path, "r") as f:
        return await f.read()


def _derive_name_prefix(tenant_code: str, geo_loc_mst_code: str = "") -> str:
    """Build the k8s name prefix: {tenant}-{env}-{region_name}-{index}.

    Uses tenant code, onboarding defaults for env/index, and extracts
    the region name from geo_loc_mst_code (e.g. "region-aslam-mumbai" → "mumbai").
    """
    if not tenant_code:
        return ""
    env = settings.onboarding_default_env
    index = settings.onboarding_default_index
    region_name = ""
    if geo_loc_mst_code:
        parts = geo_loc_mst_code.rsplit("-", 1)
        if len(parts) > 1:
            region_name = parts[-1]
    if not region_name:
        region_name = settings.onboarding_default_region_code
    return f"{tenant_code}-{env}-{region_name}-{index}"


async def _fetch_infra_values(db, infrastructure_mst_code: str, tenant_code: str = "", geo_loc_mst_code: str = "") -> Dict[str, Any]:
    if not infrastructure_mst_code or not db:
        return {"name_prefix": _derive_name_prefix(tenant_code, geo_loc_mst_code)}
    from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
    infra_repo = InfrastructureMstRepository(db)
    infrastructure = await infra_repo.get_by_code(infrastructure_mst_code)
    if not infrastructure:
        logger.warning("infrastructure_mst record not found: %s", infrastructure_mst_code)
        return {"name_prefix": _derive_name_prefix(tenant_code, geo_loc_mst_code)}

    locator = infrastructure.locator or {}
    vendor_auth = (infrastructure.infra_vendor_account.auth_config or {}) if infrastructure.infra_vendor_account else {}

    return {
        "aws_region": locator.get("region") or vendor_auth.get("region", "us-east-1"),
        "account_id": vendor_auth.get("account_id", ""),
        "subnet_ids": locator.get("subnetIds") or locator.get("subnet_ids") or EKS_SUBNET_IDS,
        "subdomain": infrastructure.tenants_mst_code or "",
        "name_prefix": _derive_name_prefix(tenant_code, geo_loc_mst_code),
        "acm_cert_arn": locator.get("acm_cert_arn", ""),
        "efs_volume_handle": locator.get("efs_volume_handle", ""),
        "default_role_arn": vendor_auth.get("default_role_arn", ""),
    }


async def _render_namespace(namespace: str, environment: str, tenant_code: str = "") -> str:
    content = await _read_template("namespace")
    return (
        content
        .replace("{{NAMESPACE}}", namespace)
        .replace("{{ENVIRONMENT}}", environment)
        .replace("{{TENANT_CODE}}", tenant_code)
    )


async def _render_serviceaccount(service_name: str, namespace: str, environment: str, secrets_access_role_arn: str = "") -> str:
    content = await _read_template("serviceaccount")
    return (
        content
        .replace("{{SERVICE_NAME}}", service_name)
        .replace("{{NAMESPACE}}", namespace)
        .replace("{{ENVIRONMENT}}", environment)
        .replace("{{SECRETS_ACCESS_ROLE_ARN}}", secrets_access_role_arn)
    )


async def _render_deployment(
    service_name: str, namespace: str,
    ecr_registry: str, container_port: int,
    cpu_requested: str, cpu_limit: str,
    memory_requested: str, memory_limit: str,
    replica_count: int, health_endpoint: str,
    env_variables: list[dict] | None = None,
    tenant_code: str = "",
    environment: str = "",
) -> str:
    content = await _read_template("deployment")

    deployment_strategy = (
        "    type: RollingUpdate\n"
        "    rollingUpdate:\n"
        "      maxSurge: 1\n"
        "      maxUnavailable: 0"
    )

    # Build env vars section
    env_lines = []
    has_secrets = False
    if env_variables:
        for var in env_variables:
            name = var.get("name", "")
            is_secret = str(var.get("is_secret", "false")).lower() == "true"
            if not name:
                continue
            if is_secret:
                has_secrets = True
                env_lines.append(
                    f"            - name: {name}\n"
                    f"              valueFrom:\n"
                    f"                secretKeyRef:\n"
                    f"                  name: {service_name}-secrets\n"
                    f"                  key: {name}"
                )
            else:
                # Plain env vars — value from config_snapshot
                value = var.get("value", "")
                env_lines.append(
                    f"            - name: {name}\n"
                    f"              value: \"{value}\""
                )

    env_section = ""
    if env_lines:
        env_section = "          env:\n" + "\n".join(env_lines)

    # CSI volume/volumeMount for SecretProviderClass when secrets exist
    volume_mounts_section = ""
    volumes_section = ""
    if has_secrets:
        volume_mounts_section = (
            "          volumeMounts:\n"
            "            - name: secrets-store\n"
            "              mountPath: \"/var/secrets\"\n"
            "              readOnly: true"
        )
        volumes_section = (
            "      volumes:\n"
            "        - name: secrets-store\n"
            "          csi:\n"
            "            driver: secrets-store.csi.k8s.io\n"
            "            readOnly: true\n"
            "            volumeAttributes:\n"
            f"              secretProviderClass: {service_name}-secrets-csi"
        )

    return (
        content
        .replace("{{SERVICE_NAME}}", service_name)
        .replace("{{NAMESPACE}}", namespace)
        .replace("{{REPLICA_COUNT}}", str(replica_count))
        .replace("{{DEPLOYMENT_STRATEGY}}", deployment_strategy)
        .replace("{{ECR_REGISTRY}}", ecr_registry)
        .replace("{{ECR_REPO_NAME}}", service_name)
        .replace("{{IMAGE_TAG}}", IMAGE_TAG_PLACEHOLDER)
        .replace("{{CONTAINER_PORT}}", str(container_port))
        .replace("{{ENV_VARS}}", env_section)
        .replace("{{VOLUME_MOUNTS}}", volume_mounts_section)
        .replace("{{CPU_REQUESTED}}", cpu_requested)
        .replace("{{MEMORY_REQUESTED}}", memory_requested)
        .replace("{{CPU_LIMIT}}", cpu_limit)
        .replace("{{MEMORY_LIMIT}}", memory_limit)
        .replace("{{HEALTH_ENDPOINT}}", health_endpoint)
        .replace("{{VOLUMES}}", volumes_section)
        .replace("{{TENANT_CODE}}", tenant_code)
        .replace("{{ENVIRONMENT}}", environment)
    )


def _render_model_serving_deployment(
    service_name: str,
    namespace: str,
    model_name: str,
    vllm_image: str,
    container_port: int,
    gpu_count: str,
    cpu_requested: str,
    cpu_limit: str,
    memory_requested: str,
    memory_limit: str,
    replica_count: int,
    health_endpoint: str,
    hf_token_secret_name: str,
    instance_type: str = "",
    fallback_instance_types: list[str] | None = None,
    additional_args: str = "",
    efs_path: str = "",
) -> str:
    """Render a K8s Deployment YAML for vLLM model serving."""
    node_selector_block = ""
    affinity_block = ""
    if instance_type:
        all_instance_types = fallback_instance_types if fallback_instance_types else [instance_type]
        values_yaml = "\n".join(f"                    - {t}" for t in all_instance_types)

        preference_entries = []
        if fallback_instance_types and len(fallback_instance_types) > 1:
            max_weight = 100
            for idx, inst_type in enumerate(fallback_instance_types):
                weight = max(1, max_weight - (idx * (max_weight // len(fallback_instance_types))))
                preference_entries.append(
                    f"""          - weight: {weight}
            preference:
              matchExpressions:
                - key: node.kubernetes.io/instance-type
                  operator: In
                  values:
                    - {inst_type}"""
                )

        preferences_block = ""
        if preference_entries:
            preferences_yaml = "\n".join(preference_entries)
            preferences_block = f"""
          preferredDuringSchedulingIgnoredDuringExecution:
{preferences_yaml}"""

        affinity_block = f"""      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: node.kubernetes.io/instance-type
                    operator: In
                    values:
{values_yaml}{preferences_block}
"""

    extra_args_block = ""
    if additional_args and additional_args.strip():
        tokens = additional_args.strip().split()
        extra_lines = "\n".join(f'            - "{t}"' for t in tokens)
        extra_args_block = f"\n{extra_lines}"

    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {service_name}
  namespace: {namespace}
  labels:
    app: {service_name}
    app.kubernetes.io/component: model-serving
spec:
  replicas: {replica_count}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app: {service_name}
  template:
    metadata:
      labels:
        app: {service_name}
        app.kubernetes.io/component: model-serving
    spec:
      serviceAccountName: {service_name}-sa
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
        - key: CriticalAddonsOnly
          operator: Exists
          effect: NoSchedule
{node_selector_block}{affinity_block}      containers:
        - name: {service_name}
          image: {vllm_image}
          args:
            - "--model"
            - "{f"/models/{efs_path}" if efs_path else model_name}"
            - "--port"
            - "{container_port}"
            - "--gpu-memory-utilization"
            - "0.9"
            - "--allowed-origins"
            - '["*"]'{f'''
            - "--served-model-name"
            - "{model_name}"''' if model_name and efs_path else ""}{extra_args_block}
          ports:
            - name: http
              containerPort: {container_port}
          env:
            - name: HF_HOME
              value: "/models/.cache"
          volumeMounts:
            - name: secrets-store
              mountPath: "/var/secrets"
              readOnly: true
            - name: model-store
              mountPath: /models
              readOnly: true
          resources:
            requests:
              cpu: "{cpu_requested}"
              memory: "{memory_requested}"
              nvidia.com/gpu: "{gpu_count}"
            limits:
              cpu: "{cpu_limit}"
              memory: "{memory_limit}"
              nvidia.com/gpu: "{gpu_count}"
          startupProbe:
            httpGet:
              path: {health_endpoint}
              port: {container_port}
            initialDelaySeconds: 60
            periodSeconds: 30
            failureThreshold: 40
            timeoutSeconds: 5
          livenessProbe:
            httpGet:
              path: {health_endpoint}
              port: {container_port}
            initialDelaySeconds: 10
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: {health_endpoint}
              port: {container_port}
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
      volumes:
        - name: secrets-store
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: {service_name}-secrets-csi
        - name: model-store
          persistentVolumeClaim:
            claimName: model-store-pvc
"""


async def _render_secret_provider_class(
    service_name: str,
    namespace: str,
    aws_region: str,
    secrets_manager_path: str,
    secret_keys: list[str],
) -> str | None:
    """
    Generate SecretProviderClass manifest for AWS Secrets Manager CSI driver.

    The CSI driver pulls secrets from AWS Secrets Manager at pod startup and
    syncs them into a K8s Secret named {service_name}-secrets, which the
    deployment references via secretKeyRef.

    No plaintext secret values are embedded in this manifest or in the Jenkinsfile.
    """
    if not secret_keys:
        return None

    content = await _read_template("secret-provider-class")

    # Build jmesPath items — each key in the consolidated JSON secret
    jmes_items = []
    for key in secret_keys:
        jmes_items.append(f"          - path: {key}")
        jmes_items.append(f"            objectAlias: {key}")

    # Build secretObjects — syncs AWS secret keys into a K8s Secret
    secret_obj_lines = [
        f"    - secretName: {service_name}-secrets",
        "      type: Opaque",
        "      data:",
    ]
    for key in secret_keys:
        secret_obj_lines.append(f"        - key: {key}")
        secret_obj_lines.append(f"          objectName: {key}")

    return (
        content
        .replace("{{SERVICE_NAME}}", service_name)
        .replace("{{NAMESPACE}}", namespace)
        .replace("{{AWS_REGION}}", aws_region)
        .replace("{{SECRETS_MANAGER_PATH}}", secrets_manager_path)
        .replace("{{SECRET_JMES_PATH_ITEMS}}", "\n".join(jmes_items))
        .replace("{{SECRET_OBJECTS}}", "\n".join(secret_obj_lines))
    )


async def _render_service(service_name: str, namespace: str, container_port: int) -> str:
    content = await _read_template("service")
    return (
        content
        .replace("{{SERVICE_NAME}}", service_name)
        .replace("{{NAMESPACE}}", namespace)
        .replace("{{CONTAINER_PORT}}", str(container_port))
    )


async def _render_ingress(
    service_name: str, namespace: str, container_port: int,
    health_endpoint: str, ingress_class_name: str,
    service_path: str = None,
    alb_group_name: str = "",
    ingress_hostname: str = "",
) -> str:
    content = await _read_template("ingress")
    if service_path:
        # Strip trailing wildcard — pathType: Prefix already matches sub-paths,
        # and ALB ingress controller rejects wildcards like "/*".
        clean_path = service_path.rstrip("*").rstrip("/") or "/"
        content = content.replace("path: /", f"path: {clean_path}")

    if alb_group_name:
        alb_group_annotations = (
            f"    alb.ingress.kubernetes.io/group.name: {alb_group_name}\n"
            f"    alb.ingress.kubernetes.io/group.order: '100'"
        )
        certificate_annotations = (
            f"    alb.ingress.kubernetes.io/certificate-arn: {SHARED_ACM_CERT_ARN}\n"
            f"    alb.ingress.kubernetes.io/listen-ports: '[{{\"HTTP\":80}},{{\"HTTPS\":443}}]'\n"
            f"    alb.ingress.kubernetes.io/ssl-redirect: '443'"
        )
    else:
        alb_group_annotations = ""
        certificate_annotations = ""

    ingress_host_rule = (
        f"  - host: {ingress_hostname}\n    http:" if ingress_hostname else "  - http:"
    )

    return (
        content
        .replace("{{SERVICE_NAME}}", service_name)
        .replace("{{NAMESPACE}}", namespace)
        .replace("{{CONTAINER_PORT}}", str(container_port))
        .replace("{{HEALTH_ENDPOINT}}", health_endpoint)
        .replace("{{INGRESS_CLASS_NAME}}", ingress_class_name)
        .replace("{{CERTIFICATE_ANNOTATIONS}}", certificate_annotations)
        .replace("{{ALB_GROUP_ANNOTATIONS}}", alb_group_annotations)
        .replace("{{INGRESS_HOST_RULE}}", ingress_host_rule)
    )


def _build_inference_service_affinity(instance_types: list[str]) -> dict:
    """Build K8s affinity for InferenceService CRD.

    requiredDuringScheduling: all valid instance types (hard constraint).
    preferredDuringScheduling: weighted ordering, cheapest first.
    """
    if not instance_types:
        return {}

    affinity: dict = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{
                    "matchExpressions": [{
                        "key": "node.kubernetes.io/instance-type",
                        "operator": "In",
                        "values": instance_types,
                    }]
                }]
            }
        }
    }

    if len(instance_types) > 1:
        max_weight = 100
        preferences = []
        for idx, inst_type in enumerate(instance_types):
            weight = max(1, max_weight - (idx * (max_weight // len(instance_types))))
            preferences.append({
                "weight": weight,
                "preference": {
                    "matchExpressions": [{
                        "key": "node.kubernetes.io/instance-type",
                        "operator": "In",
                        "values": [inst_type],
                    }]
                },
            })
        affinity["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"] = preferences

    return affinity


class DefaultEksDeployScriptGenComponent:
    """
    Renders one Kubernetes manifest from templates/eks/kustomize/base/ per call.
    The file_type in file_location.config determines which template is used.

    Supported file_types: namespace, serviceaccount, deployment, service,
                          ingressclass, ingressclassparams, ingress
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = True,
        db=None,
    ) -> str:
        config_snapshot = queue_dict.get("config_snapshot") or {}

        service_name = config_snapshot.get("service_name", "")
        if service_name.lower().endswith("-service"):
            service_name = service_name[:-8]
        if not service_name:
            raise ValueError("service_name is required for default EKS manifest generation")

        file_type = (file_location.config or {}).get("file_type", "")
        environment = config_snapshot.get("environment") or queue_dict.get("environment") or "stage"
        identifier = config_snapshot.get("identifier") or service_name
        language = config_snapshot.get("language") or config_snapshot.get("language_name") or "java"

        # ── Fetch infrastructure values from DB ──────────────────────────────
        infrastructure_mst_code = config_snapshot.get("infrastructure_mst_code", "")
        geo_loc_mst_code = config_snapshot.get("geo_loc_mst_code", "")
        infra_values = {}
        if db and infrastructure_mst_code:
            try:
                infra_values = await _fetch_infra_values(db, infrastructure_mst_code, tenant, geo_loc_mst_code)
            except Exception as exc:
                self.logger.warning("Failed to fetch infra values from DB: %s", exc)
        if not infra_values.get("name_prefix"):
            infra_values["name_prefix"] = _derive_name_prefix(tenant, geo_loc_mst_code)

        aws_region = config_snapshot.get("aws_region") or infra_values.get("aws_region", "us-east-1")
        account_id = config_snapshot.get("aws_account_id") or infra_values.get("account_id", "")
        subnet_ids = config_snapshot.get("subnet_ids") or infra_values.get("subnet_ids") or EKS_SUBNET_IDS
        # Per-tenant default runtime role (populated at signup). Fall back to the
        # shared PlatformAccess role for tenants provisioned before that change.
        secrets_access_role_arn = (
            infra_values.get("default_role_arn")
            or (f"arn:aws:iam::{account_id}:role/Devlift-PlatformAccess" if account_id else "")
        )

        if account_id:
            ecr_registry = config_snapshot.get("ecr_registry") or f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com"
        else:
            ecr_registry = config_snapshot.get("ecr_registry") or DEFAULT_ECR_REGISTRY

        container_port = int(
            config_snapshot.get("port") or config_snapshot.get("container_port") or _get_default_port(language)
        )
        health_endpoint = (
            config_snapshot.get("health")
            or config_snapshot.get("health_endpoint")
            or config_snapshot.get("health_check_path")
            or DEFAULT_HEALTH_ENDPOINT
        )
        # Derive K8s resource name.
        # MODEL_SERVING uses a short name ({tenant}-{service}) so the KServe URL
        # stays short ({tenant}-{service}-predictor.models.devlift.ai).
        # Regular services keep the long infra naming convention for ALB compat.
        # Lowercase + strip invalid chars for RFC 1123 compliance.
        safe_name = re.sub(r"[^a-z0-9-]", "-", service_name.lower()).strip("-")
        is_model_serving_for_name = config_snapshot.get("service_type") == "MODEL_SERVING"
        if is_model_serving_for_name:
            k8s_name = f"{tenant}-{safe_name}"
        else:
            name_prefix = infra_values.get("name_prefix", "")
            if name_prefix:
                k8s_name = f"{name_prefix}-{safe_name}"
            else:
                k8s_name = safe_name
        namespace = f"{tenant}-ns"
        replica_count = int(config_snapshot.get("replica_count") or DEFAULT_REPLICA_COUNT)
        cpu_requested = config_snapshot.get("cpu_requested") or DEFAULT_CPU_REQUESTED
        cpu_limit = config_snapshot.get("cpu_limit") or DEFAULT_CPU_LIMIT
        memory_requested = config_snapshot.get("memory_requested") or DEFAULT_MEMORY_REQUESTED
        memory_limit = config_snapshot.get("memory_limit") or DEFAULT_MEMORY_LIMIT
        # Safety-net normalization: ensure CPU has 'm' suffix, memory has 'Mi'/'Gi' suffix
        if not str(cpu_requested).endswith("m"):
            cpu_requested = f"{cpu_requested}m"
        if not str(cpu_limit).endswith("m"):
            cpu_limit = f"{cpu_limit}m"
        if not str(memory_requested).endswith(("Mi", "Gi")):
            memory_requested = f"{memory_requested}Mi"
        if not str(memory_limit).endswith(("Mi", "Gi")):
            memory_limit = f"{memory_limit}Mi"
        alb_scheme = config_snapshot.get("alb_scheme") or DEFAULT_ALB_SCHEME
        service_path = config_snapshot.get("service_path") or config_snapshot.get("path_pattern") or None
        ingress_class_name = (
            config_snapshot.get("ingress_class_name")
            or ingress_class_name_for(tenant, environment)
        )
        acm_cert_arn = infra_values.get("acm_cert_arn") or settings.acm_wildcard_cert_arn
        # Shared ALB group — used by both IngressClassParams (EKS Auto Mode native) and
        # the Ingress annotation (no-op for Auto Mode, kept for LBC compatibility).
        derived_group_name = (
            config_snapshot.get("alb_group_name")
            or f"{tenant}-{environment}"
        )
        derived_hostname = (
            config_snapshot.get("ingress_host")
            or f"{safe_name}-{tenant}-{environment}.apps.{DEVLIFT_BASE_DOMAIN}"
        )

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # ── Check staged cache ───────────────────────────────────────────────
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context, file_location.repo, base_branch, file_location.file_path
            )
        if cached_entry:
            existing_file = {"exists": True, "content": cached_entry.get("content")}
        else:
            fetch_context = f"repo={repo} branch={feature_branch} path={file_location.file_path}"
            with log_timing(logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db, tenant=tenant, owner=owner, repo=repo,
                    file_path=file_location.file_path, branch=feature_branch,
                )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        # ── Render the correct template ──────────────────────────────────────
        gen_context = f"path={file_location.file_path} type={file_type} service={service_name}"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            if file_type == "namespace":
                content = await _render_namespace(namespace, environment, tenant_code=tenant)
            elif file_type == "serviceaccount":
                content = await _render_serviceaccount(
                    k8s_name, namespace, environment,
                    secrets_access_role_arn=secrets_access_role_arn,
                )
            elif file_type == "deployment":
                is_model_serving = config_snapshot.get("service_type") == "MODEL_SERVING"
                if is_model_serving:
                    gpu_type = config_snapshot.get("gpu_type", "L4")
                    gpu_count_int = int(config_snapshot.get("gpu_count", 1))
                    if gpu_type not in GPU_CONFIGS:
                        gpu_type = "L4"
                    defaults = get_default_resources(gpu_type, gpu_count_int)
                    raw_cpu = int(config_snapshot.get("cpu_requested") or config_snapshot.get("cpu") or defaults["cpu_requested"])
                    raw_mem_str = config_snapshot.get("memory_requested") or config_snapshot.get("ram") or defaults["memory_requested"]
                    raw_mem = int(str(raw_mem_str).replace("Gi", "").replace("G", "").strip() or defaults["memory_requested"].replace("Gi", ""))
                    clamped_cpu, clamped_mem, actual_gpu_count = clamp_resources(gpu_type, gpu_count_int, raw_cpu, raw_mem)
                    best_fit = get_best_fit_instance(gpu_type, actual_gpu_count, clamped_cpu, clamped_mem)
                    fallback_list = [i.name for i in get_fallback_instances(gpu_type, actual_gpu_count, clamped_cpu, clamped_mem)]
                    content = _render_model_serving_deployment(
                        service_name=k8s_name,
                        namespace=namespace,
                        model_name=config_snapshot.get("model_name", ""),
                        vllm_image=config_snapshot.get("vllm_image", "vllm/vllm-openai:latest"),
                        container_port=container_port,
                        gpu_count=str(actual_gpu_count),
                        cpu_requested=str(clamped_cpu),
                        cpu_limit=str(clamped_cpu),
                        memory_requested=f"{clamped_mem}Gi",
                        memory_limit=f"{clamped_mem}Gi",
                        replica_count=replica_count,
                        health_endpoint=health_endpoint,
                        hf_token_secret_name=f"{k8s_name}-hf-token",
                        instance_type=best_fit.name,
                        fallback_instance_types=fallback_list,
                        additional_args=config_snapshot.get("additional_args", "") or "",
                        efs_path=config_snapshot.get("efs_path", ""),
                    )
                else:
                    # Fetch secret key names from variable_mst — covers both:
                    # Case 1: vars added at deploy time (stored in variable_mst via queue flow)
                    # Case 2: vars added post-deploy via project-variables API
                    # Values are never stored here — CSI driver injects them from Secrets Manager.
                    transaction_code = queue_dict.get("transaction_code", "")
                    db_secret_keys = await _fetch_secret_keys_from_db(db, transaction_code, environment)
                    db_plain_vars = await _fetch_plain_variables_from_db(db, transaction_code, environment)
                    env_variables = [{"name": k, "is_secret": "true"} for k in db_secret_keys]
                    env_variables += [{"name": v["name"], "value": v["value"], "is_secret": "false"} for v in db_plain_vars]
                    content = await _render_deployment(
                        service_name=k8s_name,
                        namespace=namespace,
                        ecr_registry=ecr_registry,
                        container_port=container_port,
                        cpu_requested=cpu_requested,
                        cpu_limit=cpu_limit,
                        memory_requested=memory_requested,
                        memory_limit=memory_limit,
                        replica_count=replica_count,
                        health_endpoint=health_endpoint,
                        env_variables=env_variables,
                        tenant_code=tenant,
                        environment=environment,
                    )
            elif file_type == "service":
                content = await _render_service(k8s_name, namespace, container_port)
            elif file_type == "ingress":
                content = await _render_ingress(
                    service_name=k8s_name,
                    namespace=namespace,
                    container_port=container_port,
                    health_endpoint=health_endpoint,
                    ingress_class_name=ingress_class_name,
                    service_path=service_path,
                    alb_group_name=derived_group_name,
                    ingress_hostname=derived_hostname,
                )
            elif file_type == "secret-provider-class":
                # Determine secrets_manager_path and secret_keys from config.
                # Model serving: HF token path from hf_token_secret_path
                # Regular services: path from env_variables project variable metadata
                is_model_serving = config_snapshot.get("service_type") == "MODEL_SERVING"
                secrets_manager_path = ""
                secret_keys: list[str] = []

                if is_model_serving:
                    secrets_manager_path = config_snapshot.get("hf_token_secret_path", "")
                    if secrets_manager_path:
                        secret_keys = ["HF_TOKEN"]
                else:
                    # Regular services: fetch secret path + keys from variable_mst.
                    # This covers both initial deploy and post-deploy API-added vars.
                    transaction_code = queue_dict.get("transaction_code", "")
                    secret_keys = await _fetch_secret_keys_from_db(db, transaction_code, environment)
                    # Fallback path must match ProjectVariablesService._build_secret_path:
                    #   {tenant}/{app_name}/{env}/{resource_name}/{resource_name}
                    # Priority: config_snapshot.product_name → DB lookup → tenant as last resort.
                    app_name = config_snapshot.get("product_name") or ""
                    if not app_name and db and config_snapshot.get("applications_mst_code"):
                        try:
                            from app.repository.applications_mst_repository import ApplicationsMstRepository
                            app_repo = ApplicationsMstRepository(db)
                            app_record = await app_repo.get_by_code(config_snapshot["applications_mst_code"])
                            app_name = app_record.name if app_record else ""
                        except Exception:
                            pass
                    if not app_name:
                        app_name = tenant
                    secrets_manager_path = (
                        config_snapshot.get("secrets_manager_path")
                        or f"{tenant}/{app_name}/{environment}/{service_name}/{service_name}"
                    )

                if secrets_manager_path and secret_keys:
                    content = await _render_secret_provider_class(
                        service_name=k8s_name,
                        namespace=namespace,
                        aws_region=aws_region,
                        secrets_manager_path=secrets_manager_path,
                        secret_keys=secret_keys,
                    )
                else:
                    # No secrets configured — emit empty content (file won't be committed)
                    content = ""
            elif file_type == "inference-service":
                import yaml as pyyaml
                gpu_type = config_snapshot.get("gpu_type", "L4")
                gpu_count_int = int(config_snapshot.get("gpu_count", 1))
                if gpu_type not in GPU_CONFIGS:
                    gpu_type = "L4"
                defaults = get_default_resources(gpu_type, gpu_count_int)

                def _parse_cpu_cores(v, default):
                    if v is None or v == "":
                        return int(default)
                    s = str(v).strip()
                    # Tolerate K8s millicore quantity strings ("500m") from older rows
                    if s.endswith("m"):
                        return max(1, round(int(s[:-1]) / 1000))
                    return int(s)

                raw_cpu = _parse_cpu_cores(
                    config_snapshot.get("cpu_requested") or config_snapshot.get("cpu"),
                    defaults["cpu_requested"],
                )
                raw_mem_str = config_snapshot.get("memory_requested") or config_snapshot.get("ram") or defaults["memory_requested"]
                raw_mem = int(str(raw_mem_str).replace("Gi", "").replace("G", "").strip() or defaults["memory_requested"].replace("Gi", ""))
                clamped_cpu, clamped_mem, actual_gpu_count = clamp_resources(gpu_type, gpu_count_int, raw_cpu, raw_mem)
                fallback_list = [i.name for i in get_fallback_instances(gpu_type, actual_gpu_count, clamped_cpu, clamped_mem)]

                # Autoscaling (Phase 6 merged into Phase 2)
                min_replicas_val = int(config_snapshot.get("min_replicas", 0))
                max_replicas_val = int(config_snapshot.get("max_replicas", 3))
                scale_target_val = int(config_snapshot.get("scale_target", 1))

                # vLLM args (runtime provides command only, all args come from here)
                model_name = config_snapshot.get("model_name", "")
                additional_args_str = (config_snapshot.get("additional_args", "") or "").strip()

                # If the user typed a flag manually in additional_args, their flag wins —
                # skip the config-based equivalent to avoid duplicate / conflicting flags.
                def _user_has(flag: str) -> bool:
                    return flag in additional_args_str

                # Tensor / pipeline parallelism — default: TP=gpu_count, PP=1
                tp = int(config_snapshot.get("tensor_parallel_size") or actual_gpu_count)
                pp = int(config_snapshot.get("pipeline_parallel_size") or 1)
                # Safety: ensure TP * PP == actual_gpu_count; fall back to TP=gpu_count, PP=1
                if tp * pp != actual_gpu_count:
                    tp = actual_gpu_count
                    pp = 1

                isvc_args = [
                    "--model", "/mnt/models",
                    "--port", "8000",
                    "--gpu-memory-utilization", "0.9",
                    "--allowed-origins", '["*"]',
                ]
                if not _user_has("--tensor-parallel-size"):
                    isvc_args += ["--tensor-parallel-size", str(tp)]
                if pp > 1 and not _user_has("--pipeline-parallel-size"):
                    isvc_args += ["--pipeline-parallel-size", str(pp)]

                # Register the HuggingFace model name so vLLM accepts it
                # in API requests instead of the raw EFS path "/mnt/models".
                if model_name and not _user_has("--served-model-name"):
                    isvc_args += ["--served-model-name", model_name]

                # Quantization — skip if user already passed --quantization
                if not _user_has("--quantization"):
                    quantization = (config_snapshot.get("quantization") or "").strip().lower()
                    if quantization and quantization != "none":
                        isvc_args += ["--quantization", quantization]

                if config_snapshot.get("trust_remote_code") and not _user_has("--trust-remote-code"):
                    isvc_args += ["--trust-remote-code"]

                # User's additional args appended last (highest priority)
                if additional_args_str:
                    isvc_args += additional_args_str.split()

                # Default max-model-len to prevent OOM from large context windows.
                # Use the explicit value from config if set, else cap at 8192.
                explicit_max_model_len = config_snapshot.get("max_model_len")
                if not _user_has("--max-model-len"):
                    isvc_args += ["--max-model-len", str(int(explicit_max_model_len)) if explicit_max_model_len else "8192"]

                # Node affinity (lesson: never use nodeSelector for GPU)
                affinity = _build_inference_service_affinity(fallback_list)

                manifest = {
                    "apiVersion": "serving.kserve.io/v1beta1",
                    "kind": "InferenceService",
                    "metadata": {
                        "name": k8s_name,
                        "namespace": namespace,
                        "annotations": {
                            "serving.kserve.io/deploymentMode": "Serverless",
                        },
                    },
                    "spec": {
                        "predictor": {
                            "serviceAccountName": f"{k8s_name}-sa",
                            "minReplicas": min_replicas_val,
                            "maxReplicas": max_replicas_val,
                            "scaleTarget": scale_target_val,
                            "scaleMetric": "concurrency",
                            "tolerations": [
                                {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                                {"key": "CriticalAddonsOnly", "operator": "Exists", "effect": "NoSchedule"},
                            ],
                            "affinity": affinity,
                            "model": {
                                "modelFormat": {"name": "pytorch"},
                                "runtime": "vllm-runtime",
                                "storageUri": f"pvc://model-store-pvc/{config_snapshot.get('efs_path', '')}",
                                "args": isvc_args,
                                "resources": {
                                    "limits": {
                                        "nvidia.com/gpu": str(actual_gpu_count),
                                        "memory": f"{clamped_mem}Gi",
                                        "cpu": str(clamped_cpu),
                                    },
                                    "requests": {
                                        "nvidia.com/gpu": str(actual_gpu_count),
                                        "memory": f"{clamped_mem}Gi",
                                        "cpu": str(clamped_cpu),
                                    },
                                },
                            },
                        },
                    },
                }
                content = pyyaml.dump(manifest, default_flow_style=False)
            elif file_type == "efs-pvc":
                content = await _read_template("efs-pvc")
            elif file_type == "efs-pvc-tenant":
                efs_vol_handle = infra_values.get("efs_volume_handle", "")
                if efs_vol_handle:
                    content = await _read_template("efs-pvc-tenant")
                    content = (
                        content
                        .replace("{{NAMESPACE}}", namespace)
                        .replace("{{EFS_VOLUME_HANDLE}}", efs_vol_handle)
                    )
                else:
                    content = ""
            elif file_type == "model-download-sa":
                # ServiceAccount in model-download-ns with IRSA so the
                # download job pod can access Secrets Manager via CSI.
                content = await _render_serviceaccount(
                    service_name="model-downloader",
                    namespace="model-download-ns",
                    environment=environment,
                    secrets_access_role_arn=secrets_access_role_arn,
                )
            elif file_type == "model-download-spc":
                # SecretProviderClass in model-download-ns so the download job
                # can access HF_TOKEN via CSI secrets store.
                hf_secret_path = config_snapshot.get("hf_token_secret_path", "")
                if hf_secret_path:
                    content = await _render_secret_provider_class(
                        service_name="model-download",
                        namespace="model-download-ns",
                        aws_region=aws_region,
                        secrets_manager_path=hf_secret_path,
                        secret_keys=["HF_TOKEN"],
                    )
                else:
                    content = ""
            elif file_type == "model-download-pvc":
                efs_vol_handle = infra_values.get("efs_volume_handle", "")
                if efs_vol_handle:
                    content = await _read_template("efs-pvc-tenant")
                    content = (
                        content
                        .replace("{{NAMESPACE}}", "model-download-ns")
                        .replace("{{EFS_VOLUME_HANDLE}}", efs_vol_handle)
                    )
                else:
                    content = ""
            elif file_type == "model-download-job":
                job_template_path = os.path.join(_JOBS_TEMPLATE_DIR, "model-download-job.yaml")
                async with aiofiles.open(job_template_path, "r") as f:
                    content = await f.read()
                model_id = config_snapshot.get("model_name", "")
                revision = config_snapshot.get("model_revision", "main")
                efs_path = config_snapshot.get("efs_path", "")
                job_name = config_snapshot.get("download_job_name", "")
                # Fallback: derive job name if not pre-populated (e.g. old config_snapshot)
                if not job_name and model_id:
                    import re as _re
                    _slug = _re.sub(r"[^a-z0-9-]", "-", model_id.lower().replace("/", "--")).strip("-")
                    _short = revision[:12] if revision else "main"
                    job_name = f"dl-{_slug}-{_short}"
                    if len(job_name) > 63:
                        import hashlib
                        _h = hashlib.md5(job_name.encode()).hexdigest()[:6]
                        job_name = f"dl-{_slug[:40].rstrip('-')}-{_short}-{_h}"
                slug = model_id.replace("/", "--")
                short_sha = revision[:12] if revision else ""
                content = (
                    content
                    .replace("{{JOB_NAME}}", job_name)
                    .replace("{{NAMESPACE}}", "model-download-ns")
                    .replace("{{MODEL_SLUG}}", slug)
                    .replace("{{SHORT_SHA}}", short_sha)
                    .replace("{{MODEL_ID}}", model_id)
                    .replace("{{REVISION}}", revision)
                    .replace("{{EFS_PATH}}", efs_path)
                    .replace("{{HF_TOKEN_SECRET_NAME}}", "model-download-secrets")
                    .replace("{{SECRET_PROVIDER_CLASS}}", "model-download-secrets-csi")
                )
            else:
                raise ValueError(f"Unknown file_type for EKS manifest generation: '{file_type}'")

        # ── Upload to S3 ─────────────────────────────────────────────────────
        if upload_to_s3:
            try:
                original_s3_key = f"default/eks/deployment/{identifier}-{file_type}.yaml"
                await FileManagerHandler.upload_file(
                    key=original_s3_key, content=content, content_type="text/plain",
                )
                repo_for_db = repository or self.repository
                if repo_for_db and queue_dict.get("code"):
                    await repo_for_db.update_artifact_s3_key(
                        queue_dict["code"],
                        json.dumps({"original_s3_key": original_s3_key}),
                    )
            except Exception as exc:
                logger.error("Failed to upload manifest to S3: %s", exc, exc_info=True)
                raise

        # ── Stage for git commit ─────────────────────────────────────────────
        if tenant and file_location and workflow_context and content:
            if not workflow_context.skip_commit:
                skip_commit = False
                existing_content = existing_file.get("content")
                if existing_file.get("exists") and existing_content:
                    skip_commit = should_skip_commit(existing_content, content)
                    if skip_commit:
                        logger.info(
                            "Manifest %s unchanged on %s, skipping commit",
                            file_location.file_path, feature_branch,
                        )
                if not skip_commit:
                    _upsert_staged_entry(
                        workflow_context=workflow_context,
                        repo=file_location.repo,
                        base_branch=base_branch,
                        feature_branch=feature_branch,
                        file_path=file_location.file_path,
                        content=content,
                        queue_id=queue_dict.get("id"),
                        script_gen_key=file_location.script_gen_key,
                    )
                    queue_label = queue_dict.get("code") or queue_dict.get("id")
                    commit_line = (
                        f"{queue_label}: {file_location.script_gen_key} -> {file_location.file_path}"
                        if queue_label
                        else f"{file_location.script_gen_key} -> {file_location.file_path}"
                    )
                    _append_commit_message(workflow_context, file_location.repo, base_branch, commit_line)

            if queue_dict.get("id"):
                workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                    "original_content": content,
                    "preview_content": content,
                }

        return content
