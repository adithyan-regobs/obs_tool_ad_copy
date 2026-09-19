# config/tenant_config.py
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

class ResourceType(str, Enum):
    SQS = "sqs"
    SNS = "sns"
    S3 = "s3"
    DYNAMODB = "dynamodb"

@dataclass
class ParameterConfig:
    name: str
    mandatory: bool
    description: str
    allowed_values: Optional[List[str]] = None

@dataclass
class ResourceConfig:
    parameters: List[ParameterConfig]

# Example phrases per resource type (global, not tenant-specific)
RESOURCE_EXAMPLES: Dict[ResourceType, List[str]] = {
    ResourceType.SQS: ["create sqs", "new queue", "setup sqs queue", "create a queue", "add queue"],
    ResourceType.SNS: ["create sns", "new topic", "setup sns topic", "create a topic", "add topic"],
    ResourceType.S3: ["create s3", "new bucket", "setup s3 bucket", "create a bucket", "add bucket"],
    ResourceType.DYNAMODB: ["create dynamodb", "new table", "setup dynamodb table", "create a table", "add table"],
}

# Examples of parameter extraction per resource type
PARAMETER_EXTRACTION_EXAMPLES: Dict[ResourceType, List[str]] = {
    ResourceType.SQS: [
        '"environment=prod queue_type=FIFO"',
        '"fifo"',
        '"environment=stage queue_name=my-queue"',
        '"stage fifo core-messages"',
        '"queue_type=Standard environment=prod"',
    ],
    ResourceType.SNS: [
        '"environment=prod topic_name=alerts"',
        '"prod"',
        '"topic_name=user-events"',
        '"environment=stage topic_name=orders"',
        '"stage"',
    ],
    ResourceType.S3: [
        '"environment=prod bucket_name=my-bucket"',
        '"stage"',
        '"bucket_name=logs versioning_enabled=yes"',
        '"prod my-bucket"',
        '"bucket_name=backup-data"',
    ],
    ResourceType.DYNAMODB: [
        '"environment=prod table_name=users"',
        '"stage"',
        '"table_name=sessions"',
        '"environment=prod table_name=orders"',
        '"prod transactions"',
    ],
}

@dataclass
class ServiceConfig:
    """Service metadata for querying."""
    name: str
    cpu: int
    memory: int
    description: str

@dataclass
class TenantConfig:
    resources: Dict[ResourceType, ResourceConfig]
    services: List[str]  # List of service names for this tenant
    service_metadata: Dict[str, ServiceConfig]  # name -> metadata mapping
    users: Dict[str, str]  # Maps user_id to role (e.g., "admin@default" -> "admin")
    recommendation_tools: List[str]  # List of available recommendation tools
    recommendation_tool_parameter_list: Dict[str, List[str]]  # Maps tool name to its parameters

TENANT_CONFIG: Dict[str, TenantConfig] = {
    "aspora": TenantConfig(
        resources={
            ResourceType.S3: ResourceConfig(
                parameters=[
                    ParameterConfig("environment", True, "Deployment environment", ["dev", "stage", "prod"]),
                    ParameterConfig("bucket_name", True, "Name of the S3 bucket"),
                    ParameterConfig("versioning_enabled", False, "Enable versioning", ["yes", "no"]),
                ]
            ),
        },
        recommendation_tools=[],
        recommendation_tool_parameter_list={},
        services=[],
        service_metadata={},
        users={
            "admin@aspora": "admin",
            "af7c5d64-2744-430a-893f-9cf3ddbaf5b8": "admin",  # Test user from database
        }
    ),
    "default": TenantConfig(
        resources={
            ResourceType.SQS: ResourceConfig(
                parameters=[
                    ParameterConfig("environment", True, "Deployment environment", ["stage", "prod"]),
                    ParameterConfig("queue_type", True, "Queue type", ["Standard", "FIFO"]),
                    ParameterConfig("queue_name", True, "Name of the SQS queue"),
                    ParameterConfig("dlq_enabled", False, "Enable Dead Letter Queue", ["yes", "no"]),
                ]
            ),
            ResourceType.SNS: ResourceConfig(
                parameters=[
                    ParameterConfig("environment", True, "Deployment environment", ["stage", "prod"]),
                    ParameterConfig("topic_name", True, "Name of the SNS topic"),
                ]
            ),
            ResourceType.S3: ResourceConfig(
                parameters=[
                    ParameterConfig("environment", True, "Deployment environment", ["stage", "prod"]),
                    ParameterConfig("bucket_name", True, "Name of the S3 bucket"),
                    ParameterConfig("versioning_enabled", False, "Enable versioning", ["yes", "no"]),
                ]
            ),
            ResourceType.DYNAMODB: ResourceConfig(
                parameters=[
                    ParameterConfig("environment", True, "Deployment environment", ["stage", "prod"]),
                    ParameterConfig("table_name", True, "Name of the DynamoDB table"),
                ]
            ),
        },
        recommendation_tools=[
            "list_services",
            "list_config_of_service",
            "list_a_param_of_service"
        ],
        recommendation_tool_parameter_list={
            "list_services": [],
            "list_config_of_service": ["service_name"],
            "list_a_param_of_service": ["service_name", "parameter_name"]
        },
        services=[
            "user-api",
            "user-worker",
            "status-worker",
            "order-processor",
            "order-handler",
            "notification-service",
            "auth-service",
        ],
        service_metadata={
            "user-api": ServiceConfig(
                name="user-api",
                cpu=4,
                memory=8,
                description="User authentication and profile management API"
            ),
            "user-worker": ServiceConfig(
                name="user-worker",
                cpu=2,
                memory=4,
                description="Background worker for user data processing"
            ),
            "status-worker": ServiceConfig(
                name="status-worker",
                cpu=2,
                memory=4,
                description="Background worker for processing status updates"
            ),
            "order-processor": ServiceConfig(
                name="order-processor",
                cpu=8,
                memory=16,
                description="High-throughput order processing service"
            ),
            "order-handler": ServiceConfig(
                name="order-handler",
                cpu=4,
                memory=8,
                description="Order validation and routing handler"
            ),
            "notification-service": ServiceConfig(
                name="notification-service",
                cpu=2,
                memory=4,
                description="Email and push notification service"
            ),
            "auth-service": ServiceConfig(
                name="auth-service",
                cpu=4,
                memory=8,
                description="OAuth2 and JWT authentication service"
            ),
        },
        users={
            "admin@default": "admin",
            "viewer@default": "viewer",
            "test_user": "admin",  # TODO: Remove after testing
        }
    ),
    "fintech-prod": TenantConfig(
        resources={
            ResourceType.SQS: ResourceConfig(
                parameters=[
                    ParameterConfig("environment", True, "Environment (prod only)", ["prod"]),
                    ParameterConfig("queue_type", True, "FIFO enforced in prod", ["FIFO"]),
                    ParameterConfig("queue_name", True, "Queue name"),
                    ParameterConfig("dlq_enabled", True, "DLQ mandatory in prod", ["yes"]),
                ]
            )
        },
        recommendation_tools=[
            "list_services",
            "list_config_of_service",
            "list_a_param_of_service"
        ],
        recommendation_tool_parameter_list={
            "list_services": [],
            "list_config_of_service": ["service_name"],
            "list_a_param_of_service": ["service_name", "parameter_name"]
        },
        services=[
            "transaction-processor",
            "transaction-handler",
            "payment-worker",
            "payment-service",
            "fraud-detector",
            "ledger-writer",
        ],
        service_metadata={
            "transaction-processor": ServiceConfig(
                name="transaction-processor",
                cpu=16,
                memory=32,
                description="High-volume transaction processing engine"
            ),
            "transaction-handler": ServiceConfig(
                name="transaction-handler",
                cpu=8,
                memory=16,
                description="Transaction validation and routing handler"
            ),
            "payment-worker": ServiceConfig(
                name="payment-worker",
                cpu=8,
                memory=16,
                description="Payment gateway integration worker"
            ),
            "payment-service": ServiceConfig(
                name="payment-service",
                cpu=4,
                memory=8,
                description="Payment API and service interface"
            ),
            "fraud-detector": ServiceConfig(
                name="fraud-detector",
                cpu=4,
                memory=8,
                description="Real-time fraud detection service"
            ),
            "ledger-writer": ServiceConfig(
                name="ledger-writer",
                cpu=4,
                memory=8,
                description="Accounting ledger persistence service"
            ),
        },
        users={
            "admin@fintech": "admin",
            "viewer@fintech": "viewer",
        }
    )
}

def get_resource_config(tenant: str, resource: ResourceType) -> ResourceConfig:
    """
    Resolve resource config for tenant, fallback to default.
    """
    tenant_cfg = TENANT_CONFIG.get(tenant, TENANT_CONFIG["default"])
    if resource in tenant_cfg.resources:
        return tenant_cfg.resources[resource]
    return TENANT_CONFIG["default"].resources[resource]

def get_next_param(tenant: str, resource: ResourceType, collected: dict) -> Optional[ParameterConfig]:
    cfg = get_resource_config(tenant, resource)

    for p in cfg.parameters:
        if p.mandatory and p.name not in collected:
            return p

    for p in cfg.parameters:
        if p.name not in collected:
            return p

    return None

def search_services(tenant: str, query: str) -> tuple[List[ServiceConfig], bool]:
    """
    Search for services using fuzzy matching.

    Returns:
        (matches, is_exact): List of matching ServiceConfig and whether it's an exact match
    """
    tenant_cfg = TENANT_CONFIG.get(tenant, TENANT_CONFIG["default"])
    query_lower = query.lower().strip()

    # Try exact match first
    if query_lower in tenant_cfg.service_metadata:
        return [tenant_cfg.service_metadata[query_lower]], True

    # Fuzzy matching - calculate score for each service
    from difflib import SequenceMatcher

    matches = []
    for service_name, service_cfg in tenant_cfg.service_metadata.items():
        # Calculate similarity ratio
        similarity = SequenceMatcher(None, query_lower, service_name.lower()).ratio()

        # Check if query is a substring of service name (stronger signal)
        is_substring = query_lower in service_name.lower()
        substring_bonus = 0.3 if is_substring else 0

        # Check if service name starts with query (prefix match - even stronger signal)
        prefix_bonus = 0.3 if service_name.lower().startswith(query_lower) else 0

        # Combined score
        score = similarity + substring_bonus + prefix_bonus

        # Lower threshold for substring/prefix matches
        threshold = 0.4 if (is_substring or prefix_bonus) else 0.6

        if score >= threshold:
            matches.append((service_cfg, score))

    # Sort by score descending
    matches.sort(key=lambda x: x[1], reverse=True)

    # Return top matches (max 3) and indicate not exact
    return [m[0] for m in matches[:3]], False

def get_service_config(tenant: str, service_name: str) -> Optional[ServiceConfig]:
    """
    Get service config by exact name.
    """
    tenant_cfg = TENANT_CONFIG.get(tenant, TENANT_CONFIG["default"])
    return tenant_cfg.service_metadata.get(service_name)
