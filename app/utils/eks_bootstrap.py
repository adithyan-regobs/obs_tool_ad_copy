"""
EKS Bootstrap YAML Builders

Pure async helpers that load YAML templates and substitute placeholders to
produce the per-tenant namespace, IngressClass+IngressClassParams, and seed
Ingress manifests committed to the infra GitHub repo and applied by the
Jenkins bootstrap pipeline. The backend process never runs kubectl directly.
"""
import os
import textwrap

import aiofiles

# Base domain for all tenant service hostnames.
DEVLIFT_BASE_DOMAIN = "devlift.ai"


def ingress_class_name_for(tenant_code: str, env: str) -> str:
    """Per-tenant IngressClass + IngressClassParams name.

    Cluster-scoped objects must not collide across tenants: if every tenant
    shared a single IngressClass, the first-applied IngressClassParams group
    wins and every subsequent tenant's ingresses land on the first tenant's
    ALB (because Auto Mode reads group from IngressClassParams, not the
    per-ingress annotation).
    """
    return f"alb-public-{tenant_code}-{env}"

_TEMPLATES_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "..", "templates", "eks"
)


async def _read_template(rel_path: str) -> str:
    template_path = os.path.join(_TEMPLATES_ROOT, rel_path)
    async with aiofiles.open(template_path, "r") as f:
        return await f.read()


def build_namespace_yaml(namespace: str) -> str:
    """
    Return a Kubernetes Namespace manifest YAML string for *namespace*.

    The resulting manifest is idempotent when applied with ``kubectl apply``.
    """
    return textwrap.dedent(f"""\
        apiVersion: v1
        kind: Namespace
        metadata:
          name: {namespace}
          labels:
            devlift.ai/managed-by: devlift
    """)


async def build_ingress_class_yaml(
    tenant_code: str,
    env: str,
    acm_cert_arn: str,
    scheme: str = "internet-facing",
) -> str:
    """
    Return combined IngressClass + IngressClassParams YAML for this tenant's ALB.

    Both objects are named ``alb-public-{tenant}-{env}`` so each tenant gets
    their own cluster-scoped pair. EKS Auto Mode ALB controller
    (``eks.amazonaws.com/alb``) reads the group from IngressClassParams, so a
    per-tenant name is the only way to guarantee each tenant's ingresses land
    on their own ALB.
    """
    ingress_class_name = ingress_class_name_for(tenant_code, env)
    group_name = f"{tenant_code}-{env}"
    group_block = f"  group:\n    name: {group_name}"
    cert_block = f"  certificateARNs:\n    - {acm_cert_arn}"

    ic_template = await _read_template("kustomize/base/ingressclass.yaml")
    icp_template = await _read_template("kustomize/base/ingressclassparams.yaml")

    ingress_class = ic_template.replace(
        "{{INGRESS_CLASS_NAME}}", ingress_class_name
    )
    ingress_class_params = (
        icp_template
        .replace("{{INGRESS_CLASS_NAME}}", ingress_class_name)
        .replace("{{ALB_SCHEME}}", scheme)
        .replace("{{ALB_GROUP_BLOCK}}", group_block)
        .replace("{{CERTIFICATE_ARNS_BLOCK}}", cert_block)
    )

    return f"{ingress_class.rstrip()}\n---\n{ingress_class_params}"


async def build_seed_ingress_yaml(
    namespace: str,
    tenant_code: str,
    env: str,
    acm_cert_arn: str,
) -> str:
    """
    Return a seed Ingress manifest YAML string that pre-provisions this tenant's ALB.

    Uses a fixed-response action so no backend pod is required. The EKS Auto
    Mode ALB controller provisions the ALB as soon as this Ingress is applied,
    using the per-tenant ``alb-public-{tenant}-{env}`` IngressClass +
    IngressClassParams created by ``build_ingress_class_yaml``. Group
    membership is taken from IngressClassParams, so every per-service Ingress
    that later references the same class name joins this tenant's ALB.

    Args:
        namespace:    Kubernetes namespace, e.g. ``aslam-ns``
        tenant_code:  Tenant subdomain, e.g. ``aslam``
        env:          Environment string, e.g. ``stage``
        acm_cert_arn: ARN of the per-tenant wildcard ACM certificate
    """
    seed_host = f"healthz.{tenant_code}-{env}.{DEVLIFT_BASE_DOMAIN}"

    template = await _read_template("bootstrap/seed-ingress.yaml")
    return (
        template
        .replace("{{NAMESPACE}}", namespace)
        .replace("{{ACM_CERT_ARN}}", acm_cert_arn)
        .replace("{{INGRESS_CLASS_NAME}}", ingress_class_name_for(tenant_code, env))
        .replace("{{SEED_HOST}}", seed_host)
    )
