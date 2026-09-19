import json
import logging
import os
import re
from typing import List, Dict
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


# Empty psql_users.hcl template used when the file does not yet exist in GitHub.
_EMPTY_PSQL_USERS_HCL = "psql_users = [\n]\n"


class AsporaDatabaseSeparateUsersFileScriptGenComponent:
    """
    Script gen component for PostgreSQL servers that store user/permission
    definitions in a separate psql_users.hcl file (loaded by Terragrunt via
    extra_arguments -var-file) instead of inline in terragrunt.hcl inputs.

    Behaviour:
    - Fetches psql_users.hcl from GitHub.
    - If the file does not exist yet, starts from an empty template.
    - Merges / adds each user in the server config.
    - Stages the resulting psql_users.hcl content (never touches terragrunt.hcl).
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository

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
        config_snapshot = queue_dict.get("config_snapshot") or {}

        file_path = file_location.file_path
        environment = config_snapshot.get('environment') or queue_dict.get("environment")

        db_server_name = None
        if file_location and getattr(file_location, "config", None):
            db_server_name = file_location.config.get("db_server_name")

        if not db_server_name and file_path:
            db_server_name = os.path.basename(os.path.dirname(file_path))

        pgsql_servers = config_snapshot.get('pgsql_servers', [])
        server_config = next(
            (s for s in pgsql_servers if s.get('db_server_name') == db_server_name),
            None
        )
        if not server_config:
            raise ValueError(f"No server config found for '{db_server_name}' in pgsql_servers")

        users_list = server_config.get('users', [])
        if not users_list:
            raise ValueError(f"No users found for server '{db_server_name}'")

        replace_grants = config_snapshot.get('replace_grants', True)

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # Check staged cache first
        existing_content = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context,
                file_location.repo,
                base_branch,
                file_path
            )
            if cached_entry:
                existing_content = cached_entry.get("content")

        if existing_content is None:
            fetch_context = f"repo={repo} branch={feature_branch} path={file_path}"
            with log_timing(self.logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db,
                    tenant=tenant,
                    owner=owner,
                    repo=repo,
                    file_path=file_path,
                    branch=feature_branch,
                )

            if existing_file.get("status") == "error":
                raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

            if existing_file.get("exists"):
                existing_content = existing_file["content"]
                self.logger.info(f"psql_users.hcl found in GitHub: {file_path}")
            else:
                # File does not exist yet — start from the empty template
                existing_content = _EMPTY_PSQL_USERS_HCL
                self.logger.info(
                    f"psql_users.hcl not found at {file_path}; initialising from empty template"
                )

        # Apply each user sequentially
        preview_hcl = ""
        username = None
        for user_entry in users_list:
            username = user_entry.get('db_user_name')
            password = user_entry.get('db_password')
            update_password = user_entry.get('update_password', False)
            grants = user_entry.get('grants', [])

            if not username:
                self.logger.warning("Skipping user entry with no db_user_name")
                continue

            gen_context = f"path={file_path} user={username}"
            with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
                existing_content = self._generate_psql_user(
                    existing_content, username, password, grants,
                    update_password, replace_grants
                )

                user_preview = self._preview_psql_user_hcl(username, password,
                                                            self._flatten_psql_grants(grants))
                preview_hcl = f"{preview_hcl}\n{user_preview}" if preview_hcl else user_preview

        modified_content = existing_content

        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            if not db_server_name:
                raise ValueError("db_server_name is required when upload_to_s3 is True")

            try:
                original_s3_key = f"database-users/{db_server_name}-psql-users.hcl"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=modified_content,
                    content_type="text/plain"
                )
                self.logger.info(f"Uploaded psql_users.hcl to S3: {result['location']}")
            except Exception as e:
                self.logger.error(f"Failed to upload psql_users.hcl to S3: {str(e)}")
                raise

            try:
                preview_s3_key = f"preview/database-users/{db_server_name}-psql-users.hcl"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_hcl,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": environment,
                        "identifier": db_server_name,
                        "db_type": "postgresql",
                        "username": username or "",
                        "generated_by": component_name,
                    }
                )
                self.logger.info(f"Uploaded preview to S3: {result['location']}")
            except Exception as e:
                self.logger.warning(f"Failed to upload preview to S3: {str(e)}")

            artifact_s3_key_json = {
                "original_s3_key": original_s3_key,
                "preview": preview_s3_key,
            }

            if repository and queue_dict.get("code"):
                update_payload = json.dumps(artifact_s3_key_json)
                await repository.update_artifact_s3_key(queue_dict.get("code"), update_payload)
                self.logger.info(
                    "Saved artifact_s3_key for queue item %s: %s",
                    queue_dict.get("code"),
                    update_payload,
                )

        if tenant and file_location and workflow_context:
            if not workflow_context.skip_commit:
                _upsert_staged_entry(
                    workflow_context=workflow_context,
                    repo=file_location.repo,
                    base_branch=base_branch,
                    feature_branch=file_location.feature_branch,
                    file_path=file_location.file_path,
                    content=modified_content,
                    queue_id=queue_dict.get("id"),
                    script_gen_key=file_location.script_gen_key,
                )
                queue_label = queue_dict.get("code") or queue_dict.get("id")
                commit_line = (
                    f"{queue_label}: {file_location.script_gen_key} -> {file_location.file_path}"
                    if queue_label
                    else f"{file_location.script_gen_key} -> {file_location.file_path}"
                )
                _append_commit_message(workflow_context, file_location.repo, base_branch, commit_line)

            if queue_dict.get("id"):
                workflow_context.script_gen_responses[queue_dict.get("id")][
                    file_location.script_gen_key
                ] = {
                    "original_content": modified_content,
                    "preview_content": preview_hcl,
                }

        return modified_content

    # =========================================================================
    # PostgreSQL user manipulation helpers
    # =========================================================================

    def _generate_psql_user(
        self,
        existing_content: str,
        username: str,
        password: str,
        grants: List[Dict],
        update_password: bool,
        replace_grants: bool,
    ) -> str:
        databases_to_update = {grant['database'] for grant in grants}
        hcl_grants = self._flatten_psql_grants(grants)

        if self._check_psql_user_exists(existing_content, username):
            return self._merge_psql_user_grants(
                existing_content, username, hcl_grants,
                password=password, update_password=update_password,
                replace_grants=replace_grants,
                databases_to_update=databases_to_update,
            )
        return self._add_psql_user(existing_content, username, password, hcl_grants)

    def _check_psql_user_exists(self, content: str, username: str) -> bool:
        return bool(re.search(f'name\\s*=\\s*"{username}"', content))

    def _add_psql_user(
        self,
        content: str,
        username: str,
        password: str,
        grants: List[Dict],
    ) -> str:
        user_hcl = self._format_psql_user_hcl(username, password, grants)

        match = re.search(r'(psql_users\s*=\s*\[)', content)
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
            separator = "\n" if content_before_bracket.endswith(',') else ",\n"
            return content_before_bracket + separator + user_hcl + "\n  " + content[array_end:]
        return content[:array_end] + f"\n{user_hcl}\n  " + content[array_end:]

    def _merge_psql_user_grants(
        self,
        content: str,
        username: str,
        new_grants: List[Dict],
        password: str = None,
        update_password: bool = False,
        replace_grants: bool = True,
        databases_to_update: set = None,
    ) -> str:
        user_match = re.search(f'name\\s*=\\s*"{username}"', content)
        if not user_match:
            raise ValueError(f"PostgreSQL user '{username}' not found in content")

        grants_search_start = user_match.end()
        grants_match = re.search(r'grants\s*=\s*\[', content[grants_search_start:])
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
            dbs_being_updated = databases_to_update or {g['database'] for g in new_grants}
            for g in existing_grants:
                if g['database'] not in dbs_being_updated:
                    merged_grants[(g['database'], g['schema'], g['object_type'])] = list(g['privileges'])
            for g in new_grants:
                merged_grants[(g['database'], g['schema'], g['object_type'])] = sorted(g['privileges'])
        else:
            for g in existing_grants:
                merged_grants[(g['database'], g['schema'], g['object_type'])] = list(g['privileges'])
            for g in new_grants:
                key = (g['database'], g['schema'], g['object_type'])
                if key in merged_grants:
                    merged_grants[key] = sorted(set(merged_grants[key]) | set(g['privileges']))
                else:
                    merged_grants[key] = sorted(g['privileges'])

        hcl_items = []
        for (database, schema, object_type), privileges in merged_grants.items():
            hcl_items.append(f'''        {{
          database    = "{database}"
          schema      = "{schema}"
          object_type = "{object_type}"
          privileges  = {json.dumps(privileges)}
        }}''')

        joined_items = ",\n".join(hcl_items)
        replacement = f"\n{joined_items}\n      "
        modified_content = content[:grants_array_start] + replacement + content[grants_array_end:]

        if update_password and password:
            password_pattern = f'(name\\s*=\\s*"{username}"[^}}]*password\\s*=\\s*")[^"]*(")'
            modified_content = re.sub(password_pattern, f'\\g<1>{password}\\g<2>', modified_content)

        return modified_content

    def _format_psql_user_hcl(
        self,
        username: str,
        password: str,
        grants: List[Dict],
    ) -> str:
        grants_hcl_items = []
        for g in grants:
            grants_hcl_items.append(f'''        {{
          database    = "{g['database']}"
          schema      = "{g['schema']}"
          object_type = "{g['object_type']}"
          privileges  = {json.dumps(g['privileges'])}
        }}''')

        grants_hcl = ",\n".join(grants_hcl_items)
        return f'''    {{
      name     = "{username}"
      password = "{password}"
      grants = [
{grants_hcl}
      ]
    }}'''

    def _preview_psql_user_hcl(
        self,
        username: str,
        password: str,
        grants: List[Dict],
    ) -> str:
        grants_hcl_items = []
        for grant in grants:
            grants_hcl_items.append(f'''        {{
          database    = "{grant['database']}"
          schema      = "{grant['schema']}"
          object_type = "{grant['object_type']}"
          privileges  = {json.dumps(grant['privileges'])}
        }}''')

        grants_hcl = ",\n".join(grants_hcl_items)
        return f'''    {{
      name     = "{username}"
      password = "{password}"
      grants = [
{grants_hcl}
      ]
    }}'''

    def _flatten_psql_grants(self, grants: List[Dict]) -> List[Dict]:
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
                    "privileges": db_permissions,
                })
                hcl_grants.append({
                    "database": database,
                    "schema": schema_name,
                    "object_type": "schema",
                    "privileges": schema_permissions,
                })
                tables = schema_grant.get("tables", [])
                table_privileges = tables[0].get("privileges", []) if tables else []
                hcl_grants.append({
                    "database": database,
                    "schema": schema_name,
                    "object_type": "table",
                    "privileges": table_privileges,
                })
        return hcl_grants

    def _parse_psql_grants_from_hcl(self, grants_content: str) -> List[Dict]:
        grants = []
        pattern = (
            r'\{[^}]*database\s*=\s*"([^"]+)"[^}]*schema\s*=\s*"([^"]+)"'
            r'[^}]*object_type\s*=\s*"([^"]+)"[^}]*privileges\s*=\s*\[([^\]]*)\][^}]*\}'
        )
        for match in re.finditer(pattern, grants_content, re.DOTALL):
            grants.append({
                'database': match.group(1),
                'schema': match.group(2),
                'object_type': match.group(3),
                'privileges': re.findall(r'"([^"]+)"', match.group(4)),
            })
        return grants
