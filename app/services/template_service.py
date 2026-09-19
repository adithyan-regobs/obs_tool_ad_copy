"""
Template Service for rendering Terragrunt configurations
"""
from pathlib import Path
import re
from typing import Optional
import aiofiles
from app.domain.validators.kong_route_validator import KongRouteValidator


class TemplateService:
    """Service for rendering infrastructure-as-code templates"""

    def __init__(self):
        """Initialize template service with templates directory"""
        # templates directory is at project root/templates (not app/templates)
        self.templates_dir = Path(__file__).parent.parent.parent / "templates"

    def render_s3_terragrunt(
        self,
        identifier: str,
        versioning: bool = False,
        enable_s3_replication: bool = False,
        cross_account_account_id: Optional[str] = None
    ) -> str:
        """
        Render S3 Terragrunt preview for chat display.

        Returns a formatted HCL preview showing the S3 bucket configuration
        with user-configured values highlighted and auto-configured values noted.

        Args:
            identifier: Bucket identifier/name (e.g., "customer-data", "app-logs")
            versioning: Enable versioning on the bucket (default: False)
            enable_s3_replication: Enable cross-account S3 replication (default: False)
            cross_account_account_id: AWS account ID for replication (only needed if replication enabled)

        Returns:
            str: Formatted HCL preview for chat display

        Raises:
            ValueError: If identifier is empty or invalid

        Example:
            >>> service = TemplateService()
            >>> preview = service.render_s3_terragrunt("customer-data")
            >>> # Returns HCL-style preview showing inputs block
        """
        # Validate identifier
        if not identifier or not identifier.strip():
            raise ValueError("Identifier cannot be empty")

        # Clean identifier (remove spaces, lowercase, replace special chars)
        identifier = identifier.strip().lower().replace(" ", "-")

        # Build optional parameter lines - only include if non-default
        optional_lines = []
        if versioning:  # Only show if true (non-default)
            optional_lines.append('  versioning               = true')
        if enable_s3_replication:  # Only show if true (non-default)
            optional_lines.append('  enable_s3_replication    = true')
        if cross_account_account_id:
            optional_lines.append(f'  cross_account_account_id = "{cross_account_account_id}"')

        optional_section = ""
        if optional_lines:
            optional_section = "\n" + "\n".join(optional_lines)

        # Return Terraform-style HCL preview
        return f"""S3 Bucket Configuration:

inputs = {{
  identifier               = "{identifier}"{optional_section}

  # Auto-configured from environment
  organization = include.env.locals.organization
  env          = include.env.locals.env
  region       = include.env.locals.region
  index        = include.env.locals.index

  # Dependencies
  s3_access_logs_bucket_id = dependency.regional_bootstrap.outputs.s3_access_logs_bucket_id
  tags         = include.env.locals.tags
}}"""

    def render_sqs_terragrunt(
        self,
        identifier: str,
        create_dlq: bool = True,
        fifo_queue: bool = True,
        visibility_timeout_seconds: Optional[int] = None,
        max_receive_count: Optional[int] = None,
        message_retention_seconds: Optional[int] = None,
        dlq_message_retention_seconds: Optional[int] = None,
        cross_account_ids: Optional[list] = None
    ) -> str:
        """
        Render SQS Terragrunt preview for chat display.

        Returns a formatted HCL preview showing the SQS queue configuration
        with user-configured values highlighted and auto-configured values noted.

        Args:
            identifier: Queue identifier/name (e.g., "order-queue", "event-processor")
            create_dlq: Whether to create a dead letter queue (defaults to False)
            fifo_queue: Whether to create a FIFO queue (defaults to False for standard queue)
            visibility_timeout_seconds: Optional visibility timeout in seconds (0-43200)
            max_receive_count: Optional max receive count before moving to DLQ (1-1000)
            message_retention_seconds: Optional message retention in seconds (60-1209600, default: 345600 = 4 days)
            dlq_message_retention_seconds: Optional DLQ message retention in seconds (60-1209600, default: 1209600 = 14 days)
            cross_account_ids: Optional list of AWS account IDs for cross-account access

        Returns:
            str: Formatted HCL preview for chat display

        Raises:
            ValueError: If identifier is empty or invalid

        Example:
            >>> service = TemplateService()
            >>> # Standard queue without DLQ
            >>> preview = service.render_sqs_terragrunt("order-queue")
            >>> # FIFO queue with DLQ and optional parameters
            >>> preview = service.render_sqs_terragrunt(
            ...     "critical-events",
            ...     create_dlq=True,
            ...     fifo_queue=True,
            ...     visibility_timeout_seconds=300,
            ...     max_receive_count=3
            ... )
        """
        # Validate identifier
        if not identifier or not identifier.strip():
            raise ValueError("Identifier cannot be empty")

        # Clean identifier (remove spaces, lowercase, replace special chars)
        identifier = identifier.strip().lower().replace(" ", "-")

        # Convert Python booleans to HCL booleans (lowercase)
        create_dlq_str = str(create_dlq).lower()
        fifo_queue_str = str(fifo_queue).lower()

        # Build parameter lines conditionally
        param_lines = [
            f'  identifier   = "{identifier}"',
            f'  fifo_queue   = {fifo_queue_str}',
            f'  create_dlq   = {create_dlq_str}'
        ]

        # Add optional parameters only if provided
        if visibility_timeout_seconds is not None:
            param_lines.append(f'  visibility_timeout_seconds = {visibility_timeout_seconds}')
        if max_receive_count is not None:
            param_lines.append(f'  max_receive_count         = {max_receive_count}')
        if message_retention_seconds is not None:
            param_lines.append(f'  message_retention_seconds = {message_retention_seconds}')
        if dlq_message_retention_seconds is not None:
            param_lines.append(f'  dlq_message_retention_seconds = {dlq_message_retention_seconds}')
        if cross_account_ids:
            ids_formatted = ', '.join(f'"{aid}"' for aid in cross_account_ids)
            param_lines.append(f'  cross_account_ids            = [{ids_formatted}]')
            param_lines.append('  enable_cross_account_access = true')

        # Return Terraform-style HCL preview
        return f"""SQS Queue Configuration:

inputs = {{
{chr(10).join(param_lines)}

  # Auto-configured from environment
  organization = include.env.locals.organization
  env          = include.env.locals.env
  region       = include.env.locals.region

  # Alarms
  alarms_sns_topic_arn = dependency.slack_ops.outputs.sns_topic_arn
}}"""

    def render_dynamodb_terragrunt(self, identifier: str, partition_key: str, partition_key_type: str = "S") -> str:
        """
        Render DynamoDB Terragrunt preview for chat display.

        Returns a formatted HCL preview showing the DynamoDB table configuration
        with user-configured values highlighted and auto-configured values noted.

        Args:
            identifier: Table identifier/name (e.g., "user-sessions", "events")
            partition_key: Partition key attribute name (e.g., "user_id", "event_id")
            partition_key_type: Partition key type - S (String), N (Number), or B (Binary). Defaults to "S"

        Returns:
            str: Formatted HCL preview for chat display

        Raises:
            ValueError: If identifier or partition_key is empty, or partition_key_type is invalid

        Example:
            >>> service = TemplateService()
            >>> # String partition key (default)
            >>> preview = service.render_dynamodb_terragrunt("user-sessions", "user_id")
            >>> # Number partition key
            >>> preview = service.render_dynamodb_terragrunt("metrics", "timestamp", "N")
        """
        # Validate identifier
        if not identifier or not identifier.strip():
            raise ValueError("Identifier cannot be empty")

        # Validate partition key
        if not partition_key or not partition_key.strip():
            raise ValueError("Partition key cannot be empty")

        # Validate partition key type
        partition_key_type = partition_key_type.strip().upper()
        if partition_key_type not in ["S", "N", "B"]:
            raise ValueError(f"Partition key type must be S, N, or B. Got: {partition_key_type}")

        # Clean identifier (remove spaces, lowercase, replace special chars)
        identifier = identifier.strip().lower().replace(" ", "-")

        # Clean partition key (keep original casing for attribute names)
        partition_key = partition_key.strip()

        # Return Terraform-style HCL preview
        return f"""DynamoDB Table Configuration:

inputs = {{
  identifier            = "{identifier}"
  partition_key         = "{partition_key}"
  attributes            = [
                            {{
                              name = "{partition_key}"
                              type = "{partition_key_type}"
                            }}
                          ]

  # Auto-configured from environment
  organization          = include.env.locals.organization
  env                   = include.env.locals.env
  region                = include.env.locals.region
  index                 = include.env.locals.index
  deletion_protection   = include.env.locals.deletion_protection
  tags                  = include.env.locals.tags
}}"""

    def add_gateway_route(self, api_name: str, method: str, route: str) -> str:
        """
        Format a Kong Gateway route configuration for display.

        This method validates the route parameters and returns a formatted
        message showing the route configuration. The actual template file
        is not modified - this is for display purposes only.

        Validation performed:
        - Route must start with '~/' (Kong pattern requirement)
        - Route regex syntax must be valid
        - HTTP method must be valid (GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD)

        Args:
            api_name: Name of the Kong API (e.g., "partner-dashboard-api", "falcon-service-api")
            method: HTTP method (GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD)
            route: Kong route pattern (e.g., "~/api/v1/users$", "~/api/v1/orders/(?<id>[^/]+)$")

        Returns:
            str: Formatted message with route configuration details

        Raises:
            ValueError: If invalid parameters or invalid route format

        Example:
            >>> service = TemplateService()
            >>> result = service.add_gateway_route("partner-dashboard-api", "GET", "~/api/v1/users$")
            >>> # Returns formatted message showing the route configuration
        """
        # Validate inputs
        if not api_name or not api_name.strip():
            raise ValueError("API name cannot be empty")
        if not method or not method.strip():
            raise ValueError("HTTP method cannot be empty")
        if not route or not route.strip():
            raise ValueError("Route pattern cannot be empty")

        # Normalize method to uppercase
        method = method.strip().upper()
        api_name = api_name.strip()
        route = route.strip()

        # Use KongRouteValidator for all validation (single source of truth)
        KongRouteValidator.validate_http_method(method)
        KongRouteValidator.validate_api_name(api_name)
        KongRouteValidator.validate_route_path(route)

        # Format success message to show HCL structure
        return f"""Route configuration:

"{api_name}" = {{
  routes = {{
    "{method}" = ["{route}"]
  }}
}}"""

    def render_database_creation_terragrunt(
        self,
        database_name: str,
        db_server_name: str,
        db_type: Optional[str] = None
    ) -> str:
        """
        Render Database Creation Terragrunt preview for display.

        Shows the database array format based on the database type.
        If db_type is provided, shows only that type's array format.
        If db_type is None, shows a generic preview.

        Args:
            database_name: Name of the database to create
            db_server_name: Name of the database server
            db_type: Database type - 'mysql' or 'postgresql' (optional)

        Returns:
            str: Formatted HCL preview for display

        Raises:
            ValueError: If database_name or db_server_name is empty
        """
        if not database_name or not database_name.strip():
            raise ValueError("database_name cannot be empty")
        if not db_server_name or not db_server_name.strip():
            raise ValueError("db_server_name cannot be empty")

        database_name = database_name.strip()
        db_server_name = db_server_name.strip()

        # Normalize db_type
        if db_type:
            db_type = db_type.strip().lower()

        if db_type == 'mysql':
            return f"""Server: {db_server_name}

mysql_databases = [
    # ...existing databases,
    {database_name},
]"""

        elif db_type in ['postgresql', 'psql', 'postgres']:
            return f"""Server: {db_server_name}

psql_databases = [
    # ...existing databases,
    {database_name},
]"""

        else:
            # Generic preview when type is unknown
            return f"""

Server: {db_server_name}
Database: {database_name}

# Database type will be auto-detected from server configuration
# The database name will be added to the appropriate array:

mysql_databases = [
    # ...existing databases,
    "{database_name}"
]

# OR

psql_databases = [
    # ...existing databases,
    "{database_name}"
]"""

    async def get_available_gateway_apis(self) -> list[str]:
        """
        Get list of available Kong Gateway APIs from the configuration.

        Returns:
            list: List of available API names

        Example:
            ["partner-dashboard-api", "falcon-service-api", "eventbus-service", "omega-api"]
        """
        # Path to gateway template
        template_path = self.templates_dir / "terragrunt" / "gateway" / "terragrunt.hcl"

        if not template_path.exists():
            return []

        # Read template content
        async with aiofiles.open(template_path, 'r') as f:
            content = await f.read()

        # Find kong_configs block
        kong_configs_pattern = r'kong_configs\s*=\s*\{'
        kong_configs_match = re.search(kong_configs_pattern, content)

        if not kong_configs_match:
            return []

        # Extract all API names
        kong_configs_start = kong_configs_match.end()
        api_names_pattern = r'^\s{4}"([^"]+)"\s*=\s*\{'
        available_apis = re.findall(api_names_pattern, content[kong_configs_start:], re.MULTILINE)

        return available_apis

    def get_available_templates(self) -> dict:
        """
        Get list of available templates.

        Returns:
            dict: Available templates by resource type

        Example:
            {
                "s3": ["terragrunt.hcl"],
                "ec2": ["terragrunt.hcl"] (future)
            }
        """
        available = {}

        # Check for S3 template
        s3_template = self.templates_dir / "terragrunt" / "s3" / "terragrunt.hcl"
        if s3_template.exists():
            available["s3"] = ["terragrunt.hcl"]

        return available
