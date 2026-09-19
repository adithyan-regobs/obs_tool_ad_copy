"""
Integration tests for ECS Terragrunt S3 Upload

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
    pytest tests/integration/plugins/test_aspora_ecs_s3_upload_integration.py -v -s

To run all integration tests:
    pytest tests/integration/ -v

To skip integration tests (run only unit tests):
    pytest tests/unit/ -v
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
import boto3
from botocore.exceptions import ClientError
from app.plugin.aspora.script_gen_components.aspora_ecs_terragrunt_script_gen_component import AsporaEcsTerragruntScriptGenComponent
from app.core.config import settings


class TestAsporaEcsS3UploadIntegration:
    """Integration tests for actual S3 upload of ECS terragrunt files"""

    # Test configuration
    TEST_SERVICE_NAME = "integration-test-ecs-service"
    TEST_ORIGINAL_S3_KEY = f"ecs/{TEST_SERVICE_NAME}.hcl"
    TEST_PREVIEW_S3_KEY = f"preview/ecs/{TEST_SERVICE_NAME}.hcl"

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
    async def test_upload_api_common_alb_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload for API service with common ALB (most common scenario)"""
        # Arrange
        generator = AsporaEcsTerragruntScriptGenComponent()
        parameters = {
            'service_name': self.TEST_SERVICE_NAME,
            'service_type': 'API',
            'alb_selection': 'existing_alb',
            'environment': 'dev',
            'tenant': 'aspora',
            'product_name': 'test-product',
            'port': 8080,
            'cpu': 2.0,  # vCPU
            'ram': 4,     # GB
            'service_path': '/api/test',
            'listener_rule_priority': 50000,
            'health': '/health',
            'autoscaling': {
                'enabled': True,
                'desired': 2,
                'min': 1,
                'max': 5
            },
            # GitHub parameters (required for file existence check)
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: API Service with Common ALB S3 Upload")
        print(f"{'=' * 80}")
        print(f"Service Name: {self.TEST_SERVICE_NAME}")
        print(f"Service Type: API")
        print(f"ALB Selection: existing_alb (common ALB)")
        print(f"S3 Bucket: {self.test_bucket}")
        print(f"Original S3 Key: {self.TEST_ORIGINAL_S3_KEY}")
        print(f"Preview S3 Key: {self.TEST_PREVIEW_S3_KEY}")

        # Act - Generate and upload to S3
        print("\n[*] Generating and uploading to S3...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert - Verify the content was generated
        assert result is not None
        assert isinstance(result, str)
        # CPU should be converted from vCPU to units (2.0 * 1024 = 2048)
        assert 'cpu' in result and '2048' in result
        # RAM should be converted from GB to MB (4 * 1024 = 4096)
        assert 'memory' in result and '4096' in result
        assert 'service_path' in result and '"/api/test"' in result
        assert 'listener_rule_priority' in result and '50000' in result
        assert 'health_check_path' in result and '"/health"' in result
        assert 'enable_autoscaling' in result and 'true' in result
        assert 'desired_count' in result and '2' in result
        print("[OK] Terragrunt content generated successfully")

        # Verify - Check ORIGINAL file exists in S3
        print("\n[*] Verifying ORIGINAL file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_ORIGINAL_S3_KEY)
            s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Original file found in S3: s3://{self.test_bucket}/{self.TEST_ORIGINAL_S3_KEY}")
            print(f"[OK] File size: {len(s3_content)} bytes")

            # Verify content matches
            assert s3_content == result, "S3 content doesn't match generated content"
            print("[OK] S3 original content matches generated terragrunt")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"Original file not found in S3: s3://{self.test_bucket}/{self.TEST_ORIGINAL_S3_KEY}")
            elif error_code == "NoSuchBucket":
                pytest.fail(f"Bucket not found: {self.test_bucket}")
            else:
                pytest.fail(f"S3 error: {e}")

        # Verify - Check PREVIEW file exists in S3
        print("\n[*] Verifying PREVIEW file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_PREVIEW_S3_KEY)
            preview_s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Preview file found in S3: s3://{self.test_bucket}/{self.TEST_PREVIEW_S3_KEY}")
            print(f"[OK] File size: {len(preview_s3_content)} bytes")

            # Verify preview content format
            assert 'inputs = {' in preview_s3_content, "Preview HCL doesn't have expected format"
            assert 'identifier' in preview_s3_content, "Preview HCL doesn't have identifier"
            print("[OK] Preview HCL has correct format")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"Preview file not found in S3: s3://{self.test_bucket}/{self.TEST_PREVIEW_S3_KEY}")
            else:
                pytest.fail(f"S3 error: {e}")

        print("\n[SUCCESS] API Common ALB integration test PASSED!")

    @pytest.mark.asyncio
    async def test_upload_worker_no_alb_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test S3 upload for background service (worker) with no ALB"""
        # Arrange
        generator = AsporaEcsTerragruntScriptGenComponent()
        test_identifier = f"{self.TEST_SERVICE_NAME}-worker"
        test_original_key = f"ecs/{test_identifier}.hcl"
        test_preview_key = f"preview/ecs/{test_identifier}.hcl"

        parameters = {
            'service_name': test_identifier,
            'service_type': 'BACKGROUND_SERVICE',
            'alb_selection': 'no_alb',
            'environment': 'prod',
            'tenant': 'aspora',
            'product_name': 'test-product',
            'port': 3000,
            'cpu': 1.0,
            'ram': 2,
            'autoscaling': {
                'enabled': False,
                'desired': 1
            },
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Background Service (Worker) No ALB S3 Upload")
        print(f"{'=' * 80}")
        print(f"Service: {test_identifier}")

        # Act
        print("\n[*] Generating worker service terragrunt...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert - Worker should NOT have ALB fields
        assert result is not None
        # Worker template has create_alb = false by default
        assert 'create_alb' in result and 'false' in result
        # Should NOT have service_path or listener_rule_priority
        # These fields are removed in no-alb template
        assert result.count('service_path') == 0 or 'service_path        =' not in result
        print("[OK] Worker service has no ALB configuration")

        # Verify in S3
        print("\n[*] Verifying files in S3...")
        response_original = s3_client.get_object(Bucket=self.test_bucket, Key=test_original_key)
        response_preview = s3_client.get_object(Bucket=self.test_bucket, Key=test_preview_key)
        assert response_original and response_preview
        print("[OK] Both files found in S3")

        print("\n[SUCCESS] Worker service integration test PASSED!")

    @pytest.mark.asyncio
    async def test_upload_api_new_alb_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test S3 upload for API service with new dedicated ALB"""
        # Arrange
        generator = AsporaEcsTerragruntScriptGenComponent()
        test_identifier = f"{self.TEST_SERVICE_NAME}-new-alb"
        test_original_key = f"ecs/{test_identifier}.hcl"
        test_preview_key = f"preview/ecs/{test_identifier}.hcl"

        parameters = {
            'service_name': test_identifier,
            'service_type': 'API',
            'alb_selection': 'create_new_alb',
            'environment': 'staging',
            'tenant': 'aspora',
            'product_name': 'test-product',
            'port': 9000,
            'cpu': 4.0,
            'ram': 8,
            'service_path': '/v2/api',
            'listener_rule_priority': 60000,
            'health': '/api/health',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: API Service with New Dedicated ALB S3 Upload")
        print(f"{'=' * 80}")
        print(f"Service: {test_identifier}")

        # Act
        print("\n[*] Generating API service with new ALB...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert - Should have create_alb and alb_identifier
        assert result is not None
        assert 'create_alb' in result and 'true' in result
        assert 'alb_identifier' in result
        print("[OK] New ALB configuration present")

        # Verify in S3
        print("\n[*] Verifying files in S3...")
        response_original = s3_client.get_object(Bucket=self.test_bucket, Key=test_original_key)
        response_preview = s3_client.get_object(Bucket=self.test_bucket, Key=test_preview_key)
        assert response_original and response_preview
        print("[OK] Both files found in S3")

        print("\n[SUCCESS] New ALB integration test PASSED!")

    @pytest.mark.asyncio
    async def test_upload_ops_tools_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test S3 upload for OPS_TOOLS service type"""
        # Arrange
        generator = AsporaEcsTerragruntScriptGenComponent()
        test_identifier = f"{self.TEST_SERVICE_NAME}-ops-tools"
        test_original_key = f"ecs/{test_identifier}.hcl"
        test_preview_key = f"preview/ecs/{test_identifier}.hcl"

        parameters = {
            'service_name': test_identifier,
            'service_type': 'OPS_TOOLS',
            'environment': 'dev',
            'tenant': 'aspora',
            'product_name': 'test-product',
            'port': 8080,
            'cpu': 0.5,
            'ram': 1,
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: OPS_TOOLS Service S3 Upload")
        print(f"{'=' * 80}")
        print(f"Service: {test_identifier}")

        # Act
        print("\n[*] Generating OPS_TOOLS service...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert
        assert result is not None
        print("[OK] OPS_TOOLS service generated")

        # Verify in S3
        print("\n[*] Verifying files in S3...")
        response_original = s3_client.get_object(Bucket=self.test_bucket, Key=test_original_key)
        response_preview = s3_client.get_object(Bucket=self.test_bucket, Key=test_preview_key)
        assert response_original and response_preview
        print("[OK] Both files found in S3")

        print("\n[SUCCESS] OPS_TOOLS integration test PASSED!")

    @pytest.mark.asyncio
    async def test_datadog_sidecar_configuration_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test S3 upload with Datadog sidecar enabled"""
        # Arrange
        generator = AsporaEcsTerragruntScriptGenComponent()
        test_identifier = f"{self.TEST_SERVICE_NAME}-datadog"
        test_original_key = f"ecs/{test_identifier}.hcl"

        parameters = {
            'service_name': test_identifier,
            'service_type': 'API',
            'alb_selection': 'existing_alb',
            'environment': 'dev',
            'tenant': 'aspora',
            'product_name': 'test-product',
            'language_name': 'python 3.12',
            'sidecar_config': [
                {
                    'name': 'datadog',
                    'enabled': True,
                    'cpu': 256,
                    'ram': 512,
                    'datadog_logs_enabled': True
                }
            ],
            'port': 8080,
            'cpu': 2.0,
            'ram': 4,
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Datadog Sidecar Configuration S3 Upload")
        print(f"{'=' * 80}")
        print(f"Service: {test_identifier}")

        # Act
        print("\n[*] Generating service with Datadog sidecar...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert - Datadog fields should be present
        assert result is not None
        assert 'enable_datadog_sidecar' in result and 'true' in result
        assert 'datadog_sidecar_cpu' in result and '256' in result
        assert 'datadog_sidecar_memory' in result and '512' in result
        assert 'datadog_logs_enabled' in result and 'true' in result
        assert 'datadog_log_source' in result and 'python' in result
        # Should have datadog dependency block
        assert 'dependency "datadog_api_key"' in result
        # OTel should be disabled when Datadog enabled
        assert 'enable_otel_sidecar' in result and 'false' in result
        print("[OK] Datadog configuration present")
        print("[OK] Datadog dependency block injected")
        print("[OK] OTel disabled when Datadog enabled")

        # Verify in S3
        print("\n[*] Verifying file in S3...")
        response = s3_client.get_object(Bucket=self.test_bucket, Key=test_original_key)
        s3_content = response['Body'].read().decode('utf-8')

        # Verify datadog in S3 content
        assert 'enable_datadog_sidecar = true' in s3_content
        assert 'dependency "datadog_api_key"' in s3_content
        print("[OK] Datadog configuration verified in S3")

        print("\n[SUCCESS] Datadog sidecar integration test PASSED!")

    @pytest.mark.asyncio
    async def test_environment_specific_paths_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test that environment-specific paths are correctly applied"""
        # Arrange
        generator = AsporaEcsTerragruntScriptGenComponent()
        test_identifier = f"{self.TEST_SERVICE_NAME}-env-test"
        test_original_key = f"ecs/{test_identifier}.hcl"

        # Test dev environment
        parameters = {
            'service_name': test_identifier,
            'service_type': 'API',
            'alb_selection': 'existing_alb',
            'environment': 'dev',
            'tenant': 'aspora',
            'product_name': 'test-product',
            'port': 8080,
            'cpu': 2.0,
            'ram': 4,
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Environment-Specific Paths (dev)")
        print(f"{'=' * 80}")

        # Act
        print("\n[*] Generating service for dev environment...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert - dev should use common-dev-infra
        assert result is not None
        assert '../common-dev-infra' in result or 'common-dev-infra' in result
        print("[OK] Dev environment uses common-dev-infra")

        # Verify in S3
        print("\n[*] Verifying file in S3...")
        response = s3_client.get_object(Bucket=self.test_bucket, Key=test_original_key)
        s3_content = response['Body'].read().decode('utf-8')
        assert 'common-dev-infra' in s3_content
        print("[OK] Environment-specific path verified in S3")

        print("\n[SUCCESS] Environment-specific paths test PASSED!")

    @pytest.mark.asyncio
    async def test_repository_update_after_s3_upload(
        self,
        aws_credentials,
        s3_client
    ):
        """Test that repository.update_artifact_s3_key() is called with JSON after successful S3 upload"""
        # Arrange
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        generator = AsporaEcsTerragruntScriptGenComponent(repository=mock_repository)
        parameters = {
            'service_name': self.TEST_SERVICE_NAME,
            'service_type': 'API',
            'alb_selection': 'existing_alb',
            'environment': 'dev',
            'tenant': 'aspora',
            'product_name': 'test-product',
            'port': 8080,
            'cpu': 2.0,
            'ram': 4,
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        test_queue_item_code = "test-ecs-queue-item-123"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Repository Update After S3 Upload (JSON format)")
        print(f"{'=' * 80}")
        print(f"Queue Item Code: {test_queue_item_code}")
        print(f"Expected JSON: {{\"original_s3_key\": \"{self.TEST_ORIGINAL_S3_KEY}\", \"preview\": \"{self.TEST_PREVIEW_S3_KEY}\"}}")

        # Act - Upload to S3 with repository
        print("\n[*] Uploading to S3 with repository...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket,
            queue_item_code=test_queue_item_code
        )

        # Assert - Verify upload succeeded
        assert result is not None
        print("[OK] Terragrunt content generated")

        # Verify - Check both files exist in S3
        print("\n[*] Verifying files in S3...")
        response_original = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_ORIGINAL_S3_KEY)
        response_preview = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_PREVIEW_S3_KEY)
        assert response_original and response_preview
        print("[OK] Both files found in S3")

        # Assert - Repository update was called with JSON
        print("\n[*] Verifying repository.update_artifact_s3_key() was called with JSON...")

        # Build expected JSON
        import json
        expected_json = {
            "original_s3_key": self.TEST_ORIGINAL_S3_KEY,
            "preview": self.TEST_PREVIEW_S3_KEY
        }
        expected_json_str = json.dumps(expected_json)

        # Verify called with correct JSON
        mock_repository.update_artifact_s3_key.assert_called_once()
        call_args = mock_repository.update_artifact_s3_key.call_args
        actual_queue_item_code = call_args[0][0]
        actual_json_str = call_args[0][1]

        assert actual_queue_item_code == test_queue_item_code
        assert json.loads(actual_json_str) == expected_json

        print(f"[OK] Repository.update_artifact_s3_key() called with:")
        print(f"     - queue_item_code: {test_queue_item_code}")
        print(f"     - artifact_s3_key (JSON): {actual_json_str}")

        print("\n[SUCCESS] Repository update test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_missing_identifier_raises_error(self):
        """Test that missing identifier raises ValueError"""
        # Arrange
        generator = AsporaEcsTerragruntScriptGenComponent()
        parameters = {
            'service_type': 'API',
            'environment': 'dev',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main',
            # Missing BOTH 'identifier' AND 'service_name'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Missing Identifier Validation")
        print(f"{'=' * 80}")

        # Act & Assert
        with pytest.raises(ValueError, match="Identifier cannot be empty"):
            await generator.generate(parameters=parameters, upload_to_s3=True)

        print("[OK] ValueError raised as expected")
        print("\n[SUCCESS] Validation test PASSED!")

    @pytest.mark.asyncio
    async def test_repository_not_called_without_queue_item_code(
        self,
        aws_credentials,
        s3_client
    ):
        """Test that repository is NOT called when queue_item_code is None"""
        # Arrange
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        generator = AsporaEcsTerragruntScriptGenComponent(repository=mock_repository)
        parameters = {
            'service_name': self.TEST_SERVICE_NAME,
            'service_type': 'API',
            'alb_selection': 'existing_alb',
            'environment': 'dev',
            'tenant': 'aspora',
            'port': 8080,
            'cpu': 2.0,
            'ram': 4,
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Repository NOT Called Without queue_item_code")
        print(f"{'=' * 80}")

        # Act - Upload to S3 WITHOUT queue_item_code
        print("\n[*] Uploading to S3 without queue_item_code...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket,
            queue_item_code=None  # No queue_item_code
        )

        # Assert - Upload succeeded
        assert result is not None
        print("[OK] Terragrunt content generated")

        # Assert - Repository update was NOT called
        print("\n[*] Verifying repository.update_artifact_s3_key() was NOT called...")
        mock_repository.update_artifact_s3_key.assert_not_called()
        print("[OK] Repository.update_artifact_s3_key() was NOT called (as expected)")

        print("\n[SUCCESS] Repository skip test PASSED!")
        print(f"{'=' * 80}\n")
