import json
import logging
import os
import re
from typing import List, Dict
from app.domain.validators.github_rules import GitHubValidationError
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.timing import log_timing


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


class AsporaDatabaseUserManagementScriptGenComponent:
    """
    Component responsible for generating database user management script content.
    This component focuses on script generation including fetching existing content from GitHub,
    modifying it, and returning the modified content.

    Supports both MySQL and PostgreSQL user management by modifying existing terragrunt.hcl
    content (mysql_users or psql_users arrays).
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
        Generate modified database user terragrunt.hcl content.

        Returns:
            str: Modified terragrunt.hcl content with updated mysql_users or psql_users array

        Raises:
            ValueError: If db_type is invalid, users array not found, or user not found during merge
            GitHubValidationError: If file doesn't exist in GitHub repository
        """
        config_snapshot = queue_dict.get("config_snapshot") or {}

        # Extract parameters
        # github_token = config_snapshot.get('github_token')
        # github_base_url = config_snapshot.get('github_base_url')
        owner = config_snapshot.get('owner')
        repo = config_snapshot.get('repo')
        file_path = file_location.file_path
        db_type = None
        username = config_snapshot.get('db_user_name')
        password = config_snapshot.get('db_password')
        update_password = config_snapshot.get('update_password', False)
        replace_grants = config_snapshot.get('replace_grants', True)
        environment =config_snapshot.get('environment') or queue_dict.get("environment")

        db_server_name = None
        if file_location and getattr(file_location, "config", None):
            db_type = file_location.config.get("db_type")
            db_server_name = file_location.config.get("db_server_name")

        if not db_server_name and file_path:
            db_server_name = os.path.basename(os.path.dirname(file_path))

        # Validate db_type
        if db_type not in ['mysql', 'psql', 'postgresql']:
            raise ValueError(f"Invalid db_type: {db_type}. Must be 'mysql' or 'psql'")

        server_list = []
        if db_type == 'mysql':
            server_list = config_snapshot.get('mysql_servers', [])
        else:
            server_list = config_snapshot.get('pgsql_servers', [])

        if not db_server_name:
            raise ValueError("db_server_name is required to resolve per-server grants")

        server_config = next(
            (server for server in server_list if server.get('db_server_name') == db_server_name),
            None
        )
        if not server_config:
            raise ValueError(f"No server config found for '{db_server_name}' in {db_type} servers")

        grants = server_config.get('grants')
        if grants is None:
            raise ValueError(f"No grants found for server '{db_server_name}'")

        self.logger.info(f"Fetching existing file from GitHub: {file_path}")

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # Fetch existing file from GitHub (file existence is verified before calling this component)
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context,
                file_location.repo,
                base_branch,
                file_path
            )
        if cached_entry:
            existing_file = {
                "exists": True,
                "content": cached_entry.get("content")
            }
        else:
            fetch_context = f"repo={repo} branch={feature_branch} path={file_path}"
            with log_timing(self.logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db,
                    tenant=tenant,
                    owner=owner,
                    repo=repo,
                    file_path=file_path,
                    branch=feature_branch,
                    # github_base_url=github_base_url,
                    # github_token=github_token
                )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        if not existing_file.get("exists"):
            raise GitHubValidationError([f"File not found in GitHub: {file_path}"])

        self.logger.info(f"File found in GitHub: {file_path}")
        existing_content = existing_file["content"]

        self.logger.info(f"Generating {db_type} user script for user '{username}'")

        gen_context = f"path={file_path} mode=update"
        with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
            # Route to appropriate method based on db_type
            if db_type == 'mysql':
                modified_content = self._generate_mysql_user(
                    existing_content, username, password, grants,
                    update_password, replace_grants
                )
            else:  # psql
                modified_content = self._generate_psql_user(
                    existing_content, username, password, grants,
                    update_password, replace_grants
                )

            preview_hcl = self._preview_db_user_hcl(
                db_type=db_type,
                username=username,
                password=password,
                grants=grants
            )
            self.logger.info(f"Generated {db_type} user preview HCL for '{username}'")

        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            if not db_server_name:
                raise ValueError("Parameter 'file_path' is required when upload_to_s3 is True")

            try:
                original_s3_key = f"database-users/{db_server_name}.hcl"
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
                preview_s3_key = f"preview/database-users/{db_server_name}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_hcl,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": environment,
                        "identifier": db_server_name,
                        "db_type": db_type,
                        "username": username,
                        "generated_by": "aspora_database_user_management_script_gen_component"
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

    # ========================================================================
    # MySQL User Management Methods
    # ========================================================================

    def _generate_mysql_user(
        self,
        existing_content: str,
        username: str,
        password: str,
        grants: List[Dict],
        update_password: bool,
        replace_grants: bool
    ) -> str:
        """Generate MySQL user script content."""
        # Flatten grants to HCL format
        hcl_grants = []
        for grant in grants:
            database = grant["database"]
            for table_grant in grant.get("tables", []):
                table = table_grant["table"]
                # Map "all" → "*" for HCL
                hcl_table = "*" if table == "all" else table
                privileges = table_grant["privileges"]
                hcl_grants.append({
                    "database": database,
                    "table": hcl_table,
                    "privileges": privileges
                })

        if self._check_mysql_user_exists(existing_content, username):
            # User exists → merge or replace grants
            if replace_grants:
                self.logger.info(f"MySQL user '{username}' exists - replacing grants")
            else:
                self.logger.info(f"MySQL user '{username}' exists - merging grants")
            return self._merge_mysql_user_grants(
                existing_content, username, hcl_grants,
                password=password, update_password=update_password,
                replace_grants=replace_grants
            )
        else:
            # User doesn't exist → add new user
            self.logger.info(f"MySQL user '{username}' does not exist - adding new user")
            return self._add_mysql_user(existing_content, username, password, hcl_grants)

    def _preview_mysql_user_hcl(
        self,
        username: str,
        password: str,
        grants: List[Dict]
    ) -> str:
        """
        Generate preview HCL block for a MySQL user.

        Args:
            username: Database username
            password: KMS encrypted password
            grants: List of flattened grants [{database, table, privileges}]

        Returns:
            HCL string for preview
        """
        grants_hcl_items = []
        for grant in grants:
            grant_hcl = f'''        {{
          database   = "{grant['database']}"
          table      = "{grant['table']}"
          privileges = {json.dumps(grant['privileges'])}
        }}'''
            grants_hcl_items.append(grant_hcl)

        grants_hcl = ",\n".join(grants_hcl_items)

        return f'''    {{
      name     = "{username}"
      password = "{password}"
      host     = "%"
      grants = [
{grants_hcl}
      ]
    }}'''

    def _flatten_mysql_grants(self, grants: List[Dict]) -> List[Dict]:
        """
        Flatten MySQL grants to HCL format.

        Input format:
          [{database: str, tables: [{table: str, privileges: [str]}]}]

        Output format:
          [{database, table, privileges}, ...]
        """
        hcl_grants = []
        for grant in grants:
            database = grant["database"]
            for table_grant in grant.get("tables", []):
                table = table_grant["table"]
                hcl_table = "*" if table == "all" else table
                hcl_grants.append({
                    "database": database,
                    "table": hcl_table,
                    "privileges": table_grant.get("privileges", [])
                })

        return hcl_grants

    def _check_mysql_user_exists(self, content: str, username: str) -> bool:
        """Check if a MySQL user exists in mysql_users array."""
        pattern = f'name\\s*=\\s*"{username}"'
        return bool(re.search(pattern, content))

    def _add_mysql_user(
        self,
        content: str,
        username: str,
        password: str,
        grants: List[Dict]
    ) -> str:
        """Add new user entry to mysql_users array."""
        user_hcl = self._format_mysql_user_hcl(username, password, grants)

        # Find mysql_users = [ and its closing ]
        pattern = r'(mysql_users\s*=\s*\[)'
        match = re.search(pattern, content)
        if not match:
            raise ValueError("mysql_users array not found in content")

        # Find the closing bracket of mysql_users array
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

        array_end = i - 1

        # Check if array is empty or has content
        array_content = content[array_start:array_end].strip()
        if array_content:
            content_before_bracket = content[:array_end].rstrip()
            if content_before_bracket.endswith(','):
                insert_text = f"\n{user_hcl}\n  "
            else:
                insert_text = f",\n{user_hcl}\n  "
            return content_before_bracket + insert_text + content[array_end:]
        else:
            insert_text = f"\n{user_hcl}\n  "
            return content[:array_end] + insert_text + content[array_end:]

    def _merge_mysql_user_grants(
        self,
        content: str,
        username: str,
        new_grants: List[Dict],
        password: str = None,
        update_password: bool = False,
        replace_grants: bool = True
    ) -> str:
        """Merge or replace grants in existing MySQL user's grants array."""
        user_pattern = f'name\\s*=\\s*"{username}"'
        user_match = re.search(user_pattern, content)
        if not user_match:
            raise ValueError(f"User '{username}' not found in content")

        grants_search_start = user_match.end()
        grants_pattern = r'grants\s*=\s*\['
        grants_match = re.search(grants_pattern, content[grants_search_start:])
        if not grants_match:
            raise ValueError(f"grants array not found for user '{username}'")

        grants_array_start = grants_search_start + grants_match.end()

        # Find the closing ] of grants array
        bracket_depth = 1
        i = grants_array_start
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

        grants_array_end = i - 1

        # Parse existing grants from HCL
        grants_content = content[grants_array_start:grants_array_end]
        existing_grants = self._parse_mysql_grants_from_hcl(grants_content)

        # Build merged grants dict keyed by (database, table)
        merged_grants: Dict[tuple, List[str]] = {}

        if replace_grants:
            databases_being_updated = {grant['database'] for grant in new_grants}

            for grant in existing_grants:
                if grant['database'] not in databases_being_updated:
                    key = (grant['database'], grant['table'])
                    merged_grants[key] = list(grant['privileges'])

            for grant in new_grants:
                key = (grant['database'], grant['table'])
                merged_grants[key] = sorted(grant['privileges'])
        else:
            for grant in existing_grants:
                key = (grant['database'], grant['table'])
                merged_grants[key] = list(grant['privileges'])

            for grant in new_grants:
                key = (grant['database'], grant['table'])
                if key in merged_grants:
                    existing_privs = set(merged_grants[key])
                    new_privs = set(grant['privileges'])
                    merged_grants[key] = sorted(existing_privs | new_privs)
                else:
                    merged_grants[key] = sorted(grant['privileges'])

        # Format merged grants as HCL
        merged_grants_hcl_items = []
        for (database, table), privileges in merged_grants.items():
            grant_hcl = f'''      {{
        database   = "{database}"
        table      = "{table}"
        privileges = {json.dumps(privileges)}
      }}'''
            merged_grants_hcl_items.append(grant_hcl)

        merged_grants_hcl = ",\n".join(merged_grants_hcl_items)
        replacement = f"\n{merged_grants_hcl}\n    "

        modified_content = content[:grants_array_start] + replacement + content[grants_array_end:]

        # Update password if requested
        if update_password and password:
            self.logger.info(f"Updating password for user '{username}'")
            password_pattern = f'(name\\s*=\\s*"{username}"[^}}]*password\\s*=\\s*")[^"]*(")'
            modified_content = re.sub(
                password_pattern,
                f'\\g<1>{password}\\g<2>',
                modified_content
            )

        return modified_content

    def _format_mysql_user_hcl(
        self,
        username: str,
        password: str,
        grants: List[Dict]
    ) -> str:
        """Generate HCL block for a MySQL user."""
        grants_hcl_items = []
        for g in grants:
            grant_hcl = f'''        {{
          database   = "{g['database']}"
          table      = "{g['table']}"
          privileges = {json.dumps(g['privileges'])}
        }}'''
            grants_hcl_items.append(grant_hcl)

        grants_hcl = ",\n".join(grants_hcl_items)

        return f'''    {{
      name     = "{username}"
      password = "{password}"
      host     = "%"
      grants = [
{grants_hcl}
      ]
    }}'''

    def _parse_mysql_grants_from_hcl(self, grants_content: str) -> List[Dict]:
        """Parse MySQL grants array content from HCL into a list of dicts."""
        grants = []

        grant_block_pattern = r'\{[^}]*database\s*=\s*"([^"]+)"[^}]*table\s*=\s*"([^"]+)"[^}]*privileges\s*=\s*\[([^\]]*)\][^}]*\}'

        for match in re.finditer(grant_block_pattern, grants_content, re.DOTALL):
            database = match.group(1)
            table = match.group(2)
            privileges_str = match.group(3)
            privileges = re.findall(r'"([^"]+)"', privileges_str)

            grants.append({
                'database': database,
                'table': table,
                'privileges': privileges
            })

        return grants

    # ========================================================================
    # PostgreSQL User Management Methods
    # ========================================================================

    def _generate_psql_user(
        self,
        existing_content: str,
        username: str,
        password: str,
        grants: List[Dict],
        update_password: bool,
        replace_grants: bool
    ) -> str:
        """Generate PostgreSQL user script content."""
        databases_to_update = {grant['database'] for grant in grants}
        hcl_grants = self._flatten_psql_grants(grants)

        if self._check_psql_user_exists(existing_content, username):
            if replace_grants:
                self.logger.info(f"PostgreSQL user '{username}' exists - replacing grants")
            else:
                self.logger.info(f"PostgreSQL user '{username}' exists - merging grants")
            return self._merge_psql_user_grants(
                existing_content, username, hcl_grants,
                password=password, update_password=update_password,
                replace_grants=replace_grants,
                databases_to_update=databases_to_update
            )
        else:
            self.logger.info(f"PostgreSQL user '{username}' does not exist - adding new user")
            return self._add_psql_user(existing_content, username, password, hcl_grants)

    def _preview_psql_user_hcl(
        self,
        username: str,
        password: str,
        grants: List[Dict]
    ) -> str:
        """
        Generate preview HCL block for a PostgreSQL user.

        Args:
            username: Database username
            password: KMS encrypted password
            grants: List of flattened grants [{database, schema, object_type, privileges}]

        Returns:
            HCL string for preview
        """
        grants_hcl_items = []
        for grant in grants:
            grant_hcl = f'''        {{
          database    = "{grant['database']}"
          schema      = "{grant['schema']}"
          object_type = "{grant['object_type']}"
          privileges  = {json.dumps(grant['privileges'])}
        }}'''
            grants_hcl_items.append(grant_hcl)

        grants_hcl = ",\n".join(grants_hcl_items)

        return f'''    {{
      name     = "{username}"
      password = "{password}"
      grants = [
{grants_hcl}
      ]
    }}'''

    def _flatten_psql_grants(self, grants: List[Dict]) -> List[Dict]:
        """
        Flatten nested API grants to HCL format with object_type entries.

        Input format:
          [{
              database: str,
              permissions: [str],
              schemas: [{
                  schemaName: str,
                  permissions: [str],
                  tables: [{tableName: str, privileges: [str]}]
              }]
          }]

        Output format:
          [{ database, schema, object_type, privileges }, ...]
        """
        hcl_grants = []

        for grant in grants:
            database = grant["database"]
            db_permissions = grant.get("permissions", [])

            for schema_grant in grant.get("schemas", []):
                schema_name = schema_grant["schemaName"]
                schema_permissions = schema_grant.get("permissions", [])

                hcl_grants.append({
                    "database": database,
                    "schema": schema_name,
                    "object_type": "database",
                    "privileges": db_permissions
                })

                hcl_grants.append({
                    "database": database,
                    "schema": schema_name,
                    "object_type": "schema",
                    "privileges": schema_permissions
                })

                tables = schema_grant.get("tables", [])
                table_privileges = tables[0].get("privileges", []) if tables else []
                hcl_grants.append({
                    "database": database,
                    "schema": schema_name,
                    "object_type": "table",
                    "privileges": table_privileges
                })

        return hcl_grants

    def _preview_db_user_hcl(
        self,
        db_type: str,
        username: str,
        password: str,
        grants: List[Dict]
    ) -> str:
        """
        Generate preview HCL for database user management.

        Args:
            db_type: 'mysql' or 'psql'
            username: Database username
            password: KMS encrypted password
            grants: List of grants in nested API format
        """
        if db_type == "mysql":
            flattened_grants = self._flatten_mysql_grants(grants)
            return self._preview_mysql_user_hcl(username, password, flattened_grants)

        flattened_grants = self._flatten_psql_grants(grants)
        return self._preview_psql_user_hcl(username, password, flattened_grants)

    def _check_psql_user_exists(self, content: str, username: str) -> bool:
        """Check if a PostgreSQL user exists in psql_users array."""
        pattern = f'name\\s*=\\s*"{username}"'
        return bool(re.search(pattern, content))

    def _flatten_psql_grants(self, grants: List[Dict]) -> List[Dict]:
        """Flatten nested API grants to HCL format with object_type entries."""
        hcl_grants = []

        for grant in grants:
            database = grant["database"]
            db_permissions = grant.get("permissions", [])

            for schema_grant in grant.get("schemas", []):
                schema_name = schema_grant["schemaName"]
                schema_permissions = schema_grant.get("permissions", [])

                hcl_grants.append({
                    "database": database,
                    "schema": schema_name,
                    "object_type": "database",
                    "privileges": db_permissions
                })

                hcl_grants.append({
                    "database": database,
                    "schema": schema_name,
                    "object_type": "schema",
                    "privileges": schema_permissions
                })

                tables = schema_grant.get("tables", [])
                table_privileges = tables[0].get("privileges", []) if tables else []
                hcl_grants.append({
                    "database": database,
                    "schema": schema_name,
                    "object_type": "table",
                    "privileges": table_privileges
                })

        return hcl_grants

    def _add_psql_user(
        self,
        content: str,
        username: str,
        password: str,
        grants: List[Dict]
    ) -> str:
        """Add new user entry to psql_users array."""
        user_hcl = self._format_psql_user_hcl(username, password, grants)

        pattern = r'(psql_users\s*=\s*\[)'
        match = re.search(pattern, content)
        if not match:
            raise ValueError("psql_users array not found in content")

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

        array_end = i - 1

        array_content = content[array_start:array_end].strip()
        if array_content:
            content_before_bracket = content[:array_end].rstrip()
            if content_before_bracket.endswith(','):
                insert_text = f"\n{user_hcl}\n  "
            else:
                insert_text = f",\n{user_hcl}\n  "
            return content_before_bracket + insert_text + content[array_end:]
        else:
            insert_text = f"\n{user_hcl}\n  "
            return content[:array_end] + insert_text + content[array_end:]

    def _merge_psql_user_grants(
        self,
        content: str,
        username: str,
        new_grants: List[Dict],
        password: str = None,
        update_password: bool = False,
        replace_grants: bool = True,
        databases_to_update: set = None
    ) -> str:
        """Merge or replace grants in existing PostgreSQL user's grants array."""
        user_pattern = f'name\\s*=\\s*"{username}"'
        user_match = re.search(user_pattern, content)
        if not user_match:
            raise ValueError(f"PostgreSQL user '{username}' not found in content")

        grants_search_start = user_match.end()
        grants_pattern = r'grants\s*=\s*\['
        grants_match = re.search(grants_pattern, content[grants_search_start:])
        if not grants_match:
            raise ValueError(f"grants array not found for PostgreSQL user '{username}'")

        grants_array_start = grants_search_start + grants_match.end()

        bracket_depth = 1
        i = grants_array_start
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

        grants_array_end = i - 1

        grants_content = content[grants_array_start:grants_array_end]
        existing_grants = self._parse_psql_grants_from_hcl(grants_content)

        merged_grants: Dict[tuple, List[str]] = {}

        if replace_grants:
            if databases_to_update is not None:
                databases_being_updated = databases_to_update
            else:
                databases_being_updated = {grant['database'] for grant in new_grants}

            for grant in existing_grants:
                if grant['database'] not in databases_being_updated:
                    key = (grant['database'], grant['schema'], grant['object_type'])
                    merged_grants[key] = list(grant['privileges'])

            for grant in new_grants:
                key = (grant['database'], grant['schema'], grant['object_type'])
                merged_grants[key] = sorted(grant['privileges'])
        else:
            for grant in existing_grants:
                key = (grant['database'], grant['schema'], grant['object_type'])
                merged_grants[key] = list(grant['privileges'])

            for grant in new_grants:
                key = (grant['database'], grant['schema'], grant['object_type'])
                if key in merged_grants:
                    existing_privs = set(merged_grants[key])
                    new_privs = set(grant['privileges'])
                    merged_grants[key] = sorted(existing_privs | new_privs)
                else:
                    merged_grants[key] = sorted(grant['privileges'])

        merged_grants_hcl_items = []
        for (database, schema, object_type), privileges in merged_grants.items():
            grant_hcl = f'''        {{
          database    = "{database}"
          schema      = "{schema}"
          object_type = "{object_type}"
          privileges  = {json.dumps(privileges)}
        }}'''
            merged_grants_hcl_items.append(grant_hcl)

        merged_grants_hcl = ",\n".join(merged_grants_hcl_items)
        replacement = f"\n{merged_grants_hcl}\n      "

        modified_content = content[:grants_array_start] + replacement + content[grants_array_end:]

        if update_password and password:
            self.logger.info(f"Updating password for PostgreSQL user '{username}'")
            password_pattern = f'(name\\s*=\\s*"{username}"[^}}]*password\\s*=\\s*")[^"]*(")'
            modified_content = re.sub(
                password_pattern,
                f'\\g<1>{password}\\g<2>',
                modified_content
            )

        return modified_content

    def _format_psql_user_hcl(
        self,
        username: str,
        password: str,
        grants: List[Dict]
    ) -> str:
        """Generate HCL block for a PostgreSQL user."""
        grants_hcl_items = []
        for g in grants:
            grant_hcl = f'''        {{
          database    = "{g['database']}"
          schema      = "{g['schema']}"
          object_type = "{g['object_type']}"
          privileges  = {json.dumps(g['privileges'])}
        }}'''
            grants_hcl_items.append(grant_hcl)

        grants_hcl = ",\n".join(grants_hcl_items)

        return f'''    {{
      name     = "{username}"
      password = "{password}"
      grants = [
{grants_hcl}
      ]
    }}'''

    def _parse_psql_grants_from_hcl(self, grants_content: str) -> List[Dict]:
        """Parse PostgreSQL grants array content from HCL into a list of dicts."""
        grants = []

        grant_block_pattern = r'\{[^}]*database\s*=\s*"([^"]+)"[^}]*schema\s*=\s*"([^"]+)"[^}]*object_type\s*=\s*"([^"]+)"[^}]*privileges\s*=\s*\[([^\]]*)\][^}]*\}'

        for match in re.finditer(grant_block_pattern, grants_content, re.DOTALL):
            database = match.group(1)
            schema = match.group(2)
            object_type = match.group(3)
            privileges_str = match.group(4)
            privileges = re.findall(r'"([^"]+)"', privileges_str)

            grants.append({
                'database': database,
                'schema': schema,
                'object_type': object_type,
                'privileges': privileges
            })

        return grants
