"""
Unit tests for Terragrunt Management Service
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from app.services.terragrunt_mgmt_service import TerragruntMgmtService
from app.domain.validators.github_rules import GitHubValidationError


class TestTerragruntMgmtService:
    """Unit tests for TerragruntMgmtService"""

    def setup_method(self):
        """Set up test fixtures"""
        self.service = TerragruntMgmtService()

    def test_init_loads_settings(self):
        """Test that service initializes with settings from environment"""
        assert hasattr(self.service, 'github_token')
        assert hasattr(self.service, 'github_base_url')

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_success(self):
        """Test successful commit of terragrunt file"""
        # Arrange
        terragrunt_content = "terraform {\n  source = \"test\"\n}"
        tenant = "test-tenant"
        environment = "dev"
        github_repository = "owner/repo"
        branch_name = "main"
        commit_message = "Test commit"

        mock_result = {
            "commit_sha": "abc123",
            "file_path": "tenant/test-tenant/environment/dev/terragrunt.hcl",
            "commit_url": "https://github.com/owner/repo/commit/abc123",
            "html_url": "https://github.com/owner/repo/blob/main/tenant/test-tenant/environment/dev/terragrunt.hcl"
        }

        with patch('app.services.terragrunt_mgmt_service.GitHubIntegration.commit_workflow_file', return_value=mock_result):
            # Act
            result = await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name,
                commit_message=commit_message
            )

            # Assert
            assert result["success"] is True
            assert result["commit_sha"] == "abc123"
            assert result["file_path"] == "tenant/test-tenant/environment/dev/terragrunt.hcl"
            assert "message" in result

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_invalid_repository_format(self):
        """Test validation error for invalid repository format"""
        # Arrange
        terragrunt_content = "terraform {}"
        tenant = "test-tenant"
        environment = "dev"
        github_repository = "invalid-format"  # Missing slash
        branch_name = "main"

        # Act & Assert
        with pytest.raises(GitHubValidationError) as exc_info:
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name
            )

        assert "Invalid GitHub repository format" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_empty_tenant(self):
        """Test validation error for empty tenant"""
        # Arrange
        terragrunt_content = "terraform {}"
        tenant = ""
        environment = "dev"
        github_repository = "owner/repo"
        branch_name = "main"

        # Act & Assert
        with pytest.raises(GitHubValidationError) as exc_info:
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name
            )

        assert "Tenant name cannot be empty" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_empty_environment(self):
        """Test validation error for empty environment"""
        # Arrange
        terragrunt_content = "terraform {}"
        tenant = "test-tenant"
        environment = ""
        github_repository = "owner/repo"
        branch_name = "main"

        # Act & Assert
        with pytest.raises(GitHubValidationError) as exc_info:
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name
            )

        assert "Environment name cannot be empty" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_empty_content(self):
        """Test validation error for empty terragrunt content"""
        # Arrange
        terragrunt_content = ""
        tenant = "test-tenant"
        environment = "dev"
        github_repository = "owner/repo"
        branch_name = "main"

        # Act & Assert
        with pytest.raises(GitHubValidationError) as exc_info:
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name
            )

        assert "Terragrunt content cannot be empty" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_empty_branch(self):
        """Test validation error for empty branch name"""
        # Arrange
        terragrunt_content = "terraform {}"
        tenant = "test-tenant"
        environment = "dev"
        github_repository = "owner/repo"
        branch_name = ""

        # Act & Assert
        with pytest.raises(GitHubValidationError) as exc_info:
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name
            )

        assert "Branch name cannot be empty" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_auto_generates_commit_message(self):
        """Test that commit message is auto-generated when not provided"""
        # Arrange
        terragrunt_content = "terraform {}"
        tenant = "test-tenant"
        environment = "dev"
        github_repository = "owner/repo"
        branch_name = "main"

        mock_result = {
            "commit_sha": "abc123",
            "file_path": "tenant/test-tenant/environment/dev/terragrunt.hcl",
            "commit_url": "https://github.com/owner/repo/commit/abc123",
            "html_url": "https://github.com/owner/repo/blob/main/tenant/test-tenant/environment/dev/terragrunt.hcl"
        }

        with patch('app.services.terragrunt_mgmt_service.GitHubIntegration.commit_workflow_file', return_value=mock_result) as mock_commit:
            # Act
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name
            )

            # Assert - Check that commit_workflow_file was called with auto-generated message
            call_args = mock_commit.call_args
            assert call_args[1]['message'] == "Add terragrunt configuration for tenant/test-tenant/environment/dev"

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_constructs_correct_path(self):
        """Test that file path is constructed correctly"""
        # Arrange
        terragrunt_content = "terraform {}"
        tenant = "acme-corp"
        environment = "staging"
        github_repository = "owner/repo"
        branch_name = "main"

        mock_result = {
            "commit_sha": "abc123",
            "file_path": "tenant/acme-corp/environment/staging/terragrunt.hcl",
            "commit_url": "https://github.com/owner/repo/commit/abc123",
            "html_url": "https://github.com/owner/repo/blob/main/tenant/acme-corp/environment/staging/terragrunt.hcl"
        }

        with patch('app.services.terragrunt_mgmt_service.GitHubIntegration.commit_workflow_file', return_value=mock_result) as mock_commit:
            # Act
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name
            )

            # Assert - Check that file_path parameter is correct
            call_args = mock_commit.call_args
            assert call_args[1]['file_path'] == "tenant/acme-corp/environment/staging/terragrunt.hcl"

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_strips_whitespace(self):
        """Test that whitespace is stripped from tenant and environment"""
        # Arrange
        terragrunt_content = "terraform {}"
        tenant = "  test-tenant  "
        environment = "  dev  "
        github_repository = "owner/repo"
        branch_name = "main"

        mock_result = {
            "commit_sha": "abc123",
            "file_path": "tenant/test-tenant/environment/dev/terragrunt.hcl",
            "commit_url": "https://github.com/owner/repo/commit/abc123",
            "html_url": "https://github.com/owner/repo/blob/main/tenant/test-tenant/environment/dev/terragrunt.hcl"
        }

        with patch('app.services.terragrunt_mgmt_service.GitHubIntegration.commit_workflow_file', return_value=mock_result) as mock_commit:
            # Act
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name
            )

            # Assert - Check that whitespace was stripped
            call_args = mock_commit.call_args
            assert call_args[1]['file_path'] == "tenant/test-tenant/environment/dev/terragrunt.hcl"

    @pytest.mark.asyncio
    async def test_commit_terragrunt_file_uses_custom_commit_message(self):
        """Test that custom commit message is used when provided"""
        # Arrange
        terragrunt_content = "terraform {}"
        tenant = "test-tenant"
        environment = "dev"
        github_repository = "owner/repo"
        branch_name = "main"
        commit_message = "Custom commit message for testing"

        mock_result = {
            "commit_sha": "abc123",
            "file_path": "tenant/test-tenant/environment/dev/terragrunt.hcl",
            "commit_url": "https://github.com/owner/repo/commit/abc123",
            "html_url": "https://github.com/owner/repo/blob/main/tenant/test-tenant/environment/dev/terragrunt.hcl"
        }

        with patch('app.services.terragrunt_mgmt_service.GitHubIntegration.commit_workflow_file', return_value=mock_result) as mock_commit:
            # Act
            await self.service.commit_terragrunt_file(
                terragrunt_content=terragrunt_content,
                tenant=tenant,
                environment=environment,
                github_repository=github_repository,
                branch_name=branch_name,
                commit_message=commit_message
            )

            # Assert - Check that custom message was used
            call_args = mock_commit.call_args
            assert call_args[1]['message'] == "Custom commit message for testing"
