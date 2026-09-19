import pytest
from types import SimpleNamespace
from unittest.mock import patch

from app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component import AsporaEksScriptGenComponent


class TestAsporaEksScriptGenComponent:
    @pytest.mark.asyncio
    @patch('app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component.FileManagerHandler.upload_file')
    @patch('app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component.GitHubIntegration.get_file_content')
    async def test_generate_respects_upload_flags_from_parameters(
        self, mock_github, mock_upload
    ):
        mock_github.return_value = {"exists": True, "content": "service_name: {{SERVICE_NAME}}"}
        mock_upload.return_value = {"location": "s3://bucket/key"}

        generator = AsporaEksScriptGenComponent()
        parameters = {
            "service_name": "my-eks-service",
            "github_token": "token",
            "github_base_url": "https://api.github.com",
            "owner": "owner",
            "repo": "repo",
            "base_branch": "main",
            "upload_to_s3": True,
            "s3_bucket": "custom-bucket"
        }

        result = await generator.generate(
            file_path="configs/dev/my-eks-service/config.yaml",
            parameters=parameters,
            upload_to_s3=False,
            s3_bucket="ignored-bucket"
        )

        assert "my-eks-service" in result
        assert mock_upload.call_count == 2
        for call in mock_upload.call_args_list:
            assert call.kwargs["bucket"] == "custom-bucket"

    @pytest.mark.asyncio
    @patch('app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component.FileManagerHandler.upload_file')
    @patch('app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component.GitHubIntegration.get_file_content')
    async def test_generate_uses_identifier_from_file_path(
        self, mock_github, mock_upload
    ):
        mock_github.return_value = {"exists": True, "content": "service_name: {{SERVICE_NAME}}"}
        mock_upload.return_value = {"location": "s3://bucket/key"}

        generator = AsporaEksScriptGenComponent()
        parameters = {
            "service_name": "",
            "github_token": "token",
            "github_base_url": "https://api.github.com",
            "owner": "owner",
            "repo": "repo",
            "base_branch": "main",
            "upload_to_s3": True,
            "s3_bucket": "custom-bucket"
        }

        await generator.generate(
            file_path="configs/dev/eks-service/config.yaml",
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket="custom-bucket"
        )

        original_call = mock_upload.call_args_list[0]
        assert original_call.kwargs["key"].startswith("eks/eks-service.yaml")

    @pytest.mark.asyncio
    @patch('app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component.GitOpsHandler.create_commit')
    @patch('app.plugin.aspora.script_gen_components.aspora_eks_script_gen_component.GitHubIntegration.get_file_content')
    async def test_generate_updates_workflow_context(
        self, mock_github, mock_commit
    ):
        mock_github.return_value = {"exists": True, "content": "service_name: {{SERVICE_NAME}}"}

        generator = AsporaEksScriptGenComponent()
        parameters = {
            "service_name": "my-eks-service",
            "github_token": "token",
            "github_base_url": "https://api.github.com",
            "owner": "owner",
            "repo": "repo",
            "base_branch": "main"
        }

        file_location = SimpleNamespace(
            repo="owner/repo",
            file_path="configs/dev/my-eks-service/config.yaml",
            base_branch="main",
            feature_branch="feature/eks",
            script_gen_key="eks"
        )
        workflow_context = SimpleNamespace(script_gen_responses={1: {"eks": []}})

        result = await generator.generate(
            file_path=file_location.file_path,
            parameters=parameters,
            tenant="aspora",
            queue_id=1,
            file_location=file_location,
            workflow_context=workflow_context
        )

        assert result is not None
        assert workflow_context.script_gen_responses[1]["eks"][0] == result
        mock_commit.assert_called_once()
