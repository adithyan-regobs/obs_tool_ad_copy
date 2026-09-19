"""Aspora tenant resource metadata — DEPRECATED.

Source of truth has moved to chat-bot-POC's tenant-keyed JSON metadata at
`backend/data/metadata/aspora/`. The legacy Python metadata is preserved as
a docstring at the bottom of this file for reference and easy revert.

IS_PAAS is retained because is_paas_tenant() in registry.py still reads it
to choose between PaaS (Jenkins) and Enterprise (GitOps PR) trigger response
shapes — chatbot does not currently surface this flag.
"""

# Aspora is an enterprise tenant — GitOps PR flow, no Jenkins, no polling.
IS_PAAS: bool = False

# Empty stub — registry.get_enabled_metadata returns nothing for aspora now;
# chatbot is the source of truth for the resource catalog.
RESOURCE_METADATA: dict = {}

__all__ = ["IS_PAAS", "RESOURCE_METADATA"]


_LEGACY_METADATA_FOR_REFERENCE = """
from app.mcp_servers.devlift_mcp.meta_data.default import (
    RESOURCE_METADATA as _DEFAULT_METADATA,
)

RESOURCE_METADATA: dict = {
    resource_type: entry
    for resource_type, entry in _DEFAULT_METADATA.items()
    if resource_type not in {"eks_service", "postgres_server"}
}

RESOURCE_METADATA["s3_bucket"] = {
    "label": "S3 Bucket",
    "description": (
        "Provision an AWS S3 bucket with optional object versioning and "
        "optional cross-region replication."
    ),
    "case_ref_code": "create_bucket",
    "infrastructuretype_ref_code": "s3_infrastructuretype_ref",
    "transaction_table": "infrastructure_mst",
    "identifier_field": "bucket_name",
    "fields": [
        {
            "name": "bucket_name",
            "canonical_name": "identifier",
            "type": "string",
            "required": True,
            "description": (
                "Globally unique S3 bucket name. Lowercase letters, numbers, "
                "dots, and hyphens only. 3-63 characters. No uppercase, "
                "spaces, or underscores."
            ),
        },
        {
            "name": "versioning",
            "canonical_name": "versioning",
            "type": "boolean",
            "required": True,
            "description": "Enable S3 object versioning.",
        },
        {
            "name": "replication",
            "canonical_name": "enable_s3_replication",
            "type": "boolean",
            "required": True,
            "description": "Enable S3 cross-region replication.",
        },
        {
            "name": "cross_account_id",
            "canonical_name": "cross_account_account_id",
            "type": "string",
            "required": False,
            "description": (
                "12-digit AWS account ID for the replication target. "
                "Only ask for this when replication is set to true; otherwise "
                "omit it entirely."
            ),
        },
    ],
}

RESOURCE_METADATA["dynamodb_table"] = {
    "label": "DynamoDB Table",
    "description": (
        "Provision an AWS DynamoDB table with a single partition key. "
        "Sort key and secondary indexes are not supported in this form."
    ),
    "case_ref_code": "table_management",
    "infrastructuretype_ref_code": "dynamodb_infrastructuretype_ref",
    "transaction_table": "infrastructure_mst",
    "identifier_field": "table_name",
    "fields": [
        {
            "name": "table_name",
            "canonical_name": "identifier",
            "type": "string",
            "required": True,
            "description": (
                "DynamoDB table name. Letters, numbers, underscores, "
                "dots, and hyphens; 3-255 characters."
            ),
        },
        {
            "name": "partition_key",
            "canonical_name": "partition_key",
            "type": "string",
            "required": True,
            "description": "Partition (hash) key attribute name.",
        },
        {
            "name": "partition_key_type",
            "canonical_name": "partition_key_type",
            "type": "string",
            "required": True,
            "description": (
                "Partition key data type. 'S' = String, 'N' = Number, "
                "'B' = Binary. Default 'S'."
            ),
            "options": [
                {"code": "S", "name": "String (S)"},
                {"code": "N", "name": "Number (N)"},
                {"code": "B", "name": "Binary (B)"},
            ],
        },
    ],
}

RESOURCE_METADATA["sqs_queue"] = {
    "label": "SQS Queue",
    "description": (
        "Provision an AWS SQS queue, optionally FIFO, optionally with a "
        "dead-letter queue and cross-account access."
    ),
    "case_ref_code": "create_queue",
    "infrastructuretype_ref_code": "sqs_infrastructuretype_ref",
    "transaction_table": "infrastructure_mst",
    "identifier_field": "queue_name",
    "fields": [
        {
            "name": "queue_name",
            "canonical_name": "identifier",
            "type": "string",
            "required": True,
            "description": (
                "SQS queue name. Letters, numbers, hyphens, and "
                "underscores; 1-80 characters. Do NOT include the "
                "'.fifo' suffix — it is appended automatically for "
                "FIFO queues."
            ),
        },
        {
            "name": "fifo_queue",
            "canonical_name": "fifo_queue",
            "type": "boolean",
            "required": True,
            "description": "FIFO queue (ordered, exactly-once). Default false.",
        },
        {
            "name": "create_dlq",
            "canonical_name": "create_dlq",
            "type": "boolean",
            "required": True,
            "description": "Create a companion dead-letter queue. Default false.",
        },
        {
            "name": "max_receive_count",
            "canonical_name": "max_receive_count",
            "type": "integer",
            "required": False,
            "description": (
                "Number of receive attempts before a message is moved "
                "to the DLQ. Only relevant when create_dlq is true."
            ),
        },
        {
            "name": "visibility_timeout_seconds",
            "canonical_name": "visibility_timeout_seconds",
            "type": "integer",
            "required": False,
            "description": "Visibility timeout in seconds (0-43200).",
        },
        {
            "name": "message_retention_seconds",
            "canonical_name": "message_retention_seconds",
            "type": "integer",
            "required": False,
            "description": "Main queue message retention in seconds (60-1209600).",
        },
        {
            "name": "dlq_message_retention_seconds",
            "canonical_name": "dlq_message_retention_seconds",
            "type": "integer",
            "required": False,
            "description": (
                "DLQ message retention in seconds (60-1209600). Only "
                "relevant when create_dlq is true."
            ),
        },
        {
            "name": "cross_account_ids",
            "canonical_name": "cross_account_ids",
            "type": "string_array",
            "required": False,
            "description": (
                "List of 12-digit AWS account IDs allowed to access "
                "the queue cross-account. Omit when not needed."
            ),
        },
    ],
}
"""
