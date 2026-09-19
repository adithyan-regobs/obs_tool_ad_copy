"""
Default Aurora Database Creation Script Gen Component

Appends a new database name to the psql_databases or mysql_databases array
in an existing Aurora terragrunt.hcl.  The file must already exist in the
infra repo (i.e. the cluster was provisioned by DefaultAuroraScriptGenComponent).

config_snapshot keys consumed:
  - database_name   : name of the new database to create
  - db_server_name  : identifier used in commit message / logging
"""

import logging
import re
from typing import Tuple

from app.handlers.gitops_handler import GitOpsHandler
from app.utils.timing import log_timing

logger = logging.getLogger(__name__)


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


class DefaultAuroraDatabaseCreationScriptGenComponent:
    """
    Appends a database name to the psql_databases / mysql_databases array
    in an existing Aurora terragrunt.hcl.

    Raises ValueError if:
      - The file does not exist in the repo.
      - Neither psql_databases nor mysql_databases array is found.
      - The database name already exists in the array.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = False,
        db=None,
    ) -> str:
        config_snapshot = queue_dict.get("config_snapshot") or {}

        database_name = config_snapshot.get("database_name") or queue_dict.get("database_name")
        db_server_name = config_snapshot.get("db_server_name") or queue_dict.get("db_server_name")

        if not database_name:
            raise ValueError("[AuroraDbCreation] database_name is required in config_snapshot")

        # Resolve db_server_name from infra mst when not directly supplied
        if not db_server_name:
            infra_code = queue_dict.get("transaction_code")
            if infra_code and db:
                from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
                infra_repo = InfrastructureMstRepository(db)
                infra = await infra_repo.get_by_code(infra_code)
                if infra:
                    db_server_name = infra.name
                    self.logger.info(
                        "[AuroraDbCreation] resolved db_server_name=%r from transaction_code=%r",
                        db_server_name, infra_code,
                    )
                else:
                    self.logger.warning(
                        "[AuroraDbCreation] no infra record found for transaction_code=%r", infra_code
                    )

        repo_parts = file_location.repo.split("/")
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        base_branch = file_location.base_branch or file_location.target_branch or ""
        feature_branch = file_location.feature_branch
        component_name = self.__class__.__name__

        self.logger.info(
            "[AuroraDbCreation] db=%r server=%r path=%s branch=%s",
            database_name, db_server_name, file_location.file_path, feature_branch,
        )

        # Prefer already-staged content from an earlier step in the same workflow
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context,
                file_location.repo,
                base_branch,
                file_location.file_path,
            )

        if cached_entry:
            existing_file = {"exists": True, "content": cached_entry.get("content")}
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
                )

        if existing_file.get("status") == "error":
            raise ValueError(f"[AuroraDbCreation] GitOps get_content failed: {existing_file.get('error')}")

        if not existing_file.get("exists"):
            raise ValueError(
                f"[AuroraDbCreation] File does not exist at {file_location.file_path} "
                f"on branch {feature_branch}. Provision the cluster first."
            )

        original_content = existing_file["content"]

        gen_context = f"path={file_location.file_path} db={database_name}"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            modified_content, db_type = self._check_and_add_database(original_content, database_name)

        self.logger.info(
            "[AuroraDbCreation] appended %r to %s_databases in %s",
            database_name, db_type, file_location.file_path,
        )

        if workflow_context and not workflow_context.skip_commit:
            _upsert_staged_entry(
                workflow_context=workflow_context,
                repo=file_location.repo,
                base_branch=base_branch,
                feature_branch=feature_branch,
                file_path=file_location.file_path,
                content=modified_content,
                queue_id=queue_dict.get("id"),
                script_gen_key=file_location.script_gen_key,
            )
            queue_label = queue_dict.get("code") or queue_dict.get("id")
            identifier = db_server_name or queue_label
            _append_commit_message(
                workflow_context,
                file_location.repo,
                base_branch,
                f"{queue_label}: add database {database_name} to {identifier}",
            )

        if queue_dict.get("id") and workflow_context:
            preview_hcl = self._preview(database_name, db_server_name, db_type)
            workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                "original_content": modified_content,
                "preview_content": preview_hcl,
            }

        return modified_content

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_and_add_database(self, content: str, database_name: str) -> Tuple[str, str]:
        """
        Detect array type, validate no duplicate, then append.

        Returns (modified_content, db_type) where db_type is 'mysql' or 'psql'.
        Raises ValueError on duplicate or missing array.
        """
        for db_type, array_name in (("mysql", "mysql_databases"), ("psql", "psql_databases")):
            if re.search(rf'{array_name}\s*=\s*\[', content):
                self._assert_not_duplicate(content, array_name, database_name)
                return self._append_to_array(content, array_name, database_name), db_type

        raise ValueError(
            "[AuroraDbCreation] Neither psql_databases nor mysql_databases array found in file. "
            "Ensure the cluster was provisioned with the updated template."
        )

    def _assert_not_duplicate(self, content: str, array_name: str, database_name: str) -> None:
        match = re.search(rf'{array_name}\s*=\s*\[([^\]]*)\]', content, re.DOTALL)
        if not match:
            return
        existing = re.findall(r'"([^"]+)"', match.group(1))
        if database_name in existing:
            raise ValueError(
                f"[AuroraDbCreation] Database '{database_name}' already exists in {array_name}."
            )

    def _append_to_array(self, content: str, array_name: str, database_name: str) -> str:
        pattern = rf'({array_name}\s*=\s*\[)'
        match = re.search(pattern, content)

        # Walk to closing bracket
        array_start = match.end()
        depth = 1
        i = array_start
        in_string = False
        escape_next = False

        while i < len(content) and depth > 0:
            ch = content[i]
            if escape_next:
                escape_next = False
            elif ch == "\\":
                escape_next = True
            elif ch == '"' and not in_string:
                in_string = True
            elif ch == '"' and in_string:
                in_string = False
            elif not in_string:
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
            i += 1

        array_end = i - 1  # index of closing ]
        array_body = content[array_start:array_end].strip()

        if array_body:
            before = content[:array_end].rstrip()
            # Derive indentation from last non-empty line inside the array
            indent = "    "
            for line in reversed(content[:array_end].split("\n")):
                stripped = line.strip()
                if stripped and stripped != "[":
                    indent = "".join(ch for ch in line if ch in " \t")
                    if not indent:
                        indent = "    "
                    break
            sep = "" if before.endswith(",") else ","
            return before + sep + f'\n{indent}"{database_name}"\n' + content[array_end:]
        else:
            return content[:array_start] + f'\n    "{database_name}"\n' + content[array_end:]

    def _preview(self, database_name: str, db_server_name: str, db_type: str) -> str:
        array_name = "mysql_databases" if db_type == "mysql" else "psql_databases"
        server_line = f"Server: {db_server_name}\n\n" if db_server_name else ""
        return (
            f"{server_line}"
            f"{array_name} = [\n"
            f"  # ...existing databases\n"
            f'  "{database_name}",\n'
            f"]"
        )
