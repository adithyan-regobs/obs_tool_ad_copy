"""
Integration tests for per-server grants resolution in database user management.

These tests make REAL GitHub API calls to read terragrunt files and then
exercise per-server grants selection in memory (no writes to GitHub).

Requirements:
- GITHUB_TOKEN in environment with read access
- GITHUB_BASE_URL in environment (defaults to https://api.github.com)
- GITHUB_OWNER / GITHUB_REPO / GITHUB_BRANCH
- MYSQL_SERVER_FILE_PATH / PSQL_SERVER_FILE_PATH

To run:
  pytest tests/integration/plugins/test_aspora_database_user_management_per_server_integration.py -v -s
"""
import os
import pytest

from app.core.config import settings
from app.plugin.aspora.script_gen_components.aspora_database_user_management_script_gen_component import (
    AsporaDatabaseUserManagementScriptGenComponent
)
from app.schemas.file_location_response_schema import FileLocationItem


class TestAsporaDatabaseUserManagementPerServerIntegration:
    @pytest.fixture(scope="class")
    def github_env(self):
        token = os.getenv("GITHUB_TOKEN")
        base_url = os.getenv("GITHUB_BASE_URL", "https://api.github.com")
        owner = os.getenv("GITHUB_OWNER")
        repo = os.getenv("GITHUB_REPO")
        branch = os.getenv("GITHUB_BRANCH", "main")
        mysql_path = os.getenv("MYSQL_SERVER_FILE_PATH")
        psql_path = os.getenv("PSQL_SERVER_FILE_PATH")

        if not token:
            pytest.skip("GITHUB_TOKEN not set; skipping integration tests.")
        if not owner or not repo or not mysql_path or not psql_path:
            pytest.skip("Repo/path env vars not set; skipping integration tests.")

        settings.github_token = token
        settings.github_base_url = base_url

        return {
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "mysql_path": mysql_path,
            "psql_path": psql_path,
        }

    def _build_file_location(self, owner, repo, file_path, branch, db_type, db_server_name):
        return FileLocationItem(
            repo=f"{owner}/{repo}",
            file_path=file_path,
            config={
                "db_type": db_type,
                "db_server_name": db_server_name
            },
            base_branch=branch,
            feature_branch="integration-feature-branch",
            script_gen_key="user_management"
        )

    def test_mysql_per_server_grants(self, github_env):
        generator = AsporaDatabaseUserManagementScriptGenComponent()
        db_server_name = os.path.basename(os.path.dirname(github_env["mysql_path"]))
        queue_dict = {
            "id": 1,
            "code": "INTEGRATION-MYSQL",
            "config_snapshot": {
                "username": "integration_user",
                "password": "encrypted_password_mysql",
                "mysql_servers": [
                    {
                        "db_server_name": db_server_name,
                        "grants": [
                            {
                                "database": "app_db",
                                "tables": [
                                    {"table": "all", "privileges": ["SELECT", "INSERT"]}
                                ]
                            }
                        ]
                    }
                ],
                "pgsql_servers": []
            }
        }
        file_location = self._build_file_location(
            owner=github_env["owner"],
            repo=github_env["repo"],
            file_path=github_env["mysql_path"],
            branch=github_env["branch"],
            db_type="mysql",
            db_server_name=db_server_name
        )

        result = generator.generate(
            tenant="aspora",
            repository=None,
            file_location=file_location,
            queue_dict=queue_dict,
            workflow_context=None,
            upload_to_s3=False
        )

        assert "integration_user" in result
        assert "app_db" in result
        assert "SELECT" in result
        assert "INSERT" in result

    def test_psql_per_server_grants(self, github_env):
        generator = AsporaDatabaseUserManagementScriptGenComponent()
        db_server_name = os.path.basename(os.path.dirname(github_env["psql_path"]))
        queue_dict = {
            "id": 2,
            "code": "INTEGRATION-PSQL",
            "config_snapshot": {
                "username": "integration_user",
                "password": "encrypted_password_psql",
                "mysql_servers": [],
                "pgsql_servers": [
                    {
                        "db_server_name": db_server_name,
                        "grants": [
                            {
                                "database": "app_db",
                                "permissions": ["CREATE"],
                                "schemas": [
                                    {
                                        "schemaName": "public",
                                        "permissions": ["USAGE"],
                                        "tables": [
                                            {"tableName": "all", "privileges": ["SELECT"]}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        file_location = self._build_file_location(
            owner=github_env["owner"],
            repo=github_env["repo"],
            file_path=github_env["psql_path"],
            branch=github_env["branch"],
            db_type="postgresql",
            db_server_name=db_server_name
        )

        result = generator.generate(
            tenant="aspora",
            repository=None,
            file_location=file_location,
            queue_dict=queue_dict,
            workflow_context=None,
            upload_to_s3=False
        )

        assert "integration_user" in result
        assert "app_db" in result
        assert "object_type" in result
        assert "public" in result
