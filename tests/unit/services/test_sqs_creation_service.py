"""
Unit tests for SqsCreationService validation methods.
"""
import pytest
from unittest.mock import AsyncMock

from app.services.sqs_creation_service import SqsCreationService
from app.infra_chat_agent.config.tools_enum.reference_enums import Environment


class TestValidateIdentifier:
    """Test SQS queue identifier validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = SqsCreationService(self.db)

    def test_empty_identifier_raises(self):
        with pytest.raises(ValueError, match="required and cannot be empty"):
            self.service._validate_identifier("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="required and cannot be empty"):
            self.service._validate_identifier("   ")

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="1-80 characters"):
            self.service._validate_identifier("a" * 81)

    def test_fifo_suffix_rejected(self):
        with pytest.raises(ValueError, match="must NOT include '.fifo' suffix"):
            self.service._validate_identifier("my-queue.fifo")

    def test_fifo_suffix_case_insensitive(self):
        with pytest.raises(ValueError, match="must NOT include '.fifo' suffix"):
            self.service._validate_identifier("my-queue.FIFO")

    def test_invalid_characters_rejected(self):
        with pytest.raises(ValueError, match="invalid characters"):
            self.service._validate_identifier("my queue!")

    def test_spaces_rejected(self):
        with pytest.raises(ValueError, match="invalid characters"):
            self.service._validate_identifier("my queue")

    def test_dots_rejected(self):
        with pytest.raises(ValueError, match="invalid characters"):
            self.service._validate_identifier("my.queue")

    def test_valid_simple_name(self):
        self.service._validate_identifier("my-queue")

    def test_valid_with_underscores(self):
        self.service._validate_identifier("order_events_queue")

    def test_valid_with_numbers(self):
        self.service._validate_identifier("queue-123")

    def test_valid_uppercase_allowed(self):
        self.service._validate_identifier("MyQueue")

    def test_valid_single_char(self):
        self.service._validate_identifier("q")

    def test_valid_max_length(self):
        self.service._validate_identifier("a" * 80)


class TestValidateOptionalIntParams:
    """Test optional integer parameter range validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = SqsCreationService(self.db)

    def test_visibility_timeout_below_min_raises(self):
        with pytest.raises(ValueError, match="visibility_timeout_seconds"):
            self.service._validate_optional_int_params(-1, None, None, None)

    def test_visibility_timeout_above_max_raises(self):
        with pytest.raises(ValueError, match="visibility_timeout_seconds"):
            self.service._validate_optional_int_params(43201, None, None, None)

    def test_visibility_timeout_valid_zero(self):
        self.service._validate_optional_int_params(0, None, None, None)

    def test_visibility_timeout_valid_max(self):
        self.service._validate_optional_int_params(43200, None, None, None)

    def test_max_receive_count_below_min_raises(self):
        with pytest.raises(ValueError, match="max_receive_count"):
            self.service._validate_optional_int_params(None, 0, None, None)

    def test_max_receive_count_above_max_raises(self):
        with pytest.raises(ValueError, match="max_receive_count"):
            self.service._validate_optional_int_params(None, 1001, None, None)

    def test_max_receive_count_valid(self):
        self.service._validate_optional_int_params(None, 5, None, None)

    def test_message_retention_below_min_raises(self):
        with pytest.raises(ValueError, match="message_retention_seconds"):
            self.service._validate_optional_int_params(None, None, 59, None)

    def test_message_retention_above_max_raises(self):
        with pytest.raises(ValueError, match="message_retention_seconds"):
            self.service._validate_optional_int_params(None, None, 1209601, None)

    def test_message_retention_valid(self):
        self.service._validate_optional_int_params(None, None, 345600, None)

    def test_dlq_retention_below_min_raises(self):
        with pytest.raises(ValueError, match="dlq_message_retention_seconds"):
            self.service._validate_optional_int_params(None, None, None, 59)

    def test_dlq_retention_above_max_raises(self):
        with pytest.raises(ValueError, match="dlq_message_retention_seconds"):
            self.service._validate_optional_int_params(None, None, None, 1209601)

    def test_dlq_retention_valid(self):
        self.service._validate_optional_int_params(None, None, None, 1209600)

    def test_all_none_passes(self):
        self.service._validate_optional_int_params(None, None, None, None)


class TestValidateCrossAccountIds:
    """Test cross-account ID validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = SqsCreationService(self.db)

    def test_invalid_too_short(self):
        with pytest.raises(ValueError, match="exactly 12 digits"):
            self.service._validate_cross_account_ids(["1234"])

    def test_invalid_too_long(self):
        with pytest.raises(ValueError, match="exactly 12 digits"):
            self.service._validate_cross_account_ids(["1234567890123"])

    def test_invalid_non_numeric(self):
        with pytest.raises(ValueError, match="exactly 12 digits"):
            self.service._validate_cross_account_ids(["12345678901a"])

    def test_valid_single_id(self):
        self.service._validate_cross_account_ids(["123456789012"])

    def test_valid_multiple_ids(self):
        self.service._validate_cross_account_ids(["123456789012", "919497413314"])

    def test_none_passes(self):
        self.service._validate_cross_account_ids(None)

    def test_empty_list_passes(self):
        self.service._validate_cross_account_ids([])


class TestValidatePlacementParams:
    """Test placement parameter validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = SqsCreationService(self.db)

    def test_empty_tenant_code_raises(self):
        with pytest.raises(ValueError, match="tenant_code is required"):
            self.service._validate_placement_params(
                "", "Core", Environment.QA, "region-aspora-mumbai"
            )

    def test_empty_product_name_raises(self):
        with pytest.raises(ValueError, match="product_name is required"):
            self.service._validate_placement_params(
                "vance", "", Environment.QA, "region-aspora-mumbai"
            )

    def test_invalid_geo_loc_code_raises(self):
        with pytest.raises(ValueError, match="Invalid geo_loc_code"):
            self.service._validate_placement_params(
                "vance", "Core", Environment.QA, "invalid-region"
            )

    def test_valid_placement_params(self):
        self.service._validate_placement_params(
            "vance", "Core", Environment.QA, "region-aspora-mumbai"
        )

    def test_valid_london_geo_loc(self):
        self.service._validate_placement_params(
            "aspora", "Falcon", Environment.PROD, "region-aspora-london"
        )


class TestCreateSqsQueueIntegration:
    """Test create_sqs_queue end-to-end validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = SqsCreationService(self.db)

    @pytest.mark.asyncio
    async def test_invalid_identifier_raises(self):
        with pytest.raises(ValueError, match="invalid characters"):
            await self.service.create_sqs_queue(
                identifier="invalid queue!",
                tenant_code="vance",
                product_name="Core",
                environment=Environment.QA,
                geo_loc_code="region-aspora-mumbai",
            )

    @pytest.mark.asyncio
    async def test_valid_defaults_returns_success(self):
        result = await self.service.create_sqs_queue(
            identifier="order-events",
            tenant_code="vance",
            product_name="Core",
            environment=Environment.QA,
            geo_loc_code="region-aspora-mumbai",
        )
        assert result["status"] == "success"
        assert result["is_ready"] is True
        assert result["attribute_parameters"]["identifier"] == "order-events"
        assert result["attribute_parameters"]["fifo_queue"] is True
        assert result["attribute_parameters"]["create_dlq"] is True
        assert "max_receive_count" not in result["attribute_parameters"]
        assert "cross_account_ids" not in result["attribute_parameters"]

    @pytest.mark.asyncio
    async def test_valid_with_all_optional_params(self):
        result = await self.service.create_sqs_queue(
            identifier="payment-processor",
            tenant_code="aspora",
            product_name="Falcon",
            environment=Environment.PROD,
            geo_loc_code="region-aspora-london",
            fifo_queue=False,
            create_dlq=True,
            max_receive_count=5,
            visibility_timeout_seconds=300,
            message_retention_seconds=345600,
            dlq_message_retention_seconds=1209600,
            cross_account_ids=["123456789012", "919497413314"],
        )
        assert result["status"] == "success"
        assert result["is_ready"] is True
        assert result["attribute_parameters"]["fifo_queue"] is False
        assert result["attribute_parameters"]["create_dlq"] is True
        assert result["attribute_parameters"]["max_receive_count"] == 5
        assert result["attribute_parameters"]["visibility_timeout_seconds"] == 300
        assert result["attribute_parameters"]["message_retention_seconds"] == 345600
        assert result["attribute_parameters"]["dlq_message_retention_seconds"] == 1209600
        assert result["attribute_parameters"]["cross_account_ids"] == ["123456789012", "919497413314"]
        assert result["placement_parameters"]["tenant_code"] == "aspora"
        assert result["placement_parameters"]["environment"] == "prod"

    @pytest.mark.asyncio
    async def test_invalid_visibility_timeout_raises(self):
        with pytest.raises(ValueError, match="visibility_timeout_seconds"):
            await self.service.create_sqs_queue(
                identifier="valid-queue",
                tenant_code="vance",
                product_name="Core",
                environment=Environment.QA,
                geo_loc_code="region-aspora-mumbai",
                visibility_timeout_seconds=50000,
            )

    @pytest.mark.asyncio
    async def test_invalid_cross_account_id_raises(self):
        with pytest.raises(ValueError, match="exactly 12 digits"):
            await self.service.create_sqs_queue(
                identifier="valid-queue",
                tenant_code="vance",
                product_name="Core",
                environment=Environment.QA,
                geo_loc_code="region-aspora-mumbai",
                cross_account_ids=["invalid"],
            )
