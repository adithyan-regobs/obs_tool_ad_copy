"""
Kubernetes deployment operations for EKS service onboarding.
Handles kubectl commands and resource deployment.
"""
import subprocess
import tempfile
import shutil
import time
import os
from typing import Dict, List

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    KUBECTL_APPLY_TIMEOUT,
    KUBECTL_GET_TIMEOUT,
    ALB_WAIT_TIME
)


def check_kubectl_ready(cluster_name: str) -> Dict:
    """
    Check if kubectl is configured and ready for deployment.

    Args:
        cluster_name: Name of the EKS cluster

    Returns:
        Dict with 'ready' status and context/error information
    """
    try:
        # Check if kubectl exists
        subprocess.run(
            ["kubectl", "version", "--client"],
            capture_output=True,
            check=True,
            timeout=10
        )

        # Get current context
        result = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=10
        )

        current_context = result.stdout.strip()

        return {
            "ready": True,
            "context": current_context,
            "note": f"Using kubectl context: {current_context}"
        }

    except subprocess.TimeoutExpired:
        return {
            "ready": False,
            "error": "kubectl command timed out"
        }
    except subprocess.CalledProcessError:
        return {
            "ready": False,
            "error": "kubectl not found or not configured. Please install kubectl and configure for your EKS cluster."
        }
    except Exception as e:
        return {
            "ready": False,
            "error": f"kubectl check failed: {str(e)}"
        }


def deploy_to_k8s(manifests: List[Dict], service_name: str, namespace: str) -> Dict:
    """
    Deploy Kubernetes manifests using kubectl apply.

    This function:
    1. Writes manifests to a temporary directory
    2. Applies them using kubectl apply -k
    3. Waits briefly for ingress creation
    4. Retrieves the ALB URL from the ingress

    Args:
        manifests: List of dicts with 'path' and 'content' keys
        service_name: Name of the service
        namespace: Kubernetes namespace

    Returns:
        Dict with deployment status, ALB URL, and messages
    """
    # Create unique temporary directory
    temp_dir = tempfile.mkdtemp(prefix=f"eks-{service_name}-")

    try:
        # Write all manifest files to temp directory
        for manifest in manifests:
            # Extract just the filename (e.g., "deployment.yaml")
            filename = os.path.basename(manifest["path"])
            file_path = os.path.join(temp_dir, filename)

            with open(file_path, 'w') as f:
                f.write(manifest["content"])

        # Execute kubectl apply with kustomize
        apply_result = subprocess.run(
            ["kubectl", "apply", "-k", temp_dir],
            capture_output=True,
            text=True,
            timeout=KUBECTL_APPLY_TIMEOUT
        )

        if apply_result.returncode != 0:
            return {
                "success": False,
                "error": apply_result.stderr,
                "message": f"Failed to apply K8s manifests: {apply_result.stderr}"
            }

        # Wait for ingress to be created
        time.sleep(ALB_WAIT_TIME)

        # Get ALB URL from ingress status
        alb_result = subprocess.run(
            [
                "kubectl", "get", "ingress",
                f"{service_name}-ingress",
                "-n", namespace,
                "-o", "jsonpath={.status.loadBalancer.ingress[0].hostname}"
            ],
            capture_output=True,
            text=True,
            timeout=KUBECTL_GET_TIMEOUT
        )

        alb_hostname = alb_result.stdout.strip()

        # Format ALB URL
        if alb_hostname:
            alb_url = f"http://{alb_hostname}"
            alb_status = "ALB provisioned successfully"
        else:
            alb_url = "ALB URL pending (will be available in 2-3 minutes)"
            alb_status = "ALB is being created"

        return {
            "success": True,
            "alb_url": alb_url,
            "alb_hostname": alb_hostname or "pending",
            "kubectl_output": apply_result.stdout,
            "message": f"Infrastructure deployed to namespace '{namespace}'. {alb_status}.",
            "note": "Pods will be in ImagePullBackOff until Docker image is pushed to ECR. Commit and push files to trigger GitHub Actions build.",
            "temp_dir": temp_dir  # For debugging if needed
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "kubectl command timed out",
            "message": f"Deployment timed out after {KUBECTL_APPLY_TIMEOUT} seconds"
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"Deployment failed: {str(e)}"
        }
    finally:
        # Always cleanup temporary directory
        shutil.rmtree(temp_dir, ignore_errors=True)


def get_deployment_status(service_name: str, namespace: str) -> Dict:
    """
    Get the current deployment status.

    Args:
        service_name: Name of the service
        namespace: Kubernetes namespace

    Returns:
        Dict with deployment status information
    """
    try:
        result = subprocess.run(
            ["kubectl", "get", "deployment", service_name,
             "-n", namespace, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=KUBECTL_GET_TIMEOUT
        )

        if result.returncode == 0:
            return {
                "exists": True,
                "output": result.stdout
            }
        else:
            return {
                "exists": False,
                "message": "Deployment not found"
            }

    except Exception as e:
        return {
            "exists": False,
            "error": str(e)
        }


def get_pod_status(service_name: str, namespace: str) -> Dict:
    """
    Get pod status for a service.

    Args:
        service_name: Name of the service
        namespace: Kubernetes namespace

    Returns:
        Dict with pod status information
    """
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", namespace,
             "-l", f"app={service_name}", "-o", "wide"],
            capture_output=True,
            text=True,
            timeout=KUBECTL_GET_TIMEOUT
        )

        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "message": "Retrieved pod status"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }
