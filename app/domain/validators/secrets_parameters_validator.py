"""
Secrets and Parameters Validators

Helper validators for secrets and parameters management.
Provides validation for resource identifiers, naming conventions, and vendor-specific formats.
"""

import re
from typing import Dict, Optional


def validate_aws_arn_format(arn: str) -> bool:
    """
    Validate AWS ARN format.

    Args:
        arn: AWS ARN string

    Returns:
        True if valid ARN format, False otherwise

    Examples:
        - Valid Secrets Manager: arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret-abc123
        - Valid SSM Parameter: arn:aws:ssm:us-east-1:123456789012:parameter/my/parameter/path
    """
    # ARN format: arn:partition:service:region:account-id:resource-type/resource-id
    pattern = r"^arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:(secret|parameter):.+"
    return bool(re.match(pattern, arn))


def parse_aws_arn(arn: str) -> Dict[str, str]:
    """
    Parse AWS ARN into components.

    Args:
        arn: AWS ARN string

    Returns:
        Dict with ARN components

    Example:
        Input: arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret-abc123
        Output: {
            "partition": "aws",
            "service": "secretsmanager",
            "region": "us-east-1",
            "account_id": "123456789012",
            "resource_type": "secret",
            "resource_name": "my-secret-abc123"
        }
    """
    parts = arn.split(":")
    if len(parts) < 6:
        raise ValueError(f"Invalid ARN format: {arn}")

    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account_id": parts[4],
        "resource_type": parts[5],
        "resource_name": ":".join(parts[6:]) if len(parts) > 6 else ""
    }


def extract_secret_name_from_arn(arn: str) -> str:
    """
    Extract secret name from AWS Secrets Manager ARN.

    Args:
        arn: Secrets Manager ARN

    Returns:
        Secret name (without random suffix)

    Example:
        Input: arn:aws:secretsmanager:us-east-1:123456789012:secret:acme-corp/prod/api-key-Abc123
        Output: acme-corp/prod/api-key
    """
    parsed = parse_aws_arn(arn)
    resource_name = parsed.get("resource_name", "")

    # AWS appends random suffix to secret names (e.g., -Abc123)
    # Remove it by splitting on last hyphen if it looks like a suffix
    if "-" in resource_name:
        parts = resource_name.rsplit("-", 1)
        if len(parts) == 2 and len(parts[1]) == 6 and parts[1].isalnum():
            return parts[0]

    return resource_name


def validate_aws_parameter_path(path: str) -> bool:
    """
    Validate AWS SSM Parameter Store parameter path format.

    Args:
        path: Parameter path

    Returns:
        True if valid, False otherwise

    Valid formats:
        - /my/parameter/path
        - /tenant/environment/region/name
        - Must start with /
        - Can contain alphanumeric, -, _, .
    """
    pattern = r"^/[a-zA-Z0-9/_.-]+$"
    return bool(re.match(pattern, path))


def validate_gcp_secret_resource_name(resource_name: str) -> bool:
    """
    Validate GCP Secret Manager resource name format.

    Args:
        resource_name: GCP secret resource name

    Returns:
        True if valid, False otherwise

    Valid format:
        projects/{project}/secrets/{secret}
        projects/{project}/secrets/{secret}/versions/{version}
    """
    pattern = r"^projects/[a-z0-9-]+/secrets/[a-zA-Z0-9_-]+(/versions/\d+)?$"
    return bool(re.match(pattern, resource_name))


def parse_gcp_secret_resource_name(resource_name: str) -> Dict[str, str]:
    """
    Parse GCP Secret Manager resource name.

    Args:
        resource_name: GCP resource name

    Returns:
        Dict with components

    Example:
        Input: projects/my-project/secrets/my-secret/versions/1
        Output: {
            "project": "my-project",
            "secret_name": "my-secret",
            "version": "1"
        }
    """
    pattern = r"^projects/(?P<project>[^/]+)/secrets/(?P<secret>[^/]+)(/versions/(?P<version>\d+))?$"
    match = re.match(pattern, resource_name)

    if not match:
        raise ValueError(f"Invalid GCP secret resource name: {resource_name}")

    return {
        "project": match.group("project"),
        "secret_name": match.group("secret"),
        "version": match.group("version") or "latest"
    }


def validate_azure_keyvault_uri(uri: str) -> bool:
    """
    Validate Azure Key Vault secret URI format.

    Args:
        uri: Key Vault secret URI

    Returns:
        True if valid, False otherwise

    Valid format:
        https://{vault-name}.vault.azure.net/secrets/{secret-name}
        https://{vault-name}.vault.azure.net/secrets/{secret-name}/{version}
    """
    pattern = r"^https://[a-zA-Z0-9-]+\.vault\.azure\.net/secrets/[a-zA-Z0-9-]+(/[a-zA-Z0-9]+)?$"
    return bool(re.match(pattern, uri))


def parse_azure_keyvault_uri(uri: str) -> Dict[str, str]:
    """
    Parse Azure Key Vault secret URI.

    Args:
        uri: Key Vault secret URI

    Returns:
        Dict with components

    Example:
        Input: https://myvault.vault.azure.net/secrets/my-secret/abc123
        Output: {
            "vault_name": "myvault",
            "secret_name": "my-secret",
            "version": "abc123"
        }
    """
    pattern = r"^https://(?P<vault>[^.]+)\.vault\.azure\.net/secrets/(?P<secret>[^/]+)(/(?P<version>[^/]+))?$"
    match = re.match(pattern, uri)

    if not match:
        raise ValueError(f"Invalid Azure Key Vault URI: {uri}")

    return {
        "vault_name": match.group("vault"),
        "secret_name": match.group("secret"),
        "version": match.group("version") or "latest"
    }


def validate_resource_name(name: str) -> bool:
    """
    Validate resource name format (generic across vendors).

    Args:
        name: Resource name

    Returns:
        True if valid, False otherwise

    Rules:
        - Alphanumeric, hyphens, underscores only
        - 1-255 characters
        - Cannot start/end with hyphen
    """
    if not name or len(name) > 255:
        return False

    pattern = r"^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$"
    return bool(re.match(pattern, name))


def sanitize_resource_name(name: str) -> str:
    """
    Sanitize resource name to conform to naming rules.

    Args:
        name: Input name

    Returns:
        Sanitized name

    Transformations:
        - Lowercase
        - Replace invalid chars with hyphen
        - Remove leading/trailing hyphens
        - Truncate to 255 chars
    """
    # Lowercase
    sanitized = name.lower()

    # Replace invalid characters with hyphen
    sanitized = re.sub(r"[^a-z0-9_-]", "-", sanitized)

    # Remove leading/trailing hyphens
    sanitized = sanitized.strip("-")

    # Truncate
    if len(sanitized) > 255:
        sanitized = sanitized[:255].rstrip("-")

    return sanitized
