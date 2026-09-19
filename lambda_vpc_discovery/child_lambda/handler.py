"""
Child Lambda — EKS Workload Discovery
======================================
Deployed inside VPCs that contain EKS clusters.
Called by the parent VPC Discovery Lambda via lambda.invoke().

Architecture:
  - Parent Lambda (outside VPC, has internet) calls eks.describe_cluster()
    and passes endpoint + CA cert to this child Lambda.
  - This child Lambda (inside VPC, NO internet) generates a Bearer token
    locally and calls the K8s private API endpoint.

Network requirements:
  - NO internet access needed
  - NO NAT gateway needed
  - NO VPC endpoints needed
  - Only requires: K8s private API endpoint reachable inside the same VPC
    (endpointPrivateAccess must be true on the EKS cluster)

Invocation (from parent):
  lambda.invoke(FunctionName="eks-discovery-vpc-xxx", Payload={
      "cluster_name": "my-cluster",
      "endpoint":     "https://XXXX.gr7.ap-south-1.eks.amazonaws.com",
      "ca_data":      "<base64-CA-cert>",
      "region":       "ap-south-1"
  })
"""
import json
import ssl
import base64
import logging
import urllib.request
import urllib.error

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ──────────────────────────────────────────────────────────────
# Token Generation — purely local, zero network calls
# ──────────────────────────────────────────────────────────────

def generate_eks_token(cluster_name: str, region: str) -> str:
    """
    Generate EKS Bearer token using Lambda's own IAM execution role credentials.

    Mechanism:
      - Same as `aws eks get-token` CLI command.
      - Creates a STS presigned URL with cluster header, then base64-encodes it.
      - Credentials come from Lambda runtime environment variables
        (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN).
      - All computation is local — no network call is made here.
      - Token is valid for 15 minutes.

    When the K8s API server receives this token, it calls STS itself
    to verify the identity and maps the IAM role via aws-auth ConfigMap
    or EKS Access Entries.
    """
    session = boto3.Session()
    credentials = session.get_credentials().get_frozen_credentials()

    url = (
        f"https://sts.{region}.amazonaws.com/"
        f"?Action=GetCallerIdentity&Version=2011-06-15"
    )
    request = AWSRequest(
        method="GET",
        url=url,
        headers={"x-k8s-aws-id": cluster_name}
    )

    # Sign the request using Lambda's IAM credentials (local computation)
    # SigV4QueryAuth puts signature in query params (presigned URL format)
    # SigV4Auth would put it in headers — which doesn't work for EKS tokens
    SigV4QueryAuth(credentials, "sts", region, expires=60).add_auth(request)

    # URL now contains signature in query params — proper presigned URL
    presigned_url = request.url
    token = "k8s-aws-v1." + base64.urlsafe_b64encode(
        presigned_url.encode()
    ).rstrip(b"=").decode()

    return token


# ──────────────────────────────────────────────────────────────
# SSL Context from Cluster CA
# ──────────────────────────────────────────────────────────────

def build_ssl_context(ca_data: str) -> ssl.SSLContext:
    """
    Build SSL context from base64-encoded cluster CA certificate.
    Writes CA cert to /tmp (Lambda's writable directory).
    """
    ca_bytes = base64.b64decode(ca_data)
    ca_file = "/tmp/eks_ca.crt"
    with open(ca_file, "wb") as f:
        f.write(ca_bytes)
    return ssl.create_default_context(cafile=ca_file)


# ──────────────────────────────────────────────────────────────
# K8s API — paginated GET (private endpoint, inside VPC)
# ──────────────────────────────────────────────────────────────

def k8s_list(endpoint: str, path: str, token: str, ssl_ctx: ssl.SSLContext) -> list:
    """
    Call K8s API LIST endpoint with pagination.
    All traffic stays inside the VPC via the private K8s endpoint.
    """
    items = []
    continue_token = None

    while True:
        url = f"{endpoint}{path}?limit=500"
        if continue_token:
            url += f"&continue={continue_token}"

        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"}
        )

        try:
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as resp:
                data = json.loads(resp.read())
                items.extend(data.get("items", []))
                continue_token = data.get("metadata", {}).get("continue")
                if not continue_token:
                    break

        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            logger.error(f"K8s API HTTP {e.code} on {path}: {e.reason} — {body}")
            break
        except Exception as e:
            logger.error(f"K8s API call failed on {path}: {e}")
            break

    return items


# ──────────────────────────────────────────────────────────────
# Extractors — keep only fields needed for observability
# ──────────────────────────────────────────────────────────────

def extract_pod(pod: dict) -> dict:
    metadata = pod.get("metadata", {})
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "phase": status.get("phase"),
        "nodeName": spec.get("nodeName"),
        "labels": metadata.get("labels", {}),
        "containers": [
            {
                "name": c.get("name"),
                "image": c.get("image"),
                "resources": c.get("resources", {}),
            }
            for c in spec.get("containers", [])
        ],
        "conditions": [
            {"type": c.get("type"), "status": c.get("status")}
            for c in status.get("conditions", [])
        ],
    }


def extract_deployment(dep: dict) -> dict:
    metadata = dep.get("metadata", {})
    spec = dep.get("spec", {})
    status = dep.get("status", {})
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "labels": metadata.get("labels", {}),
        "replicas": spec.get("replicas", 0),
        "readyReplicas": status.get("readyReplicas", 0),
        "availableReplicas": status.get("availableReplicas", 0),
        "containers": [
            {
                "name": c.get("name"),
                "image": c.get("image"),
                "resources": c.get("resources", {}),
            }
            for c in spec.get("template", {}).get("spec", {}).get("containers", [])
        ],
    }


def extract_service(svc: dict) -> dict:
    metadata = svc.get("metadata", {})
    spec = svc.get("spec", {})
    status = svc.get("status", {})
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "type": spec.get("type"),
        "clusterIP": spec.get("clusterIP"),
        "ports": spec.get("ports", []),
        "selector": spec.get("selector", {}),
        "labels": metadata.get("labels", {}),
        "loadBalancerIngress": status.get("loadBalancer", {}).get("ingress", []),
    }


def extract_daemonset(ds: dict) -> dict:
    metadata = ds.get("metadata", {})
    status = ds.get("status", {})
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "desiredNumberScheduled": status.get("desiredNumberScheduled", 0),
        "numberReady": status.get("numberReady", 0),
        "containers": [
            {"name": c.get("name"), "image": c.get("image")}
            for c in ds.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        ],
    }


def extract_statefulset(ss: dict) -> dict:
    metadata = ss.get("metadata", {})
    spec = ss.get("spec", {})
    status = ss.get("status", {})
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "replicas": spec.get("replicas", 0),
        "readyReplicas": status.get("readyReplicas", 0),
        "containers": [
            {"name": c.get("name"), "image": c.get("image")}
            for c in spec.get("template", {}).get("spec", {}).get("containers", [])
        ],
    }


# ──────────────────────────────────────────────────────────────
# Lambda Handler
# ──────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Entry point — called by parent VPC Discovery Lambda.

    Event payload (all provided by parent Lambda):
        cluster_name : EKS cluster name
        endpoint     : K8s API server endpoint (from describe_cluster)
        ca_data      : Base64-encoded cluster CA cert (from describe_cluster)
        region       : AWS region

    Returns:
        statusCode   : 200 on success, 400/500 on failure
        cluster_name : Echo of input cluster name
        workloads    : Dict of pods/deployments/services/daemonsets/statefulsets/namespaces
    """
    cluster_name = event.get("cluster_name")
    endpoint = event.get("endpoint")
    ca_data = event.get("ca_data")
    region = event.get("region")

    logger.info(f"=== Child Lambda started for cluster: {cluster_name} ===")

    # Validate inputs
    if not all([cluster_name, endpoint, ca_data, region]):
        missing = [f for f in ["cluster_name", "endpoint", "ca_data", "region"] if not event.get(f)]
        return {
            "statusCode": 400,
            "cluster_name": cluster_name,
            "error": f"Missing required fields: {', '.join(missing)}",
        }

    try:
        # Step 1 — generate Bearer token (local, no network call)
        logger.info(f"Generating EKS token for {cluster_name}")
        token = generate_eks_token(cluster_name, region)

        # Step 2 — build SSL context from cluster CA
        ssl_ctx = build_ssl_context(ca_data)

        # Step 3 — fetch workloads from K8s private endpoint (inside VPC)
        logger.info(f"Fetching workloads from K8s API: {endpoint}")

        pods = k8s_list(endpoint, "/api/v1/pods", token, ssl_ctx)
        deployments = k8s_list(endpoint, "/apis/apps/v1/deployments", token, ssl_ctx)
        services = k8s_list(endpoint, "/api/v1/services", token, ssl_ctx)
        daemonsets = k8s_list(endpoint, "/apis/apps/v1/daemonsets", token, ssl_ctx)
        statefulsets = k8s_list(endpoint, "/apis/apps/v1/statefulsets", token, ssl_ctx)
        namespaces = k8s_list(endpoint, "/api/v1/namespaces", token, ssl_ctx)

        workloads = {
            "pods": [extract_pod(p) for p in pods],
            "deployments": [extract_deployment(d) for d in deployments],
            "services": [extract_service(s) for s in services],
            "daemonsets": [extract_daemonset(d) for d in daemonsets],
            "statefulsets": [extract_statefulset(s) for s in statefulsets],
            "namespaces": [n["metadata"]["name"] for n in namespaces],
        }

        logger.info(
            f"Fetched workloads for {cluster_name} — "
            f"pods: {len(workloads['pods'])}, "
            f"deployments: {len(workloads['deployments'])}, "
            f"services: {len(workloads['services'])}, "
            f"daemonsets: {len(workloads['daemonsets'])}, "
            f"statefulsets: {len(workloads['statefulsets'])}, "
            f"namespaces: {len(workloads['namespaces'])}"
        )
        logger.info(f"=== Child Lambda completed for cluster: {cluster_name} ===")

        return {
            "statusCode": 200,
            "cluster_name": cluster_name,
            "workloads": workloads,
        }

    except Exception as e:
        logger.exception(f"Child Lambda failed for cluster {cluster_name}: {e}")
        return {
            "statusCode": 500,
            "cluster_name": cluster_name,
            "error": str(e),
        }
