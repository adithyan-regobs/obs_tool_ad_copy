"""
Locator -> derived variable value mapping.

An infrastructure resource's provisioning attributes live in
``infrastructure_mst.locator`` (a large JSONB with many fields). Reference
variables (S3_BUCKET_NAME, QUEUE_URL, AWS_REGION, …) derive their value from a
specific locator field. This module is the single source of truth for that
mapping, used by:
  - /project-variables/with-values  (resolve saved reference variables' values)
  - /infrastructure-mst/list        (pre-fill the Add-variable picker)

The frontend mirrors this map in AddVariablePicker.tsx only for typing; the
authoritative values are produced here.
"""
from typing import Dict, Optional

# Predefined reference-variable KEY -> ordered candidate fields in the resource's
# locator JSONB, per short resource type (the infrastructuretype_ref_code with the
# "_infrastructuretype_ref" suffix stripped). First present, non-empty candidate
# wins (locator field names vary across provisioners).
LOCATOR_FIELD_MAP: Dict[str, Dict[str, tuple]] = {
    "s3": {
        "S3_BUCKET_NAME": ("s3_bucket_name", "bucket_name"),
        "S3_BUCKET_ARN": ("s3_bucket_arn", "bucket_arn"),
    },
    "sqs": {
        "QUEUE_URL": ("queue_url",),
        "QUEUE_NAME": ("queue_name",),
        "QUEUE_ARN": ("queue_arn",),
        # DLQ fields — present in the locator only when create_dlq=true, so these
        # keys are emitted only for queues that actually have a dead-letter queue.
        "DLQ_URL": ("dlq_url",),
        "DLQ_NAME": ("dlq_name",),
        "DLQ_ARN": ("dlq_arn",),
    },
    "dynamodb": {
        "TABLE_NAME": ("table_name",),
        "DYNAMODB_TABLE_NAME": ("table_name",),
        "TABLE_ARN": ("table_arn",),
        "PARTITION_KEY": ("partition_key",),
    },
    "sns": {
        "TOPIC_ARN": ("topic_arn", "sns_topic_arn"),
        "TOPIC_NAME": ("topic_name",),
    },
    "secrets": {
        "SECRET_ARN": ("secret_arn",),
    },
    "ssm": {
        "PARAM_PREFIX": ("parameter_prefix", "param_prefix", "prefix"),
    },
    # Relational DBs share host/port/name shape (fields vary by provisioner).
    "rds": {
        "DB_HOST": ("host", "endpoint", "address", "server_name"),
        "DB_PORT": ("port",),
        "DB_NAME": ("db_name", "database", "server_name"),
    },
}
# Aurora engines reuse the relational-DB mapping.
for _alias in ("aurora", "aurora_mysql", "aurora_postgres"):
    LOCATOR_FIELD_MAP[_alias] = LOCATOR_FIELD_MAP["rds"]


def _short_type(type_ref: Optional[str]) -> Optional[str]:
    """s3_infrastructuretype_ref -> s3 (accepts already-short types too)."""
    if not type_ref:
        return None
    return type_ref.replace("_infrastructuretype_ref", "").lower()


def map_locator_to_values(type_ref: Optional[str], locator: Optional[dict]) -> Dict[str, str]:
    """Extract the predefined variable values from an infra resource's locator.

    Returns a ``{variable_key: value}`` map for the keys the UI derives from the
    resource (e.g. S3_BUCKET_NAME, QUEUE_URL, AWS_REGION). Unknown types or
    missing fields yield an empty / partial map — never raises.
    """
    if not isinstance(locator, dict):
        return {}
    field_map = LOCATOR_FIELD_MAP.get(_short_type(type_ref) or "")
    if not field_map:
        return {}
    out: Dict[str, str] = {}
    for var_key, candidates in field_map.items():
        for field in candidates:
            v = locator.get(field)
            if v not in (None, ""):
                out[var_key] = str(v)
                break
    return out
