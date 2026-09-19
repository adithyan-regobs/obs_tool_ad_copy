import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_env_configs_script_gen_component import (
    AsporaEnvConfigsScriptGenComponent,
)
from app.plugin.aspora.script_gen_components.aspora_env_secrets_script_gen_component import (
    AsporaEnvSecretsScriptGenComponent,
    DUMMY_SECRETS_MAPPING,
)


@pytest.fixture
def workflow_context():
    return SimpleNamespace(
        staged_files=[],
        commit_messages={},
        script_gen_responses={1: {"env_configs": {}, "env_secrets": {}}},
        skip_commit=True,
    )


@pytest.fixture
def env_configs_file_location():
    return SimpleNamespace(
        repo="owner/repo",
        file_path="environment/core-dev-01/ap-south-1/envs/test-service/non-secure/test-service-configs.json",
        base_branch="main",
        feature_branch="feature/test",
        script_gen_key="env_configs",
    )


@pytest.fixture
def env_secrets_file_location():
    return SimpleNamespace(
        repo="owner/repo",
        file_path="environment/core-dev-01/ap-south-1/envs/test-service/secure/test-service-secrets.json",
        base_branch="main",
        feature_branch="feature/test",
        script_gen_key="env_secrets",
    )


@pytest.mark.asyncio
async def test_env_configs_missing_service_name_raises(env_configs_file_location, workflow_context):
    generator = AsporaEnvConfigsScriptGenComponent()
    queue_dict = {"id": 1, "config_snapshot": {}}

    with pytest.raises(ValueError, match="service_name is required"):
        await generator.generate(
            tenant="aspora",
            repository=None,
            file_location=env_configs_file_location,
            queue_dict=queue_dict,
            workflow_context=workflow_context,
            upload_to_s3=False,
        )


@pytest.mark.asyncio
async def test_env_configs_skip_falcon(env_configs_file_location, workflow_context):
    generator = AsporaEnvConfigsScriptGenComponent()
    queue_dict = {
        "id": 1,
        "config_snapshot": {
            "service_name": "falcon-api",
            "product_name": "falcon",
        },
    }

    result = await generator.generate(
        tenant="vance",
        repository=None,
        file_location=env_configs_file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert result == ""
    assert workflow_context.script_gen_responses[1]["env_configs"]["original_content"] == ""


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_env_configs_script_gen_component.GitOpsHandler.get_content")
async def test_env_configs_existing_file_preserved(
    mock_get_content,
    env_configs_file_location,
    workflow_context,
):
    generator = AsporaEnvConfigsScriptGenComponent()
    mock_get_content.return_value = {"exists": True, "content": '{"sentinel": "keep"}\n'}
    queue_dict = {
        "id": 1,
        "config_snapshot": {"service_name": "test-service", "product_name": "core"},
    }

    result = await generator.generate(
        tenant="aspora",
        repository=None,
        file_location=env_configs_file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert result == '{"sentinel": "keep"}\n'


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_env_configs_script_gen_component.GitOpsHandler.get_content")
async def test_env_configs_missing_file_creates_dummy(
    mock_get_content,
    env_configs_file_location,
    workflow_context,
):
    generator = AsporaEnvConfigsScriptGenComponent()
    mock_get_content.return_value = {"exists": False}
    queue_dict = {
        "id": 1,
        "config_snapshot": {"service_name": "test-service", "product_name": "core"},
    }

    result = await generator.generate(
        tenant="aspora",
        repository=None,
        file_location=env_configs_file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    data = json.loads(result)
    assert data == {"dummy_config": "dummy-value"}


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_env_configs_script_gen_component.GitOpsHandler.get_content")
async def test_env_configs_gitops_error_raises(
    mock_get_content,
    env_configs_file_location,
    workflow_context,
):
    generator = AsporaEnvConfigsScriptGenComponent()
    mock_get_content.return_value = {"status": "error", "error": "boom"}
    queue_dict = {
        "id": 1,
        "config_snapshot": {"service_name": "test-service", "product_name": "core"},
    }

    with pytest.raises(ValueError, match="GitOps get_content failed: boom"):
        await generator.generate(
            tenant="aspora",
            repository=None,
            file_location=env_configs_file_location,
            queue_dict=queue_dict,
            workflow_context=workflow_context,
            upload_to_s3=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "queue_dict,expected_message",
    [
        ({"id": 1, "config_snapshot": {}}, "service_name is required"),
        ({"id": 1, "config_snapshot": {"service_name": "svc"}}, "product_name is required"),
        (
            {"id": 1, "config_snapshot": {"service_name": "svc", "product_name": "core"}},
            "region is required",
        ),
    ],
)
async def test_env_secrets_missing_required_fields_raise(
    queue_dict,
    expected_message,
    env_secrets_file_location,
    workflow_context,
):
    generator = AsporaEnvSecretsScriptGenComponent()

    with pytest.raises(ValueError, match=expected_message):
        await generator.generate(
            tenant="aspora",
            repository=None,
            file_location=env_secrets_file_location,
            queue_dict=queue_dict,
            workflow_context=workflow_context,
            upload_to_s3=False,
        )


@pytest.mark.asyncio
async def test_env_secrets_skip_falcon(env_secrets_file_location, workflow_context):
    generator = AsporaEnvSecretsScriptGenComponent()
    queue_dict = {
        "id": 1,
        "config_snapshot": {
            "service_name": "falcon-api",
            "product_name": "falcon",
            "region": "ap-south-1",
        },
    }

    result = await generator.generate(
        tenant="vance",
        repository=None,
        file_location=env_secrets_file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert result == ""
    assert workflow_context.script_gen_responses[1]["env_secrets"]["original_content"] == ""


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_env_secrets_script_gen_component.GitOpsHandler.get_content")
async def test_env_secrets_existing_file_preserved(
    mock_get_content,
    env_secrets_file_location,
    workflow_context,
):
    generator = AsporaEnvSecretsScriptGenComponent()
    mock_get_content.return_value = {"exists": True, "content": '{"sentinel": "keep"}\n'}
    queue_dict = {
        "id": 1,
        "config_snapshot": {
            "service_name": "test-service",
            "product_name": "core",
            "region": "ap-south-1",
        },
    }

    result = await generator.generate(
        tenant="aspora",
        repository=None,
        file_location=env_secrets_file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    assert result == '{"sentinel": "keep"}\n'


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_env_secrets_script_gen_component.GitOpsHandler.get_content")
async def test_env_secrets_missing_file_creates_dummy(
    mock_get_content,
    env_secrets_file_location,
    workflow_context,
):
    generator = AsporaEnvSecretsScriptGenComponent()
    mock_get_content.return_value = {"exists": False}
    queue_dict = {
        "id": 1,
        "config_snapshot": {
            "service_name": "test-service",
            "product_name": "core",
            "environment": "stage",
            "geo_loc_mst_code": "region-aspora-mumbai",
        },
    }

    result = await generator.generate(
        tenant="aspora",
        repository=None,
        file_location=env_secrets_file_location,
        queue_dict=queue_dict,
        workflow_context=workflow_context,
        upload_to_s3=False,
    )

    data = json.loads(result)
    assert data["AWS_REGION"] == DUMMY_SECRETS_MAPPING["core-stage-mumbai"]


@pytest.mark.asyncio
@patch("app.plugin.aspora.script_gen_components.aspora_env_secrets_script_gen_component.GitOpsHandler.get_content")
async def test_env_secrets_gitops_error_raises(
    mock_get_content,
    env_secrets_file_location,
    workflow_context,
):
    generator = AsporaEnvSecretsScriptGenComponent()
    mock_get_content.return_value = {"status": "error", "error": "boom"}
    queue_dict = {
        "id": 1,
        "config_snapshot": {
            "service_name": "test-service",
            "product_name": "core",
            "region": "ap-south-1",
        },
    }

    with pytest.raises(ValueError, match="GitOps get_content failed: boom"):
        await generator.generate(
            tenant="aspora",
            repository=None,
            file_location=env_secrets_file_location,
            queue_dict=queue_dict,
            workflow_context=workflow_context,
            upload_to_s3=False,
        )
