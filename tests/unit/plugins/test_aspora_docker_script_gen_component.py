"""
Unit tests for AsporaDockerScriptGenComponent
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component import (
    AsporaDockerScriptGenComponent,
)
from app.handlers.gitops_handler import GitOpsHandler


@pytest.fixture
def base_params():
    return {
        "github_token": "token",
        "github_base_url": "https://api.github.com",
        "owner": "owner",
        "repo": "repo",
        "base_branch": "main",
        "file_path": "Dockerfile",
        "language_name": "Java",
        "language_version": "21",
        "service_name": "my-service",
    }


@pytest.fixture
def script_generator():
    return AsporaDockerScriptGenComponent()

@pytest.fixture
def file_location():
    return SimpleNamespace(
        repo="owner/repo",
        file_path="Dockerfile",
        base_branch="main",
        feature_branch="feature/test",
        script_gen_key="ecs_dockerfile",
        queue_code="queue-001"
    )

@pytest.fixture
def workflow_context():
    return SimpleNamespace(script_gen_responses={1: {"ecs_dockerfile": []}})

@pytest.mark.asyncio
@patch(
    "app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component.GitHubIntegration.get_file_content"
)
@patch(
    "app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component.FileManagerHandler.upload_file"
)
async def test_generate_returns_existing_content_no_upload(
    mock_upload_file,
    mock_get_file_content,
    script_generator,
    base_params
):
    mock_get_file_content.return_value = {"exists": True, "content": "FROM alpine\n"}
    with patch.object(script_generator, "_generate_dockerfile_content") as mock_generate:
        result = await script_generator.generate(base_params)

    assert result == "FROM alpine\n"
    mock_generate.assert_not_called()
    mock_upload_file.assert_not_called()


@pytest.mark.asyncio
@patch(
    "app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component.GitHubIntegration.get_file_content"
)
async def test_generate_generates_when_missing(
    mock_get_file_content,
    script_generator,
    base_params
):
    mock_get_file_content.return_value = {"exists": False}
    with patch.object(script_generator, "_generate_dockerfile_content", return_value="generated") as mock_generate:
        result = await script_generator.generate(base_params)

    assert result == "generated"
    mock_generate.assert_called_once()


@pytest.mark.asyncio
@patch(
    "app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component.GitHubIntegration.get_file_content"
)
async def test_generate_unsupported_language_raises(
    mock_get_file_content,
    script_generator,
    base_params
):
    mock_get_file_content.return_value = {"exists": False}
    params = dict(base_params)
    params["language_name"] = "Ruby"

    with pytest.raises(ValueError, match="Unsupported language"):
        await script_generator.generate(params)


@pytest.mark.asyncio
@patch(
    "app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component.GitHubIntegration.get_file_content"
)
@patch(
    "app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component.FileManagerHandler.upload_file"
)
async def test_generate_uploads_original_to_s3_and_updates_db(
    mock_upload_file,
    mock_get_file_content,
    base_params
):
    mock_get_file_content.return_value = {"exists": False}
    mock_upload_file.return_value = {"location": "s3://bucket/dockerfiles/Q1.Dockerfile"}

    repository = MagicMock()
    repository.update_artifact_s3_key = AsyncMock()
    script_generator = AsporaDockerScriptGenComponent(repository=repository)

    with patch.object(script_generator, "_generate_dockerfile_content", return_value="generated"):
        result = await script_generator.generate(
            base_params,
            upload_to_s3=True,
            s3_bucket="bucket",
            queue_item_code="Q1"
        )

    assert result == "generated"
    mock_upload_file.assert_called_once()
    upload_call = mock_upload_file.call_args.kwargs
    assert upload_call["bucket"] == "bucket"
    assert upload_call["key"] == "dockerfiles/Q1.Dockerfile"
    assert upload_call["content"] == "generated"

    repository.update_artifact_s3_key.assert_awaited_once()
    update_call = repository.update_artifact_s3_key.call_args.args
    assert update_call[0] == "Q1"
    artifact_payload = json.loads(update_call[1])
    assert artifact_payload["original_s3_key"] == "dockerfiles/Q1.Dockerfile"
    assert artifact_payload["preview"] == "dockerfiles/Q1.Dockerfile"


@pytest.mark.asyncio
@patch(
    "app.plugin.aspora.script_gen_components.aspora_docker_script_gen_component.GitHubIntegration.get_file_content"
)
async def test_generate_creates_commit_and_updates_workflow_context(
    mock_get_file_content,
    script_generator,
    base_params,
    file_location,
    workflow_context,
    monkeypatch
):
    mock_get_file_content.return_value = {"exists": False}
    commit_calls = []

    def fake_create_commit(**kwargs):
        commit_calls.append(kwargs)
        return {"commit_sha": "abc123"}

    monkeypatch.setattr(GitOpsHandler, "create_commit", fake_create_commit)

    with patch.object(script_generator, "_generate_dockerfile_content", return_value="generated"):
        result = await script_generator.generate(
            base_params,
            tenant="aspora",
            queue_id=1,
            file_location=file_location,
            workflow_context=workflow_context
        )

    assert result == "generated"
    assert commit_calls
    assert workflow_context.script_gen_responses[1]["ecs_dockerfile"][0] == result
