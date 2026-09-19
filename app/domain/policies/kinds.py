"""Resource-kind → IAM statement template mapping.

One entry per supported kind. ``Sid`` is stable per kind so that attach/detach
operations can locate the existing statement and upsert/remove ARNs in its
``Resource`` list.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class PolicyKind:
    name: str
    sid: str
    actions: List[str]


SECRETS_READ = PolicyKind(
    name="secrets-read",
    sid="TenantDefaultSecretsRead",
    actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
)

DYNAMODB_RW = PolicyKind(
    name="dynamodb-rw",
    sid="TenantDefaultDynamoDbRW",
    actions=[
        "dynamodb:GetItem",
        "dynamodb:BatchGetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
    ],
)

# S3 needs bucket-level (ListBucket / GetBucketLocation) and object-level
# (GetObject / PutObject / DeleteObject) actions. A single statement works
# because IAM evaluates (action, resource) compatibility per request — the
# caller just has to include both ARN forms (``arn:aws:s3:::bucket`` and
# ``arn:aws:s3:::bucket/*``) in the Resource list.
S3_RW = PolicyKind(
    name="s3-rw",
    sid="TenantDefaultS3RW",
    actions=[
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
    ],
)

SQS_RW = PolicyKind(
    name="sqs-rw",
    sid="TenantDefaultSqsRW",
    actions=[
        "sqs:ReceiveMessage",
        "sqs:SendMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:ChangeMessageVisibility",
    ],
)

REDIS_CONNECT = PolicyKind(
    name="redis-connect",
    sid="TenantDefaultRedisConnect",
    actions=["elasticache:Connect"],
)

# rds-db:connect ARN form:
#   arn:aws:rds-db:{region}:{account}:dbuser:{DbClusterResourceId}/iam_user
# DbClusterResourceId is AWS-generated (cluster-XXXXX) and only known after
# Terraform apply — set via Phase 2 webhook after the cluster is provisioned.
RDS_AURORA_CONNECT = PolicyKind(
    name="rds-aurora-connect",
    sid="TenantDefaultRdsAuroraConnect",
    actions=["rds-db:connect"],
)


SUPPORTED_KINDS: Dict[str, PolicyKind] = {
    SECRETS_READ.name: SECRETS_READ,
    DYNAMODB_RW.name: DYNAMODB_RW,
    S3_RW.name: S3_RW,
    SQS_RW.name: SQS_RW,
    REDIS_CONNECT.name: REDIS_CONNECT,
    RDS_AURORA_CONNECT.name: RDS_AURORA_CONNECT,
}


# Map infrastructuretype_ref_code / resource type hints to policy kinds.
# Unknown resources return None — the role component silently skips them.
_INFRA_TYPE_TO_KIND: Dict[str, str] = {
    "dynamodb_infrastructuretype_ref": DYNAMODB_RW.name,
    "s3_infrastructuretype_ref": S3_RW.name,
    "sqs_infrastructuretype_ref": SQS_RW.name,
    "elasticache_redis_infrastructuretype_ref": REDIS_CONNECT.name,
    "aurora_postgres_infrastructuretype_ref": RDS_AURORA_CONNECT.name,
    "aurora_mysql_infrastructuretype_ref": RDS_AURORA_CONNECT.name,
}


def get_kind_for_resource(
    infrastructuretype_ref_code: Optional[str] = None,
    *,
    is_secret: bool = False,
) -> Optional[PolicyKind]:
    """Return the policy kind for an infrastructure row or a secret.

    Secrets are not stored in ``infrastructure_mst`` so callers signal them
    explicitly via ``is_secret=True``.
    """
    if is_secret:
        return SECRETS_READ
    if infrastructuretype_ref_code is None:
        return None
    kind_name = _INFRA_TYPE_TO_KIND.get(infrastructuretype_ref_code)
    if kind_name is None:
        return None
    return SUPPORTED_KINDS[kind_name]


def build_statement(kind: PolicyKind, resource_arns: List[str]) -> Dict:
    """Build a single IAM policy statement for a kind + list of ARNs."""
    return {
        "Sid": kind.sid,
        "Effect": "Allow",
        "Action": list(kind.actions),
        "Resource": list(resource_arns),
    }
