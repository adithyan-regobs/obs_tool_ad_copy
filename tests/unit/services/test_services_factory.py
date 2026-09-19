"""
Unit tests for services_mst_factory
"""
import pytest
from app.domain.factories.services_mst_factory import make_service
from app.schemas.service_schemas import CreateServiceRequest
from app.core.enum import ServiceTypeEnum


class TestMakeService:
    """Test cases for make_service factory function"""

    def test_make_service_with_api_type(self):
        """Test creating service data with API type"""
        # Arrange
        request = CreateServiceRequest(
            tenant_code="test_tenant",
            application_code="test_app",
            resource_group_code="test_rg",
            service_name="Payment API",
            service_type=ServiceTypeEnum.API,
            description="Handles payment processing",
            is_active=True
        )

        # Act
        service_data = make_service(request)

        # Assert
        assert service_data["name"] == "Payment API"
        assert service_data["description"] == "Handles payment processing"
        assert service_data["tenants_mst_code"] == "test_tenant"
        assert service_data["applications_mst_code"] == "test_app"
        assert service_data["resource_group_mst_code"] == "test_rg"
        assert service_data["service_type"] == ServiceTypeEnum.API
        assert service_data["is_active"] is True
        assert service_data["is_deleted"] is False
        assert "code" in service_data
        assert len(service_data["code"]) == 36  # UUID format

    def test_make_service_with_background_service_type(self):
        """Test creating service data with Background Service type"""
        # Arrange
        request = CreateServiceRequest(
            tenant_code="test_tenant",
            application_code="test_app",
            resource_group_code="test_rg",
            service_name="Email Worker",
            service_type=ServiceTypeEnum.BACKGROUND_SERVICE,
            description="Sends email notifications",
            is_active=True
        )

        # Act
        service_data = make_service(request)

        # Assert
        assert service_data["name"] == "Email Worker"
        assert service_data["service_type"] == ServiceTypeEnum.BACKGROUND_SERVICE

    def test_make_service_without_description(self):
        """Test creating service data without description uses default"""
        # Arrange
        request = CreateServiceRequest(
            tenant_code="test_tenant",
            application_code="test_app",
            resource_group_code="test_rg",
            service_name="Test Service",
            service_type=ServiceTypeEnum.API,
            is_active=True
        )

        # Act
        service_data = make_service(request)

        # Assert
        assert service_data["description"] == "Service: Test Service"

    def test_make_service_generates_unique_codes(self):
        """Test that factory generates unique service codes"""
        # Arrange
        request = CreateServiceRequest(
            tenant_code="test_tenant",
            application_code="test_app",
            resource_group_code="test_rg",
            service_name="Test Service",
            service_type=ServiceTypeEnum.API,
            is_active=True
        )

        # Act
        service_data_1 = make_service(request)
        service_data_2 = make_service(request)

        # Assert
        assert service_data_1["code"] != service_data_2["code"]

    def test_make_service_with_inactive_status(self):
        """Test creating service data with inactive status"""
        # Arrange
        request = CreateServiceRequest(
            tenant_code="test_tenant",
            application_code="test_app",
            resource_group_code="test_rg",
            service_name="Test Service",
            service_type=ServiceTypeEnum.API,
            is_active=False
        )

        # Act
        service_data = make_service(request)

        # Assert
        assert service_data["is_active"] is False

    def test_make_service_contains_all_required_fields(self):
        """Test that factory output contains all required database fields"""
        # Arrange
        request = CreateServiceRequest(
            tenant_code="test_tenant",
            application_code="test_app",
            resource_group_code="test_rg",
            service_name="Test Service",
            service_type=ServiceTypeEnum.API,
            is_active=True
        )

        # Act
        service_data = make_service(request)

        # Assert - verify all required fields are present
        required_fields = [
            "code",
            "name",
            "description",
            "tenants_mst_code",
            "applications_mst_code",
            "resource_group_mst_code",
            "service_type",
            "is_active",
            "is_deleted"
        ]
        for field in required_fields:
            assert field in service_data, f"Missing required field: {field}"
