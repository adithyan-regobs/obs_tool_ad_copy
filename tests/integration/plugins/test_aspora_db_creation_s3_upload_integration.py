"""
Integration tests for Database Creation Terragrunt S3 Upload

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
    pytest tests/integration/plugins/test_aspora_db_creation_s3_upload_integration.py -v -s

To run all integration tests:
    pytest tests/integration/ -v

To skip integration tests (run only unit tests):
    pytest tests/unit/ -v
"""
import json
import pytest
import boto3
from unittest.mock import AsyncMock, MagicMock, patch
from botocore.exceptions import ClientError
from app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component import (
    AsporaDbCreationScriptGenComponent
)
from app.core.config import settings


class TestAsporaDbCreationS3UploadIntegration:
    """Integration tests for actual S3 upload of database creation terragrunt files"""

    TEST_DB_SERVER_NAME = "integration-mysql-server-01"
    TEST_DATABASE_NAME = "integration_test_db"
    TEST_ORIGINAL_S3_KEY = f"database/{TEST_DB_SERVER_NAME}.hcl"
    TEST_PREVIEW_S3_KEY = f"preview/database/{TEST_DB_SERVER_NAME}.hcl"
    MOCK_EXISTING_CONTENT = """terraform {
  source = "../../../../../layers/rds-mysql"
}
include "root" {
  path = find_in_parent_folders()
}
inputs = {
  identifier        = "mysql-server-01"
  mysql_databases   = [
    "existing_db1"
  ]
}"""

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

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_upload_db_creation_to_s3_real(
        self,
        mock_github,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload with real AWS credentials (file kept for manual verification)"""
        mock_github.return_value = {"exists": True, "content": self.MOCK_EXISTING_CONTENT}

        generator = AsporaDbCreationScriptGenComponent()
        parameters = {
            'database_name': self.TEST_DATABASE_NAME,
            'db_server_name': self.TEST_DB_SERVER_NAME,
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'base_branch': 'main'
        }

        file_path = f"environment/test-env-01/ap-south-1/database/{self.TEST_DB_SERVER_NAME}/terragrunt.hcl"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Real S3 Upload (Original + Preview)")
        print(f"{'=' * 80}")
        print(f"DB Server: {self.TEST_DB_SERVER_NAME}")
        print(f"Database: {self.TEST_DATABASE_NAME}")
        print(f"S3 Bucket: {self.test_bucket}")
        print(f"Original S3 Key: {self.TEST_ORIGINAL_S3_KEY}")
        print(f"Preview S3 Key: {self.TEST_PREVIEW_S3_KEY}")
        print(f"AWS Region: {aws_credentials['region']}")

        print("\n[*] Generating and uploading to S3...")
        result = generator.generate(
            file_path=file_path,
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        assert result is not None
        assert isinstance(result, str)
        assert f'"{self.TEST_DATABASE_NAME}"' in result
        print("[OK] Terragrunt content generated successfully")

        print("\n[*] Verifying ORIGINAL file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_ORIGINAL_S3_KEY)
            s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Original file found in S3: s3://{self.test_bucket}/{self.TEST_ORIGINAL_S3_KEY}")
            print(f"[OK] File size: {len(s3_content)} bytes")
            print(f"[OK] ETag: {response['ETag']}")

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

        print("\n[*] Verifying PREVIEW file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_PREVIEW_S3_KEY)
            preview_s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Preview file found in S3: s3://{self.test_bucket}/{self.TEST_PREVIEW_S3_KEY}")
            print(f"[OK] File size: {len(preview_s3_content)} bytes")
            print(f"[OK] ETag: {response['ETag']}")

            assert "Server:" in preview_s3_content
            assert "mysql_databases" in preview_s3_content
            assert self.TEST_DATABASE_NAME in preview_s3_content
            print("[OK] Preview HCL has correct format")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"Preview file not found in S3: s3://{self.test_bucket}/{self.TEST_PREVIEW_S3_KEY}")
            elif error_code == "NoSuchBucket":
                pytest.fail(f"Bucket not found: {self.test_bucket}")
            else:
                pytest.fail(f"S3 error: {e}")

        print("\n[SUCCESS] Integration test PASSED - Both files successfully uploaded and verified in S3!")

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_repository_update_after_s3_upload(
        self,
        mock_github,
        aws_credentials,
        s3_client
    ):
        """Test that repository.update_artifact_s3_key() is called with JSON after successful S3 upload"""
        mock_github.return_value = {"exists": True, "content": self.MOCK_EXISTING_CONTENT}
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        generator = AsporaDbCreationScriptGenComponent(repository=mock_repository)
        parameters = {
            'database_name': self.TEST_DATABASE_NAME,
            'db_server_name': self.TEST_DB_SERVER_NAME,
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'base_branch': 'main'
        }

        file_path = f"environment/test-env-01/ap-south-1/database/{self.TEST_DB_SERVER_NAME}/terragrunt.hcl"
        test_queue_item_code = "test-queue-item-123"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Repository Update After S3 Upload (JSON format)")
        print(f"{'=' * 80}")

        result = generator.generate(
            file_path=file_path,
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket,
            queue_item_code=test_queue_item_code
        )

        assert result is not None

        response_original = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_ORIGINAL_S3_KEY)
        response_preview = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_PREVIEW_S3_KEY)
        assert response_original and response_preview

        expected_json = {
            "original_s3_key": self.TEST_ORIGINAL_S3_KEY,
            "preview": self.TEST_PREVIEW_S3_KEY
        }
        expected_json_str = json.dumps(expected_json)

        mock_repository.update_artifact_s3_key.assert_called_once()
        call_args = mock_repository.update_artifact_s3_key.call_args
        actual_queue_item_code = call_args[0][0]
        actual_json_str = call_args[0][1]

        assert actual_queue_item_code == test_queue_item_code
        assert json.loads(actual_json_str) == expected_json

        print("[OK] Repository.update_artifact_s3_key() called with JSON")
        print("\n[SUCCESS] Repository update test PASSED!")
