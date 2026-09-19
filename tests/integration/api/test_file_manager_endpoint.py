"""
Integration tests for File Manager API endpoint

These tests make REAL calls to the database and S3 to verify:
- Database queries for queue items
- S3 presigned URL generation
- Full request/response cycle

Requirements:
- Database connection configured
- S3 upload credentials configured in .env
- Test queue items exist in database with artifact_s3_key

To run ONLY these integration tests:
    pytest tests/integration/api/test_file_manager_endpoint.py -v -s

To run all integration tests:
    pytest tests/integration/ -v

To skip integration tests (run only unit tests):
    pytest tests/unit/ -v
"""
import pytest
import json
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.services.file_manager_service import FileManagerService
from app.schemas.file_manager_schemas import FileTypeEnum
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel


@pytest.fixture
async def auth_headers():
    """Create mock authentication headers for testing"""
    # For testing, we'll override the auth dependency in tests
    return {}


@pytest.fixture
async def mock_user_tenant():
    """Create mock user and tenant for testing"""
    # This will be used to override the get_current_user_and_tenant dependency
    from unittest.mock import AsyncMock, MagicMock

    mock_user = AsyncMock(spec=UserMstModel)
    mock_user.id = 1
    mock_user.user_code = "test-user"
    mock_user.email = "test@example.com"

    mock_tenant = AsyncMock(spec=TenantsMstModel)
    mock_tenant.id = 1
    mock_tenant.tenant_code = "aspora"
    mock_tenant.tenant_name = "Aspora"

    return (mock_user, mock_tenant)


class TestFileManagerEndpointIntegration:
    """Integration tests for file manager API endpoint"""

    @pytest.mark.asyncio
    async def test_get_preview_hcl_presigned_url_success(
        self,
        db: AsyncSession,
        auth_headers
    ):
        """Test successful presigned URL generation for preview HCL file"""
        # Arrange - Create test queue item with artifact_s3_key
        test_queue_item = TransactionQueueModel(
            code="test-queue-preview-123",
            tenant_code="aspora",
            infra_type="sqs",
            environment="dev",
            artifact_s3_key=json.dumps({
                "original_s3_key": "sqs/test-queue.hcl",
                "preview": "preview/sqs/test-queue.hcl"
            })
        )
        db.add(test_queue_item)
        await db.commit()
        await db.refresh(test_queue_item)

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Get Preview HCL Presigned URL")
        print(f"{'=' * 80}")
        print(f"Queue Item ID: {test_queue_item.id}")
        print(f"Artifact S3 Key: {test_queue_item.artifact_s3_key}")

        # Act - Call the API endpoint
        from app.api.dependencies import get_db
        from app.core.config import settings

        # Create test client with auth headers
        client = TestClient(app)

        response = client.get(
            f"/api/v1/file-manager/view/{test_queue_item.id}",
            headers=auth_headers
        )

        # Assert - Verify response
        assert response.status_code == 200
        data = response.json()

        print(f"\n[OK] Response Status: {response.status_code}")
        print(f"[OK] Response Data:")
        print(f"     - URL: {data.get('url', 'N/A')[:100]}...")
        print(f"     - Key: {data.get('key')}")
        print(f"     - Bucket: {data.get('bucket_name')}")
        print(f"     - Expires In: {data.get('expires_in')} seconds")
        print(f"     - Method: {data.get('method')}")
        print(f"     - File Type: {data.get('file_type')}")

        # Verify response structure
        assert "url" in data
        assert "key" in data
        assert "bucket_name" in data
        assert "expires_in" in data
        assert "method" in data
        assert "file_type" in data

        # Verify values
        assert data["key"] == "preview/sqs/test-queue.hcl"
        assert data["file_type"] == "preview"
        assert data["method"] == "GET"
        assert data["expires_in"] == 3600
        assert settings.s3_upload_bucket in data["bucket_name"]

        # Verify URL is a valid presigned URL
        assert "X-Amz-Signature=" in data["url"] or "AWSAccessKeyId=" in data["url"]
        assert settings.s3_upload_bucket in data["url"]
        assert "preview/sqs/test-queue.hcl" in data["url"]

        print("\n[SUCCESS] Integration test PASSED - Presigned URL generated successfully!")

        # Cleanup
        await db.delete(test_queue_item)
        await db.commit()

        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_get_original_hcl_presigned_url(
        self,
        db: AsyncSession,
        auth_headers
    ):
        """Test getting presigned URL for original HCL file (not preview)"""
        # Arrange
        test_queue_item = TransactionQueueModel(
            code="test-queue-original-456",
            tenant_code="aspora",
            infra_type="s3",
            environment="prod",
            artifact_s3_key=json.dumps({
                "original_s3_key": "s3/my-bucket.hcl",
                "preview": "preview/s3/my-bucket.hcl"
            })
        )
        db.add(test_queue_item)
        await db.commit()
        await db.refresh(test_queue_item)

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Get Original HCL Presigned URL")
        print(f"{'=' * 80}")

        # Act - Request original file explicitly
        client = TestClient(app)

        response = client.get(
            f"/api/v1/file-manager/view/{test_queue_item.id}?file_type=original",
            headers=auth_headers
        )

        # Assert
        assert response.status_code == 200
        data = response.json()

        print(f"[OK] File Type: {data.get('file_type')}")
        print(f"[OK] Key: {data.get('key')}")

        assert data["file_type"] == "original"
        assert data["key"] == "s3/my-bucket.hcl"

        print("\n[SUCCESS] Original file integration test PASSED!")

        # Cleanup
        await db.delete(test_queue_item)
        await db.commit()

        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_queue_item_not_found(
        self,
        db: AsyncSession,
        auth_headers
    ):
        """Test 404 error when queue item doesn't exist"""
        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Queue Item Not Found")
        print(f"{'=' * 80}")

        # Act - Request non-existent queue item
        client = TestClient(app)

        response = client.get(
            "/api/v1/file-manager/view/999999",
            headers=auth_headers
        )

        # Assert - Should return 404
        assert response.status_code == 404
        data = response.json()

        print(f"[OK] Status Code: {response.status_code}")
        print(f"[OK] Error Detail: {data.get('detail')}")

        assert "not found" in data.get("detail", "").lower()

        print("\n[SUCCESS] Not found error handling test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_no_artifact_s3_key(
        self,
        db: AsyncSession,
        auth_headers
    ):
        """Test 400 error when queue item has no artifact_s3_key"""
        # Arrange - Create queue item without artifact_s3_key
        test_queue_item = TransactionQueueModel(
            code="test-queue-no-s3-789",
            tenant_code="aspora",
            infra_type="sqs",
            environment="dev",
            artifact_s3_key=None  # No S3 key
        )
        db.add(test_queue_item)
        await db.commit()
        await db.refresh(test_queue_item)

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: No Artifact S3 Key")
        print(f"{'=' * 80}")

        # Act
        client = TestClient(app)

        response = client.get(
            f"/api/v1/file-manager/view/{test_queue_item.id}",
            headers=auth_headers
        )

        # Assert - Should return 400
        assert response.status_code == 400
        data = response.json()

        print(f"[OK] Status Code: {response.status_code}")
        print(f"[OK] Error Detail: {data.get('detail')}")

        assert "no hcl file uploaded" in data.get("detail", "").lower()

        # Cleanup
        await db.delete(test_queue_item)
        await db.commit()

        print("\n[SUCCESS] No artifact_s3_key error handling test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_missing_preview_key(
        self,
        db: AsyncSession,
        auth_headers
    ):
        """Test 400 error when preview key is missing from artifact_s3_key JSON"""
        # Arrange - Create queue item with malformed artifact_s3_key (no preview key)
        test_queue_item = TransactionQueueModel(
            code="test-queue-malformed-999",
            tenant_code="aspora",
            infra_type="dynamodb",
            environment="dev",
            artifact_s3_key=json.dumps({
                "original_s3_key": "dynamodb/table.hcl"
                # Missing "preview" key
            })
        )
        db.add(test_queue_item)
        await db.commit()
        await db.refresh(test_queue_item)

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Missing Preview Key in artifact_s3_key")
        print(f"{'=' * 80}")

        # Act - Request preview file (doesn't exist)
        client = TestClient(app)

        response = client.get(
            f"/api/v1/file-manager/view/{test_queue_item.id}?file_type=preview",
            headers=auth_headers
        )

        # Assert - Should return 400
        assert response.status_code == 400
        data = response.json()

        print(f"[OK] Status Code: {response.status_code}")
        print(f"[OK] Error Detail: {data.get('detail')}")

        assert "not available" in data.get("detail", "").lower()

        # Cleanup
        await db.delete(test_queue_item)
        await db.commit()

        print("\n[SUCCESS] Missing preview key error handling test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_invalid_file_type_parameter(
        self,
        db: AsyncSession,
        auth_headers
    ):
        """Test 400 error for invalid file_type query parameter"""
        # Arrange
        test_queue_item = TransactionQueueModel(
            code="test-queue-invalid-111",
            tenant_code="aspora",
            infra_type="sqs",
            environment="dev",
            artifact_s3_key=json.dumps({
                "original_s3_key": "sqs/test.hcl",
                "preview": "preview/sqs/test.hcl"
            })
        )
        db.add(test_queue_item)
        await db.commit()
        await db.refresh(test_queue_item)

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Invalid file_type Parameter")
        print(f"{'=' * 80}")

        # Act - Request with invalid file_type
        client = TestClient(app)

        response = client.get(
            f"/api/v1/file-manager/view/{test_queue_item.id}?file_type=invalid_type",
            headers=auth_headers
        )

        # Assert - Should return 422 (FastAPI validation error)
        assert response.status_code == 422

        print(f"[OK] Status Code: {response.status_code}")
        print("[OK] FastAPI validation error raised")

        # Cleanup
        await db.delete(test_queue_item)
        await db.commit()

        print("\n[SUCCESS] Invalid parameter validation test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_service_layer_directly(
        self,
        db: AsyncSession
    ):
        """Test FileManagerService directly (bypass API endpoint)"""
        # Arrange
        test_queue_item = TransactionQueueModel(
            code="test-service-layer-222",
            tenant_code="aspora",
            infra_type="kong",
            environment="prod",
            artifact_s3_key=json.dumps({
                "original_s3_key": "kong/routes/route-123.hcl",
                "preview": "preview/kong/routes/route-123.hcl"
            })
        )
        db.add(test_queue_item)
        await db.commit()
        await db.refresh(test_queue_item)

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Service Layer Direct Call")
        print(f"{'=' * 80}")

        # Act - Call service directly
        service = FileManagerService(db)

        result = await service.get_hcl_presigned_url(
            queue_item_id=test_queue_item.id,
            file_type=FileTypeEnum.PREVIEW
        )

        # Assert
        assert result is not None
        assert "url" in result
        assert result["key"] == "preview/kong/routes/route-123.hcl"
        assert result["file_type"] == "preview"

        print(f"[OK] Service returned presigned URL")
        print(f"[OK] Key: {result['key']}")
        print(f"[OK] File Type: {result['file_type']}")

        # Cleanup
        await db.delete(test_queue_item)
        await db.commit()

        print("\n[SUCCESS] Service layer integration test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_all_infrastructure_types(
        self,
        db: AsyncSession,
        auth_headers
    ):
        """Test that endpoint works for all infrastructure types"""
        infrastructure_types = [
            ("sqs", "sqs/payment-queue.hcl"),
            ("s3", "s3/data-bucket.hcl"),
            ("dynamodb", "dynamodb/users-table.hcl"),
            ("gateway", "kong/routes/api-route.hcl"),
            ("database_user_management", "db-user-mgmt/admin.hcl"),
        ]

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: All Infrastructure Types")
        print(f"{'=' * 80}")

        client = TestClient(app)

        for infra_type, s3_key in infrastructure_types:
            # Arrange
            test_queue_item = TransactionQueueModel(
                code=f"test-{infra_type}-333",
                tenant_code="aspora",
                infra_type=infra_type,
                environment="dev",
                artifact_s3_key=json.dumps({
                    "original_s3_key": s3_key,
                    "preview": f"preview/{s3_key}"
                })
            )
            db.add(test_queue_item)
            await db.commit()
            await db.refresh(test_queue_item)

            # Act
            response = client.get(
                f"/api/v1/file-manager/view/{test_queue_item.id}",
                headers=auth_headers
            )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["key"] == f"preview/{s3_key}"

            print(f"[OK] {infra_type}: {data['key']}")

            # Cleanup
            await db.delete(test_queue_item)
            await db.commit()

        print("\n[SUCCESS] All infrastructure types integration test PASSED!")
        print(f"{'=' * 80}\n")
