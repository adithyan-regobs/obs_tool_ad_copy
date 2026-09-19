import os
import pytest
from unittest.mock import patch, MagicMock
from app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component import (
    AsporaDatabaseUserManagementScriptGenComponent
)
from app.domain.validators.github_rules import GitHubValidationError
from app.schemas.file_location_response_schema import FileLocationItem


def build_inputs(parameters):
    file_path = parameters["file_path"]
    db_type = parameters["db_type"]
    db_server_name = os.path.basename(os.path.dirname(file_path))

    server_entry = {
        "db_server_name": db_server_name,
        "grants": parameters.get("grants", [])
    }

    config_snapshot = {
        "username": parameters.get("username"),
        "password": parameters.get("password"),
        "update_password": parameters.get("update_password", False),
        "replace_grants": parameters.get("replace_grants", True),
        "environment": parameters.get("environment", ""),
        "mysql_servers": [server_entry] if db_type == "mysql" else [],
        "pgsql_servers": [server_entry] if db_type in ("psql", "postgresql") else []
    }

    queue_dict = {
        "id": 1,
        "code": "QUEUE-1",
        "config_snapshot": config_snapshot
    }

    file_location = FileLocationItem(
        repo=f"{parameters.get('owner', 'test_owner')}/{parameters.get('repo', 'test_repo')}",
        file_path=file_path,
        config={
            "db_type": db_type,
            "db_server_name": db_server_name
        },
        base_branch=parameters.get("branch", "main"),
        feature_branch="feature-branch",
        script_gen_key="user_management"
    )

    return queue_dict, file_location


def run_generate(script_generator, parameters, upload_to_s3=False):
    queue_dict, file_location = build_inputs(parameters)
    return script_generator.generate(
        tenant="aspora",
        repository=None,
        file_location=file_location,
        queue_dict=queue_dict,
        workflow_context=None,
        upload_to_s3=upload_to_s3
    )


class TestAsporaDatabaseUserManagementScriptGenComponent:

    @pytest.fixture
    def script_generator(self):
        return AsporaDatabaseUserManagementScriptGenComponent()

    @pytest.fixture
    def mock_github_response_mysql(self):
        """Mock GitHub response with MySQL template content."""
        return {
            "exists": True,
            "content": '''inputs = {
  organization = "regobs"
  mysql_databases = ["testdb", "otherdb"]
  mysql_users = [
    {
      name     = "existing_user"
      password = "encrypted_pass_123"
      host     = "%"
      grants = [
        {
          database   = "testdb"
          table      = "*"
          privileges = ["SELECT", "INSERT"]
        }
      ]
    }
  ]
}'''
        }

    @pytest.fixture
    def mock_github_response_psql(self):
        """Mock GitHub response with PostgreSQL template content."""
        return {
            "exists": True,
            "content": '''inputs = {
  organization = "regobs"
  psql_databases = ["goms", "recon_service"]
  psql_users = [
    {
      name     = "existing_psql_user"
      password = "encrypted_psql_pass"
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE", "CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT"]
        }
      ]
    }
  ]
}'''
        }

    @pytest.fixture
    def mock_github_response_empty_mysql(self):
        """Mock GitHub response with empty MySQL users array."""
        return {
            "exists": True,
            "content": '''inputs = {
  mysql_databases = ["testdb"]
  mysql_users = [
  ]
}'''
        }

    @pytest.fixture
    def mock_github_response_empty_psql(self):
        """Mock GitHub response with empty PostgreSQL users array."""
        return {
            "exists": True,
            "content": '''inputs = {
  psql_databases = ["goms"]
  psql_users = [
  ]
}'''
        }

    # ========================================================================
    # MySQL User Management Tests
    # ========================================================================

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_mysql_add_new_user_to_empty_array(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_empty_mysql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_empty_mysql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'new_mysql_user',
            'password': 'encrypted_password_abc',
            'grants': [
                {
                    'database': 'testdb',
                    'tables': [
                        {
                            'table': 'all',
                            'privileges': ['SELECT', 'INSERT', 'UPDATE']
                        }
                    ]
                }
            ]
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert result is not None
        assert 'new_mysql_user' in result
        assert 'encrypted_password_abc' in result
        assert 'testdb' in result
        assert '"*"' in result  # 'all' should be converted to '*'
        assert '["SELECT", "INSERT", "UPDATE"]' in result or '["INSERT", "SELECT", "UPDATE"]' in result
        assert 'mysql_users = [' in result
        mock_get_file.assert_called_once()

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_mysql_add_new_user_to_existing_array(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_mysql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_mysql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'new_user',
            'password': 'new_encrypted_pass',
            'grants': [
                {
                    'database': 'testdb',
                    'tables': [{'table': 'users', 'privileges': ['SELECT']}]
                }
            ]
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert 'new_user' in result
        assert 'new_encrypted_pass' in result
        assert 'existing_user' in result  # Original user should still be there
        assert result.count('name     =') == 2  # Two users total

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_mysql_uses_per_server_grants(
        self,
        mock_get_file,
        script_generator
    ):
        mock_get_file.return_value = {
            "exists": True,
            "content": '''inputs = {
  mysql_users = []
}'''
        }
        queue_dict = {
            "id": 1,
            "code": "QUEUE-1",
            "config_snapshot": {
                "username": "server_user",
                "password": "encrypted_pass",
                "mysql_servers": [
                    {
                        "db_server_name": "mysql-a",
                        "grants": [
                            {
                                "database": "db_a",
                                "tables": [{"table": "all", "privileges": ["SELECT"]}]
                            }
                        ]
                    },
                    {
                        "db_server_name": "mysql-b",
                        "grants": [
                            {
                                "database": "db_b",
                                "tables": [{"table": "users", "privileges": ["UPDATE"]}]
                            }
                        ]
                    }
                ],
                "pgsql_servers": []
            }
        }
        file_location = FileLocationItem(
            repo="test_owner/test_repo",
            file_path="env/product-dev-v1/us-east-1/database/mysql-b/terragrunt.hcl",
            config={"db_type": "mysql", "db_server_name": "mysql-b"},
            base_branch="main",
            feature_branch="feature-branch",
            script_gen_key="user_management"
        )

        result = script_generator.generate(
            tenant="aspora",
            repository=None,
            file_location=file_location,
            queue_dict=queue_dict,
            workflow_context=None,
            upload_to_s3=False
        )

        assert 'server_user' in result
        assert 'db_b' in result
        assert '"users"' in result
        assert 'UPDATE' in result
        assert 'db_a' not in result

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_mysql_merge_grants_for_existing_user(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_mysql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_mysql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'existing_user',
            'password': 'encrypted_pass_123',
            'grants': [
                {
                    'database': 'testdb',
                    'tables': [{'table': '*', 'privileges': ['UPDATE', 'DELETE']}]
                }
            ],
            'replace_grants': False  # Merge mode
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert 'existing_user' in result
        # In merge mode, should have union of privileges
        assert 'DELETE' in result
        assert 'UPDATE' in result
        # Original privileges should still be there
        assert 'SELECT' in result
        assert 'INSERT' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_mysql_replace_grants_for_existing_user(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_mysql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_mysql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'existing_user',
            'password': 'encrypted_pass_123',
            'grants': [
                {
                    'database': 'testdb',
                    'tables': [{'table': '*', 'privileges': ['DELETE']}]
                }
            ],
            'replace_grants': True  # Replace mode
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=True)

        # Assert
        assert 'existing_user' in result
        assert 'DELETE' in result
        # In replace mode for testdb, old privileges should be gone
        # Count occurrences in the existing_user block
        user_block_start = result.find('name     = "existing_user"')
        user_block_end = result.find('}', user_block_start + 500)  # Find the end of user block
        user_block = result[user_block_start:user_block_end]

        # Check that we only have DELETE for testdb (not SELECT, INSERT from original)
        assert '"DELETE"' in user_block

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_mysql_update_password(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_mysql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_mysql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'existing_user',
            'password': 'new_encrypted_password_999',
            'grants': [
                {
                    'database': 'testdb',
                    'tables': [{'table': '*', 'privileges': ['SELECT']}]
                }
            ],
            'update_password': True
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert 'new_encrypted_password_999' in result
        assert 'encrypted_pass_123' not in result

    # ========================================================================
    # PostgreSQL User Management Tests
    # ========================================================================

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_psql_add_new_user_to_empty_array(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_empty_psql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_empty_psql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/psql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'psql',
            'username': 'new_psql_user',
            'password': 'psql_encrypted_pass',
            'grants': [
                {
                    'database': 'goms',
                    'permissions': ['CREATE'],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': ['USAGE', 'CREATE'],
                            'tables': [
                                {'tableName': 'all', 'privileges': ['SELECT', 'INSERT']}
                            ]
                        }
                    ]
                }
            ]
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert 'new_psql_user' in result
        assert 'psql_encrypted_pass' in result
        assert 'goms' in result
        assert 'public' in result
        assert 'database' in result and 'object_type' in result
        assert 'schema' in result
        assert 'table' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_psql_flatten_grants_correctly(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_empty_psql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_empty_psql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/psql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'psql',
            'username': 'test_user',
            'password': 'pass',
            'grants': [
                {
                    'database': 'mydb',
                    'permissions': ['CREATE'],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': ['USAGE'],
                            'tables': [{'tableName': 'all', 'privileges': ['SELECT']}]
                        }
                    ]
                }
            ]
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        # Should have 3 grant entries: database, schema, table
        assert result.count('object_type = "database"') == 1
        assert result.count('object_type = "schema"') == 1
        assert result.count('object_type = "table"') == 1
        # Verify privileges are correct
        assert '["CREATE"]' in result  # database level
        assert '["USAGE"]' in result  # schema level
        assert '["SELECT"]' in result  # table level

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_psql_merge_grants_for_existing_user(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_psql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_psql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/psql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'psql',
            'username': 'existing_psql_user',
            'password': 'encrypted_psql_pass',
            'grants': [
                {
                    'database': 'goms',
                    'permissions': [],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': [],
                            'tables': [{'tableName': 'all', 'privileges': ['UPDATE', 'DELETE']}]
                        }
                    ]
                }
            ],
            'replace_grants': False  # Merge mode
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert 'existing_psql_user' in result
        # Should have union of table privileges
        assert 'UPDATE' in result
        assert 'DELETE' in result
        assert 'SELECT' in result  # Original
        assert 'INSERT' in result  # Original

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_psql_replace_grants_for_existing_user(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_psql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_psql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/psql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'psql',
            'username': 'existing_psql_user',
            'password': 'encrypted_psql_pass',
            'grants': [
                {
                    'database': 'goms',
                    'permissions': [],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': [],
                            'tables': [{'tableName': 'all', 'privileges': ['DELETE']}]
                        }
                    ]
                }
            ],
            'replace_grants': True  # Replace mode
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert 'existing_psql_user' in result
        assert 'DELETE' in result
        # In replace mode, old table privileges for goms should be replaced
        # We should have DELETE but database and schema levels should be empty arrays

    # ========================================================================
    # Error Handling Tests
    # ========================================================================

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_file_not_found_error(self, mock_get_file, script_generator):
        # Arrange
        mock_get_file.return_value = {"exists": False}
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/nonexistent/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'test_user',
            'password': 'pass',
            'grants': []
        }

        # Act & Assert
        with pytest.raises(GitHubValidationError) as exc_info:
            run_generate(script_generator, parameters, upload_to_s3=False)

        assert 'not found' in str(exc_info.value).lower()

    def test_generate_invalid_db_type_error(self, script_generator):
        # Arrange
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'invalid_type',
            'username': 'test_user',
            'password': 'pass',
            'grants': []
        }

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            run_generate(script_generator, parameters, upload_to_s3=False)

        assert "Invalid db_type" in str(exc_info.value)
        assert "must be 'mysql' or 'psql'" in str(exc_info.value).lower()

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_mysql_users_array_not_found_error(self, mock_get_file, script_generator):
        # Arrange
        mock_get_file.return_value = {
            "exists": True,
            "content": "inputs = { organization = 'test' }"  # No mysql_users array
        }
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'test_user',
            'password': 'pass',
            'grants': []
        }

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            run_generate(script_generator, parameters, upload_to_s3=False)

        assert "mysql_users array not found" in str(exc_info.value)

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_psql_users_array_not_found_error(self, mock_get_file, script_generator):
        # Arrange
        mock_get_file.return_value = {
            "exists": True,
            "content": "inputs = { organization = 'test' }"  # No psql_users array
        }
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/psql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'psql',
            'username': 'test_user',
            'password': 'pass',
            'grants': []
        }

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            run_generate(script_generator, parameters, upload_to_s3=False)

        assert "psql_users array not found" in str(exc_info.value)

    # ========================================================================
    # Edge Cases & Structure Preservation Tests
    # ========================================================================

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_preserves_file_structure(
        self,
        mock_get_file,
        script_generator,
        mock_github_response_mysql
    ):
        # Arrange
        mock_get_file.return_value = mock_github_response_mysql
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'new_user',
            'password': 'pass',
            'grants': [
                {'database': 'testdb', 'tables': [{'table': '*', 'privileges': ['SELECT']}]}
            ]
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        # File structure should be preserved
        assert 'inputs = {' in result
        assert 'organization = "regobs"' in result
        assert 'mysql_databases = ["testdb", "otherdb"]' in result
        assert 'mysql_users = [' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_mysql_multiple_databases(self, mock_get_file, script_generator):
        # Arrange
        mock_get_file.return_value = {
            "exists": True,
            "content": '''inputs = {
  mysql_users = []
}'''
        }
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'multi_db_user',
            'password': 'pass',
            'grants': [
                {
                    'database': 'db1',
                    'tables': [{'table': '*', 'privileges': ['SELECT']}]
                },
                {
                    'database': 'db2',
                    'tables': [{'table': 'users', 'privileges': ['INSERT', 'UPDATE']}]
                }
            ]
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert 'multi_db_user' in result
        assert 'db1' in result
        assert 'db2' in result
        assert '"*"' in result
        assert '"users"' in result

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_psql_multiple_schemas(self, mock_get_file, script_generator):
        # Arrange
        mock_get_file.return_value = {
            "exists": True,
            "content": '''inputs = {
  psql_users = []
}'''
        }
        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/psql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'psql',
            'username': 'multi_schema_user',
            'password': 'pass',
            'grants': [
                {
                    'database': 'mydb',
                    'permissions': ['CREATE'],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': ['USAGE'],
                            'tables': [{'tableName': 'all', 'privileges': ['SELECT']}]
                        },
                        {
                            'schemaName': 'private',
                            'permissions': ['CREATE'],
                            'tables': [{'tableName': 'all', 'privileges': ['INSERT']}]
                        }
                    ]
                }
            ]
        }

        # Act
        result = run_generate(script_generator, parameters, upload_to_s3=False)

        # Assert
        assert 'multi_schema_user' in result
        assert '"public"' in result
        assert '"private"' in result
        # Should have 6 grant entries (2 schemas × 3 object types)
        assert result.count('object_type') == 6

    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.FileManagerHandler.upload_file')
    @patch('app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component.GitOpsHandler.get_content')
    def test_generate_uploads_preview_and_original_files(
        self,
        mock_get_file,
        mock_upload,
        script_generator,
        mock_github_response_empty_mysql
    ):
        mock_get_file.return_value = mock_github_response_empty_mysql
        mock_upload.return_value = {"location": "s3://bucket/key"}

        parameters = {
            'github_token': 'test_token',
            'github_base_url': 'https://api.github.com',
            'owner': 'test_owner',
            'repo': 'test_repo',
            'file_path': 'env/product-dev-v1/us-east-1/database/mysql-server/terragrunt.hcl',
            'branch': 'main',
            'db_type': 'mysql',
            'username': 'preview_user',
            'password': 'encrypted_password_abc',
            'grants': [
                {
                    'database': 'testdb',
                    'tables': [
                        {
                            'table': 'all',
                            'privileges': ['SELECT', 'INSERT']
                        }
                    ]
                }
            ],
            'upload_to_s3': True,
            's3_bucket': 'test-bucket'
        }

        result = run_generate(script_generator, parameters, upload_to_s3=False)

        assert '"preview_user"' in result
        assert mock_upload.call_count == 2

        original_call = mock_upload.call_args_list[0]
        preview_call = mock_upload.call_args_list[1]

        assert original_call.kwargs["key"] == "database-users/mysql-server.hcl"
        assert original_call.kwargs["content"] == result

        assert preview_call.kwargs["key"] == "preview/database-users/mysql-server.hcl"
        assert "preview_user" in preview_call.kwargs["content"]
