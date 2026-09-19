"""
Integration tests for Dockerfile S3 Upload

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
    pytest tests/integration/plugins/test_aspora_docker_s3_upload_integration.py -v -s
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
from botocore.exceptions import ClientError
import pytest

from app.core.config import settings
from app.handlers.gitops_handler import GitOpsHandler
from app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component import (
    AsporaDockerScriptGenComponent,
)


class TestAsporaDockerS3UploadIntegration:
    """Integration tests for actual S3 upload of Dockerfiles"""

    TEST_SERVICE_NAME = "integration-test-service"
    TEST_ORIGINAL_S3_KEY = f"dockerfiles/{TEST_SERVICE_NAME}.Dockerfile"

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

    @patch('app.integrations.github_integration.GitHubIntegration.get_file_content')
    @pytest.mark.asyncio
    async def test_upload_dockerfile_to_s3_real(
        self,
        mock_github,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload with real AWS credentials for Dockerfile"""
        mock_github.return_value = {"exists": True, "content": "FROM alpine\n"}

        generator = AsporaDockerScriptGenComponent()
        parameters = {
            "github_token": "token",
            "github_base_url": "https://api.github.com",
            "owner": "owner",
            "repo": "repo",
            "base_branch": "main",
            "file_path": "Dockerfile",
            "language_name": "Java",
            "language_version": "21",
            "service_name": self.TEST_SERVICE_NAME,
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Real S3 Upload for Dockerfile")
        print(f"{'=' * 80}")
        print(f"Service Name: {self.TEST_SERVICE_NAME}")
        print(f"S3 Bucket: {self.test_bucket}")
        print(f"Original S3 Key: {self.TEST_ORIGINAL_S3_KEY}")
        print(f"AWS Region: {aws_credentials['region']}")

        print("\n[*] Generating Dockerfile and uploading to S3...")
        result = await generator.generate(
            parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        assert result == "FROM alpine\n"

        print("\n[*] Verifying ORIGINAL file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=self.TEST_ORIGINAL_S3_KEY)
            s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Original file found in S3: s3://{self.test_bucket}/{self.TEST_ORIGINAL_S3_KEY}")
            print(f"[OK] File size: {len(s3_content)} bytes")
            print(f"[OK] ETag: {response['ETag']}")

            assert s3_content == result, "S3 content doesn't match generated content"
            print("[OK] S3 original content matches generated Dockerfile")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"Original file not found in S3: s3://{self.test_bucket}/{self.TEST_ORIGINAL_S3_KEY}")
            elif error_code == "NoSuchBucket":
                pytest.fail(f"Bucket not found: {self.test_bucket}")
            else:
                pytest.fail(f"S3 error: {e}")

        print("\n[SUCCESS] Integration test PASSED - Dockerfile uploaded and verified in S3!")

    @patch('app.integrations.github_integration.GitHubIntegration.get_file_content')
    @pytest.mark.asyncio
    async def test_repository_update_after_s3_upload(
        self,
        mock_github,
        aws_credentials,
        s3_client
    ):
        """Test that repository.update_artifact_s3_key() is called with JSON after successful S3 upload"""
        mock_github.return_value = {"exists": True, "content": "FROM alpine\n"}
        mock_repository = AsyncMock()
        mock_repository.update_artifact_s3_key.return_value = MagicMock()

        generator = AsporaDockerScriptGenComponent(repository=mock_repository)
        parameters = {
            "github_token": "token",
            "github_base_url": "https://api.github.com",
            "owner": "owner",
            "repo": "repo",
            "base_branch": "main",
            "file_path": "Dockerfile",
            "language_name": "Java",
            "language_version": "21",
            "service_name": self.TEST_SERVICE_NAME,
        }

        queue_item_code = "queue-item-123"
        expected_key = f"dockerfiles/{queue_item_code}.Dockerfile"

        result = await generator.generate(
            parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket,
            queue_item_code=queue_item_code
        )

        assert result == "FROM alpine\n"

        response_original = s3_client.get_object(Bucket=self.test_bucket, Key=expected_key)
        assert response_original

        expected_json = {
            "original_s3_key": expected_key,
            "preview": expected_key
        }

        mock_repository.update_artifact_s3_key.assert_called_once()
        call_args = mock_repository.update_artifact_s3_key.call_args
        actual_queue_item_code = call_args[0][0]
        actual_json_str = call_args[0][1]

        assert actual_queue_item_code == queue_item_code
        assert json.loads(actual_json_str) == expected_json

    @patch('app.integrations.github_integration.GitHubIntegration.get_file_content')
    @pytest.mark.asyncio
    async def test_commit_workflow_updates_context(self, mock_github):
        """Test commit workflow updates workflow_context when metadata is provided."""
        mock_github.return_value = {"exists": True, "content": "FROM alpine\n"}

        generator = AsporaDockerScriptGenComponent()
        parameters = {
            "github_token": "token",
            "github_base_url": "https://api.github.com",
            "owner": "owner",
            "repo": "repo",
            "base_branch": "main",
            "file_path": "Dockerfile",
            "language_name": "Java",
            "language_version": "21",
            "service_name": self.TEST_SERVICE_NAME,
        }

        file_location = SimpleNamespace(
            repo="owner/repo",
            file_path="Dockerfile",
            base_branch="main",
            feature_branch="feature/test",
            script_gen_key="ecs_dockerfile"
        )
        workflow_context = SimpleNamespace(
            script_gen_responses={1: {"ecs_dockerfile": []}},
            gitops_responses={}
        )

        commit_calls = []

        def fake_create_commit(**kwargs):
            commit_calls.append(kwargs)
            return {"commit_sha": "abc123"}

        with patch.object(GitOpsHandler, "create_commit", side_effect=fake_create_commit):
            result = await generator.generate(
                parameters,
                tenant="aspora",
                queue_id=1,
                file_location=file_location,
                workflow_context=workflow_context
            )

        assert result == "FROM alpine\n"
        assert commit_calls
        assert workflow_context.script_gen_responses[1]["ecs_dockerfile"][0] == result
