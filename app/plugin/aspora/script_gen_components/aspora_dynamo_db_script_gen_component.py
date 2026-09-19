"""
Aspora DynamoDB Script Generation Component

Handles DynamoDB terragrunt script generation for Aspora tenant.
"""

import logging
import re
import os
import json
import aiofiles
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing
from app.utils.hclfmt import hcl_escape
from app.utils.hcl_patch import replace_scalar_field


_ATTR_BLOCK_RE = re.compile(r'(attributes\s*=\s*\[)(.*?)(\])', re.DOTALL)
_ATTR_ENTRY_RE = re.compile(r'\{[^{}]*\}', re.DOTALL)


def _entry_name(entry_text: str):
    m = re.search(r'name\s*=\s*"([^"]*)"', entry_text)
    return m.group(1) if m else None


def _upsert_attribute(content: str, old_name, name: str, attr_type: str) -> str:
    """Update the partition-key entry inside `attributes = [...]` in place.

    Matches the entry by new name, else by the previous partition key's name
    (covers the template placeholder and key renames); appends a new entry when
    neither exists. Other entries — hand-added range-key or GSI attributes —
    are never touched or removed.
    """
    block = _ATTR_BLOCK_RE.search(content)
    if not block:
        return content
    body = block.group(2)

    entries = list(_ATTR_ENTRY_RE.finditer(body))
    target = next((e for e in entries if _entry_name(e.group(0)) == name), None)
    if target is None and old_name:
        target = next((e for e in entries if _entry_name(e.group(0)) == old_name), None)

    if target:
        new_entry = re.sub(
            r'(name\s*=\s*)"[^"]*"', lambda m: m.group(1) + f'"{name}"', target.group(0), count=1
        )
        new_entry = re.sub(
            r'(type\s*=\s*)"[^"]*"', lambda m: m.group(1) + f'"{attr_type}"', new_entry, count=1
        )
        new_body = body[:target.start()] + new_entry + body[target.end():]
    else:
        indent_m = re.search(r'([ \t]*)\{', body)
        indent = indent_m.group(1) if indent_m else "  "
        close_m = re.search(r'\n([ \t]*)$', body)
        closing_indent = close_m.group(1) if close_m else ""
        entry = (
            f"{indent}{{\n"
            f"{indent}  name = \"{name}\"\n"
            f"{indent}  type = \"{attr_type}\"\n"
            f"{indent}}}"
        )
        if entries:
            new_body = body.rstrip() + ",\n" + entry + "\n" + closing_indent
        else:
            new_body = "\n" + entry + "\n" + closing_indent
    return content[:block.start(2)] + new_body + content[block.end(2):]

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


class AsporaDynamoDbScriptgenComponent:
    """
    Component for generating DynamoDB table terragrunt scripts for Aspora tenant.

    Responsibilities:
    - Load DynamoDB template from disk (if file doesn't exist)
    - Update existing DynamoDB configuration (if file exists)
    - Replace partition key placeholders
    - Update attributes block configuration
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
        Generate DynamoDB terragrunt configuration.

        This function:
        1. Checks if file exists in GitHub using GitHubIntegration.get_file_content
        2. If file does NOT exist:
           - Load DynamoDB template from disk
           - Replace partition_key placeholder
           - Update attributes block with partition key configuration
        3. If file exists:
           - Update partition_key in existing content
           - Update attributes block with new partition key configuration
        4. Return the final terragrunt content

        Returns:
            Generated/updated terragrunt configuration content

        Raises:
            ValueError: If required parameters are missing
        """
        config_snapshot = queue_dict.get('config_snapshot') or {}

        # Extract required parameters
        identifier = config_snapshot.get('identifier')
        partition_key = config_snapshot.get('partition_key')

        partition_key_type = config_snapshot.get('partition_key_type') or 'S'  # Default to String if None or missing

        if not identifier and file_location and file_location.file_path:
            identifier = os.path.basename(os.path.dirname(file_location.file_path))

        logger.info(f"Generating DynamoDB configuration for: {file_location.file_path}")
        logger.info(f"  Partition Key: {partition_key} ({partition_key_type})")

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # github_token = config_snapshot.get('github_token')
        # github_base_url = config_snapshot.get('github_base_url')

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
            existing_file = await fetch_existing_content(
                db=db,
                tenant=tenant,
                owner=owner,
                repo=repo,
                file_path=file_location.file_path,
                base_branch=base_branch,
                feature_branch=feature_branch,
                workflow_context=workflow_context,
                logger=logger,
                component_name=component_name,
            )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        terragrunt_content: str
        gen_mode = "create" if not existing_file["exists"] else "update"
        gen_context = f"path={file_location.file_path} mode={gen_mode}"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            if not existing_file["exists"]:
                # File does NOT exist → Load template from disk
                logger.info(f"File does not exist - loading template from disk")

                # Load DynamoDB template from disk
                template_path = os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/terragrunt/dynamoDB/terragrunt.hcl"
                )

                logger.info(f"📄 Loading DynamoDB template from: {template_path}")
                async with aiofiles.open(template_path, "r") as f:
                    terragrunt_content = await f.read()

                logger.info(f"✅ Template loaded successfully")
            else:
                # File exists → Use existing content as base
                logger.info(f"File exists - updating existing configuration")
                terragrunt_content = existing_file["content"]

            # The current key in the file (template placeholder or a previous
            # deploy's value) — used to find the matching attributes entry and
            # as the preview fallback when the snapshot omits partition_key.
            current_pk_m = re.search(r'partition_key\s*=\s*"([^"]*)"', terragrunt_content)
            current_partition_key = current_pk_m.group(1) if current_pk_m else None

            if partition_key:
                # partition_key is user-supplied and lands inside HCL quoted
                # strings, so it goes through hcl_escape() first. Only the
                # matching attributes entry is touched — hand-added range-key
                # or GSI attributes survive.
                safe_partition_key = hcl_escape(partition_key)
                safe_partition_key_type = hcl_escape(partition_key_type)

                terragrunt_content, _ = replace_scalar_field(
                    terragrunt_content, "partition_key", f'"{safe_partition_key}"'
                )
                terragrunt_content = _upsert_attribute(
                    terragrunt_content,
                    old_name=current_partition_key,
                    name=safe_partition_key,
                    attr_type=safe_partition_key_type,
                )
                logger.info(f"✅ Updated partition_key to: {partition_key} ({partition_key_type})")
            else:
                # Not provided → leave the existing lines exactly as they are.
                logger.info("partition_key not in config — existing content left untouched")

            self.logger.info("✅ DynamoDB configuration generated successfully")

            preview_hcl = self.preview_dynamodb_hcl(
                identifier=identifier,
                partition_key=partition_key or current_partition_key,
                partition_key_type=partition_key_type
            )
        self.logger.info("Generated DynamoDB preview HCL")

        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            if not identifier:
                raise ValueError("Parameter 'identifier' is required when upload_to_s3 is True")

            try:
                original_s3_key = f"dynamodb/{identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=terragrunt_content,
                    content_type="text/plain"
                )
                self.logger.info(f"Uploaded original HCL to S3: {result['location']}")
            except Exception as e:
                self.logger.error(f"Failed to upload original HCL to S3: {str(e)}")
                raise

            try:
                preview_s3_key = f"preview/dynamodb/{identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_hcl,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": config_snapshot.get('environment') or queue_dict.get("environment"),
                        "identifier": identifier,
                        "generated_by": "aspora_dynamo_db_script_gen_component"
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
                    content=terragrunt_content,
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
            #TODO: we need to update the git commit sha response in the workflow context here after commit

            if queue_dict.get("id"):
                script_gen_key = file_location.script_gen_key
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                    "original_content": terragrunt_content,
                    "preview_content": preview_hcl
                }

        return terragrunt_content

    def preview_dynamodb_hcl(
        self,
        identifier: str,
        partition_key: str,
        partition_key_type: str = "S"
    ) -> str:
        """
        Render DynamoDB Terragrunt preview for chat display.

        Returns a formatted HCL preview showing the DynamoDB table configuration
        with user-configured values highlighted and auto-configured values noted.
        """
        if not identifier or not identifier.strip():
            raise ValueError("Identifier cannot be empty")

        if not partition_key or not partition_key.strip():
            raise ValueError("Partition key cannot be empty")

        partition_key_type = partition_key_type.strip().upper()
        if partition_key_type not in ["S", "N", "B"]:
            raise ValueError(f"Partition key type must be S, N, or B. Got: {partition_key_type}")

        identifier = identifier.strip().lower().replace(" ", "-")
        partition_key = partition_key.strip()

        # Interpolated into HCL quoted strings — escape so a `"` or `${...}`
        # can't break out. No re.sub here, so escaping alone is sufficient.
        identifier = hcl_escape(identifier)
        partition_key = hcl_escape(partition_key)

        return f"""DynamoDB Table Configuration:

inputs = {{
  identifier            = "{identifier}"
  partition_key         = "{partition_key}"
  attributes            = [
                            {{
                              name = "{partition_key}"
                              type = "{partition_key_type}"
                            }}
                          ]

  # Auto-configured from environment
  organization          = include.env.locals.organization
  env                   = include.env.locals.env
  region                = include.env.locals.region
  index                 = include.env.locals.index
  deletion_protection   = include.env.locals.deletion_protection
  tags                  = include.env.locals.tags
}}"""
