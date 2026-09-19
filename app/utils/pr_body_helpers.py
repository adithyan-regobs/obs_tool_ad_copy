"""
PR Body Generation Helpers

Shared utilities for generating PR titles and descriptions.
Used by:
- script_pr_workflow_service.py
- transaction_queue_service.py
"""

from typing import List, Dict, Any, Set, Optional
from app.core.config import settings
from app.services.terragrunt_sync_service import TerragruntSyncService


# Mapping from case_type/infra_type_ref to display-friendly infra type names
INFRA_TYPE_DISPLAY_MAP = {
    # S3/Bucket types
    'create_bucket': 'S3',
    'create_s3': 'S3',
    's3': 'S3',
    'bucket': 'S3',
    # SQS/Queue types
    'create_queue': 'SQS',
    'create_sqs': 'SQS',
    'sqs': 'SQS',
    'queue': 'SQS',
    # DynamoDB types
    'create_table': 'DynamoDB',
    'create_dynamodb': 'DynamoDB',
    'dynamodb': 'DynamoDB',
    'dynamo': 'DynamoDB',
    # ECS types
    'create_service': 'ECS',
    'update_service': 'ECS',
    'create_ecs': 'ECS',
    'ecs': 'ECS',
    'ecs_ec2': 'ECS',
    'ecs_ec2_infrastructuretype_ref': 'ECS',
    # EKS types
    'eks': 'EKS',
    'eks_infrastructuretype_ref': 'EKS',
    # Kong/Gateway types
    'create_gateway': 'Kong Gateway',
    'create_kong': 'Kong Gateway',
    'kong': 'Kong Gateway',
    'gateway': 'Kong Gateway',
    'kong_route': 'Kong Gateway',
    # The case_ref a gateway queue row actually carries — without it the generic
    # title-caser renders "Add Route" instead of naming the resource type.
    'add_route': 'Kong Gateway',
}


def normalize_infra_type(infra_type: str) -> str:
    """
    Normalize infrastructure type to a display-friendly name.

    Handles case_type values like 'CREATE_BUCKET' -> 'S3'.
    If type is not in the mapping, returns the original type in title case.

    Args:
        infra_type: Raw infra type (e.g., 'CREATE_BUCKET', 's3', 'ECS')

    Returns:
        Display-friendly infra type (e.g., 'S3', 'SQS', 'ECS')
    """
    if not infra_type:
        return ''

    # Normalize to lowercase for lookup
    type_lower = infra_type.lower().strip()

    # Check mapping
    if type_lower in INFRA_TYPE_DISPLAY_MAP:
        return INFRA_TYPE_DISPLAY_MAP[type_lower]

    # Graceful fallback: return original in title case (cleaned up)
    # Remove common prefixes like 'create_'
    cleaned = type_lower.replace('create_', '').replace('_', ' ')
    return cleaned.title()


def _join_with_and(items: List[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _format_infra_type_for_title(infra_type: str) -> str:
    normalized = normalize_infra_type(infra_type)
    if not normalized:
        return ""
    title_map = {
        "S3": "S3 bucket",
        "SQS": "SQS Queue",
        "DynamoDB": "Dynamo Db",
        "Kong Gateway": "Kong route",
        "DB Creation": "DB Creation",
        "ECS": "ECS",
        "EKS": "EKS",
    }
    return title_map.get(normalized, normalized)


def generate_pr_title(
    item_count: int,
    environments: Set[str],
    infra_types: Optional[Set[str]] = None
) -> str:
    """
    Generate PR title with [DevLift] prefix.

    Format: [DevLift] Deploy {types} in {environments}
    (falls back to count when types are not provided)

    Args:
        item_count: Number of items being deployed
        environments: Set of environment names
        infra_types: Optional set of infra/case types for title display

    Returns:
        PR title string
    """
    normalized_envs: Set[str] = set()
    for env in environments:
        env_normalized = str(env).lower()
        if env_normalized in ("staging", "stage"):
            env_normalized = "stage"
        normalized_envs.add(env_normalized)

    env_list = sorted(normalized_envs)
    if len(env_list) == 0:
        env_str = "unknown"
    else:
        env_str = _join_with_and(env_list)

    types_label = ""
    if infra_types:
        display_types = sorted({
            _format_infra_type_for_title(infra_type)
            for infra_type in infra_types
            if infra_type
        })
        if display_types:
            if len(display_types) <= 6:
                types_label = _join_with_and(display_types)
            else:
                types_label = _join_with_and(display_types[:3]) + f" and {len(display_types) - 3} more"

    if types_label:
        title = f"[DevLift] Deploy {types_label} in {env_str}"
    else:
        title = (
            f"[DevLift] Deploy {item_count} config{'s' if item_count != 1 else ''} in {env_str}"
        )

    if len(title) > 250:
        title = title[:247].rstrip() + "..."
    return title


def generate_pr_body(
    items_data: List[Dict[str, Any]],
    environments: Set[str],
    infra_types: Set[str],
    user_email: str = "",
    atlantis_entries: Optional[Set[str]] = None
) -> str:
    """
    Generate PR description body with rich markdown format.

    Generates a markdown body with:
    - Summary section with item counts
    - Card-style changes list with Type, Service, Environment, Region, Atlantis Commands
    - Footer with app name and user

    Args:
        items_data: List of dicts with keys:
            - infra_type: str
            - service_name: str
            - environment: str
            - region: str
            - atlantis_name: str
            - has_atlantis_entry: Optional[bool] — whether this item has an
              atlantis.yaml project. Set it per item when the caller knows;
              omit it to fall back to the atlantis_entries name lookup.
            - changes: Optional[List[str]] — markdown bullets rendered under
              env/region as a "Changes:" block.
        environments: Set of environment names
        infra_types: Set of infrastructure type names
        user_email: User email/code for footer
        atlantis_entries: Set of service/resource names that have atlantis.yaml
            entries. Legacy per-name gate, kept for callers that pass None to
            suppress the commands entirely.

    Returns:
        PR body markdown string
    """
    # Normalize infra types for display
    display_infra_types = set()
    for infra_type in infra_types:
        display_infra_types.add(normalize_infra_type(infra_type))

    body = "## Deploy Queue - Batch Deployment\n\n"

    # Summary section
    env_list = sorted(environments)
    env_str = _join_with_and(env_list) if env_list else "unknown"
    body += "### Summary\n\n"
    body += f"<p>Add resources in the {env_str}</p>\n\n"
    body += f"**Items:** {len(items_data)} total\n"
    body += f"**Environments:** {', '.join(sorted(environments)) if environments else 'N/A'}\n"
    body += f"**Infrastructure Types:** {', '.join(sorted(display_infra_types)) if display_infra_types else 'N/A'}\n\n"

    # Resources section with card-style format
    body += "### Resources created\n\n"

    for idx, item in enumerate(items_data, 1):
        # Normalize infra type for display
        infra_type_display = normalize_infra_type(item.get('infra_type', ''))
        service_name_raw = item.get('service_name', '')
        service_name = _sanitize_pr_service_name(service_name_raw)
        environment = item.get('environment', '')
        region = item.get('region', '')
        atlantis_name = item.get('atlantis_name', '')

        # Card header with number, type, and service name
        if service_name:
            body += f"#### {idx}. {infra_type_display}: `{service_name}`\n\n"
        else:
            body += f"#### {idx}. {infra_type_display}\n\n"

        # Compact details on single line (environment | region)
        details = []
        if environment:
            details.append(f"**Env:** {environment}")
        if region:
            details.append(f"**Region:** {region}")
        if details:
            body += " | ".join(details) + "\n\n"

        # What moved, for cards whose resource name alone doesn't say it.
        changes = item.get('changes')
        if changes:
            body += "**Changes:**\n\n"
            for change in changes:
                body += f"- {change}\n"
            body += "\n"

        # Atlantis commands - only if this specific item has an atlantis.yaml entry.
        # Prefer the per-item flag: matching by service NAME cannot answer this for a
        # gateway change, whose queue row carries only a change set and so has no
        # resource name to match on — it was silently dropping the commands for the
        # one resource type where the project name is hardest to work out by hand.
        has_entry = item.get('has_atlantis_entry')
        if has_entry is None:
            has_entry = bool(atlantis_entries) and service_name_raw in atlantis_entries
        if has_entry and atlantis_name:
            body += "**Atlantis Commands:**\n\n"
            body += f"```\natlantis plan -p {atlantis_name}\n```\n\n"
            body += f"```\natlantis apply -p {atlantis_name}\n```\n\n"

    # Footer
    body += "---\n"
    body += f"*Generated by {settings.app_name}*"
    if user_email:
        body += f"\n*Requested by: {user_email}*"

    return body


def _sanitize_pr_service_name(service_name: str) -> str:
    """
    Remove queue-id style values from PR display names.
    """
    if not service_name:
        return ""

    stripped = service_name.strip()
    if not stripped:
        return ""

    if stripped.isdigit():
        return ""

    import re
    if re.fullmatch(r"(queue|q)[-_ ]?\d+", stripped.lower()):
        return ""

    # Generated queue codes are "queue-" + 12 hex chars, so the digits-only rule
    # above never caught them and they leaked into the PR as the resource name.
    if re.fullmatch(r"queue-[0-9a-f]{8,}", stripped.lower()):
        return ""

    return stripped


def get_region_display(geo_loc_code: str) -> str:
    """
    Convert geo location code to AWS region display.

    Uses TerragruntSyncService._get_aws_region_from_geo_loc for consistency.

    Args:
        geo_loc_code: Geo location master code (e.g., 'mumbai', 'london', 'ap-south-1')

    Returns:
        AWS region string (e.g., 'ap-south-1', 'eu-west-2')
    """
    if not geo_loc_code:
        return ''

    geo_lower = geo_loc_code.lower()

    # If already an AWS region format, return as-is
    if geo_lower.startswith(('ap-', 'eu-', 'us-', 'sa-', 'ca-', 'me-', 'af-')):
        return geo_lower

    # Use TerragruntSyncService's mapping for consistency
    return TerragruntSyncService._get_aws_region_from_geo_loc(geo_loc_code)


def _get_service_name_for_env_files(service_name: str) -> str:
    """
    Get service name with -service suffix for env files and atlantis entries.
    Matches the logic in AsporaAtlantisScriptGenComponent._get_service_name_for_env_files
    """
    if service_name.endswith("-service"):
        return service_name
    return f"{service_name}-service"


def compute_atlantis_project_name(
    infra_type: str,
    service_name: str,
    environment: str,
    tenant_code: str,
    product_name: str,
    geo_loc: str = ""
) -> str:
    """
    Compute the Atlantis project name based on infra type.

    This function mirrors the logic in AsporaAtlantisScriptGenComponent to ensure
    PR description commands match the actual atlantis.yaml entries.

    For core-prod with geo_loc:
        - ECS: {product}-{env}-{geo_loc}-{service_name_with_suffix}
        - Standalone: {product}-{env}-{geo_loc}-{name}-{suffix}
    For other cases:
        - ECS: {product}-{env}-{service_name_with_suffix}
        - Standalone: {product}-{env}-{name}-{suffix}

    Suffixes:
        - S3: -bucket
        - SQS: -queue
        - DynamoDB: -table

    Args:
        infra_type: Infrastructure type (ECS, S3, SQS, DYNAMODB, etc.)
        service_name: Service/resource name
        environment: Environment
        tenant_code: Tenant code
        product_name: Product name
        geo_loc: Geo location code (e.g., 'london', 'mumbai')

    Returns:
        Atlantis project name string
    """
    # Normalize environment for display
    env_display = TerragruntSyncService._normalize_environment_for_display(
        environment, tenant_code
    )

    # Sanitize names
    product_sanitized = _sanitize_name(product_name)
    service_sanitized = _sanitize_name(service_name)

    # Suffix mapping for infra types (standalone resources)
    suffix_map = {
        'S3': 'bucket',
        'SQS': 'queue',
        'DYNAMODB': 'table',
        'GATEWAY': 'gateway',
        'KONG': 'gateway',
    }

    infra_upper = infra_type.upper()

    # Check if geo_loc should be included (core-prod pattern)
    include_geo_loc = (
        product_sanitized == "core" and
        env_display == "prod" and
        geo_loc
    )

    # For ECS, use _get_service_name_for_env_files to match atlantis.yaml generation
    if infra_upper in ('ECS', 'ECS_EC2'):
        service_for_name = _get_service_name_for_env_files(service_sanitized)
        if include_geo_loc:
            geo_loc_normalized = TerragruntSyncService._normalize_geo_loc_for_atlantis(geo_loc)
            return f"{product_sanitized}-{env_display}-{geo_loc_normalized}-{service_for_name}"
        return f"{product_sanitized}-{env_display}-{service_for_name}"

    # For standalone resources, use suffix
    suffix = suffix_map.get(infra_upper, infra_type.lower())
    if include_geo_loc:
        geo_loc_normalized = TerragruntSyncService._normalize_geo_loc_for_atlantis(geo_loc)
        return f"{product_sanitized}-{env_display}-{geo_loc_normalized}-{service_sanitized}-{suffix}"

    return f"{product_sanitized}-{env_display}-{service_sanitized}-{suffix}"


def _sanitize_name(name: str) -> str:
    """
    Sanitize a name for use in file paths and atlantis project names.

    Args:
        name: Name to sanitize

    Returns:
        Sanitized name (lowercase, alphanumeric and hyphens only)
    """
    if not name:
        return ''
    import re
    sanitized = re.sub(r'[^a-zA-Z0-9-]', '-', name.lower())
    sanitized = re.sub(r'-+', '-', sanitized)
    return sanitized.strip('-')
