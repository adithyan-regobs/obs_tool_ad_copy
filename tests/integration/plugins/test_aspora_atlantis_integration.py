"""
Integration tests for AsporaAtlantisScriptGenComponent
"""
from types import SimpleNamespace
import json
from unittest.mock import AsyncMock, patch

import pytest
import boto3
from botocore.exceptions import ClientError

from app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component import (
    AsporaAtlantisScriptGenComponent,
)
from app.core.config import settings


BASE_ATLANTIS = "version: 3\nprojects:\n\n"


@pytest.fixture
def script_generator():
    return AsporaAtlantisScriptGenComponent()


@pytest.fixture
def workflow_context():
    return SimpleNamespace(script_gen_responses={1: {"atlantis": {}}}, skip_commit=True)


@pytest.fixture
def file_location():
    return SimpleNamespace(
        repo="owner/repo",
        file_path="atlantis.yaml",
        base_branch="main",
        feature_branch="feature/test",
        script_gen_key="atlantis",
        config={"hcl_file_path": "environment/core-prod-01/us-east-1/services/payments/terragrunt.hcl"},
    )


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_integration_missing_atlantis_raises(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    mock_get_content.return_value = {"exists": False}
    queue_dict = {
        "id": 1,
        "environment": "prod",
        "config_snapshot": {
            "product_name": "core",
            "service_name": "payments",
            "infra_type": "ecs",
        },
    }

    with pytest.raises(ValueError, match="atlantis.yaml not found"):
        await script_generator.generate(
            tenant="aspora",
            repository=None,
            file_location=file_location,
            queue_dict=queue_dict,
            workflow_context=workflow_context,
            upload_to_s3=False,
        )


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_integration_ecs_core_prod_geo_loc_name(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    mock_get_content.return_value = {"exists": True, "content": BASE_ATLANTIS}
    queue_dict = {
        "id": 1,
        "environment": "prod",
        "config_snapshot": {
            "product_name": "core",
            "service_name": "Payments API",
            "infra_type": "ecs",
            "geo_loc_mst_code": "uk",
        },
    }

    result = await script_generator.generate(
        tenant="aspora",
        repository=None,
        file_location=file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert "name: core-prod-london-payments-api-service" in result
    assert "dir: environment/core-prod-01/us-east-1/services/payments" in result
    assert "branch: /main/" in result


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_integration_ecs_vance_dev_branch_pattern(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    mock_get_content.return_value = {"exists": True, "content": BASE_ATLANTIS}
    queue_dict = {
        "id": 1,
        "environment": "dev",
        "config_snapshot": {
            "product_name": "core",
            "service_name": "orders",
            "infra_type": "ecs",
        },
    }

    result = await script_generator.generate(
        tenant="vance",
        repository=None,
        file_location=file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert "name: core-dev-orders-service" in result
    assert "branch: /stage/" in result


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_integration_standalone_s3_core_prod_geo_loc(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    mock_get_content.return_value = {"exists": True, "content": BASE_ATLANTIS}
    file_location.config["hcl_file_path"] = "environment/core-prod-01/us-east-1/buckets/assets/terragrunt.hcl"
    queue_dict = {
        "id": 1,
        "environment": "prod",
        "config_snapshot": {
            "product_name": "core",
            "service_name": "assets",
            "infra_type": "s3",
            "geo_loc_mst_code": "mumbai",
        },
    }

    result = await script_generator.generate(
        tenant="aspora",
        repository=None,
        file_location=file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert "name: core-prod-mumbai-assets-bucket" in result
    assert "dir: environment/core-prod-01/us-east-1/buckets/assets" in result


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_integration_standalone_sqs_staging_default_branch(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    mock_get_content.return_value = {"exists": True, "content": BASE_ATLANTIS}
    file_location.config["hcl_file_path"] = "environment/core-staging-01/us-east-1/queues/jobs/terragrunt.hcl"
    queue_dict = {
        "id": 1,
        "environment": "staging",
        "config_snapshot": {
            "product_name": "core",
            "service_name": "jobs",
            "infra_type": "sqs",
        },
    }

    result = await script_generator.generate(
        tenant="default",
        repository=None,
        file_location=file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert "name: core-staging-jobs-queue" in result
    assert "branch: /staging/" in result


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_integration_missing_projects_section_no_insert(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    base_content = "version: 3\n"
    mock_get_content.return_value = {"exists": True, "content": base_content}
    queue_dict = {
        "id": 1,
        "environment": "prod",
        "config_snapshot": {
            "product_name": "core",
            "service_name": "payments",
            "infra_type": "ecs",
        },
    }

    result = await script_generator.generate(
        tenant="aspora",
        repository=None,
        file_location=file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert result == base_content


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_integration_missing_hcl_path_raises(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    mock_get_content.return_value = {"exists": True, "content": BASE_ATLANTIS}
    file_location.config = {}
    queue_dict = {
        "id": 1,
        "environment": "prod",
        "config_snapshot": {
            "product_name": "core",
            "service_name": "payments",
            "infra_type": "ecs",
        },
    }

    with pytest.raises(ValueError, match="Terragrunt file path is required"):
        await script_generator.generate(
            tenant="aspora",
            repository=None,
            file_location=file_location,
            queue_dict=queue_dict,
            workflow_context=workflow_context,
            upload_to_s3=False,
        )


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.create_commit")
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.FileManagerHandler.upload_file")
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_integration_upload_and_commit_paths(
    mock_get_content,
    mock_upload_file,
    mock_create_commit,
    script_generator,
    file_location,
):
    mock_get_content.return_value = {"exists": True, "content": BASE_ATLANTIS}
    mock_upload_file.side_effect = [
        {"location": "s3://bucket/atlantis/core-prod-payments.yaml"},
        {"location": "s3://bucket/preview/atlantis/core-prod-payments.yaml"},
    ]
    repository = SimpleNamespace(update_artifact_s3_key=AsyncMock())
    workflow_context = SimpleNamespace(script_gen_responses={1: {"atlantis": {}}}, skip_commit=False)

    queue_dict = {
        "id": 1,
        "code": "queue-001",
        "environment": "prod",
        "config_snapshot": {
            "product_name": "core",
            "service_name": "payments",
            "infra_type": "ecs",
        },
    }

    await script_generator.generate(
        tenant="aspora",
        repository=repository,
        file_location=file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=True,
    )

    mock_create_commit.assert_called_once()
    repository.update_artifact_s3_key.assert_called_once()
    payload = repository.update_artifact_s3_key.call_args.args[1]
    assert json.loads(payload)["original_s3_key"].startswith("atlantis/")


class TestAsporaAtlantisS3UploadIntegration:
    """Integration tests for actual S3 upload of atlantis.yaml entries"""

    TEST_SERVICE_NAME = "integration-atlantis-service"
    TEST_ORIGINAL_S3_KEY = f"atlantis/{TEST_SERVICE_NAME}.yaml"
    TEST_PREVIEW_S3_KEY = f"preview/atlantis/{TEST_SERVICE_NAME}.yaml"

    @property
    def test_bucket(self):
        return settings.s3_upload_bucket

    @pytest.fixture(scope="class")
    def aws_credentials(self):
        if not settings.s3_upload_access_key_id or not settings.s3_upload_secret_access_key:
            pytest.skip(
                "AWS S3 upload credentials not configured in .env - Skipping integration tests. "
                "Please set s3_upload_access_key_id and s3_upload_secret_access_key"
            )

        return {
            "access_key": settings.s3_upload_access_key_id,
            "secret_key": settings.s3_upload_secret_access_key,
            "region": settings.s3_upload_region or "ap-south-1",
        }

    @pytest.fixture(scope="class")
    def s3_client(self, aws_credentials):
        return boto3.client(
            "s3",
            aws_access_key_id=aws_credentials["access_key"],
            aws_secret_access_key=aws_credentials["secret_key"],
            region_name=aws_credentials["region"],
        )

    @pytest.mark.asyncio
    @patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
    async def test_upload_atlantis_to_s3_real(
        self,
        mock_get_content,
        aws_credentials,
        s3_client,
    ):
        mock_get_content.return_value = {"exists": True, "content": BASE_ATLANTIS}

        file_location = SimpleNamespace(
            repo="owner/repo",
            file_path="atlantis.yaml",
            base_branch="main",
            feature_branch="feature/test",
            script_gen_key="atlantis",
            config={
                "hcl_file_path": "environment/core-prod-01/us-east-1/services/integration/terragrunt.hcl"
            },
        )
        workflow_context = SimpleNamespace(script_gen_responses={1: {"atlantis": {}}}, skip_commit=True)

        queue_dict = {
            "id": 1,
            "environment": "prod",
            "config_snapshot": {
                "product_name": "core",
                "service_name": self.TEST_SERVICE_NAME,
                "infra_type": "ecs",
            },
        }

        generator = AsporaAtlantisScriptGenComponent()
        await generator.generate(
            tenant="aspora",
            repository=None,
            file_location=file_location,
            queue_dict=queue_dict,
            workflow_context=workflow_context,
            upload_to_s3=True,
        )

        try:
            s3_client.head_object(Bucket=self.test_bucket, Key=self.TEST_ORIGINAL_S3_KEY)
            s3_client.head_object(Bucket=self.test_bucket, Key=self.TEST_PREVIEW_S3_KEY)
        except ClientError as exc:
            pytest.fail(f"S3 upload verification failed: {exc}")
