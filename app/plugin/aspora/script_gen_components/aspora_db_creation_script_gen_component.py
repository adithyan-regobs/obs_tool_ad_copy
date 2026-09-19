"""
Aspora Database Creation Script Generation Component

Handles database creation in terragrunt files for Aspora tenant.
Extracts database creation logic from TerragruntMgmtService.
"""

import json
import logging
import os
import re
from typing import Tuple

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.timing import log_timing
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


def _find_staged_entry(workflow_context, repo: str, base_branch: str, file_path: str):
    if not workflow_context:
        return None
    for entry in workflow_context.staged_files:
        if (
            entry.get("repo") == repo
            and entry.get("base_branch") == base_branch
            and entry.get("file_path") == file_path
        ):
            return entry
    return None


def _upsert_staged_entry(
    workflow_context,
    repo: str,
    base_branch: str,
    feature_branch: str,
    file_path: str,
    content: str,
    queue_id,
    script_gen_key,
):
    if not workflow_context:
        return
    entry = _find_staged_entry(workflow_context, repo, base_branch, file_path)
    if entry:
        entry["content"] = content
        entry["feature_branch"] = feature_branch or entry.get("feature_branch")
        entry["queue_id"] = queue_id
        entry["script_gen_key"] = script_gen_key
        return
    workflow_context.staged_files.append({
        "repo": repo,
        "base_branch": base_branch,
        "feature_branch": feature_branch,
        "file_path": file_path,
        "content": content,
        "queue_id": queue_id,
        "script_gen_key": script_gen_key,
    })


def _append_commit_message(workflow_context, repo: str, base_branch: str, message: str) -> None:
    if not workflow_context or not message:
        return
    key = f"{repo}|||{base_branch}"
    existing = workflow_context.commit_messages.get(key, "")
    if existing:
        workflow_context.commit_messages[key] = f"{existing}\n{message}"
    else:
        workflow_context.commit_messages[key] = message


class AsporaDbCreationScriptGenComponent:
    """
    Component for adding database names to existing terragrunt database server files.

    Responsibilities:
    - Fetch existing database server configuration from GitHub
    - Auto-detect database type (MySQL/PostgreSQL)
    - Add database name to appropriate array (mysql_databases or psql_databases)
    - Return modified content
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository  # GitopsQueueRepository for DB operations

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = True,
        db=None,
    ) -> str:
        """
        Generate updated database configuration by adding a database to existing file.

        This function:
        1. Uses GitHubIntegration.get_file_content to fetch existing file
        2. If file does NOT exist, raises an error
        3. If file exists:
           - Auto-detects database type (MySQL/PostgreSQL) from existing arrays
           - Adds database name to appropriate array
        4. Returns the modified terragrunt content

        Returns:
            Modified terragrunt configuration content with database added
        """
        config_snapshot = queue_dict.get("config_snapshot") or {}

        # Extract parameters
        database_name = config_snapshot.get('database_name') or queue_dict.get('database_name')
        db_server_name = config_snapshot.get('db_server_name') or queue_dict.get('db_server_name')
        # github_token = config_snapshot.get('github_token')
        # github_base_url = config_snapshot.get('github_base_url')
        environment= config_snapshot.get('environment') or queue_dict.get("environment")

        if not db_server_name and file_location and file_location.file_path:
            db_server_name = os.path.basename(os.path.dirname(file_location.file_path))

        logger.info(f"Generating database configuration for: {file_location.file_path}")
        logger.info(f"  Database Name: {database_name}")

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # Check if file exists on base branch
        logger.info(f"Checking if file exists on {feature_branch}")
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context,
                file_location.repo,
                base_branch,
                file_location.file_path
            )
        if cached_entry:
            existing_file = {
                "exists": True,
                "content": cached_entry.get("content")
            }
        else:
            fetch_context = f"repo={repo} branch={feature_branch} path={file_location.file_path}"
            with log_timing(logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db,
                    tenant=tenant,
                    owner=owner,
                    repo=repo,
                    file_path=file_location.file_path,
                    branch=feature_branch,
                    # github_base_url=github_base_url,
                    # github_token=github_token
                )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        if not existing_file["exists"]:
            # File does NOT exist → raise error
            error_msg = f"File does not exist at {file_location.file_path} on branch {feature_branch}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # File exists → Use existing content as base
        logger.info(f"File exists - processing database addition")
        original_content = existing_file["content"]

        gen_context = f"path={file_location.file_path} mode=update"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            # Check if database exists and add to appropriate array
            modified_content, db_type = self._check_and_add_database(original_content, database_name)

            logger.info(f"✅ Database '{database_name}' added to {db_type}_databases array successfully")

            preview_hcl = self.preview_db_creation_hcl(
                database_name=database_name,
                db_server_name=db_server_name,
                db_type=db_type
            )
            self.logger.info("Generated database creation preview HCL")

        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            if not db_server_name:
                raise ValueError("Parameter 'db_server_name' is required when upload_to_s3 is True")

            try:
                original_s3_key = f"database/{db_server_name}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=modified_content,
                    content_type="text/plain"
                )
                self.logger.info(f"Uploaded original HCL to S3: {result['location']}")
            except Exception as e:
                self.logger.error(f"Failed to upload original HCL to S3: {str(e)}")
                raise

            try:
                preview_s3_key = f"preview/database/{db_server_name}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_hcl,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": environment,
                        "identifier": db_server_name,
                        "database_name": database_name,
                        "generated_by": "aspora_db_creation_script_gen_component"
                    }
                )
                self.logger.info(f"Uploaded preview HCL to S3: {result['location']}")
            except Exception as e:
                self.logger.warning(f"Failed to upload preview HCL to S3: {str(e)}")

            artifact_s3_key_json = {
                "original_s3_key": original_s3_key,
                "preview": preview_s3_key
            }

            if repository and queue_dict.get("code"):
                update_payload = json.dumps(artifact_s3_key_json)

                # Properly await the database update to ensure it commits
                await repository.update_artifact_s3_key(queue_dict.get("code"), update_payload)

                self.logger.info(
                    "Saved artifact_s3_key to database for queue item %s: %s",
                    queue_dict.get("code"),
                    update_payload
                )

        if tenant and file_location and workflow_context:
            # Skip commit if in preview mode
            if not workflow_context.skip_commit:
                _upsert_staged_entry(
                    workflow_context=workflow_context,
                    repo=file_location.repo,
                    base_branch=base_branch,
                    feature_branch=file_location.feature_branch,
                    file_path=file_location.file_path,
                    content=modified_content,
                    queue_id=queue_dict.get("id"),
                    script_gen_key=file_location.script_gen_key
                )
                queue_label = queue_dict.get("code") or queue_dict.get("id")
                if queue_label:
                    commit_line = f"{queue_label}: {file_location.script_gen_key} -> {file_location.file_path}"
                else:
                    commit_line = f"{file_location.script_gen_key} -> {file_location.file_path}"
                _append_commit_message(
                    workflow_context,
                    file_location.repo,
                    base_branch,
                    commit_line
                )
            # TODO: update the git commit sha response in the workflow context here after commit

            if queue_dict.get("id"):
                script_gen_key = file_location.script_gen_key
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                    "original_content": modified_content,
                    "preview_content": preview_hcl
                }

        return modified_content

    def _check_and_add_database(self, content: str, database_name: str) -> Tuple[str, str]:
        """
        Add database to appropriate array (mysql_databases or psql_databases).

        Logic:
        1. Check if mysql_databases array exists → append there
        2. Else check if psql_databases array exists → append there
        3. If neither exists → raise error

        Args:
            content: Full HCL file content
            database_name: Name of the database to add

        Returns:
            tuple: (modified_content, db_type) where db_type is 'mysql' or 'psql'
        """
        # Check for mysql_databases array first
        mysql_pattern = r'mysql_databases\s*=\s*\['
        mysql_match = re.search(mysql_pattern, content)

        if mysql_match:
            # Add database to mysql_databases array
            modified_content = self._add_database_to_array(content, database_name, 'mysql')
            return modified_content, 'mysql'

        # Check for psql_databases array
        psql_pattern = r'psql_databases\s*=\s*\['
        psql_match = re.search(psql_pattern, content)

        if psql_match:
            # Add database to psql_databases array
            modified_content = self._add_database_to_array(content, database_name, 'psql')
            return modified_content, 'psql'

        # Neither array found
        raise ValueError("No database array found in file (neither mysql_databases nor psql_databases)")

    def _add_database_to_array(self, content: str, database_name: str, db_type: str) -> str:
        """
        Add a database name to the specified database array.

        Args:
            content: Full HCL file content
            database_name: Database name to add
            db_type: 'mysql' or 'psql'

        Returns:
            Modified HCL content with database added to array
        """
        array_name = 'mysql_databases' if db_type == 'mysql' else 'psql_databases'

        # Find the array
        pattern = rf'({array_name}\s*=\s*\[)'
        match = re.search(pattern, content)

        # Find the closing bracket of the array
        array_start = match.end()
        bracket_depth = 1
        i = array_start
        in_string = False
        escape_next = False

        while i < len(content) and bracket_depth > 0:
            char = content[i]

            if escape_next:
                escape_next = False
                i += 1
                continue

            if char == '\\':
                escape_next = True
                i += 1
                continue

            if char == '"' and not in_string:
                in_string = True
                i += 1
                continue

            if char == '"' and in_string:
                in_string = False
                i += 1
                continue

            if not in_string:
                if char == '[':
                    bracket_depth += 1
                elif char == ']':
                    bracket_depth -= 1

            i += 1

        array_end = i - 1  # Position of closing ]

        # Check if array is empty or has content
        array_content = content[array_start:array_end].strip()

        if array_content:
            # Has existing databases - add comma and new database on new line
            content_before_bracket = content[:array_end].rstrip()

            # Detect indentation from existing array content
            # Find the last line before closing bracket to get indentation
            lines_before = content[:array_end].split('\n')
            last_content_line = ''
            for line in reversed(lines_before):
                stripped = line.strip()
                if stripped and stripped != '[':
                    last_content_line = line
                    break

            # Extract indentation (spaces/tabs before content)
            indent = ''
            for char in last_content_line:
                if char in ' \t':
                    indent += char
                else:
                    break

            # If no indentation found, use 4 spaces as default
            if not indent:
                indent = '    '

            # Check if already ends with comma to avoid double comma
            # Add newline before closing bracket to keep it on its own line
            if content_before_bracket.endswith(','):
                insert_text = f'\n{indent}"{database_name}"\n'
            else:
                insert_text = f',\n{indent}"{database_name}"\n'
            return content_before_bracket + insert_text + content[array_end:]
        else:
            # Empty array - add database with newline before closing bracket
            insert_text = f'\n    "{database_name}"\n'
            return content[:array_start] + insert_text + content[array_end:]

    def preview_db_creation_hcl(
        self,
        database_name: str,
        db_server_name: str,
        db_type: str = None
    ) -> str:
        """
        Render Database Creation Terragrunt preview for display.

        Shows the database array format based on the database type.
        If db_type is provided, shows only that type's array format.
        If db_type is None, shows a generic preview.
        """
        if not database_name or not database_name.strip():
            raise ValueError("database_name cannot be empty")
        if not db_server_name or not db_server_name.strip():
            raise ValueError("db_server_name cannot be empty")

        database_name = database_name.strip()
        db_server_name = db_server_name.strip()

        if db_type:
            db_type = db_type.strip().lower()

        if db_type == 'mysql':
            return f"""Server: {db_server_name}

mysql_databases = [
    # ...existing databases,
    {database_name},
]"""

        if db_type in ['postgresql', 'psql', 'postgres']:
            return f"""Server: {db_server_name}

psql_databases = [
    # ...existing databases,
    {database_name},
]"""

        return f"""

Server: {db_server_name}
Database: {database_name}

# Database type will be auto-detected from server configuration
# The database name will be added to the appropriate array:

mysql_databases = [
    # ...existing databases,
    "{database_name}"
]

# OR

psql_databases = [
    # ...existing databases,
    "{database_name}"
]"""
