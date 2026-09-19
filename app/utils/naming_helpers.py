"""
Naming Helper Utilities

Simple helper for building branch names and hosting type mappings.
"""

import time


# Mapping of hosting type display names to short codes for branch naming
HOSTING_TYPE_SHORT_CODES = {
    "AWS ECS Fargate": "ecs",
    "AWS ECS on EC2": "ecs",
    "AWS EKS (Kubernetes)": "eks",
    "AWS Lambda Function": "lambda",
    "AWS EC2 Instance": "ec2",
    "AWS Batch": "batch",
    "AWS Elastic Beanstalk": "beanstalk",
    "AWS Lightsail": "lightsail",
}


def get_hosting_type_short_code(hosting_type_name: str) -> str:
    """
    Get short code for hosting type.

    Args:
        hosting_type_name: Full hosting type display name (e.g., "AWS ECS Fargate")

    Returns:
        Short code for branch naming (e.g., "ecs"). Defaults to "service" if not found.
    """
    return HOSTING_TYPE_SHORT_CODES.get(hosting_type_name, "service")


def build_dockerfile_feature_branch(
    service_sanitized: str,
    branch_sanitized: str,
    env_normalized: str = "",
    geo_loc_sanitized: str = ""
) -> str:
    """
    Build timestamp-based feature branch name for Dockerfile modification (Datadog).

    Args:
        service_sanitized: Already sanitized service name
        branch_sanitized: Already sanitized branch name
        env_normalized: Kept for backwards compatibility (unused)
        geo_loc_sanitized: Kept for backwards compatibility (unused)

    Returns:
        Feature branch name: datadog/{service}-{branch}-{timestamp}
    """
    timestamp = int(time.time())
    return f"datadog/{service_sanitized}-{branch_sanitized}-{timestamp}"
