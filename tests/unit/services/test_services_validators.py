"""
Unit tests for services_mst_rules validators
"""
import pytest
from app.domain.validators.services_mst_rules import (
    ServicesMstValidator,
    ServiceValidationError
)
from app.core.enum import ServiceTypeEnum


class TestServiceNameValidator:
    """Test cases for validate_service_name"""

    def test_valid_service_name(self):
        """Test validation passes for valid service name"""
        # Should not raise exception
        ServicesMstValidator.validate_service_name("Payment API")

    def test_empty_service_name_raises_error(self):
        """Test validation fails for empty service name"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_service_name("")

        assert "service_name cannot be empty" in str(exc_info.value)

    def test_whitespace_only_service_name_raises_error(self):
        """Test validation fails for whitespace-only service name"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_service_name("   ")

        assert "service_name cannot be empty" in str(exc_info.value)

    def test_service_name_exceeds_max_length_raises_error(self):
        """Test validation fails for service name exceeding 255 characters"""
        long_name = "A" * 256

        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_service_name(long_name)

        assert "service_name cannot exceed 255 characters" in str(exc_info.value)

    def test_service_name_at_max_length_passes(self):
        """Test validation passes for service name at exactly 255 characters"""
        max_length_name = "A" * 255

        # Should not raise exception
        ServicesMstValidator.validate_service_name(max_length_name)


class TestResourceGroupCodeValidator:
    """Test cases for validate_resource_group_code"""

    def test_valid_resource_group_code(self):
        """Test validation passes for valid resource group code"""
        # Should not raise exception
        ServicesMstValidator.validate_resource_group_code("prod_rg")

    def test_empty_resource_group_code_raises_error(self):
        """Test validation fails for empty resource group code"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_resource_group_code("")

        assert "resource_group_code cannot be empty" in str(exc_info.value)

    def test_whitespace_only_resource_group_code_raises_error(self):
        """Test validation fails for whitespace-only resource group code"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_resource_group_code("   ")

        assert "resource_group_code cannot be empty" in str(exc_info.value)


class TestServiceTypeValidator:
    """Test cases for validate_service_type"""

    def test_valid_api_service_type(self):
        """Test validation passes for API service type"""
        # Should not raise exception
        ServicesMstValidator.validate_service_type(ServiceTypeEnum.API)

    def test_valid_background_service_type(self):
        """Test validation passes for Background Service type"""
        # Should not raise exception
        ServicesMstValidator.validate_service_type(ServiceTypeEnum.BACKGROUND_SERVICE)


class TestCreateServiceRequestValidator:
    """Test cases for validate_create_service_request (composite validator)"""

    def test_valid_create_service_request(self):
        """Test validation passes for completely valid request"""
        # Should not raise exception
        ServicesMstValidator.validate_create_service_request(
            tenant_code="test_tenant",
            application_code="test_app",
            resource_group_code="test_rg",
            service_name="Payment API",
            service_type=ServiceTypeEnum.API
        )

    def test_invalid_tenant_code_raises_error(self):
        """Test validation fails for empty tenant code"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_create_service_request(
                tenant_code="",
                application_code="test_app",
                resource_group_code="test_rg",
                service_name="Payment API",
                service_type=ServiceTypeEnum.API
            )

        assert "tenant_code cannot be empty" in str(exc_info.value)

    def test_invalid_application_code_raises_error(self):
        """Test validation fails for empty application code"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_create_service_request(
                tenant_code="test_tenant",
                application_code="   ",
                resource_group_code="test_rg",
                service_name="Payment API",
                service_type=ServiceTypeEnum.API
            )

        assert "application_code cannot be empty" in str(exc_info.value)

    def test_invalid_resource_group_code_raises_error(self):
        """Test validation fails for empty resource group code"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_create_service_request(
                tenant_code="test_tenant",
                application_code="test_app",
                resource_group_code="",
                service_name="Payment API",
                service_type=ServiceTypeEnum.API
            )

        assert "resource_group_code cannot be empty" in str(exc_info.value)

    def test_invalid_service_name_raises_error(self):
        """Test validation fails for empty service name"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_create_service_request(
                tenant_code="test_tenant",
                application_code="test_app",
                resource_group_code="test_rg",
                service_name="",
                service_type=ServiceTypeEnum.API
            )

        assert "service_name cannot be empty" in str(exc_info.value)

    def test_multiple_validation_errors_accumulated(self):
        """Test that multiple validation errors are accumulated and returned together"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_create_service_request(
                tenant_code="",
                application_code="",
                resource_group_code="",
                service_name="",
                service_type=ServiceTypeEnum.API
            )

        error_message = str(exc_info.value)
        # All errors should be present
        assert "tenant_code cannot be empty" in error_message
        assert "application_code cannot be empty" in error_message
        assert "resource_group_code cannot be empty" in error_message
        assert "service_name cannot be empty" in error_message

    def test_validation_error_has_errors_list(self):
        """Test that ServiceValidationError contains list of individual errors"""
        with pytest.raises(ServiceValidationError) as exc_info:
            ServicesMstValidator.validate_create_service_request(
                tenant_code="",
                application_code="",
                resource_group_code="test_rg",
                service_name="Test",
                service_type=ServiceTypeEnum.API
            )

        # Check that errors list is accessible
        assert hasattr(exc_info.value, 'errors')
        assert len(exc_info.value.errors) == 2
        assert "tenant_code cannot be empty" in exc_info.value.errors
        assert "application_code cannot be empty string" in exc_info.value.errors
