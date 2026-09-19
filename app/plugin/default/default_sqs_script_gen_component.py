"""
Default SQS Queue Terragrunt Script Generation Component

Generates a terragrunt.hcl for SQS queue provisioning.
The generated file is committed to the tenant's infra repo for
disaster recovery and GitOps record keeping.

Template: templates/terragrunt/default/sqs_terragrunt.hcl

Variables replaced:
  - create_dlq (true/false)
  - fifo_queue (true/false)
  - visibility_timeout_seconds (inserted if provided)
  - max_receive_count (inserted if provided)
  - message_retention_seconds (inserted if provided)
  - dlq_message_retention_seconds (inserted if provided)
"""

import os
import re
import logging
from typing import Optional

import aiofiles

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "terragrunt", "default", "sqs_terragrunt.hcl"
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


class DefaultSqsScriptGenComponent:
    """
    Generates SQS queue terragrunt.hcl from the default template.

    Replaces create_dlq, fifo_queue and optionally inserts timeout / retention /
    cross-account settings.  The generated content is staged for git commit.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def _coerce_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes", "y", "t"):
                return True
            if lowered in ("false", "0", "no", "n", "f", ""):
                return False
        return bool(value)

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
        create_dlq = self._coerce_bool(config_snapshot.get("create_dlq", True))
        fifo_queue = self._coerce_bool(config_snapshot.get("fifo_queue", True))
        visibility_timeout_seconds = config_snapshot.get("visibility_timeout_seconds")
        max_receive_count = config_snapshot.get("max_receive_count")
        message_retention_seconds = (
            config_snapshot.get("message_retention_seconds")
            or config_snapshot.get("main_queue_retention_seconds")
        )
        dlq_message_retention_seconds = (
            config_snapshot.get("dlq_message_retention_seconds")
            or config_snapshot.get("dlq_retention_seconds")
        )
        # Load template
        async with aiofiles.open(_TEMPLATE_PATH, "r") as f:
            content = await f.read()

        # Replace create_dlq and fifo_queue
        content = re.sub(
            r'(create_dlq\s*=\s*)\w+',
            f'\\1{str(create_dlq).lower()}',
            content,
            count=1,
        )
        content = re.sub(
            r'(fifo_queue\s*=\s*)\w+',
            f'\\1{str(fifo_queue).lower()}',
            content,
            count=1,
        )

        # Build optional lines to insert after fifo_queue
        optional_lines = []
        if visibility_timeout_seconds is not None:
            optional_lines.append(f"  visibility_timeout_seconds = {visibility_timeout_seconds}")
        if max_receive_count is not None:
            optional_lines.append(f"  max_receive_count          = {max_receive_count}")
        if message_retention_seconds is not None:
            optional_lines.append(f"  message_retention_seconds  = {message_retention_seconds}")
        if dlq_message_retention_seconds is not None:
            optional_lines.append(f"  dlq_message_retention_seconds = {dlq_message_retention_seconds}")
        if not fifo_queue:
            optional_lines.append("  content_based_deduplication = false")

        if optional_lines:
            extra = "\n" + "\n".join(optional_lines)
            content = re.sub(
                r'(fifo_queue\s*=\s*(?:true|false))',
                rf'\1{extra}',
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
            msg = f"{queue_label}: add SQS queue {identifier}"
            _append_commit_message(workflow_context, file_location.repo, base_branch, msg)

        # Store preview
        if queue_dict.get("id") and workflow_context:
            workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                "original_content": content,
                "preview_content": content,
            }

        return content
