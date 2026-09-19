"""
Integration tests for Terragrunt Management API Endpoints
"""
import pytest
from httpx import AsyncClient
from unittest.mock import patch


@pytest.mark.asyncio
class TestTerragruntMgmtEndpoint:
    """Integration tests for terragrunt management API endpoint"""

    async def test_push_infra_config_success(self, client: AsyncClient):
        """Test successful push of terragrunt infrastructure config"""
        # Arrange
        payload = {
            "terragrunt_content": "terraform {\n  source = \"../layers/s3\"\n}\n\ninputs = {\n  bucket_name = \"test\"\n}",
            "tenant": "test-tenant",
            "environment": "dev",
            "github_repository": "owner/repo",
            "branch_name": "main",
            "commit_message": "Test commit"
        }

        mock_result = {
            "commit_sha": "abc123def456",
            "file_path": "tenant/test-tenant/environment/dev/terragrunt.hcl",
            "commit_url": "https://github.com/owner/repo/commit/abc123def456",
            "html_url": "https://github.com/owner/repo/blob/main/tenant/test-tenant/environment/dev/terragrunt.hcl"
        }

        with patch('app.integrations.github_integration.GitHubIntegration.commit_workflow_file', return_value=mock_result):
            # Act
            response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

            # Assert
            assert response.status_code == 201
            data = response.json()

            assert data["success"] is True
            assert data["commit_sha"] == "abc123def456"
            assert data["file_path"] == "tenant/test-tenant/environment/dev/terragrunt.hcl"
            assert data["commit_url"] == "https://github.com/owner/repo/commit/abc123def456"
            assert "message" in data

    async def test_push_infra_config_missing_required_field(self, client: AsyncClient):
        """Test validation error when required field is missing"""
        # Arrange - missing tenant
        payload = {
            "terragrunt_content": "terraform {}",
            "environment": "dev",
            "github_repository": "owner/repo",
            "branch_name": "main"
        }

        # Act
        response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

        # Assert
        assert response.status_code == 422  # Pydantic validation error
        data = response.json()
        assert "detail" in data

    async def test_push_infra_config_empty_terragrunt_content(self, client: AsyncClient):
        """Test validation error for empty terragrunt content"""
        # Arrange
        payload = {
            "terragrunt_content": "",
            "tenant": "test-tenant",
            "environment": "dev",
            "github_repository": "owner/repo",
            "branch_name": "main"
        }

        # Act
        response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

        # Assert
        assert response.status_code == 422  # Pydantic validation error

    async def test_push_infra_config_empty_tenant(self, client: AsyncClient):
        """Test validation error for empty tenant"""
        # Arrange
        payload = {
            "terragrunt_content": "terraform {}",
            "tenant": "",
            "environment": "dev",
            "github_repository": "owner/repo",
            "branch_name": "main"
        }

        # Act
        response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

        # Assert
        assert response.status_code == 422  # Pydantic validation error

    async def test_push_infra_config_invalid_repository_format(self, client: AsyncClient):
        """Test validation error for invalid repository format"""
        # Arrange
        payload = {
            "terragrunt_content": "terraform {}",
            "tenant": "test-tenant",
            "environment": "dev",
            "github_repository": "invalid-format",  # Missing slash
            "branch_name": "main"
        }

        # Act
        response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "repository format" in data["detail"].lower()

    async def test_push_infra_config_without_commit_message(self, client: AsyncClient):
        """Test successful push without custom commit message (auto-generated)"""
        # Arrange
        payload = {
            "terragrunt_content": "terraform {}",
            "tenant": "test-tenant",
            "environment": "prod",
            "github_repository": "owner/repo",
            "branch_name": "main"
        }

        mock_result = {
            "commit_sha": "xyz789",
            "file_path": "tenant/test-tenant/environment/prod/terragrunt.hcl",
            "commit_url": "https://github.com/owner/repo/commit/xyz789",
            "html_url": "https://github.com/owner/repo/blob/main/tenant/test-tenant/environment/prod/terragrunt.hcl"
        }

        with patch('app.integrations.github_integration.GitHubIntegration.commit_workflow_file', return_value=mock_result):
            # Act
            response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

            # Assert
            assert response.status_code == 201
            data = response.json()
            assert data["success"] is True

    async def test_push_infra_config_github_authentication_error(self, client: AsyncClient):
        """Test error handling for GitHub authentication failure"""
        # Arrange
        payload = {
            "terragrunt_content": "terraform {}",
            "tenant": "test-tenant",
            "environment": "dev",
            "github_repository": "owner/repo",
            "branch_name": "main"
        }

        with patch('app.integrations.github_integration.GitHubIntegration.commit_workflow_file',
                   side_effect=Exception("GitHub authentication failed: Invalid or expired token")):
            # Act
            response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

            # Assert
            assert response.status_code == 401
            data = response.json()
            assert "detail" in data

    async def test_push_infra_config_github_rate_limit_error(self, client: AsyncClient):
        """Test error handling for GitHub rate limit exceeded"""
        # Arrange
        payload = {
            "terragrunt_content": "terraform {}",
            "tenant": "test-tenant",
            "environment": "dev",
            "github_repository": "owner/repo",
            "branch_name": "main"
        }

        with patch('app.integrations.github_integration.GitHubIntegration.commit_workflow_file',
                   side_effect=Exception("GitHub API rate limit exceeded or insufficient permissions")):
            # Act
            response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

            # Assert
            assert response.status_code == 403
            data = response.json()
            assert "detail" in data

    async def test_push_infra_config_repository_not_found(self, client: AsyncClient):
        """Test error handling for repository not found"""
        # Arrange
        payload = {
            "terragrunt_content": "terraform {}",
            "tenant": "test-tenant",
            "environment": "dev",
            "github_repository": "owner/nonexistent-repo",
            "branch_name": "main"
        }

        with patch('app.integrations.github_integration.GitHubIntegration.commit_workflow_file',
                   side_effect=Exception("Repository 'owner/nonexistent-repo' or branch 'main' not found")):
            # Act
            response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

            # Assert
            assert response.status_code == 404
            data = response.json()
            assert "detail" in data

    async def test_push_infra_config_with_special_characters_in_content(self, client: AsyncClient):
        """Test push with special characters in terragrunt content"""
        # Arrange
        payload = {
            "terragrunt_content": "terraform {\n  source = \"git::https://example.com/repo.git?ref=v1.0.0\"\n}\n\ninputs = {\n  name = \"test-${var.env}\"\n}",
            "tenant": "test-tenant",
            "environment": "dev",
            "github_repository": "owner/repo",
            "branch_name": "main"
        }

        mock_result = {
            "commit_sha": "special123",
            "file_path": "tenant/test-tenant/environment/dev/terragrunt.hcl",
            "commit_url": "https://github.com/owner/repo/commit/special123",
            "html_url": "https://github.com/owner/repo/blob/main/tenant/test-tenant/environment/dev/terragrunt.hcl"
        }

        with patch('app.integrations.github_integration.GitHubIntegration.commit_workflow_file', return_value=mock_result):
            # Act
            response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

            # Assert
            assert response.status_code == 201
            data = response.json()
            assert data["success"] is True

    async def test_push_infra_config_different_environments(self, client: AsyncClient):
        """Test push to different environments (dev, staging, prod)"""
        # Arrange
        environments = ["dev", "staging", "prod"]

        for env in environments:
            payload = {
                "terragrunt_content": f"# Configuration for {env}",
                "tenant": "test-tenant",
                "environment": env,
                "github_repository": "owner/repo",
                "branch_name": "main"
            }

            mock_result = {
                "commit_sha": f"{env}123",
                "file_path": f"tenant/test-tenant/environment/{env}/terragrunt.hcl",
                "commit_url": f"https://github.com/owner/repo/commit/{env}123",
                "html_url": f"https://github.com/owner/repo/blob/main/tenant/test-tenant/environment/{env}/terragrunt.hcl"
            }

            with patch('app.integrations.github_integration.GitHubIntegration.commit_workflow_file', return_value=mock_result):
                # Act
                response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

                # Assert
                assert response.status_code == 201
                data = response.json()
                assert data["file_path"] == f"tenant/test-tenant/environment/{env}/terragrunt.hcl"

    async def test_push_infra_config_different_tenants(self, client: AsyncClient):
        """Test push for different tenants"""
        # Arrange
        tenants = ["acme-corp", "contoso", "fabrikam"]

        for tenant in tenants:
            payload = {
                "terragrunt_content": f"# Configuration for {tenant}",
                "tenant": tenant,
                "environment": "dev",
                "github_repository": "owner/repo",
                "branch_name": "main"
            }

            mock_result = {
                "commit_sha": f"{tenant}123",
                "file_path": f"tenant/{tenant}/environment/dev/terragrunt.hcl",
                "commit_url": f"https://github.com/owner/repo/commit/{tenant}123",
                "html_url": f"https://github.com/owner/repo/blob/main/tenant/{tenant}/environment/dev/terragrunt.hcl"
            }

            with patch('app.integrations.github_integration.GitHubIntegration.commit_workflow_file', return_value=mock_result):
                # Act
                response = await client.post("/api/v1/terragrunt/push-infra-config", json=payload)

                # Assert
                assert response.status_code == 201
                data = response.json()
                assert data["file_path"] == f"tenant/{tenant}/environment/dev/terragrunt.hcl"
