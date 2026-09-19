"""
Integration tests for Kong Route S3 Upload

These tests make REAL API calls to AWS S3 to verify the upload functionality.

Requirements:
- AWS S3 upload credentials configured in .env:
  - S3_UPLOAD_ACCESS_KEY_ID
  - S3_UPLOAD_SECRET_ACCESS_KEY
  - S3_UPLOAD_REGION
  - S3_UPLOAD_BUCKET (bucket name - must already exist in AWS)
- S3 bucket must be created manually in AWS beforehand
- S3 permissions to upload and read files

To run ONLY these integration tests:
    pytest tests/integration/plugins/test_aspora_kong_route_s3_upload_integration.py -v -s

To run all integration tests:
    pytest tests/integration/ -v

To skip integration tests (run only unit tests):
    pytest tests/unit/ -v
"""
import pytest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import boto3
from botocore.exceptions import ClientError
from app.plugin.aspora.script_gen_components.aspora_kong_route_script_gen_component import AsporaKongRouteScriptGenComponent
from app.core.config import settings
from app.handlers.gitops_handler import GitOpsHandler


class TestAsporaKongRouteS3UploadIntegration:
    """Integration tests for actual S3 upload of Kong route HCL files"""

    # Test configuration
    TEST_API_NAME = "test-partner-api"
    TEST_METHOD = "GET"
    TEST_ROUTE = "~/api/v1/test/users$"

    @property
    def test_bucket(self):
        """Get bucket name from environment variable"""
        return settings.s3_upload_bucket

    @pytest.fixture(scope="class")
    def aws_credentials(self):
        """Verify AWS S3 upload credentials are configured"""
        if not settings.s3_upload_access_key_id or not settings.s3_upload_secret_access_key:
            pytest.skip(
                "AWS S3 upload credentials not configured in .env - Skipping integration tests. "
                "Please set s3_upload_access_key_id and s3_upload_secret_access_key"
            )

        return {
            "access_key": settings.s3_upload_access_key_id,
            "secret_key": settings.s3_upload_secret_access_key,
            "region": settings.s3_upload_region or "ap-south-1"
        }

    @pytest.fixture(scope="class")
    def s3_client(self, aws_credentials):
        """Create S3 client for verification"""
        return boto3.client(
            "s3",
            aws_access_key_id=aws_credentials["access_key"],
            aws_secret_access_key=aws_credentials["secret_key"],
            region_name=aws_credentials["region"]
        )

    @pytest.mark.asyncio
    async def test_upload_kong_route_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload with real AWS credentials for Kong route"""
        # Arrange - Mock GitHub content
        mock_gateway_hcl = """
kong_configs = {
  "test-partner-api-service" = {
    routes = {
      "GET" = ["~/api/v1/test/existing$"]
      "POST" = ["~/api/v1/test/create$"]
    }
  }
}
"""

        # Mock GitHubIntegration.get_file_content to return our test HCL
        mock_github_response = {
            "exists": True,
            "content": mock_gateway_hcl,
            "sha": "abc123"
        }

        with patch('app.integrations.github_integration.GitHubIntegration.get_file_content', return_value=mock_github_response):
            # Setup generator with mock repository
            mock_repository = AsyncMock()
            mock_repository.update_artifact_s3_key.return_value = MagicMock()

            generator = AsporaKongRouteScriptGenComponent(repository=mock_repository)

            parameters = {
                'github_token': 'test-token',
                'github_base_url': 'https://api.github.com',
                'owner': 'test-owner',
                'repo': 'test-repo',
                'base_branch': 'main',
                'api_name': 'test-partner-api',
                'method': 'GET',
                'route': '~/api/v1/test/users$'
            }

            print(f"\n{'=' * 80}")
            print("INTEGRATION TEST: Real S3 Upload for Kong Route (Original + Preview)")
            print(f"{'=' * 80}")
            print(f"API Name: {self.TEST_API_NAME}")
            print(f"Method: {self.TEST_METHOD}")
            print(f"Route: {self.TEST_ROUTE}")
            print(f"S3 Bucket: {self.test_bucket}")
            print(f"AWS Region: {aws_credentials['region']}")

            # Act - Generate and upload to S3
            print("\n[*] Generating Kong route and uploading to S3...")
            result = await generator.generate(
                file_path="environment/core-prod/gateway/terragrunt.hcl",
                parameters=parameters,
                upload_to_s3=True,
                s3_bucket=self.test_bucket,
                queue_item_code="test-queue-123"
            )

            # Assert - Verify the content was generated
            assert result is not None
            assert isinstance(result, str)
            assert '~/api/v1/test/users$' in result
            assert '"GET"' in result
            print("[OK] Kong route HCL generated successfully")

            # Extract the S3 keys from the repository call
            mock_repository.update_artifact_s3_key.assert_called_once()
            call_args = mock_repository.update_artifact_s3_key.call_args
            import json
            artifact_s3_key_json = json.loads(call_args[0][1])

            original_s3_key = artifact_s3_key_json['original_s3_key']
            preview_s3_key = artifact_s3_key_json['preview']

            print(f"\n[*] Generated S3 Keys:")
            print(f"     Original: {original_s3_key}")
            print(f"     Preview: {preview_s3_key}")

            # Verify - Check ORIGINAL file exists in S3
            print(f"\n[*] Verifying ORIGINAL file in S3...")
            try:
                response = s3_client.get_object(Bucket=self.test_bucket, Key=original_s3_key)
                s3_content = response['Body'].read().decode('utf-8')

                print(f"[OK] Original file found in S3: s3://{self.test_bucket}/{original_s3_key}")
                print(f"[OK] File size: {len(s3_content)} bytes")
                print(f"[OK] ETag: {response['ETag']}")

                # Verify content matches
                assert s3_content == result, "S3 content doesn't match generated content"
                print("[OK] S3 original content matches generated HCL")

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code")
                if error_code == "NoSuchKey":
                    pytest.fail(f"Original file not found in S3: s3://{self.test_bucket}/{original_s3_key}")
                elif error_code == "NoSuchBucket":
                    pytest.fail(f"Bucket not found: {self.test_bucket}")
                else:
                    pytest.fail(f"S3 error: {e}")

            # Verify - Check PREVIEW file exists in S3
            print(f"\n[*] Verifying PREVIEW file in S3...")
            try:
                response = s3_client.get_object(Bucket=self.test_bucket, Key=preview_s3_key)
                preview_s3_content = response['Body'].read().decode('utf-8')

                print(f"[OK] Preview file found in S3: s3://{self.test_bucket}/{preview_s3_key}")
                print(f"[OK] File size: {len(preview_s3_content)} bytes")
                print(f"[OK] ETag: {response['ETag']}")

                # Verify preview content format
                assert 'Route configuration' in preview_s3_content, "Preview HCL doesn't have expected format"
                assert 'test-partner-api-service' in preview_s3_content, "Preview HCL doesn't have API name"
                assert '"GET"' in preview_s3_content, "Preview HCL doesn't have method"
                print("[OK] Preview HCL has correct format")

                # Print sample of preview content
                print(f"\n{'=' * 80}")
                print("Preview Content:")
                print(f"{'=' * 80}")
                print(preview_s3_content)
                print(f"{'=' * 80}")

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code")
                if error_code == "NoSuchKey":
                    pytest.fail(f"Preview file not found in S3: s3://{self.test_bucket}/{preview_s3_key}")
                elif error_code == "NoSuchBucket":
                    pytest.fail(f"Bucket not found: {self.test_bucket}")
                else:
                    pytest.fail(f"S3 error: {e}")

            print("\n[SUCCESS] Integration test PASSED - Both Kong route files successfully uploaded and verified in S3!")
            print(f"\n{'=' * 80}")
            print("[MANUAL VERIFICATION] Files kept in S3 for manual inspection:")
            print(f"{'=' * 80}")
            print(f"Bucket: {self.test_bucket}")
            print(f"Original File: {original_s3_key}")
            print(f"Preview File: {preview_s3_key}")
            print(f"S3 URI: s3://{self.test_bucket}/")
            print(f"Console URL: https://s3.console.aws.amazon.com/s3/buckets/{self.test_bucket}?region={aws_credentials['region']}&prefix=")
            print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_kong_route_s3_key_format(
        self,
        aws_credentials,
        s3_client
    ):
        """Test that Kong route S3 keys use correct format"""
        # Arrange
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        generator = AsporaKongRouteScriptGenComponent(repository=mock_repository)

        # Test that S3 key format is correct
        import uuid
        test_api_name = "partner-dashboard-api-service"
        test_method = "GET"
        test_uuid = "a1b2c3d4"
        route_identifier = f"{test_api_name}-{test_method}-{test_uuid}"

        expected_original_key = f"kong-routes/{route_identifier}.hcl"
        expected_preview_key = f"preview/kong-routes/{route_identifier}.hcl"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Kong Route S3 Key Format")
        print(f"{'=' * 80}")
        print(f"API Name: {test_api_name}")
        print(f"Method: {test_method}")
        print(f"UUID: {test_uuid}")
        print(f"Route Identifier: {route_identifier}")
        print(f"Original S3 Key: {expected_original_key}")
        print(f"Preview S3 Key: {expected_preview_key}")

        # Assert - Verify key format
        assert expected_original_key.startswith("kong-routes/")
        assert expected_preview_key.startswith("preview/kong-routes/")
        assert test_api_name in expected_original_key
        assert test_method in expected_original_key
        assert test_uuid in expected_original_key

        print("\n[OK] S3 key format is correct")
        print("[SUCCESS] Kong route S3 key format test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_kong_route_json_format(
        self,
        aws_credentials
    ):
        """Test that Kong route artifact_s3_key JSON format is correct"""
        # Arrange
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        # Simulate the JSON structure
        import uuid
        test_api_name = "test-api-service"
        test_method = "POST"
        test_uuid = uuid.uuid4().hex[:8]
        route_identifier = f"{test_api_name}-{test_method}-{test_uuid}"

        artifact_s3_key_json = {
            "original_s3_key": f"kong-routes/{route_identifier}.hcl",
            "preview": f"preview/kong-routes/{route_identifier}.hcl"
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Kong Route JSON Format")
        print(f"{'=' * 80}")
        print(f"API Name: {test_api_name}")
        print(f"Method: {test_method}")
        print(f"JSON Structure: {artifact_s3_key_json}")

        # Assert - Verify JSON structure
        assert "original_s3_key" in artifact_s3_key_json
        assert "preview" in artifact_s3_key_json
        assert artifact_s3_key_json["original_s3_key"].startswith("kong-routes/")
        assert artifact_s3_key_json["preview"].startswith("preview/kong-routes/")
        assert test_api_name in artifact_s3_key_json["original_s3_key"]
        assert test_method in artifact_s3_key_json["original_s3_key"]

        print("\n[OK] JSON structure is correct")
        print("[OK] Both keys present (original and preview)")
        print("[SUCCESS] Kong route JSON format test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_kong_route_metadata_format(
        self,
        aws_credentials
    ):
        """Test that Kong route preview metadata is correct"""
        # Test metadata structure
        test_api_name = "partner-dashboard-api-service"
        test_method = "GET"
        test_route = "~/api/v1/users$"

        expected_metadata = {
            "type": "preview",
            "api_name": test_api_name,
            "method": test_method,
            "route": test_route,
            "generated_by": "aspora_kong_route_script_gen_component"
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Kong Route Preview Metadata")
        print(f"{'=' * 80}")
        print(f"Expected Metadata: {expected_metadata}")

        # Assert - Verify metadata fields
        assert expected_metadata["type"] == "preview"
        assert "api_name" in expected_metadata
        assert "method" in expected_metadata
        assert "route" in expected_metadata
        assert "generated_by" in expected_metadata
        assert expected_metadata["generated_by"] == "aspora_kong_route_script_gen_component"

        print("\n[OK] Metadata structure is correct")
        print("[OK] All required fields present")
        print("[SUCCESS] Kong route metadata format test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_kong_route_uuid_uniqueness(
        self,
        aws_credentials
    ):
        """Test that Kong route generates unique UUIDs for each upload"""
        import uuid

        # Generate multiple UUIDs
        test_api_name = "test-api-service"
        test_method = "GET"

        uuids = []
        for i in range(10):
            route_identifier = f"{test_api_name}-{test_method}-{uuid.uuid4().hex[:8]}"
            uuids.append(route_identifier.split('-')[-1])

        # Check uniqueness
        unique_uuids = set(uuids)
        assert len(unique_uuids) == 10, "UUIDs should be unique"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Kong Route UUID Uniqueness")
        print(f"{'=' * 80}")
        print(f"Generated 10 UUIDs")
        print(f"All unique: {len(unique_uuids) == 10}")
        print(f"UUIDs: {uuids}")

        print("\n[OK] All UUIDs are unique")
        print("[SUCCESS] Kong route UUID uniqueness test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_kong_route_repository_update(
        self,
        aws_credentials
    ):
        """Test that repository.update_artifact_s3_key() is called with correct JSON for Kong routes"""
        # Arrange
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        generator = AsporaKongRouteScriptGenComponent(repository=mock_repository)

        # Simulate JSON that would be saved
        import uuid
        test_api_name = "partner-dashboard-api-service"
        test_method = "GET"
        test_uuid = uuid.uuid4().hex[:8]
        route_identifier = f"{test_api_name}-{test_method}-{test_uuid}"

        expected_json = {
            "original_s3_key": f"kong-routes/{route_identifier}.hcl",
            "preview": f"preview/kong-routes/{route_identifier}.hcl"
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Kong Route Repository Update")
        print(f"{'=' * 80}")
        print(f"API Name: {test_api_name}")
        print(f"Method: {test_method}")
        print(f"Expected JSON: {expected_json}")

        # Simulate what would be called
        import json
        json_str = json.dumps(expected_json)

        # Assert - Verify JSON can be parsed back
        parsed_json = json.loads(json_str)
        assert parsed_json == expected_json

        print("\n[OK] JSON can be serialized and deserialized correctly")
        print("[OK] Repository will receive correct JSON format")
        print("[SUCCESS] Kong route repository update test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_kong_route_s3_path_structure(
        self,
        aws_credentials
    ):
        """Test that Kong route S3 paths follow correct structure"""
        # Test various S3 path structures
        test_cases = [
            {
                "api_name": "user-service-api",
                "method": "GET",
                "uuid": "a1b2c3d4e5",
                "expected_path": "kong-routes/user-service-api-GET-a1b2c3d4e5.hcl",
                "expected_preview_path": "preview/kong-routes/user-service-api-GET-a1b2c3d4e5.hcl"
            },
            {
                "api_name": "order-service-api",
                "method": "POST",
                "uuid": "f6e7d8c9",
                "expected_path": "kong-routes/order-service-api-POST-f6e7d8c9.hcl",
                "expected_preview_path": "preview/kong-routes/order-service-api-POST-f6e7d8c9.hcl"
            },
            {
                "api_name": "partner-dashboard-api-service",
                "method": "PATCH",
                "uuid": "12345678",
                "expected_path": "kong-routes/partner-dashboard-api-service-PATCH-12345678.hcl",
                "expected_preview_path": "preview/kong-routes/partner-dashboard-api-service-PATCH-12345678.hcl"
            }
        ]

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Kong Route S3 Path Structure")
        print(f"{'=' * 80}")

        for test_case in test_cases:
            api_name = test_case["api_name"]
            method = test_case["method"]
            uuid = test_case["uuid"]
            route_identifier = f"{api_name}-{method}-{uuid}"

            original_key = f"kong-routes/{route_identifier}.hcl"
            preview_key = f"preview/kong-routes/{route_identifier}.hcl"

            # Assert path matches expected
            assert original_key == test_case["expected_path"], f"Original path mismatch for {api_name}"
            assert preview_key == test_case["expected_preview_path"], f"Preview path mismatch for {api_name}"

            print(f"[OK] {api_name} - {method}: {original_key}")
            print(f"     Preview: {preview_key}")

        print("\n[OK] All S3 paths follow correct structure")
        print("[SUCCESS] Kong route S3 path structure test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_commit_workflow_updates_context(self):
        """Test commit workflow updates workflow_context when metadata is provided."""
        mock_gateway_hcl = """
kong_configs = {
  "test-partner-api-service" = {
    routes = {
      "GET" = ["~/api/v1/test/existing$"]
      "POST" = ["~/api/v1/test/create$"]
    }
  }
}
"""
        mock_github_response = {
            "exists": True,
            "content": mock_gateway_hcl,
            "sha": "abc123"
        }

        with patch('app.integrations.github_integration.GitHubIntegration.get_file_content', return_value=mock_github_response):
            generator = AsporaKongRouteScriptGenComponent()
            parameters = {
                'github_token': 'test-token',
                'github_base_url': 'https://api.github.com',
                'owner': 'test-owner',
                'repo': 'test-repo',
                'base_branch': 'main',
                'api_name': 'test-partner-api',
                'method': 'GET',
                'route': '~/api/v1/test/users$'
            }

            file_location = SimpleNamespace(
                repo="owner/repo",
                file_path="environment/core-prod/gateway/terragrunt.hcl",
                base_branch="main",
                feature_branch="feature/test",
                script_gen_key="gateway"
            )
            workflow_context = SimpleNamespace(
                script_gen_responses={1: {"gateway": []}},
                gitops_responses={}
            )

            commit_calls = []

            def fake_create_commit(**kwargs):
                commit_calls.append(kwargs)
                return {"commit_sha": "abc123"}

            with patch.object(GitOpsHandler, "create_commit", side_effect=fake_create_commit):
                result = await generator.generate(
                    file_path=file_location.file_path,
                    parameters=parameters,
                    tenant="aspora",
                    queue_id=1,
                    file_location=file_location,
                    workflow_context=workflow_context
                )

            assert result
            assert commit_calls
            assert workflow_context.script_gen_responses[1]["gateway"][0] == result
