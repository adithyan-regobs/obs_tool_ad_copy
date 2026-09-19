"""
Unit tests for S3CreationService validation methods.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.s3_creation_service import S3CreationService
from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
from app.core.enum import EnvironmentEnum


class TestValidateIdentifier:
    """Test S3 bucket identifier validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = S3CreationService(self.db)

    def test_empty_identifier_raises(self):
        with pytest.raises(ValueError, match="required and cannot be empty"):
            self.service._validate_identifier("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="required and cannot be empty"):
            self.service._validate_identifier("   ")

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="3-63 characters"):
            self.service._validate_identifier("ab")

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="3-63 characters"):
            self.service._validate_identifier("a" * 64)

    def test_uppercase_rejected(self):
        with pytest.raises(ValueError, match="lowercase"):
            self.service._validate_identifier("MyBucket")

    def test_underscore_rejected(self):
        with pytest.raises(ValueError, match="invalid characters"):
            self.service._validate_identifier("my_bucket")

    def test_consecutive_hyphens_rejected(self):
        with pytest.raises(ValueError, match="consecutive hyphens"):
            self.service._validate_identifier("my--bucket")

    def test_leading_hyphen_rejected(self):
        with pytest.raises(ValueError, match="invalid characters"):
            self.service._validate_identifier("-my-bucket")

    def test_trailing_hyphen_rejected(self):
        with pytest.raises(ValueError, match="invalid characters"):
            self.service._validate_identifier("my-bucket-")

    def test_valid_simple_name(self):
        self.service._validate_identifier("my-bucket")

    def test_valid_with_numbers(self):
        self.service._validate_identifier("app-logs-2024")

    def test_valid_min_length(self):
        self.service._validate_identifier("abc")

    def test_valid_max_length(self):
        self.service._validate_identifier("a" * 63)


class TestValidateReplicationParams:
    """Test replication + cross-account ID validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = S3CreationService(self.db)

    def test_replication_enabled_without_account_id_raises(self):
        with pytest.raises(ValueError, match="cross_account_account_id is required"):
            self.service._validate_replication_params(True, None)

    def test_replication_enabled_with_empty_account_id_raises(self):
        with pytest.raises(ValueError, match="cross_account_account_id is required"):
            self.service._validate_replication_params(True, "")

    def test_invalid_account_id_too_short(self):
        with pytest.raises(ValueError, match="exactly 12 digits"):
            self.service._validate_replication_params(False, "1234")

    def test_invalid_account_id_too_long(self):
        with pytest.raises(ValueError, match="exactly 12 digits"):
            self.service._validate_replication_params(False, "1234567890123")

    def test_invalid_account_id_non_numeric(self):
        with pytest.raises(ValueError, match="exactly 12 digits"):
            self.service._validate_replication_params(False, "12345678901a")

    def test_valid_replication_with_account_id(self):
        self.service._validate_replication_params(True, "123456789012")

    def test_account_id_without_replication(self):
        self.service._validate_replication_params(False, "123456789012")

    def test_no_replication_no_account_id(self):
        self.service._validate_replication_params(False, None)


class TestValidatePlacementParams:
    """Test placement parameter validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = S3CreationService(self.db)

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


class TestCheckDuplicateBucket:
    """Test duplicate bucket detection."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = S3CreationService(self.db)

    @pytest.mark.asyncio
    async def test_duplicate_bucket_raises(self):
        mock_existing = MagicMock()
        mock_existing.code = "INFRA_S3_ABC123"

        with patch(
            "app.repository.infrastructure_mst_repository.InfrastructureMstRepository.check_s3_bucket_exists",
            new_callable=AsyncMock,
            return_value=mock_existing,
        ):
            with pytest.raises(ValueError, match="already exists"):
                await self.service._check_duplicate_bucket(
                    "my-bucket", "vance", EnvironmentEnum("qa")
                )

    @pytest.mark.asyncio
    async def test_no_duplicate_passes(self):
        with patch(
            "app.repository.infrastructure_mst_repository.InfrastructureMstRepository.check_s3_bucket_exists",
            new_callable=AsyncMock,
            return_value=None,
        ):
            # Should not raise
            await self.service._check_duplicate_bucket(
                "new-bucket", "vance", EnvironmentEnum("qa")
            )


class TestCreateS3BucketIntegration:
    """Test create_s3_bucket end-to-end validation."""

    def setup_method(self):
        self.db = AsyncMock()
        self.service = S3CreationService(self.db)

    @pytest.mark.asyncio
    async def test_invalid_identifier_raises(self):
        with pytest.raises(ValueError, match="lowercase"):
            await self.service.create_s3_bucket(
                identifier="INVALID_NAME",
                tenant_code="vance",
                product_name="Core",
                environment=Environment.QA,
                geo_loc_code="region-aspora-mumbai",
            )

    @pytest.mark.asyncio
    async def test_replication_without_cross_account_raises(self):
        with pytest.raises(ValueError, match="cross_account_account_id is required"):
            await self.service.create_s3_bucket(
                identifier="valid-bucket",
                tenant_code="vance",
                product_name="Core",
                environment=Environment.QA,
                geo_loc_code="region-aspora-mumbai",
                enable_s3_replication=True,
            )

    @pytest.mark.asyncio
    async def test_valid_params_returns_success(self):
        with patch(
            "app.repository.infrastructure_mst_repository.InfrastructureMstRepository.check_s3_bucket_exists",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await self.service.create_s3_bucket(
                identifier="valid-bucket",
                tenant_code="vance",
                product_name="Core",
                environment=Environment.QA,
                geo_loc_code="region-aspora-mumbai",
            )
            assert result["status"] == "success"
            assert result["is_ready"] is True
            assert result["attribute_parameters"]["identifier"] == "valid-bucket"
            assert result["attribute_parameters"]["versioning"] is False
            assert result["attribute_parameters"]["enable_s3_replication"] is False

    @pytest.mark.asyncio
    async def test_valid_with_replication(self):
        with patch(
            "app.repository.infrastructure_mst_repository.InfrastructureMstRepository.check_s3_bucket_exists",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await self.service.create_s3_bucket(
                identifier="replicated-bucket",
                tenant_code="vance",
                product_name="Core",
                environment=Environment.QA,
                geo_loc_code="region-aspora-mumbai",
                enable_s3_replication=True,
                cross_account_account_id="123456789012",
            )
            assert result["status"] == "success"
            assert result["attribute_parameters"]["enable_s3_replication"] is True
            assert result["attribute_parameters"]["cross_account_account_id"] == "123456789012"
