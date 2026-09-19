"""
Integration tests for SQS Terragrunt S3 Upload

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
    pytest tests/integration/plugins/test_aspora_sqs_s3_upload_integration.py -v -s

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
from app.plugin.aspora.script_gen_components.aspora_sqs_script_gen_component import AsporaSqsScriptGenComponent
from app.core.config import settings


class TestAsporaSqsS3UploadIntegration:
    """Integration tests for actual S3 upload of SQS terragrunt files"""

    # Test configuration
    TEST_QUEUE_NAME = "integration-test-queue"
    TEST_ORIGINAL_S3_KEY = f"sqs/{TEST_QUEUE_NAME}.hcl"
    TEST_PREVIEW_S3_KEY = f"preview/sqs/{TEST_QUEUE_NAME}.hcl"

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
    async def test_upload_sqs_terragrunt_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload with real AWS credentials (file kept for manual verification)"""
        # Arrange
        generator = AsporaSqsScriptGenComponent()
        parameters = {
            'environment': 'dev',
            'identifier': self.TEST_QUEUE_NAME,
            'create_dlq': True,
            'fifo_queue': True,
            'visibility_timeout_seconds': 300,
            'max_receive_count': 5
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Real S3 Upload (Original + Preview)")
        print(f"{'=' * 80}")
        print(f"Queue Name: {self.TEST_QUEUE_NAME}")
        print(f"S3 Bucket: {self.test_bucket}")
        print(f"Original S3 Key: {self.TEST_ORIGINAL_S3_KEY}")
        print(f"Preview S3 Key: {self.TEST_PREVIEW_S3_KEY}")
        print(f"AWS Region: {aws_credentials['region']}")

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
        assert 'create_dlq           = true' in result
        assert 'visibility_timeout_seconds = 300' in result
        print("[OK] Terragrunt content generated successfully")

        # Verify - Check ORIGINAL file exists in S3
        print("\n[*] Verifying ORIGINAL file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_ORIGINAL_S3_KEY)
            s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Original file found in S3: s3://{self.test_bucket}/{self.TEST_ORIGINAL_S3_KEY}")
            print(f"[OK] File size: {len(s3_content)} bytes")
            print(f"[OK] ETag: {response['ETag']}")

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
            print(f"[OK] ETag: {response['ETag']}")

            # Verify preview content format
            assert 'inputs = {' in preview_s3_content, "Preview HCL doesn't have expected format"
            assert 'identifier' in preview_s3_content, "Preview HCL doesn't have identifier"
            print("[OK] Preview HCL has correct format")

            # Print sample of preview content
            print(f"\n{'=' * 80}")
            print("Preview Content (first 500 chars):")
            print(f"{'=' * 80}")
            print(preview_s3_content[:500] + "..." if len(preview_s3_content) > 500 else preview_s3_content)
            print(f"{'=' * 80}")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"Preview file not found in S3: s3://{self.test_bucket}/{self.TEST_PREVIEW_S3_KEY}")
            elif error_code == "NoSuchBucket":
                pytest.fail(f"Bucket not found: {self.test_bucket}")
            else:
                pytest.fail(f"S3 error: {e}")

        print("\n[SUCCESS] Integration test PASSED - Both files successfully uploaded and verified in S3!")
        print(f"\n{'=' * 80}")
        print("[MANUAL VERIFICATION] Files kept in S3 for manual inspection:")
        print(f"{'=' * 80}")
        print(f"Bucket: {self.test_bucket}")
        print(f"Original File: {self.TEST_ORIGINAL_S3_KEY}")
        print(f"Preview File: {self.TEST_PREVIEW_S3_KEY}")
        print(f"S3 URI: s3://{self.test_bucket}/")
        print(f"Console URL: https://s3.console.aws.amazon.com/s3/buckets/{self.test_bucket}?region={aws_credentials['region']}&prefix=")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_upload_with_all_parameters_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test uploading full-featured SQS configuration to real S3"""
        # Arrange
        generator = AsporaSqsScriptGenComponent()
        parameters = {
            'environment': 'prod',
            'identifier': self.TEST_QUEUE_NAME,
            'create_dlq': True,
            'fifo_queue': True,
            'visibility_timeout_seconds': 600,
            'max_receive_count': 10,
            'message_retention_seconds': 345600,
            'dlq_message_retention_seconds': 1209600,
            'cross_account_ids': ['111222333444', '555666777888']
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Full-Featured Queue Upload to S3")
        print(f"{'=' * 80}")

        # Act
        print("\n[*] Uploading full-featured queue config to S3...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Verify content has all parameters
        assert 'visibility_timeout_seconds = 600' in result
        assert 'max_receive_count         = 10' in result
        assert 'message_retention_seconds = 345600' in result
        assert 'dlq_message_retention_seconds = 1209600' in result
        assert 'cross_account_ids' in result
        assert 'enable_cross_account_access = true' in result
        print("[OK] All parameters included in terragrunt")

        # Verify in S3
        print("\n[*] Verifying files in S3...")
        response_original = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_ORIGINAL_S3_KEY)
        response_preview = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_PREVIEW_S3_KEY)
        assert response_original and response_preview
        print("[OK] Both original and preview files found in S3")

        # Verify all parameters are in original S3 content
        s3_content = response_original['Body'].read().decode('utf-8')
        assert 'visibility_timeout_seconds = 600' in s3_content
        assert 'cross_account_ids            = ["111222333444", "555666777888"]' in s3_content
        print("[OK] All parameters verified in original S3 content")

        print("\n[SUCCESS] Full-featured queue integration test PASSED!")

    @pytest.mark.asyncio
    async def test_bucket_already_exists_scenario(
        self,
        aws_credentials,
        s3_client
    ):
        """Test uploading when bucket already exists (should not fail)"""
        # Arrange
        generator = AsporaSqsScriptGenComponent()
        parameters = {
            'environment': 'dev',
            'identifier': self.TEST_QUEUE_NAME,
            'create_dlq': True,
            'fifo_queue': True
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Bucket Already Exists Scenario")
        print(f"{'=' * 80}")

        # Act - Upload twice to same bucket (second upload should work)
        print("\n[*] First upload...")
        result1 = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )
        print("[OK] First upload successful")

        print("\n[*] Second upload to existing bucket...")
        result2 = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )
        print("[OK] Second upload successful (bucket already existed)")

        # Assert both succeeded
        assert result1 == result2
        print("\n[SUCCESS] Bucket already exists scenario PASSED!")

    @pytest.mark.asyncio
    async def test_missing_identifier_raises_error(self):
        """Test that missing identifier raises ValueError"""
        # Arrange
        generator = AsporaSqsScriptGenComponent()
        parameters = {
            'environment': 'dev',
            # Missing 'identifier'
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
    async def test_repository_update_after_s3_upload(
        self,
        aws_credentials,
        s3_client
    ):
        """Test that repository.update_artifact_s3_key() is called with JSON after successful S3 upload"""
        # Arrange
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()  # Mock updated queue item

        generator = AsporaSqsScriptGenComponent(repository=mock_repository)
        parameters = {
            'environment': 'dev',
            'identifier': self.TEST_QUEUE_NAME,
            'create_dlq': True,
            'fifo_queue': True
        }

        test_queue_item_code = "test-queue-item-123"

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
    async def test_repository_not_called_without_queue_item_code(
        self,
        aws_credentials,
        s3_client
    ):
        """Test that repository is NOT called when queue_item_code is None"""
        # Arrange
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        generator = AsporaSqsScriptGenComponent(repository=mock_repository)
        parameters = {
            'environment': 'dev',
            'identifier': self.TEST_QUEUE_NAME,
            'create_dlq': True,
            'fifo_queue': True
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

    @pytest.mark.asyncio
    async def test_repository_not_called_without_s3_upload(
        self,
    ):
        """Test that repository is NOT called when upload_to_s3=False"""
        # Arrange
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        generator = AsporaSqsScriptGenComponent(repository=mock_repository)
        parameters = {
            'environment': 'dev',
            'identifier': self.TEST_QUEUE_NAME,
            'create_dlq': True,
            'fifo_queue': True
        }

        test_queue_item_code = "test-queue-item-456"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Repository NOT Called Without S3 Upload")
        print(f"{'=' * 80}")

        # Act - Generate WITHOUT uploading to S3
        print("\n[*] Generating terragrunt without S3 upload...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=False,  # No S3 upload
            queue_item_code=test_queue_item_code
        )

        # Assert - Generation succeeded
        assert result is not None
        print("[OK] Terragrunt content generated")

        # Assert - Repository update was NOT called
        print("\n[*] Verifying repository.update_artifact_s3_key() was NOT called...")
        mock_repository.update_artifact_s3_key.assert_not_called()
        print("[OK] Repository.update_artifact_s3_key() was NOT called (as expected)")

        print("\n[SUCCESS] Repository skip test PASSED!")
        print(f"{'=' * 80}\n")
