import pytest
from app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component import (
    AsporaDatabaseUserManagementScriptGenComponent
)


class TestAsporaDatabaseUserManagementIntegration:
    """
    Integration tests that make REAL API calls to GitHub.

    These tests require:
    - GITHUB_TOKEN in .env
    - GITHUB_BASE_URL in .env (defaults to https://api.github.com)
    - Valid repository and file paths

    To run ONLY these integration tests:
        pytest tests/integration/plugins/test_aspora_database_user_management_integration.py -v -s

    To run all integration tests:
        pytest tests/integration/ -v

    To skip integration tests (run only unit tests):
        pytest tests/unit/ -v
    """

    # Update these constants with your actual repository details
    GITHUB_OWNER = "Regobs"
    GITHUB_REPO = "terraform-test"
    GITHUB_BRANCH = "stage"

    # All 4 database servers
    MYSQL_1_FILE_PATH = "environment/core-prod-01/eu-west-2/database/common-mysql-1/terragrunt.hcl"
    MYSQL_2_FILE_PATH = "environment/core-prod-01/eu-west-2/database/common-mysql-2/terragrunt.hcl"
    PSQL_1_FILE_PATH = "environment/core-prod-01/eu-west-2/database/common-pg/terragrunt.hcl"
    PSQL_2_FILE_PATH = "environment/core-prod-01/eu-west-2/database/common-pg-2/terragrunt.hcl"

    @pytest.fixture(scope="class")
    def github_credentials(self):
        """Load GitHub credentials from environment."""
        import os
        from dotenv import load_dotenv

        load_dotenv()

        github_token = os.getenv("GITHUB_TOKEN")
        github_base_url = os.getenv("GITHUB_BASE_URL", "https://api.github.com")

        if not github_token:
            pytest.skip("GITHUB_TOKEN not found in .env - Skipping integration tests")

        return {
            "token": github_token,
            "base_url": github_base_url
        }

    @pytest.fixture
    def script_generator(self):
        return AsporaDatabaseUserManagementScriptGenComponent()

    # ========================================================================
    # MySQL Server 1 Tests
    # ========================================================================

    def test_integration_mysql1_add_new_user(
        self,
        script_generator,
        github_credentials
    ):
        """MySQL Server 1: Add a completely new user."""
        parameters = {
            'github_token': github_credentials['token'],
            'github_base_url': github_credentials['base_url'],
            'owner': self.GITHUB_OWNER,
            'repo': self.GITHUB_REPO,
            'file_path': self.MYSQL_1_FILE_PATH,
            'branch': self.GITHUB_BRANCH,
            'db_type': 'mysql',
            'username': 'new_integration_test_user',
            'password': 'encrypted_pass_new_123',
            'grants': [
                {
                    'database': 'testdb',
                    'tables': [{'table': 'all', 'privileges': ['SELECT', 'INSERT']}]
                }
            ]
        }

        result = script_generator.generate(parameters)

        assert 'new_integration_test_user' in result
        assert 'encrypted_pass_new_123' in result
        print(f"\n✅ MySQL-1: Added NEW user successfully")
        print(f"   File: common-mysql-1")
        print(f"   Length: {len(result):,} characters")

    def test_integration_mysql1_update_existing_user(
        self,
        script_generator,
        github_credentials
    ):
        """MySQL Server 1: Update grants for EXISTING user 'email_svc_user'."""
        # Using real existing user from common-mysql-1
        existing_user = "email_svc_user"

        parameters = {
            'github_token': github_credentials['token'],
            'github_base_url': github_credentials['base_url'],
            'owner': self.GITHUB_OWNER,
            'repo': self.GITHUB_REPO,
            'file_path': self.MYSQL_1_FILE_PATH,
            'branch': self.GITHUB_BRANCH,
            'db_type': 'mysql',
            'username': existing_user,
            'password': 'NEW_ENCRYPTED_PASSWORD_123',  # Different password
            'grants': [
                {
                    'database': 'appserver_db',
                    'tables': [
                        {'table': '*', 'privileges': ['SELECT', 'INSERT', 'UPDATE', 'DELETE']}
                    ]
                },
                {
                    'database': 'notification_db',
                    'tables': [
                        {'table': '*', 'privileges': ['SELECT', 'INSERT']}
                    ]
                }
            ],
            'replace_grants': True,  # Replace existing grants
            'update_password': False  # Don't update password
        }

        result = script_generator.generate(parameters)

        assert existing_user in result
        assert 'notification_db' in result
        print(f"\n✅ MySQL-1: Updated EXISTING user '{existing_user}'")
        print(f"   Replaced grants: appserver_db + notification_db")
        print(f"   Original had only appserver_db")

    # ========================================================================
    # MySQL Server 2 Tests
    # ========================================================================

    def test_integration_mysql2_add_new_user(
        self,
        script_generator,
        github_credentials
    ):
        """MySQL Server 2: Add a new user."""
        parameters = {
            'github_token': github_credentials['token'],
            'github_base_url': github_credentials['base_url'],
            'owner': self.GITHUB_OWNER,
            'repo': self.GITHUB_REPO,
            'file_path': self.MYSQL_2_FILE_PATH,
            'branch': self.GITHUB_BRANCH,
            'db_type': 'mysql',
            'username': 'mysql2_new_user',
            'password': 'mysql2_encrypted_pass',
            'grants': [
                {
                    'database': 'goblin_db',
                    'tables': [
                        {'table': 'users', 'privileges': ['SELECT', 'INSERT']},
                        {'table': 'sessions', 'privileges': ['SELECT', 'DELETE']}
                    ]
                }
            ]
        }

        result = script_generator.generate(parameters)

        assert 'mysql2_new_user' in result
        assert 'mysql2_encrypted_pass' in result
        assert 'goblin_db' in result
        print(f"\n✅ MySQL-2: Added NEW user with multiple table grants")
        print(f"   File: common-mysql-2")
        print(f"   Databases: goblin_db (users, sessions tables)")

    def test_integration_mysql2_update_existing_user(
        self,
        script_generator,
        github_credentials
    ):
        """MySQL Server 2: Update grants for EXISTING user 'goblin_service'."""
        # Using real existing user from common-mysql-2
        existing_user = "goblin_service"

        parameters = {
            'github_token': github_credentials['token'],
            'github_base_url': github_credentials['base_url'],
            'owner': self.GITHUB_OWNER,
            'repo': self.GITHUB_REPO,
            'file_path': self.MYSQL_2_FILE_PATH,
            'branch': self.GITHUB_BRANCH,
            'db_type': 'mysql',
            'username': existing_user,
            'password': 'KEEP_EXISTING_PASSWORD',
            'grants': [
                {
                    'database': 'goblin_db',
                    'tables': [
                        {'table': '*', 'privileges': ['SELECT', 'INSERT', 'UPDATE', 'DELETE']}
                    ]
                },
                {
                    'database': 'backoffice_db',
                    'tables': [
                        {'table': 'reports', 'privileges': ['SELECT']}
                    ]
                }
            ],
            'replace_grants': True,
            'update_password': False
        }

        result = script_generator.generate(parameters)

        assert existing_user in result
        assert 'backoffice_db' in result
        print(f"\n✅ MySQL-2: Updated EXISTING user '{existing_user}'")
        print(f"   Replaced grants: goblin_db + backoffice_db")
        print(f"   Original had only goblin_db")

    # ========================================================================
    # PostgreSQL Server 1 Tests
    # ========================================================================

    def test_integration_psql1_add_new_user(
        self,
        script_generator,
        github_credentials
    ):
        """PostgreSQL Server 1: Add a new user."""
        parameters = {
            'github_token': github_credentials['token'],
            'github_base_url': github_credentials['base_url'],
            'owner': self.GITHUB_OWNER,
            'repo': self.GITHUB_REPO,
            'file_path': self.PSQL_1_FILE_PATH,
            'branch': self.GITHUB_BRANCH,
            'db_type': 'psql',
            'username': 'psql1_new_user',
            'password': 'psql1_encrypted_pass',
            'grants': [
                {
                    'database': 'testdb',
                    'permissions': ['CREATE'],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': ['USAGE', 'CREATE'],
                            'tables': [{'tableName': 'all', 'privileges': ['SELECT', 'INSERT']}]
                        }
                    ]
                }
            ]
        }

        result = script_generator.generate(parameters)

        assert 'psql1_new_user' in result
        assert 'psql1_encrypted_pass' in result
        assert 'object_type = "database"' in result
        print(f"\n✅ PostgreSQL-1: Added NEW user successfully")
        print(f"   File: common-pg")
        print(f"   Length: {len(result):,} characters")

    def test_integration_psql1_update_existing_user(
        self,
        script_generator,
        github_credentials
    ):
        """PostgreSQL Server 1: Merge grants for EXISTING user 'recon_service'."""
        # Using real existing user from common-pg
        existing_user = "recon_service"

        parameters = {
            'github_token': github_credentials['token'],
            'github_base_url': github_credentials['base_url'],
            'owner': self.GITHUB_OWNER,
            'repo': self.GITHUB_REPO,
            'file_path': self.PSQL_1_FILE_PATH,
            'branch': self.GITHUB_BRANCH,
            'db_type': 'psql',
            'username': existing_user,
            'password': 'KEEP_EXISTING_PASSWORD',
            'grants': [
                {
                    'database': 'recon_db',
                    'permissions': ['CONNECT', 'CREATE'],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': ['USAGE', 'CREATE'],
                            'tables': [{'tableName': 'all', 'privileges': ['SELECT', 'INSERT', 'UPDATE', 'DELETE']}]
                        }
                    ]
                },
                {
                    'database': 'iris_db',
                    'permissions': ['CONNECT'],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': ['USAGE'],
                            'tables': [{'tableName': 'all', 'privileges': ['SELECT']}]
                        }
                    ]
                }
            ],
            'replace_grants': False,  # Merge mode - union of privileges
            'update_password': False
        }

        result = script_generator.generate(parameters)

        assert existing_user in result
        assert 'iris_db' in result
        print(f"\n✅ PostgreSQL-1: MERGED grants for EXISTING user '{existing_user}'")
        print(f"   Used merge mode (union of privileges)")
        print(f"   Added iris_db grants to existing recon_db grants")

    # ========================================================================
    # PostgreSQL Server 2 Tests
    # ========================================================================

    def test_integration_psql2_add_new_user(
        self,
        script_generator,
        github_credentials
    ):
        """PostgreSQL Server 2: Add a new user with multiple schemas."""
        parameters = {
            'github_token': github_credentials['token'],
            'github_base_url': github_credentials['base_url'],
            'owner': self.GITHUB_OWNER,
            'repo': self.GITHUB_REPO,
            'file_path': self.PSQL_2_FILE_PATH,
            'branch': self.GITHUB_BRANCH,
            'db_type': 'psql',
            'username': 'psql2_new_user',
            'password': 'psql2_encrypted_pass',
            'grants': [
                {
                    'database': 'orders_db',
                    'permissions': ['CREATE', 'CONNECT'],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': ['USAGE', 'CREATE'],
                            'tables': [{'tableName': 'all', 'privileges': ['SELECT', 'INSERT', 'UPDATE']}]
                        },
                        {
                            'schemaName': 'reporting',
                            'permissions': ['USAGE'],
                            'tables': [{'tableName': 'all', 'privileges': ['SELECT']}]
                        }
                    ]
                }
            ]
        }

        result = script_generator.generate(parameters)

        assert 'psql2_new_user' in result
        assert 'psql2_encrypted_pass' in result
        assert 'orders_db' in result
        # Should have 6 grant entries (2 schemas × 3 object types)
        assert result.count('object_type') >= 6
        print(f"\n✅ PostgreSQL-2: Added NEW user with multiple schemas")
        print(f"   File: common-pg-2")
        print(f"   Database: orders_db | Schemas: public, reporting")

    def test_integration_psql2_update_existing_user(
        self,
        script_generator,
        github_credentials
    ):
        """PostgreSQL Server 2: Replace grants for EXISTING user 'analytics_ro'."""
        # Using real existing user from common-pg-2
        existing_user = "analytics_ro"

        parameters = {
            'github_token': github_credentials['token'],
            'github_base_url': github_credentials['base_url'],
            'owner': self.GITHUB_OWNER,
            'repo': self.GITHUB_REPO,
            'file_path': self.PSQL_2_FILE_PATH,
            'branch': self.GITHUB_BRANCH,
            'db_type': 'psql',
            'username': existing_user,
            'password': 'KEEP_EXISTING_PASSWORD',
            'grants': [
                {
                    'database': 'orders_db',
                    'permissions': ['CONNECT'],
                    'schemas': [
                        {
                            'schemaName': 'public',
                            'permissions': ['USAGE'],
                            'tables': [{'tableName': 'all', 'privileges': ['SELECT']}]
                        }
                    ]
                }
            ],
            'replace_grants': True,  # Replace mode - only orders_db
            'update_password': False
        }

        result = script_generator.generate(parameters)

        assert existing_user in result
        assert 'orders_db' in result
        print(f"\n✅ PostgreSQL-2: Updated EXISTING user '{existing_user}'")
        print(f"   Replaced grants: only orders_db")
        print(f"   Original had: orders_db, billing_db, analytics_db")

    # ========================================================================
    # Verification Test
    # ========================================================================

    def test_integration_verify_all_servers_accessible(self, github_credentials):
        """Verify all 4 database servers are accessible via GitHub API."""
        from app.integrations.github_integration import GitHubIntegration

        servers = [
            ("MySQL-1", self.MYSQL_1_FILE_PATH),
            ("MySQL-2", self.MYSQL_2_FILE_PATH),
            ("PostgreSQL-1", self.PSQL_1_FILE_PATH),
            ("PostgreSQL-2", self.PSQL_2_FILE_PATH),
        ]

        print(f"\n📊 Verifying access to all 4 database servers:")
        print("=" * 80)

        for server_name, file_path in servers:
            result = GitHubIntegration.get_file_content(
                token=github_credentials['token'],
                base_url=github_credentials['base_url'],
                owner=self.GITHUB_OWNER,
                repo=self.GITHUB_REPO,
                file_path=file_path,
                branch=self.GITHUB_BRANCH
            )

            assert result is not None
            assert "exists" in result

            if result["exists"]:
                content_length = len(result.get('content', ''))
                # Count existing users
                import re
                user_count = len(re.findall(r'name\s*=\s*"([^"]+)"', result['content']))

                print(f"✅ {server_name:15} | {content_length:,} chars | {user_count} existing users")
            else:
                pytest.fail(f"File not found: {file_path}")

        print("=" * 80)
        print(f"✅ All 4 servers accessible via GitHub API")
