"""
Default DynamoDB Terragrunt Script Generation Component

Generates a terragrunt.hcl for DynamoDB table provisioning.
The generated file is committed to the tenant's infra repo for
disaster recovery and GitOps record keeping.

Template: templates/terragrunt/dynamoDB/default_terragrunt.hcl

Variables replaced:
  - hash_key (partition key name)
  - hash_key_type (S, N, or B)
  - range_key (sort key name, null if not set)
  - ttl_enabled (true/false)
  - ttl_attribute_name (attribute name for TTL)
"""

import os
import re
import logging
from typing import Any, Optional

import aiofiles

from app.utils.hclfmt import hcl_escape

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "terragrunt", "default", "dynamo_terragrunt.hcl"
)


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


class DefaultDynamoDbScriptGenComponent:
    """
    Generates DynamoDB terragrunt.hcl from the default template.

    Replaces hash_key, hash_key_type, range_key, and TTL settings in the template.
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

        identifier = config_snapshot.get("identifier", "")
        hash_key = config_snapshot.get("partition_key") or config_snapshot.get("hash_key") or "pk"
        hash_key_type = config_snapshot.get("partition_key_type") or config_snapshot.get("hash_key_type") or "S"

        # Load template
        async with aiofiles.open(_TEMPLATE_PATH, "r") as f:
            content = await f.read()

        # Values below are user-supplied and land inside HCL quoted strings, so
        # each goes through hcl_escape(). The replacements are callables rather
        # than strings: re.sub parses a replacement *string* for group
        # references, so a `\1` in a value would expand a second time.
        # Replace hash_key and hash_key_type
        safe_hash_key = hcl_escape(hash_key)
        content = re.sub(
            r'(hash_key\s*=\s*)"[^"]*"',
            lambda m: f'{m.group(1)}"{safe_hash_key}"',
            content,
            count=1,
        )
        safe_hash_key_type = hcl_escape(hash_key_type)
        content = re.sub(
            r'(hash_key_type\s*=\s*)"[^"]*"',
            lambda m: f'{m.group(1)}"{safe_hash_key_type}"',
            content,
            count=1,
        )

        # Replace range_key if provided
        range_key = config_snapshot.get("sort_key") or config_snapshot.get("range_key")
        if range_key:
            safe_range_key = hcl_escape(range_key)
            content = re.sub(
                r'(range_key\s*=\s*)null',
                lambda m: f'{m.group(1)}"{safe_range_key}"',
                content,
                count=1,
            )

        # Replace TTL settings if provided
        ttl_enabled = config_snapshot.get("ttl_enabled")
        if ttl_enabled is not None:
            # Bare HCL bool, not a quoted string — hcl_escape() does not apply,
            # and with no quotes to break out of a crafted value would inject a
            # sibling attribute directly ("false, deletion_protection = false").
            # The emitted value is the historical str(x).lower(); anything that
            # isn't a bool literal is rejected rather than written into the file.
            safe_ttl_enabled = str(ttl_enabled).strip().lower()
            if safe_ttl_enabled not in ("true", "false"):
                raise ValueError(
                    f"ttl_enabled must be a boolean, got {ttl_enabled!r}"
                )
            content = re.sub(
                r'(ttl_enabled\s*=\s*)\w+',
                lambda m: f'{m.group(1)}{safe_ttl_enabled}',
                content,
                count=1,
            )
        ttl_attribute_name = config_snapshot.get("ttl_attribute_name")
        if ttl_attribute_name:
            safe_ttl_attribute_name = hcl_escape(ttl_attribute_name)
            content = re.sub(
                r'(ttl_attribute_name\s*=\s*)"[^"]*"',
                lambda m: f'{m.group(1)}"{safe_ttl_attribute_name}"',
                content,
                count=1,
            )

        # Stage for git commit
        if workflow_context and file_location.repo and file_location.file_path:
            base_branch = file_location.base_branch or ""
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
            msg = f"{queue_label}: add DynamoDB table {identifier}"
            _append_commit_message(workflow_context, file_location.repo, base_branch, msg)

        # Store preview
        if queue_dict.get("id") and workflow_context:
            workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                "original_content": content,
                "preview_content": content,
            }

        return content
