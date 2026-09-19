"""
DynamoDB Creation Service

Provides DynamoDB table operations for MCP server consumption.
All business-rule validation lives here (not in the MCP server or LLM prompts).
"""
import re
import logging
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
from app.domain.validators import infra_config_validator as infra_config

logger = logging.getLogger(__name__)

# The naming rules live in app/domain/validators/infra_config_validator, which
# the create flow enforces. Re-exported here so this module's callers keep the
# names they import — and so the chat flow cannot approve a table name the
# create then rejects, which is what happened while the cap here said 255 and
# the create flow said 200.
DYNAMODB_IDENTIFIER_PATTERN = infra_config.DYNAMODB_IDENTIFIER_PATTERN
PARTITION_KEY_PATTERN = infra_config.DYNAMODB_ATTRIBUTE_PATTERN

# Valid partition key types
VALID_PARTITION_KEY_TYPES = {"S", "N", "B"}

# Valid placement values (must match MCP tool schema enums)
VALID_ENVIRONMENTS = {"qa", "stage", "prod"}
VALID_GEO_LOC_CODES = {"region-aspora-mumbai", "region-aspora-london"}


class DynamoDbCreationService:
    """
    Service for DynamoDB table operations.

    Validates parameters and formats responses.
    Designed for MCP server consumption.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Private validation methods ──────────────────────────────────

    def _validate_identifier(self, identifier: str) -> None:
        """
        Validate DynamoDB table identifier against AWS naming conventions.

        Rules: 3-255 chars, letters, numbers, underscores, hyphens, dots.

        Raises:
            ValueError: If identifier is invalid.
        """
        if not identifier or not identifier.strip():
            raise ValueError(
                "identifier (table name) is required and cannot be empty."
            )

        infra_config.validate_dynamodb_identifier(identifier)

    def _validate_partition_key(self, partition_key: str) -> None:
        """
        Validate partition key attribute name.

        Raises:
            ValueError: If partition key is invalid.
        """
        if not partition_key or not partition_key.strip():
            raise ValueError(
                "partition_key is required and cannot be empty. "
                "This is the attribute name used as the table's partition key. "
                "Examples: 'user_id', 'order_id', 'event_name'"
            )

        infra_config.validate_dynamodb_partition_key(partition_key)

    def _validate_partition_key_type(self, partition_key_type: str) -> None:
        """
        Validate partition key type.

        Must be one of: S (String), N (Number), B (Binary).

        Raises:
            ValueError: If partition key type is invalid.
        """
        if not partition_key_type or not partition_key_type.strip():
            raise ValueError(
                "partition_key_type is required and cannot be empty. "
                "Must be one of: S (String), N (Number), B (Binary)."
            )

        if partition_key_type.strip().upper() not in VALID_PARTITION_KEY_TYPES:
            raise ValueError(
                f"Invalid partition_key_type: '{partition_key_type}'. "
                f"Must be one of: S (String), N (Number), B (Binary)."
            )

    def _validate_placement_params(
        self,
        tenant_code: str,
        product_name: str,
        environment: Environment,
        geo_loc_code: str,
    ) -> None:
        """
        Validate placement parameters for completeness and correctness.

        Raises:
            ValueError: If any placement parameter is missing or invalid.
        """
        if not tenant_code or not tenant_code.strip():
            raise ValueError("tenant_code is required and cannot be empty.")

        if not product_name or not product_name.strip():
            raise ValueError("product_name is required and cannot be empty.")

        if environment.value not in VALID_ENVIRONMENTS:
            raise ValueError(
                f"Invalid environment: '{environment.value}'. "
                f"Must be one of: {', '.join(sorted(VALID_ENVIRONMENTS))}"
            )

        if geo_loc_code not in VALID_GEO_LOC_CODES:
            raise ValueError(
                f"Invalid geo_loc_code: '{geo_loc_code}'. "
                f"Must be one of: {', '.join(sorted(VALID_GEO_LOC_CODES))}"
            )

    # ── Public methods ──────────────────────────────────────────────

    async def create_dynamodb_table(
        self,
        identifier: str,
        partition_key: str,
        partition_key_type: str,
        tenant_code: str,
        product_name: str,
        environment: Environment,
        geo_loc_code: str,
        geo_loc: str = "",
        applications_mst_code: Optional[str] = None,
        infra_vendor: str = "aws",
    ) -> Dict[str, Any]:
        """
        Validate DynamoDB table creation parameters and format response.

        Note: This does NOT save to database. Only validates and formats.

        Args:
            identifier: Table name/identifier
            partition_key: Partition key attribute name
            partition_key_type: Partition key type (S, N, or B)
            tenant_code: Tenant identifier
            product_name: Product name (e.g., 'Core', 'Falcon')
            environment: Environment enum
            geo_loc_code: Geographic location code

        Returns:
            Dict with attribute_parameters, placement_parameters, is_ready

        Raises:
            ValueError: If any parameter fails validation.
        """
        logger.info(
            "DYNAMODB CREATION - create_dynamodb_table",
            extra={
                "identifier": identifier,
                "partition_key": partition_key,
                "partition_key_type": partition_key_type,
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
            }
        )

        # Normalize partition_key_type: accept full forms (string→S, number→N, binary→B)
        _TYPE_ALIASES = {
            "STRING": "S", "STR": "S",
            "NUMBER": "N", "NUM": "N",
            "BINARY": "B", "BIN": "B",
        }
        normalized = partition_key_type.strip().upper()
        partition_key_type = _TYPE_ALIASES.get(normalized, normalized)

        # Validate all parameters — collect every error before raising
        errors = []

        try:
            self._validate_identifier(identifier)
        except ValueError as e:
            errors.append(str(e))

        try:
            self._validate_partition_key(partition_key)
        except ValueError as e:
            errors.append(str(e))

        try:
            self._validate_partition_key_type(partition_key_type)
        except ValueError as e:
            errors.append(str(e))

        try:
            self._validate_placement_params(tenant_code, product_name, environment, geo_loc_code)
        except ValueError as e:
            errors.append(str(e))

        if errors:
            raise ValueError(" | ".join(errors))

        # Build attribute_parameters (DynamoDB-specific)
        attribute_parameters = {
            "identifier": identifier,
            "partition_key": partition_key,
            "partition_key_type": partition_key_type,
        }

        # Build placement_parameters (environment/location context)
        placement_parameters = {
            "tenant_code": tenant_code,
            "product_name": product_name,
            "applications_mst_code": applications_mst_code or product_name,
            "environment_enum": environment.value,
            "geo_loc": geo_loc,
            "geo_loc_mst_code": geo_loc_code,
            "infra_vendor_enum": infra_vendor,
            "case_type_ref_code": "dynamodb",
            "case_ref_code": "table_management",
        }

        return {
            "status": "success",
            "message": f"DynamoDB table '{identifier}' creation request validated",
            "attribute_parameters": attribute_parameters,
            "placement_parameters": placement_parameters,
            "is_ready": True,
        }
