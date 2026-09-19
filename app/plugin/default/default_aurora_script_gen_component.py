"""
Default Aurora (RDS) Terragrunt Script Generation Component

Generates a terragrunt.hcl for Aurora Serverless v2 provisioning.
The generated file is committed to the tenant's infra repo for
disaster recovery and GitOps record keeping.

Templates:
  - templates/terragrunt/default/aurora_postgres_terragrunt.hcl
  - templates/terragrunt/default/aurora_mysql_terragrunt.hcl

Variables replaced:
  - vpc_id (from config defaults)
  - subnet_ids (from config defaults)
  - database_name (user-provided, falls back to template default "app_db")
  - engine_version (optional override)
  - master_username (optional override)
  - min_capacity / max_capacity (optional override)
"""

import os
import re
import logging
from typing import Optional

import aiofiles

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "terragrunt", "default"
)

_TEMPLATE_PATHS = {
    "aurora-postgresql": os.path.join(_TEMPLATE_DIR, "aurora_postgres_terragrunt.hcl"),
    "aurora-mysql":      os.path.join(_TEMPLATE_DIR, "aurora_mysql_terragrunt.hcl"),
}


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
    queue_id: Optional[int],
    script_gen_key: Optional[str],
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


class DefaultAuroraScriptGenComponent:
    """
    Generates Aurora Serverless v2 terragrunt.hcl from the default template.

    Selects postgres or mysql template based on the engine field in config_snapshot.
    Injects vpc_id and subnet_ids from config defaults, then optionally replaces
    database_name, engine_version, master_username, min_capacity, max_capacity.
    The generated content is staged for git commit to the infra repo.
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

        from app.core.config import settings

        identifier = config_snapshot.get("db_server_name") or config_snapshot.get("identifier", "")
        infrastructuretype_ref_code = config_snapshot.get("infrastructuretype_ref_code", "")
        engine = (
            "aurora-mysql"
            if infrastructuretype_ref_code == "aurora_mysql_infrastructuretype_ref"
            else "aurora-postgresql"
        )
        database_name = config_snapshot.get("database_name")
        engine_version = config_snapshot.get("engine_version")
        master_username = config_snapshot.get("master_username")
        min_capacity = config_snapshot.get("min_capacity")
        max_capacity = config_snapshot.get("max_capacity")

        # Network defaults from config
        vpc_id = settings.onboarding_default_vpc_id
        subnet_ids = [s.strip() for s in settings.onboarding_default_subnet_ids.split(",") if s.strip()]
        vpc_cidr = settings.onboarding_default_vpc_cidr
        self.logger.info(f"[AuroraScriptGen] vpc_id={vpc_id!r} subnet_ids={subnet_ids} vpc_cidr={vpc_cidr!r}")

        template_path = _TEMPLATE_PATHS.get(engine, _TEMPLATE_PATHS["aurora-postgresql"])

        # Resolve base content: staged cache → existing git file → fresh template
        base_branch = (file_location.base_branch or "") if file_location else ""
        staged_entry = (
            _find_staged_entry(workflow_context, file_location.repo, base_branch, file_location.file_path)
            if workflow_context and file_location and file_location.repo and file_location.file_path
            else None
        )

        if staged_entry:
            content = staged_entry["content"]
            self.logger.info("[AuroraScriptGen] using staged content as base")
        else:
            existing_content = None
            if db and file_location and file_location.repo and file_location.file_path:
                try:
                    from app.handlers.gitops_handler import GitOpsHandler
                    repo_parts = file_location.repo.split("/")
                    owner = repo_parts[0] if len(repo_parts) > 1 else None
                    repo_name = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
                    existing_file = await GitOpsHandler.get_content(
                        db=db,
                        tenant=tenant,
                        owner=owner,
                        repo=repo_name,
                        file_path=file_location.file_path,
                        branch=file_location.feature_branch or base_branch,
                    )
                    if existing_file.get("exists") and existing_file.get("content"):
                        existing_content = existing_file["content"]
                        self.logger.info("[AuroraScriptGen] using existing git file as base (re-run)")
                except Exception as exc:
                    self.logger.warning("[AuroraScriptGen] git fetch failed, falling back to template: %s", exc)

            if existing_content:
                content = existing_content
            else:
                async with aiofiles.open(template_path, "r") as f:
                    content = await f.read()
                self.logger.info("[AuroraScriptGen] using template as base (first run)")

        # Replace vpc_id (anchor to non-comment lines to skip mock_outputs in comments)
        content = re.sub(
            r'^(\s+vpc_id\s*=\s*)"[^"]*"',
            f'\\1"{vpc_id}"',
            content,
            count=1,
            flags=re.MULTILINE,
        )

        # Replace subnet_ids
        subnets_formatted = ", ".join(f'"{s}"' for s in subnet_ids)
        content = re.sub(
            r'(subnet_ids\s*=\s*)\[[^\]]*\]',
            f'\\1[{subnets_formatted}]',
            content,
            count=1,
        )

        # Replace allowed_cidr_blocks with VPC CIDR
        content = re.sub(
            r'(allowed_cidr_blocks\s*=\s*)\[[^\]]*\]',
            f'\\1["{vpc_cidr}"]',
            content,
            count=1,
        )

        # Append database_name to psql_databases/mysql_databases if not already present
        if database_name:
            array_name = "mysql_databases" if engine == "aurora-mysql" else "psql_databases"
            array_match = re.search(rf'{array_name}\s*=\s*\[([^\]]*)\]', content, re.DOTALL)
            if array_match:
                existing_dbs = re.findall(r'"([^"]+)"', array_match.group(1))
                if database_name not in existing_dbs:
                    content = re.sub(
                        rf'({array_name}\s*=\s*)\[[^\]]*\]',
                        lambda m: m.group(1) + '[' + (
                            ', '.join(f'"{d}"' for d in existing_dbs) + ', ' if existing_dbs else ''
                        ) + f'"{database_name}"]',
                        content,
                        count=1,
                        flags=re.DOTALL,
                    )

        # Replace engine_version if provided
        if engine_version is not None:
            content = re.sub(
                r'(engine_version\s*=\s*)"[^"]*"',
                f'\\1"{engine_version}"',
                content,
                count=1,
            )

        # Replace master_username if provided
        if master_username is not None:
            content = re.sub(
                r'(master_username\s*=\s*)"[^"]*"',
                f'\\1"{master_username}"',
                content,
                count=1,
            )

        # Replace min_capacity if provided
        if min_capacity is not None:
            content = re.sub(
                r'(min_capacity\s*=\s*)[0-9.]+',
                f'\\g<1>{min_capacity}',
                content,
                count=1,
            )

        # Replace max_capacity if provided
        if max_capacity is not None:
            content = re.sub(
                r'(max_capacity\s*=\s*)[0-9.]+',
                f'\\g<1>{max_capacity}',
                content,
                count=1,
            )

        # Stage for git commit
        if workflow_context and file_location.repo and file_location.file_path:
            _upsert_staged_entry(
                workflow_context=workflow_context,
                repo=file_location.repo,
                base_branch=base_branch,
                feature_branch=file_location.feature_branch or "",
                file_path=file_location.file_path,
                content=content,
                queue_id=queue_dict.get("id"),
                script_gen_key=file_location.script_gen_key,
            )

            queue_label = queue_dict.get("code") or queue_dict.get("id")
            msg = f"{queue_label}: add Aurora cluster {identifier}"
            _append_commit_message(workflow_context, file_location.repo, base_branch, msg)

        # Store preview
        if queue_dict.get("id") and workflow_context:
            workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                "original_content": content,
                "preview_content": content,
            }

        return content
