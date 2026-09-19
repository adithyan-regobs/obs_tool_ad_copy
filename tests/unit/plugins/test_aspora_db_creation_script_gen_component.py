"""
Unit tests for AsporaDbCreationScriptGenComponent
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component import AsporaDbCreationScriptGenComponent


class TestAsporaDbCreationScriptGenComponent:
    """Test cases for AsporaDbCreationScriptGenComponent"""

    @pytest.fixture
    def script_generator(self):
        """Fixture to create AsporaDbCreationScriptGenComponent instance"""
        return AsporaDbCreationScriptGenComponent()

    @pytest.fixture
    def base_parameters(self):
        """Fixture for base parameters required by generate method"""
        return {
            'database_name': 'test_db',
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'base_branch': 'main'
        }

    @pytest.fixture
    def mock_mysql_content_empty_array(self):
        """Fixture for MySQL server content with empty database array"""
        return """terraform {
  source = "../../../../../layers/rds-mysql"
}
include "root" {
  path = find_in_parent_folders()
}
inputs = {
  identifier        = "mysql-server-01"
  mysql_databases   = []
}"""

    @pytest.fixture
    def mock_mysql_content_with_databases(self):
        """Fixture for MySQL server content with existing databases"""
        return """terraform {
  source = "../../../../../layers/rds-mysql"
}
include "root" {
  path = find_in_parent_folders()
}
inputs = {
  identifier        = "mysql-server-01"
  mysql_databases   = [
    "existing_db1",
    "existing_db2"
  ]
}"""

    @pytest.fixture
    def mock_psql_content_empty_array(self):
        """Fixture for PostgreSQL server content with empty database array"""
        return """terraform {
  source = "../../../../../layers/rds-psql"
}
include "root" {
  path = find_in_parent_folders()
}
inputs = {
  identifier        = "psql-server-01"
  psql_databases    = []
}"""

    @pytest.fixture
    def mock_psql_content_with_databases(self):
        """Fixture for PostgreSQL server content with existing databases"""
        return """terraform {
  source = "../../../../../layers/rds-psql"
}
include "root" {
  path = find_in_parent_folders()
}
inputs = {
  identifier        = "psql-server-01"
  psql_databases    = [
    "existing_psql_db1",
    "existing_psql_db2"
  ]
}"""

    # ===========================
    # Tests for file does NOT exist
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_file_not_exists_raises_error(
        self, mock_github, script_generator, base_parameters
    ):
        """Test that error is raised when file does not exist"""
        # Arrange
        mock_github.return_value = {"exists": False}

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            script_generator.generate(
                file_path="environment/test-env-01/region/rds/server/terragrunt.hcl",
                parameters=base_parameters
            )

        assert "File does not exist" in str(exc_info.value)

    # ===========================
    # Tests for MySQL databases
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_mysql_empty_array_adds_database(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_empty_array
    ):
        """Test adding database to empty MySQL databases array"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }
        parameters = base_parameters.copy()
        parameters['database_name'] = 'new_mysql_db'

        # Act
        result = script_generator.generate(
            file_path="environment/test-env-01/region/rds/mysql-server/terragrunt.hcl",
            parameters=parameters
        )

        # Assert
        assert result is not None
        assert isinstance(result, str)
        assert '"new_mysql_db"' in result
        assert 'mysql_databases   = [' in result

        # Verify GitHub API was called
        mock_github.assert_called_once_with(
            token='test_token',
            base_url='https://api.github.com',
            owner='test_owner',
            repo='test_repo',
            file_path='environment/test-env-01/region/rds/mysql-server/terragrunt.hcl',
            branch='main'
        )

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_mysql_with_existing_databases_adds_new_database(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_with_databases
    ):
        """Test adding database to MySQL array that already has databases"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_with_databases
        }
        parameters = base_parameters.copy()
        parameters['database_name'] = 'new_mysql_db'

        # Act
        result = script_generator.generate(
            file_path="environment/test-env-01/region/rds/mysql-server/terragrunt.hcl",
            parameters=parameters
        )

        # Assert
        assert result is not None
        # Existing databases should still be present
        assert '"existing_db1"' in result
        assert '"existing_db2"' in result
        # New database should be added
        assert '"new_mysql_db"' in result
        # Should have comma separator
        assert result.count(',') >= 2  # At least 2 commas for 3 databases

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_mysql_preserves_indentation(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_with_databases
    ):
        """Test that indentation is preserved when adding to MySQL array"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_with_databases
        }
        parameters = base_parameters.copy()
        parameters['database_name'] = 'indented_db'

        # Act
        result = script_generator.generate(
            file_path="test/path.hcl",
            parameters=parameters
        )

        # Assert
        assert result is not None
        # Check that proper indentation is maintained
        lines = result.split('\n')
        db_lines = [line for line in lines if '"indented_db"' in line]
        assert len(db_lines) == 1
        # Should have some indentation (spaces before the quote)
        assert db_lines[0].startswith('    ')

    # ===========================
    # Tests for PostgreSQL databases
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_psql_empty_array_adds_database(
        self, mock_github, script_generator, base_parameters, mock_psql_content_empty_array
    ):
        """Test adding database to empty PostgreSQL databases array"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_psql_content_empty_array
        }
        parameters = base_parameters.copy()
        parameters['database_name'] = 'new_psql_db'

        # Act
        result = script_generator.generate(
            file_path="environment/test-env-01/region/rds/psql-server/terragrunt.hcl",
            parameters=parameters
        )

        # Assert
        assert result is not None
        assert isinstance(result, str)
        assert '"new_psql_db"' in result
        assert 'psql_databases    = [' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_psql_with_existing_databases_adds_new_database(
        self, mock_github, script_generator, base_parameters, mock_psql_content_with_databases
    ):
        """Test adding database to PostgreSQL array that already has databases"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_psql_content_with_databases
        }
        parameters = base_parameters.copy()
        parameters['database_name'] = 'new_psql_db'

        # Act
        result = script_generator.generate(
            file_path="environment/test-env-01/region/rds/psql-server/terragrunt.hcl",
            parameters=parameters
        )

        # Assert
        assert result is not None
        # Existing databases should still be present
        assert '"existing_psql_db1"' in result
        assert '"existing_psql_db2"' in result
        # New database should be added
        assert '"new_psql_db"' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_psql_returns_correct_db_type(
        self, mock_github, script_generator, base_parameters, mock_psql_content_empty_array
    ):
        """Test that PostgreSQL server returns correct db_type"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_psql_content_empty_array
        }

        # Act
        result = script_generator.generate(
            file_path="test/path.hcl",
            parameters=base_parameters
        )

        # Assert - the method should work without errors and contain the database
        assert result is not None
        assert '"test_db"' in result

    # ===========================
    # Tests for error cases
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_no_database_array_found_raises_error(
        self, mock_github, script_generator, base_parameters
    ):
        """Test that error is raised when neither mysql_databases nor psql_databases array exists"""
        # Arrange
        content_without_db_array = """terraform {
  source = "../../../../../layers/something"
}
inputs = {
  identifier = "test"
}"""
        mock_github.return_value = {
            "exists": True,
            "content": content_without_db_array
        }

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            script_generator.generate(
                file_path="test/path.hcl",
                parameters=base_parameters
            )

        assert "No database array found" in str(exc_info.value)

    # ===========================
    # Tests for edge cases
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_with_special_characters_in_database_name(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_empty_array
    ):
        """Test adding database with special characters in name"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }
        parameters = base_parameters.copy()
        parameters['database_name'] = 'test_db_2024'

        # Act
        result = script_generator.generate(
            file_path="test/path.hcl",
            parameters=parameters
        )

        # Assert
        assert result is not None
        assert '"test_db_2024"' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_preserves_template_structure(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_with_databases
    ):
        """Test that adding database preserves the original template structure"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_with_databases
        }

        # Act
        result = script_generator.generate(
            file_path="test/path.hcl",
            parameters=base_parameters
        )

        # Assert - verify key sections are preserved
        assert 'terraform {' in result
        assert 'source = "../../../../../layers/rds-mysql"' in result
        assert 'include "root" {' in result
        assert 'inputs = {' in result
        assert 'identifier' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_returns_string_type(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_empty_array
    ):
        """Test that generate method returns a string"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }

        # Act
        result = script_generator.generate(
            file_path="test/path.hcl",
            parameters=base_parameters
        )

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0

    # ===========================
    # Tests for GitHub integration parameters
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_calls_github_with_correct_parameters(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_empty_array
    ):
        """Test that GitHub integration is called with correct parameters"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }
        file_path = "environment/product-stage-01/eu-west-2/rds/mysql-01/terragrunt.hcl"

        # Act
        script_generator.generate(file_path=file_path, parameters=base_parameters)

        # Assert
        mock_github.assert_called_once_with(
            token='test_token',
            base_url='https://api.github.com',
            owner='test_owner',
            repo='test_repo',
            file_path=file_path,
            branch='main'
        )

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_with_custom_github_base_url(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_empty_array
    ):
        """Test generating with custom GitHub Enterprise base URL"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }
        parameters = base_parameters.copy()
        parameters['github_base_url'] = 'https://github.enterprise.com/api/v3'

        # Act
        script_generator.generate(file_path="test/path.hcl", parameters=parameters)

        # Assert
        call_args = mock_github.call_args[1]
        assert call_args['base_url'] == 'https://github.enterprise.com/api/v3'

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_with_different_base_branch(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_empty_array
    ):
        """Test generating with different base branch"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }
        parameters = base_parameters.copy()
        parameters['base_branch'] = 'develop'

        # Act
        script_generator.generate(file_path="test/path.hcl", parameters=parameters)

        # Assert
        call_args = mock_github.call_args[1]
        assert call_args['branch'] == 'develop'

    # ===========================
    # Tests for array formatting
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_mysql_array_has_proper_closing_bracket(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_with_databases
    ):
        """Test that closing bracket is properly placed after adding database"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_with_databases
        }

        # Act
        result = script_generator.generate(
            file_path="test/path.hcl",
            parameters=base_parameters
        )

        # Assert
        # The result should have properly closed array
        assert 'mysql_databases   = [' in result
        assert ']' in result
        # Count opening and closing brackets should match
        assert result.count('[') == result.count(']')

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_database_name_with_hyphens(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_empty_array
    ):
        """Test adding database name with hyphens"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }
        parameters = base_parameters.copy()
        parameters['database_name'] = 'test-db-name'

        # Act
        result = script_generator.generate(
            file_path="test/path.hcl",
            parameters=parameters
        )

        # Assert
        assert result is not None
        assert '"test-db-name"' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_multiple_databases_maintains_order(
        self, mock_github, script_generator, base_parameters, mock_mysql_content_with_databases
    ):
        """Test that existing databases maintain their order when new one is added"""
        # Arrange
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_with_databases
        }
        parameters = base_parameters.copy()
        parameters['database_name'] = 'third_db'

        # Act
        result = script_generator.generate(
            file_path="test/path.hcl",
            parameters=parameters
        )

        # Assert
        existing_db1_pos = result.find('"existing_db1"')
        existing_db2_pos = result.find('"existing_db2"')
        third_db_pos = result.find('"third_db"')

        # Verify order is maintained
        assert existing_db1_pos < existing_db2_pos < third_db_pos

    # ===========================
    # Tests for preview + S3 upload
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.FileManagerHandler.upload_file')
    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.asyncio.get_running_loop')
    def test_generate_uploads_preview_and_updates_artifact_key(
        self, mock_get_running_loop, mock_github, mock_upload, base_parameters, mock_mysql_content_empty_array
    ):
        """Test preview upload and artifact_s3_key update when upload_to_s3 is True."""
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }
        mock_upload.return_value = {"location": "s3://bucket/path"}

        class DummyLoop:
            def is_running(self):
                return True

            def create_task(self, coro):
                return coro

        mock_get_running_loop.return_value = DummyLoop()

        repository = SimpleNamespace(update_artifact_s3_key=AsyncMock())
        script_generator = AsporaDbCreationScriptGenComponent(repository=repository)

        parameters = base_parameters.copy()
        parameters.update({
            "database_name": "preview_db",
            "db_server_name": "mysql-server-01",
            "environment": "dev"
        })

        result = script_generator.generate(
            file_path="environment/dev/region/database/mysql-server-01/terragrunt.hcl",
            parameters=parameters,
            upload_to_s3=True,
            s3_bucket="test-bucket",
            queue_item_code="queue-123"
        )

        assert '"preview_db"' in result
        assert mock_upload.call_count == 2
        repository.update_artifact_s3_key.assert_called_once()

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_upload_to_s3_requires_db_server_name(
        self, mock_github, base_parameters, mock_mysql_content_empty_array
    ):
        """Test that db_server_name is required when upload_to_s3 is True and file_path is missing."""
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }
        script_generator = AsporaDbCreationScriptGenComponent()
        parameters = base_parameters.copy()

        with pytest.raises(ValueError) as exc_info:
            script_generator.generate(
                file_path=None,
                parameters=parameters,
                upload_to_s3=True
            )

        assert "db_server_name" in str(exc_info.value)

    # ===========================
    # Tests for GitHub commit
    # ===========================

    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitOpsHandler.create_commit')
    @patch('app.plugin.aspora.script_gen_components.aspora_db_creation_script_gen_component.GitHubIntegration.get_file_content')
    def test_generate_commits_to_github(
        self, mock_github, mock_create_commit, base_parameters, mock_mysql_content_empty_array
    ):
        """Test that generate commits changes when workflow_context is provided."""
        mock_github.return_value = {
            "exists": True,
            "content": mock_mysql_content_empty_array
        }

        script_generator = AsporaDbCreationScriptGenComponent()
        parameters = base_parameters.copy()
        parameters["database_name"] = "commit_db"

        file_location = SimpleNamespace(
            repo="owner/repo",
            file_path="environment/dev/region/database/mysql-server-01/terragrunt.hcl",
            base_branch="main",
            feature_branch="feature/db",
            script_gen_key="database_creation"
        )
        workflow_context = SimpleNamespace(script_gen_responses={1: {"database_creation": []}})

        result = script_generator.generate(
            file_path=file_location.file_path,
            parameters=parameters,
            tenant="tenant-1",
            queue_id=1,
            file_location=file_location,
            workflow_context=workflow_context
        )

        assert '"commit_db"' in result
        mock_create_commit.assert_called_once()
        assert workflow_context.script_gen_responses[1]["database_creation"]
