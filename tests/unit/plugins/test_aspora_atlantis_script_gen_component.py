"""
Unit tests for AsporaAtlantisScriptGenComponent
"""
from types import SimpleNamespace
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component import (
    AsporaAtlantisScriptGenComponent,
)


@pytest.fixture
def script_generator():
    return AsporaAtlantisScriptGenComponent()


@pytest.fixture
def workflow_context():
    return SimpleNamespace(script_gen_responses={1: {"atlantis": {}}}, skip_commit=False)


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
async def test_generate_raises_when_atlantis_missing(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    mock_get_content.return_value = {"exists": False}

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
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.create_commit")
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.FileManagerHandler.upload_file")
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_generate_ecs_entry_inserts_geo_loc_name_and_commits(
    mock_get_content,
    mock_upload_file,
    mock_create_commit,
    script_generator,
    file_location,
    workflow_context,
):
    mock_get_content.return_value = {"exists": True, "content": "version: 3\nprojects:\n\n"}
    mock_upload_file.side_effect = [
        {"location": "s3://bucket/atlantis/payments.yaml"},
        {"location": "s3://bucket/preview/atlantis/payments.yaml"},
    ]

    repository = SimpleNamespace(update_artifact_s3_key=AsyncMock())
    queue_dict = {
        "id": 1,
        "code": "queue-001",
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
        repository=repository,
        file_location=file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=True,
    )

    assert "name: core-prod-london-payments-api-service" in result
    assert "dir: environment/core-prod-01/us-east-1/services/payments/terragrunt.hcl".replace(
        "/terragrunt.hcl", ""
    ) in result
    assert "branch: /main/" in result
    assert workflow_context.script_gen_responses[1]["atlantis"]["preview_content"].startswith(
        "Atlantis project entry"
    )

    mock_create_commit.assert_called_once()
    repository.update_artifact_s3_key.assert_called_once()
    payload = repository.update_artifact_s3_key.call_args.args[1]
    assert json.loads(payload)["original_s3_key"].startswith("atlantis/")


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_atlantis_script_gen_component.GitOpsHandler.get_content")
async def test_generate_standalone_entry_skips_duplicate(
    mock_get_content,
    script_generator,
    file_location,
    workflow_context,
):
    workflow_context.skip_commit = True
    existing_name = "core-prod-mumbai-assets-bucket"
    mock_get_content.return_value = {
        "exists": True,
        "content": f"version: 3\nprojects:\n\n  - name: {existing_name}\n",
    }

    queue_dict = {
        "id": 1,
        "code": "queue-001",
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

    assert result.count(existing_name) == 1
