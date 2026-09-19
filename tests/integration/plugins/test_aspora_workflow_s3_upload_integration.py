"""
Integration tests for Workflow/Pipeline S3 Upload

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
    pytest tests/integration/plugins/test_aspora_workflow_s3_upload_integration.py -v -s

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
from app.plugin.aspora.script_gen_components.aspora_workflow_script_gen_component import AsporaWorkflowScriptGenComponent
from app.core.config import settings


class TestAsporaWorkflowS3UploadIntegration:
    """Integration tests for actual S3 upload of workflow YAML files"""

    # Test configuration
    TEST_SERVICE_NAME_EKS = "integration-test-eks-workflow"
    TEST_SERVICE_NAME_ECS = "integration-test-ecs-workflow"

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
    async def test_upload_eks_workflow_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload of EKS workflow YAML"""
        # Arrange
        generator = AsporaWorkflowScriptGenComponent()
        parameters = {
            'infrastructure_type': 'eks',
            'service_name': self.TEST_SERVICE_NAME_EKS,
            'environment': 'dev',
            'language': 'golang',
            'branches': ['main', 'develop'],
            'build_path': 'services/my-service',
            'dockerfile_path': 'Dockerfile',
            'aws_region': 'us-east-1',
            'eks_cluster_name': 'my-eks-cluster',
            # GitHub parameters (required for file existence check)
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main',
            'identifier': f"{self.TEST_SERVICE_NAME_EKS}-eks"
        }

        original_s3_key = f"workflows/eks/{self.TEST_SERVICE_NAME_EKS}-eks.yml"
        preview_s3_key = f"preview/workflows/eks/{self.TEST_SERVICE_NAME_EKS}-eks.yml"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Real S3 Upload - EKS Workflow")
        print(f"{'=' * 80}")
        print(f"Service Name: {self.TEST_SERVICE_NAME_EKS}")
        print(f"Infrastructure: EKS")
        print(f"S3 Bucket: {self.test_bucket}")
        print(f"Original S3 Key: {original_s3_key}")
        print(f"Preview S3 Key: {preview_s3_key}")

        # Act - Generate and upload to S3
        print("\n[*] Generating EKS workflow and uploading to S3...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert - Verify the content was generated
        assert result is not None
        assert isinstance(result, str)
        assert 'name:' in result  # YAML workflow name
        assert 'on:' in result  # YAML triggers
        assert 'jobs:' in result  # YAML jobs
        print("[OK] EKS Workflow YAML content generated successfully")

        # Verify - Check ORIGINAL file exists in S3
        print("\n[*] Verifying ORIGINAL EKS workflow file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=original_s3_key)
            s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Original file found in S3: s3://{self.test_bucket}/{original_s3_key}")
            print(f"[OK] File size: {len(s3_content)} bytes")
            print(f"[OK] ETag: {response['ETag']}")

            # Verify content matches
            assert s3_content == result, "S3 content doesn't match generated content"
            print("[OK] S3 original content matches generated workflow YAML")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"Original file not found in S3: s3://{self.test_bucket}/{original_s3_key}")
            elif error_code == "NoSuchBucket":
                pytest.fail(f"Bucket not found: {self.test_bucket}")
            else:
                pytest.fail(f"S3 error: {e}")

        # Verify - Check PREVIEW file exists in S3
        print("\n[*] Verifying PREVIEW EKS workflow file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=preview_s3_key)
            preview_s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Preview file found in S3: s3://{self.test_bucket}/{preview_s3_key}")
            print(f"[OK] File size: {len(preview_s3_content)} bytes")

            # Verify preview content format
            assert '# EKS Workflow Configuration Preview' in preview_s3_content
            assert 'Service:' in preview_s3_content
            assert 'Infrastructure: EKS' in preview_s3_content
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
                pytest.fail(f"Preview file not found in S3: s3://{self.test_bucket}/{preview_s3_key}")
            else:
                pytest.fail(f"S3 error: {e}")

        print("\n[SUCCESS] EKS Workflow integration test PASSED!")

    @pytest.mark.asyncio
    async def test_upload_ecs_workflow_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload of ECS workflow YAML (Go)"""
        # Arrange
        generator = AsporaWorkflowScriptGenComponent()
        parameters = {
            'infrastructure_type': 'ecs',
            'service_name': self.TEST_SERVICE_NAME_ECS,
            'environment': 'dev',
            'language': 'golang',
            'branch': 'main',
            'go_version': '1.24',
            'build_path': 'cmd/server',
            'dockerfile_path': 'Dockerfile',
            'ecr_repository': '123456789.dkr.ecr.us-east-1.amazonaws.com/my-service',
            'ecs_cluster': 'my-ecs-cluster',
            'ecs_service': 'my-service-dev',
            'aws_role_arn': 'arn:aws:iam::123456789:role/my-role',
            'aws_region': 'us-east-1',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main',
            'identifier': f"{self.TEST_SERVICE_NAME_ECS}-ecs"
        }

        original_s3_key = f"workflows/ecs/{self.TEST_SERVICE_NAME_ECS}-ecs.yml"
        preview_s3_key = f"preview/workflows/ecs/{self.TEST_SERVICE_NAME_ECS}-ecs.yml"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Real S3 Upload - ECS Workflow (Go)")
        print(f"{'=' * 80}")
        print(f"Service Name: {self.TEST_SERVICE_NAME_ECS}")
        print(f"Infrastructure: ECS")
        print(f"Language: Go")
        print(f"S3 Bucket: {self.test_bucket}")

        # Act - Generate and upload to S3
        print("\n[*] Generating ECS workflow and uploading to S3...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert - Verify the content was generated
        assert result is not None
        assert isinstance(result, str)
        assert 'name: Deploy' in result  # YAML workflow name
        assert 'on:' in result  # YAML triggers
        assert 'jobs:' in result  # YAML jobs
        assert 'build-and-deploy:' in result  # ECS job name
        print("[OK] ECS Workflow YAML content generated successfully")

        # Verify - Check ORIGINAL file exists in S3
        print("\n[*] Verifying ORIGINAL ECS workflow file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=original_s3_key)
            s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Original file found in S3: s3://{self.test_bucket}/{original_s3_key}")
            print(f"[OK] File size: {len(s3_content)} bytes")

            # Verify content matches
            assert s3_content == result, "S3 content doesn't match generated content"
            print("[OK] S3 original content matches generated workflow YAML")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"Original file not found in S3: s3://{self.test_bucket}/{original_s3_key}")
            else:
                pytest.fail(f"S3 error: {e}")

        # Verify - Check PREVIEW file exists in S3
        print("\n[*] Verifying PREVIEW ECS workflow file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=preview_s3_key)
            preview_s3_content = response['Body'].read().decode('utf-8')

            print(f"[OK] Preview file found in S3: s3://{self.test_bucket}/{preview_s3_key}")

            # Verify preview content format
            assert '# ECS Workflow Configuration Preview' in preview_s3_content
            assert 'Infrastructure: ECS' in preview_s3_content
            print("[OK] Preview YAML has correct format")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"Preview file not found in S3: s3://{self.test_bucket}/{preview_s3_key}")
            else:
                pytest.fail(f"S3 error: {e}")

        print("\n[SUCCESS] ECS Workflow integration test PASSED!")

    @pytest.mark.asyncio
    async def test_upload_ecs_workflow_java_maven_to_s3_real(
        self,
        aws_credentials,
        s3_client
    ):
        """Test actual S3 upload of ECS workflow YAML (Java Maven)"""
        # Arrange
        generator = AsporaWorkflowScriptGenComponent()
        service_name = "java-maven-test-service"
        parameters = {
            'infrastructure_type': 'ecs',
            'service_name': service_name,
            'environment': 'staging',
            'language': 'java',
            'language_ref_code': 'JAVA-MAVEN_21',
            'branch': 'main',
            'java_version': '21',
            'build_path': '',
            'dockerfile_path': 'Dockerfile',
            'ecr_repository': '123456789.dkr.ecr.us-east-1.amazonaws.com/java-service',
            'ecs_cluster': 'my-ecs-cluster',
            'aws_role_arn': 'arn:aws:iam::123456789:role/my-role',
            'aws_region': 'us-east-1',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main',
            'identifier': f"{service_name}-java-maven"
        }

        original_s3_key = f"workflows/ecs/{service_name}-java-maven.yml"

        print(f"\n{'=' * 80}")
        print("INTEGRATION TEST: Real S3 Upload - ECS Workflow (Java Maven)")
        print(f"{'=' * 80}")

        # Act
        print("\n[*] Generating Java Maven ECS workflow and uploading to S3...")
        result = await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket
        )

        # Assert - Verify Java-specific content
        assert result is not None
        assert 'Set up Java' in result or 'java-version' in result  # Java setup step
        assert 'Build application' in result or 'mvn' in result.lower()  # Maven build
        print("[OK] Java Maven ECS workflow generated successfully")

        # Verify file exists in S3
        print("\n[*] Verifying file in S3...")
        try:
            response = s3_client.get_object(Bucket=self.test_bucket, Key=original_s3_key)
            s3_content = response['Body'].read().decode('utf-8')
            print(f"[OK] File found in S3: s3://{self.test_bucket}/{original_s3_key}")
            assert s3_content == result
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                pytest.fail(f"File not found in S3: s3://{self.test_bucket}/{original_s3_key}")

        print("\n[SUCCESS] Java Maven ECS Workflow integration test PASSED!")

    @pytest.mark.asyncio
    async def test_repository_update_after_s3_upload(self):
        """Test that repository.update_artifact_s3_key is called with correct JSON"""
        # Arrange
        mock_repository = AsyncMock()
        generator = AsporaWorkflowScriptGenComponent(repository=mock_repository)

        parameters = {
            'infrastructure_type': 'eks',
            'service_name': 'test-service',
            'environment': 'dev',
            'language': 'golang',
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main',
            'identifier': 'test-service-eks'
        }

        # Act
        await generator.generate(
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket=self.test_bucket,
            queue_item_code=12345
        )

        # Assert
        mock_repository.update_artifact_s3_key.assert_called_once()
        call_args = mock_repository.update_artifact_s3_key.call_args
        queue_item_code = call_args[0][0]
        update_payload = call_args[0][1]

        assert queue_item_code == 12345
        assert "original_s3_key" in update_payload
        assert "preview" in update_payload
        print("[OK] Repository updated with correct S3 keys JSON")

    @pytest.mark.asyncio
    async def test_generate_eks_workflow_with_custom_steps(self):
        """Test EKS workflow generation with custom workflow steps"""
        # Arrange
        generator = AsporaWorkflowScriptGenComponent()

        custom_steps = [
            {
                "id": "code-checkout",
                "name": "Checkout Code",
                "enabled": True,
                "order": 1,
                "dependsOn": [],
                "category": "setup",
                "mandatory": True
            },
            {
                "id": "build",
                "name": "Build Application",
                "enabled": True,
                "order": 2,
                "dependsOn": ["code-checkout"],
                "category": "build",
                "mandatory": True
            },
            {
                "id": "deploy",
                "name": "Deploy to EKS",
                "enabled": True,
                "order": 3,
                "dependsOn": ["build"],
                "category": "deploy",
                "mandatory": True
            }
        ]

        parameters = {
            'infrastructure_type': 'eks',
            'service_name': 'custom-steps-service',
            'environment': 'prod',
            'language': 'golang',
            'steps': custom_steps,
            'branches': ['main'],
            'aws_region': 'us-west-2',
            'eks_cluster_name': 'prod-cluster',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        # Act
        result = await generator.generate(parameters=parameters)

        # Assert
        assert result is not None
        assert 'name: Deploy custom-steps-service to prod' in result
        assert 'code-checkout' in result or 'Checkout Code' in result
        assert 'build' in result
        assert 'deploy' in result or 'Deploy to EKS' in result
        print("[OK] EKS workflow generated with custom steps")

    @pytest.mark.asyncio
    async def test_generate_ecs_workflow_for_all_languages(self):
        """Test ECS workflow generation for all supported languages"""
        # Arrange
        generator = AsporaWorkflowScriptGenComponent()

        languages = [
            ('golang', '1.24'),
            ('java', '21'),
            ('nodejs', '20'),
            ('python', '3.12')
        ]

        for language, version in languages:
            print(f"\n[*] Testing {language} workflow generation...")

            parameters = {
                'infrastructure_type': 'ecs',
                'service_name': f'{language}-test-service',
                'environment': 'dev',
                'language': language,
                'branch': 'main',
                f'{language}_version' if language != 'golang' else 'go_version': version,
                'ecr_repository': '123456789.dkr.ecr.us-east-1.amazonaws.com/test',
                'ecs_cluster': 'test-cluster',
                'aws_role_arn': 'arn:aws:iam::123456789:role/test',
                'aws_region': 'us-east-1',
                # GitHub parameters
                'github_token': 'dummy-token',
                'github_base_url': 'https://github.com',
                'owner': 'dummy-owner',
                'repo': 'dummy-repo',
                'base_branch': 'main'
            }

            # Act
            result = await generator.generate(parameters=parameters)

            # Assert
            assert result is not None
            assert 'name: Deploy' in result
            assert 'on:' in result
            assert 'jobs:' in result
            print(f"[OK] {language.capitalize()} workflow generated successfully")

    @pytest.mark.asyncio
    async def test_generate_ecs_workflow_with_wire_enabled(self):
        """Test ECS workflow generation for Go with Wire code generation enabled"""
        # Arrange
        generator = AsporaWorkflowScriptGenComponent()

        parameters = {
            'infrastructure_type': 'ecs',
            'service_name': 'wire-go-service',
            'environment': 'dev',
            'language': 'golang',
            'branch': 'main',
            'go_version': '1.24',
            'wire_enabled': True,
            'wire_path': 'cmd/server',
            'build_path': 'cmd/server',
            'dockerfile_path': 'Dockerfile',
            'ecr_repository': '123456789.dkr.ecr.us-east-1.amazonaws.com/wire-service',
            'ecs_cluster': 'test-cluster',
            'aws_role_arn': 'arn:aws:iam::123456789:role/test',
            'aws_region': 'us-east-1',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        # Act
        result = await generator.generate(parameters=parameters)

        # Assert
        assert result is not None
        assert 'Wire' in result or 'wire' in result  # Wire step should be present
        print("[OK] Go workflow with Wire enabled generated successfully")

    @pytest.mark.asyncio
    async def test_commit_workflow_updates_context(self):
        """Test that Git commit is created when workflow_context is provided"""
        # Arrange
        from app.handlers.gitops_handler import GitOpsHandler
        from unittest.mock import patch

        mock_workflow_context = MagicMock()
        mock_workflow_context.script_gen_responses = {1: {'workflow': []}}
        mock_file_location = MagicMock()
        mock_file_location.repo = 'test-owner/test-repo'
        mock_file_location.file_path = '.github/workflows/test-workflow.yml'
        mock_file_location.base_branch = 'main'
        mock_file_location.feature_branch = 'feature-branch'
        mock_file_location.script_gen_key = 'workflow'

        generator = AsporaWorkflowScriptGenComponent()

        parameters = {
            'infrastructure_type': 'eks',
            'service_name': 'commit-test-service',
            'environment': 'dev',
            'language': 'golang',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'test-owner',
            'repo': 'test-repo',
            'base_branch': 'main'
        }

        # Act
        with patch.object(GitOpsHandler, 'create_commit', new=AsyncMock()) as mock_create_commit:
            result = await generator.generate(
                parameters=parameters,
                tenant='aspora',
                file_location=mock_file_location,
                workflow_context=mock_workflow_context,
                queue_id=1
            )

            # Assert
            mock_create_commit.assert_called_once()
            call_kwargs = mock_create_commit.call_args[1]
            assert call_kwargs['tenant'] == 'aspora'
            assert call_kwargs['owner'] == 'test-owner'
            assert call_kwargs['repo'] == 'test-repo'
            assert 'EKS workflow' in call_kwargs['commit_message']
            assert len(mock_workflow_context.script_gen_responses[1]['workflow']) == 1
            print("[OK] Git commit created and workflow context updated")

    @pytest.mark.asyncio
    async def test_invalid_infrastructure_type_raises_error(self):
        """Test that invalid infrastructure_type raises ValueError"""
        # Arrange
        generator = AsporaWorkflowScriptGenComponent()

        parameters = {
            'infrastructure_type': 'invalid_type',  # Invalid
            'service_name': 'test-service',
            'environment': 'dev',
            'language': 'golang',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            await generator.generate(parameters=parameters)

        assert "infrastructure_type must be 'eks' or 'ecs'" in str(exc_info.value)
        print("[OK] ValueError raised for invalid infrastructure_type")

    @pytest.mark.asyncio
    async def test_missing_service_name_raises_error(self):
        """Test that missing service_name raises ValueError"""
        # Arrange
        generator = AsporaWorkflowScriptGenComponent()

        parameters = {
            'infrastructure_type': 'eks',
            # service_name is missing
            'environment': 'dev',
            'language': 'golang',
            # GitHub parameters
            'github_token': 'dummy-token',
            'github_base_url': 'https://github.com',
            'owner': 'dummy-owner',
            'repo': 'dummy-repo',
            'base_branch': 'main'
        }

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            await generator.generate(parameters=parameters)

        assert "service_name" in str(exc_info.value).lower()
        print("[OK] ValueError raised for missing service_name")