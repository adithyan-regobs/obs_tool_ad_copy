"""
Pure helper functions for pipeline operations.

These functions have no side effects and don't depend on external state.
They are easily testable and reusable across different services.
"""

import re
import time
import uuid
from typing import Tuple


def sanitize_name(name: str) -> str:
    """
    Sanitize name for AWS resource naming.
    Replaces spaces and special characters with hyphens.
    Keeps only alphanumeric, hyphens, and underscores.

    Args:
        name: Name to sanitize

    Returns:
        Sanitized name

    Examples:
        >>> sanitize_name("Payment Processing Service")
        'Payment-Processing-Service'

        >>> sanitize_name("user_auth_service v2.0")
        'user-auth-service-v20'
    """
    # Replace spaces with hyphens
    name = name.replace(" ", "-")
    # Replace underscores with hyphens
    name = name.replace("_", "-")
    # Remove all characters except alphanumeric and hyphens
    name = re.sub(r'[^a-zA-Z0-9\-]', '', name)
    # Replace multiple consecutive hyphens with single hyphen
    name = re.sub(r'-+', '-', name)
    # Remove leading/trailing hyphens
    name = name.strip('-')
    return name


def generate_pipeline_code(service_code: str, environment: str) -> str:
    """
    Generate unique pipeline code with max length of 60 characters.

    Format: pipeline_{service_code}_{environment}_{timestamp}_{random}
    Max length: 60 chars (leaving room for branch suffix)

    Args:
        service_code: Service code
        environment: Environment (dev, staging, prod)

    Returns:
        Generated pipeline code (max 60 chars)

    Examples:
        >>> generate_pipeline_code("payment_api", "prod")
        'pipeline_payment_api_prod_1762304095_a1b2c3d4'
    """
    timestamp = int(time.time())
    random_suffix = uuid.uuid4().hex[:8]  # 8-char random hex to ensure uniqueness in parallel calls

    # Calculate available space for service_code
    # Fixed parts: "pipeline_" (9) + "_" (1) + environment (max 10) + "_" (1) + timestamp (10) + "_" (1) + random (8) = 40 chars
    # Leave 20 chars for service_code
    max_service_code_len = 20

    # Truncate service_code if needed
    if len(service_code) > max_service_code_len:
        service_code = service_code[:max_service_code_len]

    pipeline_code = f"pipeline_{service_code}_{environment}_{timestamp}_{random_suffix}"

    # Final safety check to ensure max 60 chars
    if len(pipeline_code) > 60:
        # Emergency truncate - this should rarely be needed
        pipeline_code = pipeline_code[:60]

    return pipeline_code


def generate_workflow_filename(service_name: str, environment: str) -> str:
    """
    Generate GitHub Actions workflow filename.

    Format: deploy-{sanitized_service_name}-{environment}.yml

    Args:
        service_name: Service name
        environment: Environment (dev, staging, prod)

    Returns:
        Workflow filename

    Examples:
        >>> generate_workflow_filename("Payment API", "prod")
        'deploy-payment-api-prod.yml'

        >>> generate_workflow_filename("User Auth Service", "dev")
        'deploy-user-auth-service-dev.yml'
    """
    # Sanitize service name for filename
    sanitized_name = sanitize_name(service_name).lower()
    return f"deploy-{sanitized_name}-{environment}.yml"


def generate_commit_message(service_name: str, environment: str) -> str:
    """
    Generate commit message for workflow file.

    Format: "Add GitHub Actions workflow for {service_name} ({environment})"

    Args:
        service_name: Service name
        environment: Environment

    Returns:
        Commit message

    Examples:
        >>> generate_commit_message("Payment API", "prod")
        'Add GitHub Actions workflow for Payment API (prod)'
    """
    return f"Add GitHub Actions workflow for {service_name} ({environment})"


def generate_iam_role_name(
    service_code: str,
    environment: str,
    branch_name: str
) -> str:
    """
    Generate IAM role name within AWS 64-character limit.

    Format: GHA-{service[:20]}-{env[:8]}-{branch[:10]}
    Max length: 44 characters (well under 64 limit)

    Args:
        service_code: Service code
        environment: Environment (dev, staging, prod)
        branch_name: Git branch name

    Returns:
        IAM role name

    Examples:
        >>> generate_iam_role_name('payment_api', 'prod', 'main')
        'GHA-payment-api-prod-main'

        >>> generate_iam_role_name('payment_processing_microservice_v2', 'production', 'main')
        'GHA-payment-processing-producti-main'
    """
    # Sanitize and truncate components
    service_part = sanitize_name(service_code)[:20]
    env_part = environment[:8]
    branch_part = sanitize_name(branch_name)[:10]

    # Build role name
    role_name = f"GHA-{service_part}-{env_part}-{branch_part}"

    return role_name


def parse_repo_url(repo_url: str) -> Tuple[str, str]:
    """
    Parse GitHub repository URL to extract owner and repository name.

    Handles URLs with or without .git extension and trailing slashes.

    Args:
        repo_url: GitHub repository URL

    Returns:
        Tuple of (owner, repo)

    Raises:
        ValueError: If URL format is invalid

    Examples:
        >>> parse_repo_url("https://github.com/owner/repo.git")
        ('owner', 'repo')

        >>> parse_repo_url("https://github.com/my-org/my-service/")
        ('my-org', 'my-service')
    """
    # Clean up URL
    repo_url = repo_url.rstrip("/").removesuffix(".git") 
    
    # Split and extract last two parts
    parts = repo_url.split("/")

    
    if len(parts) < 2:
        raise ValueError(f"Invalid repository URL format: {repo_url}")

    owner = parts[-2]
    repo = parts[-1]


    if not owner or not repo:
        raise ValueError(f"Could not extract owner/repo from URL: {repo_url}")

    return owner, repo


def generate_run_code(pipeline_code: str) -> str:
    """
    Generate unique pipeline run code.

    Format: {pipeline_code}_run_{timestamp}

    Args:
        pipeline_code: Pipeline code

    Returns:
        Generated run code

    Examples:
        >>> generate_run_code("pipeline_payment_prod_123")
        'pipeline_payment_prod_123_run_1762304095'
    """
    timestamp = int(time.time())
    return f"{pipeline_code}_run_{timestamp}"
