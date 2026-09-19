"""
Integration tests for EKS/Helm S3 Upload

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
    pytest tests/integration/plugins/test_aspora_eks_s3_upload_integration.py -v -s

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
from app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component import AsporaEksScriptGenComponent
from app.core.config import settings
from app.handlers.gitops_handler import GitOpsHandler


class TestAsporaEksS3UploadIntegration:
    """Integration tests for actual S3 upload of EKS config files"""

    # Test configuration
    TEST_SERVICE_NAME = "integration-test-eks-service"
    TEST_ORIGINAL_S3_KEY = f"eks/{TEST_SERVICE_NAME}.yaml"
    TEST_PREVIEW_S3_KEY = f"preview/eks/{TEST_SERVICE_NAME}.yaml"

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
    async def test_upload_eks_config_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload with real AWS credentials (file kept for manual verification)"""
        # Arrange
        generator = AsporaEksScriptGenComponent()
        parameters = {
            'service_name': self.TEST_SERVICE_NAME,
            'language': 'golang',
            'go_version': '1.24',
            'dockerfile_path': 'Dockerfile',
            'build_type': 'docker-only',
            'skip_tests': True,
            'skip_checks': True,
            'cpu_requested': '100m',
            'cpu_limit': '500m',
            'memory_requested': '128Mi',
            'memory_limit': '512Mi',
            'port': 8080,
            'health': '/health',
            'alb_schema': 'internet-facing',
            'service_path': '/api/test',
            'secrets_enabled': False,
            'ebs_enabled': False,
            'deployment_strategy': {
                'type': 'RollingUpdate',
                'max_surge': '25%',
                'max_unavailable': '25%'
            },
            'environment': 'dev',
            # GitHub parameters (required for file existence check)
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Real S3 Upload (Original + Preview)")
        print(f"{'=' * 80}")
        print(f"Service Name: {self.TEST_SERVICE_NAME}")
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
        assert 'service_name:' in result
        assert 'language: golang' in result
        assert 'go_version: "1.24"' in result
        print("[OK] Config YAML content generated successfully")

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
            print("[OK] S3 original content matches generated YAML")

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
            assert 'inputs = {' in preview_s3_content, "Preview YAML doesn't have expected format"
            assert 'service_name' in preview_s3_content, "Preview YAML doesn't have service_name"
            print("[OK] Preview YAML has correct format")

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
    async def test_upload_java_maven_config_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test uploading Java Maven configuration to real S3"""
        # Arrange
        generator = AsporaEksScriptGenComponent()
        parameters = {
            'service_name': 'java-test-service',
            'language': 'java',
            'java_version': '17',
            'build_type': 'maven',
            'maven_options': {
                'maven_goals': 'package'
            },
            'skip_tests': False,
            'cpu_requested': '250m',
            'memory_requested': '256Mi',
            'port': 8080,
            'health': '/actuator/health',
            'environment': 'dev',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Java Maven Configuration Upload to S3")
        print(f"{'=' * 80}")

        # Act
        print("\n[*] Uploading Java Maven config to S3...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Verify content has Java-specific fields
        assert 'java_version: "17"' in result
        assert 'type: maven' in result  # YAML structure: build:\n  type: maven
        assert 'maven_goals: package' in result
        assert 'cpu_requested: 250m' in result
        print("[OK] All Java parameters included in config")

        # Verify in S3
        print("\n[*] Verifying files in S3...")
        response_original = s3_client.get_object(Bucket=self.test_bucket, Key="eks/java-test-service.yaml")
        response_preview = s3_client.get_object(Bucket=self.test_bucket, Key="preview/eks/java-test-service.yaml")
        assert response_original and response_preview
        print("[OK] Both original and preview files found in S3")

        print("\n[SUCCESS] Java Maven configuration integration test PASSED!")

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

        generator = AsporaEksScriptGenComponent(repository=mock_repository)
        parameters = {
            'service_name': self.TEST_SERVICE_NAME,
            'language': 'nodejs',
            'nodejs_version': '20',
            'dockerfile_path': 'Dockerfile',
            'build_type': 'docker-only',
            'environment': 'dev',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        test_queue_item_code = "test-eks-queue-item-123"

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
        print("[OK] Config YAML content generated")

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

        generator = AsporaEksScriptGenComponent(repository=mock_repository)
        parameters = {
            'service_name': self.TEST_SERVICE_NAME,
            'language': 'python',
            'python_version': '3.11',
            'dockerfile_path': 'Dockerfile',
            'build_type': 'docker-only',
            'environment': 'dev',
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
        print("[OK] Config YAML content generated")

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

        generator = AsporaEksScriptGenComponent(repository=mock_repository)
        parameters = {
            'service_name': 'test-service',
            'language': 'golang',
            'dockerfile_path': 'Dockerfile',
            'build_type': 'docker-only',
            'environment': 'dev',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        test_queue_item_code = "test-eks-queue-item-456"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Repository NOT Called Without S3 Upload")
        print(f"{'=' * 80}")

        # Act - Generate WITHOUT uploading to S3
        print("\n[*] Generating config YAML without S3 upload...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=False,  # No S3 upload
            queue_item_code=test_queue_item_code
        )

        # Assert - Generation succeeded
        assert result is not None
        print("[OK] Config YAML content generated")

        # Assert - Repository update was NOT called
        print("\n[*] Verifying repository.update_artifact_s3_key() was NOT called...")
        mock_repository.update_artifact_s3_key.assert_not_called()
        print("[OK] Repository.update_artifact_s3_key() was NOT called (as expected)")

        print("\n[SUCCESS] Repository skip test PASSED!")
        print(f"{'=' * 80}\n")

    @pytest.mark.asyncio
    async def test_missing_identifier_raises_error(self):
        """Test that missing identifier raises ValueError"""
        # Arrange
        generator = AsporaEksScriptGenComponent()
        parameters = {
            'language': 'golang',
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
    async def test_secrets_and_ebs_configuration(
        self,
        aws_credentials,
        s3_client
    ):
        """Test uploading configuration with secrets and EBS enabled"""
        # Arrange
        generator = AsporaEksScriptGenComponent()
        parameters = {
            'service_name': 'test-service-with-secrets',
            'language': 'golang',
            'go_version': '1.25',
            'dockerfile_path': 'Dockerfile',
            'build_type': 'docker-only',
            'cpu_requested': '200m',
            'memory_requested': '256Mi',
            'port': 9090,
            'health': '/api/health',
            'secrets_enabled': True,
            'secret_keys': ['DB_PASSWORD', 'API_KEY', 'JWT_SECRET'],
            'ebs_enabled': True,
            'ebs_size': '10Gi',
            'environment': 'dev',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Secrets and EBS Configuration Upload")
        print(f"{'=' * 80}")

        # Act
        print("\n[*] Uploading config with secrets and EBS to S3...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Verify content has secrets and EBS configuration
        assert 'secrets_enabled: true' in result or 'secrets_enabled: True' in result
        assert 'secret_keys:' in result
        assert 'ebs_enabled: true' in result or 'ebs_enabled: True' in result
        assert 'ebs_size: 10Gi' in result
        assert 'DB_PASSWORD' in result
        assert 'API_KEY' in result
        assert 'JWT_SECRET' in result
        print("[OK] Secrets and EBS configuration included in YAML")

        # Verify in S3
        print("\n[*] Verifying files in S3...")
        response = s3_client.get_object(Bucket=self.test_bucket, Key="eks/test-service-with-secrets.yaml")
        assert response
        print("[OK] File found in S3")

        print("\n[SUCCESS] Secrets and EBS configuration test PASSED!")

    @patch('app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component.GitHubIntegration.get_file_content')
    @pytest.mark.asyncio
    async def test_commit_workflow_updates_context(self, mock_github):
        """Test commit workflow updates workflow_context when metadata is provided."""
        mock_github.return_value = {"exists": True, "content": "service_name: {{SERVICE_NAME}}\n"}

        generator = AsporaEksScriptGenComponent()
        parameters = {
            "service_name": self.TEST_SERVICE_NAME,
            "language": "golang",
            "github_token": "token",
            "github_base_url": "https://api.github.com",
            "owner": "owner",
            "repo": "repo",
            "base_branch": "main",
        }

        file_location = SimpleNamespace(
            repo="owner/repo",
            file_path="configs/dev/integration-test-eks-service/config.yaml",
            base_branch="main",
            feature_branch="feature/eks",
            script_gen_key="eks"
        )
        workflow_context = SimpleNamespace(
            script_gen_responses={1: {"eks": []}},
            gitops_responses={}
        )

        commit_calls = []

        def fake_create_commit(**kwargs):
            commit_calls.append(kwargs)
            return {"commit_sha": "abc123"}

        with patch.object(GitOpsHandler, "create_commit", side_effect=fake_create_commit):
            result = await generator.generate(
                parameters=parameters,
                tenant="aspora",
                queue_id=1,
                file_location=file_location,
                workflow_context=workflow_context
            )

        assert self.TEST_SERVICE_NAME in result
        assert commit_calls
        assert workflow_context.script_gen_responses[1]["eks"][0] == result
