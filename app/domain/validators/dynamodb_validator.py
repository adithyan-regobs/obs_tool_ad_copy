"""
DynamoDB Configuration Validator

Thin wrapper kept for its callers. The rules themselves live in
app/domain/validators/infra_config_validator, which every write path reaches
through InfrastructureCreationService — the HTTP upsert, the Slack handlers and
the devlift_mcp dispatcher alike.

The scope note that used to sit here said validating in the shared service
"would change behaviour" for the Slack and MCP callers. That was the bug: they
were the paths with no validation at all.
"""

from app.domain.validators.infra_config_validator import (
    DYNAMODB_ATTRIBUTE_PATTERN,
    DYNAMODB_IDENTIFIER_PATTERN,
    InfraConfigError,
    validate_config as _validate_infra_config,
    validate_dynamodb_identifier,
    validate_dynamodb_partition_key,
)

DYNAMODB_INFRA_TYPE = "dynamodb_infrastructuretype_ref"


class DynamoDbValidationError(InfraConfigError):
    """Raised when a DynamoDB configuration fails validation."""


class DynamoDbValidator:
    """Validator for DynamoDB table naming rules."""

    IDENTIFIER_PATTERN = DYNAMODB_IDENTIFIER_PATTERN
    ATTRIBUTE_PATTERN = DYNAMODB_ATTRIBUTE_PATTERN

    @staticmethod
    def validate_identifier(identifier: str) -> None:
        validate_dynamodb_identifier(identifier)

    @staticmethod
    def validate_partition_key(partition_key: str) -> None:
        validate_dynamodb_partition_key(partition_key)

    @staticmethod
    def validate_config(config: dict, *, require_all: bool) -> None:
        _validate_infra_config(config, DYNAMODB_INFRA_TYPE, require_all=require_all)
