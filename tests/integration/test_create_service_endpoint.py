"""
Integration tests for POST /api/v1/services/create-service endpoint
"""
import pytest
from httpx import AsyncClient
from app.core.enum import ServiceTypeEnum
import time


@pytest.mark.asyncio
class TestCreateServiceEndpoint:
    """Integration tests for create service API endpoint"""

    async def test_create_service_success_api_type(self, client: AsyncClient):
        """Test successful service creation with API type"""
        # Arrange
        timestamp = int(time.time() * 1000)
        payload = {
            "tenant_code": "techstart_inc",
            "application_code": "analytics_app",
            "resource_group_code": "analytics_resources",
            "service_name": f"Integration Test API {timestamp}",
            "service_type": "API",
            "description": "Test API service created via integration test",
            "is_active": True
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 200
        data = response.json()

        assert data["service_name"] == f"Integration Test API {timestamp}"
        assert data["description"] == "Test API service created via integration test"
        assert data["service_type"] == "API"
        assert data["tenant_code"] == "techstart_inc"
        assert data["application_code"] == "analytics_app"
        assert data["resource_group_code"] == "analytics_resources"
        assert data["is_active"] is True
        assert "service_code" in data
        assert "id" in data
        assert "created_at" in data
        assert data["message"] == "Service created successfully"

    async def test_create_service_success_background_service_type(self, client: AsyncClient):
        """Test successful service creation with Background Service type"""
        # Arrange
        timestamp = int(time.time() * 1000)
        payload = {
            "tenant_code": "techstart_inc",
            "application_code": "analytics_app",
            "resource_group_code": "payment_resources",
            "service_name": f"Integration Test Worker {timestamp}",
            "service_type": "BACKGROUND_SERVICE",
            "description": "Test background service",
            "is_active": True
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["service_type"] == "BACKGROUND_SERVICE"

    async def test_create_service_without_description(self, client: AsyncClient):
        """Test service creation without description uses default"""
        # Arrange
        timestamp = int(time.time() * 1000)
        service_name = f"Test Service No Desc {timestamp}"
        payload = {
            "tenant_code": "techstart_inc",
            "application_code": "analytics_app",
            "resource_group_code": "user_resources",
            "service_name": service_name,
            "service_type": "API"
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["description"] == f"Service: {service_name}"

    async def test_create_service_with_inactive_status(self, client: AsyncClient):
        """Test service creation with inactive status"""
        # Arrange
        timestamp = int(time.time() * 1000)
        payload = {
            "tenant_code": "techstart_inc",
            "application_code": "analytics_app",
            "resource_group_code": "analytics_resources",
            "service_name": f"Inactive Service {timestamp}",
            "service_type": "API",
            "is_active": False
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False

    async def test_create_service_missing_required_field(self, client: AsyncClient):
        """Test validation error when required field is missing"""
        # Arrange - missing service_name
        payload = {
            "tenant_code": "test_tenant",
            "application_code": "test_app",
            "resource_group_code": "test_rg",
            "service_type": "API"
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 422  # Pydantic validation error

    async def test_create_service_empty_tenant_code(self, client: AsyncClient):
        """Test validation error for empty tenant code"""
        # Arrange
        payload = {
            "tenant_code": "",
            "application_code": "test_app",
            "resource_group_code": "test_rg",
            "service_name": "Test Service",
            "service_type": "API"
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 422  # Pydantic validation error
        data = response.json()
        assert "detail" in data

    async def test_create_service_empty_service_name(self, client: AsyncClient):
        """Test validation error for empty service name"""
        # Arrange
        payload = {
            "tenant_code": "test_tenant",
            "application_code": "test_app",
            "resource_group_code": "test_rg",
            "service_name": "",
            "service_type": "API"
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 422  # Pydantic validation error

    async def test_create_service_invalid_service_type(self, client: AsyncClient):
        """Test validation error for invalid service type"""
        # Arrange
        payload = {
            "tenant_code": "test_tenant",
            "application_code": "test_app",
            "resource_group_code": "test_rg",
            "service_name": "Test Service",
            "service_type": "INVALID_TYPE"
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 422  # Pydantic validation error

    async def test_create_service_nonexistent_application(self, client: AsyncClient):
        """Test error when application doesn't exist"""
        # Arrange
        payload = {
            "tenant_code": "techstart_inc",
            "application_code": "nonexistent_app_code",
            "resource_group_code": "analytics_resources",
            "service_name": "Test Service",
            "service_type": "API"
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert - database constraint violation
        assert response.status_code == 500

    async def test_create_service_nonexistent_resource_group(self, client: AsyncClient):
        """Test error when resource group doesn't exist"""
        # Arrange
        payload = {
            "tenant_code": "techstart_inc",
            "application_code": "analytics_app",
            "resource_group_code": "nonexistent_rg_code",
            "service_name": "Test Service",
            "service_type": "API"
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert - database constraint violation
        assert response.status_code == 500

    async def test_create_service_long_description(self, client: AsyncClient):
        """Test validation error for description exceeding max length"""
        # Arrange
        payload = {
            "tenant_code": "techstart_inc",
            "application_code": "analytics_app",
            "resource_group_code": "analytics_resources",
            "service_name": "Test Service",
            "service_type": "API",
            "description": "A" * 501  # Exceeds 500 character limit
        }

        # Act
        response = await client.post("/api/v1/services/create-service", json=payload)

        # Assert
        assert response.status_code == 422  # Pydantic validation error

    async def test_create_service_unique_codes_generated(self, client: AsyncClient):
        """Test that multiple service creations generate unique codes"""
        # Arrange
        timestamp1 = int(time.time() * 1000)
        payload1 = {
            "tenant_code": "techstart_inc",
            "application_code": "analytics_app",
            "resource_group_code": "analytics_resources",
            "service_name": f"Unique Test Service 1 {timestamp1}",
            "service_type": "API"
        }

        timestamp2 = int(time.time() * 1000) + 1
        payload2 = {
            "tenant_code": "techstart_inc",
            "application_code": "analytics_app",
            "resource_group_code": "analytics_resources",
            "service_name": f"Unique Test Service 2 {timestamp2}",
            "service_type": "API"
        }

        # Act - create two services
        response1 = await client.post("/api/v1/services/create-service", json=payload1)
        response2 = await client.post("/api/v1/services/create-service", json=payload2)

        # Assert
        assert response1.status_code == 200
        assert response2.status_code == 200

        data1 = response1.json()
        data2 = response2.json()

        # Service codes should be different
        assert data1["service_code"] != data2["service_code"]
