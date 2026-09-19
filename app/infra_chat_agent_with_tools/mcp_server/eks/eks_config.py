"""
Centralized configuration for EKS service onboarding.
"""

from app.core.config import settings as _s

# AWS Configuration
DEFAULT_AWS_REGION = "ap-south-1"
DEFAULT_ECR_REGISTRY = "597189966628.dkr.ecr.ap-south-1.amazonaws.com"
AWS_ACCOUNT_ID = "597189966628"
AWS_ROLE_ARN = "arn:aws:iam::597189966628:role/rgb-github-role"

# EKS Configuration
DEFAULT_EKS_CLUSTER_NAME = "devlift-dev-cluster"
DEFAULT_ENVIRONMENT = "stage"

# EKS Subnet IDs
EKS_SUBNET_IDS = [
    "subnet-074be56e9734239cf",
    "subnet-03cdd8806ef53c31d",
    "subnet-01a9d28f43bb5e120"
]

# Docker Configuration
DEFAULT_IMAGE_TAG = "latest"

# Default Resource Limits
DEFAULT_CPU_REQUESTED = "500m"
DEFAULT_CPU_LIMIT = "1000m"
DEFAULT_MEMORY_REQUESTED = "512Mi"
DEFAULT_MEMORY_LIMIT = "1024Mi"
DEFAULT_REPLICA_COUNT = 1

# Default Health Check
DEFAULT_HEALTH_ENDPOINT = "/health"

# ALB Configuration
DEFAULT_ALB_SCHEME = "internet-facing"

# Shared ALB / IngressGroup configuration
DEVLIFT_BASE_DOMAIN = "devlift.ai"
DEFAULT_SHARED_ENV = "stage"                  # env suffix for shared ALB group + hostnames (namespace itself uses {tenant}-ns)
DEFAULT_ALB_GROUP_ORDER_SERVICE = "100"        # service ingresses use order 100; seed uses 1
SHARED_ACM_CERT_ARN = _s.onboarding_default_acm_cert_arn

# Git Configuration
DEFAULT_GIT_BRANCH = "main"

# Port Mappings by Language
LANGUAGE_DEFAULT_PORTS = {
    "python": 8000,
    "java": 8080,
    "java-gradle": 8080,
    "java-maven": 8080,
    "go": 8080,
    "golang": 8080,
    "nodejs": 3000,
    "node": 3000
}

# Kubectl Configuration
KUBECTL_APPLY_TIMEOUT = 120  # seconds
KUBECTL_GET_TIMEOUT = 30     # seconds
ALB_WAIT_TIME = 5            # seconds to wait for ALB after apply

# Allowed GitHub Organizations
ALLOWED_GITHUB_ORGS = [
    "Regobs"
]

# Deployment Monitoring Configuration
DEPLOYMENT_POLL_INTERVAL = 10   # seconds between polls
DEPLOYMENT_MAX_DURATION = 600   # 10 minutes max monitoring time
