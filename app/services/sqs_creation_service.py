"""
SQS Creation Service

Provides SQS queue operations for MCP server consumption.
Business rules are enforced here, never in the MCP server or LLM prompts; the
naming and range rules themselves are defined in
app/domain/validators/infra_config_validator so every write path shares them.
"""
import logging
from typing import Dict, Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
from app.core.enum import EnvironmentEnum
from app.domain.validators import infra_config_validator as infra_config

logger = logging.getLogger(__name__)

# The naming and range rules now live in one place — see
# app/domain/validators/infra_config_validator. Re-exported here so the legacy
# SQS MCP server, the chatbot executor and the devlift_mcp dispatcher keep
# importing the name they already import.
SQS_IDENTIFIER_PATTERN = infra_config.SQS_IDENTIFIER_PATTERN

# Valid placement values (must match MCP tool schema enums)
VALID_ENVIRONMENTS = {"qa", "stage", "prod"}
VALID_GEO_LOC_CODES = {"region-aspora-mumbai", "region-aspora-london"}


def validate_sqs_identifier(identifier: str) -> None:
    """Validate an SQS queue identifier. See infra_config_validator.

    InfraConfigError is a ValueError, so callers catching ValueError — the
    dispatcher and the chatbot executor both do — are unaffected.
    """
    infra_config.validate_sqs_identifier(identifier)


class SqsCreationService:
    """
    Service for SQS queue operations.

    Validates parameters and formats responses.
    Designed for MCP server consumption.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Private validation methods ──────────────────────────────────

    def _validate_identifier(self, identifier: str) -> None:
        """Validate the queue identifier — see `validate_sqs_identifier`."""
        validate_sqs_identifier(identifier)

    def _validate_optional_int_params(
        self,
        visibility_timeout_seconds: Optional[int],
        max_receive_count: Optional[int],
        message_retention_seconds: Optional[int],
        dlq_message_retention_seconds: Optional[int],
    ) -> None:
        """Range-check the optional SQS tuning fields. See infra_config_validator."""
        for field, value in (
            ("visibility_timeout_seconds", visibility_timeout_seconds),
            ("max_receive_count", max_receive_count),
            ("message_retention_seconds", message_retention_seconds),
            ("dlq_message_retention_seconds", dlq_message_retention_seconds),
        ):
            if value is not None:
                infra_config.validate_int_range(value, field=field)

    def _validate_cross_account_ids(
        self,
        cross_account_ids: Optional[List[str]],
    ) -> None:
        """Each must be exactly 12 digits. See infra_config_validator."""
        infra_config.validate_cross_account_ids(cross_account_ids)

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

    async def create_sqs_queue(
        self,
        identifier: str,
        tenant_code: str,
        product_name: str,
        environment: Environment,
        geo_loc_code: str,
        geo_loc: str = "",
        applications_mst_code: Optional[str] = None,
        infra_vendor: str = "aws",
        fifo_queue: bool = True,
        create_dlq: bool = True,
        max_receive_count: Optional[int] = None,
        visibility_timeout_seconds: Optional[int] = None,
        message_retention_seconds: Optional[int] = None,
        dlq_message_retention_seconds: Optional[int] = None,
        cross_account_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Validate SQS queue creation parameters and format response.

        Note: This does NOT save to database. Only validates and formats.

        Args:
            identifier: Queue name/identifier
            tenant_code: Tenant identifier
            product_name: Product name (e.g., 'Core', 'Falcon')
            environment: Environment enum
            geo_loc_code: Geographic location code
            fifo_queue: FIFO queue (default: true)
            create_dlq: Create dead letter queue (default: true)
            max_receive_count: Max receive count before DLQ
            visibility_timeout_seconds: Visibility timeout in seconds
            message_retention_seconds: Main queue message retention in seconds
            dlq_message_retention_seconds: DLQ message retention in seconds
            cross_account_ids: List of 12-digit AWS account IDs

        Returns:
            Dict with attribute_parameters, placement_parameters, is_ready

        Raises:
            ValueError: If any parameter fails validation.
        """
        logger.info(
            "SQS CREATION - create_sqs_queue",
            extra={
                "identifier": identifier,
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
                "fifo_queue": fifo_queue,
                "create_dlq": create_dlq,
            }
        )

        # Validate all parameters
        self._validate_identifier(identifier)
        self._validate_optional_int_params(
            visibility_timeout_seconds,
            max_receive_count,
            message_retention_seconds,
            dlq_message_retention_seconds,
        )
        self._validate_cross_account_ids(cross_account_ids)
        self._validate_placement_params(tenant_code, product_name, environment, geo_loc_code)

        # Build attribute_parameters (SQS-specific)
        attribute_parameters = {
            "identifier": identifier,
            "fifo_queue": fifo_queue,
            "create_dlq": create_dlq,
        }
        if max_receive_count is not None:
            attribute_parameters["max_receive_count"] = max_receive_count
        if visibility_timeout_seconds is not None:
            attribute_parameters["visibility_timeout_seconds"] = visibility_timeout_seconds
        if message_retention_seconds is not None:
            attribute_parameters["message_retention_seconds"] = message_retention_seconds
        if dlq_message_retention_seconds is not None:
            attribute_parameters["dlq_message_retention_seconds"] = dlq_message_retention_seconds
        if cross_account_ids:
            attribute_parameters["cross_account_ids"] = cross_account_ids

        # Build placement_parameters (environment/location context)
        placement_parameters = {
            "tenant_code": tenant_code,
            "product_name": product_name,
            "applications_mst_code": applications_mst_code or product_name,
            "environment_enum": environment.value,
            "geo_loc": geo_loc,
            "geo_loc_mst_code": geo_loc_code,
            "infra_vendor_enum": infra_vendor,
            "case_type_ref_code": "sqs",
            "case_ref_code": "create_queue",
        }

        return {
            "status": "success",
            "message": f"SQS queue '{identifier}' creation request validated",
            "attribute_parameters": attribute_parameters,
            "placement_parameters": placement_parameters,
            "is_ready": True,
        }
