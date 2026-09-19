import os
import re
import json
import logging
import aiofiles
from typing import Optional
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing
from app.utils.hcl_patch import (
    remove_list_field,
    remove_scalar_field,
    replace_list_field,
    replace_scalar_field,
    upsert_list_field,
    upsert_scalar_field,
)


def _resolve(config_snapshot: dict, queue_dict: dict, *keys):
    """Resolve one optional field from its possible keys.

    Returns (value, clear): the first key present in config_snapshot wins, and
    an explicit None there is the clear signal — the user emptied a field that
    had a value, so its line must be removed. Falls back to queue_dict; a field
    absent everywhere returns (None, False) and is left untouched.
    """
    for key in keys:
        if key in config_snapshot:
            value = config_snapshot[key]
            return value, value is None
    for key in keys:
        if key in queue_dict:
            value = queue_dict[key]
            return value, value is None
    return None, False


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


class AsporaSqsScriptGenComponent:
    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository  # GitopsQueueRepository for DB operations

    def _coerce_bool(self, value: Optional[object]) -> bool:
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
        upload_to_s3: bool = True,
        db=None,
    ) -> str:
        """
        Generate SQS terragrunt configuration content.

        Follows Single Responsibility Principle - only handles script generation,
        no validation or business logic.

        Args:
            parameters: Dictionary containing:
                - environment (str): Target environment (dev/staging/prod)
                - identifier (str): Unique identifier (required for S3 upload)
                - create_dlq (bool): Whether to create dead letter queue (default: True)
                - fifo_queue (bool): Whether queue is FIFO type (default: True)
                - visibility_timeout_seconds (int, optional): Visibility timeout
                - max_receive_count (int, optional): Max receive count
                - message_retention_seconds (int, optional): Message retention
                - dlq_message_retention_seconds (int, optional): DLQ retention
                - cross_account_ids (list, optional): List of cross-account AWS account IDs
            upload_to_s3 (bool): Upload generated HCL to S3 (default: False)
            queue_item_code (str): Queue item code for updating s3_key in database

        Returns:
            str: Generated terragrunt.hcl content

        Raises:
            ValueError: If upload_to_s3 is True but identifier is not provided
        """
        config_snapshot = queue_dict.get('config_snapshot') or {}

        # Extract required parameters
        environment = config_snapshot.get('environment') or queue_dict.get('environment')

        # Extract optional parameters with defaults. The *_set flags track
        # whether the field was actually provided: an absent field leaves the
        # existing line untouched instead of resetting it.
        identifier = config_snapshot.get('identifier')
        create_dlq_set = 'create_dlq' in config_snapshot or 'dlq' in config_snapshot
        if 'create_dlq' in config_snapshot:
            create_dlq = config_snapshot.get('create_dlq')
        else:
            create_dlq = config_snapshot.get('dlq', True)

        fifo_queue_set = 'fifo_queue' in config_snapshot or 'fifo' in config_snapshot
        if 'fifo_queue' in config_snapshot:
            fifo_queue = config_snapshot.get('fifo_queue')
        else:
            fifo_queue = config_snapshot.get('fifo', True)

        # create_dlq / fifo_queue always exist in the template — an explicit
        # null just leaves the line alone (never coerced to False, never removed).
        if create_dlq_set and create_dlq is None:
            create_dlq_set = False
        if fifo_queue_set and fifo_queue is None:
            fifo_queue_set = False

        create_dlq = self._coerce_bool(create_dlq)
        fifo_queue = self._coerce_bool(fifo_queue)

        # Optional fields: (value, clear) — explicit null means "remove the
        # line so the terraform module default applies".
        visibility_timeout_seconds, visibility_clear = _resolve(
            config_snapshot, queue_dict, 'visibility_timeout_seconds'
        )
        max_receive_count, max_receive_clear = _resolve(
            config_snapshot, queue_dict, 'max_receive_count'
        )
        message_retention_seconds, message_retention_clear = _resolve(
            config_snapshot, queue_dict,
            'message_retention_seconds', 'main_queue_retention_seconds',
        )
        dlq_message_retention_seconds, dlq_retention_clear = _resolve(
            config_snapshot, queue_dict,
            'dlq_message_retention_seconds', 'dlq_retention_seconds',
        )

        cross_account_ids_set = (
            'cross_account_ids' in config_snapshot or 'cross_account_ids' in queue_dict
        )
        cross_account_ids, cross_account_clear = _resolve(
            config_snapshot, queue_dict, 'cross_account_ids'
        )

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        self.logger.info(f"Checking if file exists on {feature_branch}")
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
                logger=self.logger,
                component_name=component_name,
            )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        # File may already exist (re-deploy of the same queue). When it does,
        # the existing content is the base and only the managed fields below
        # are patched, so hand-added inputs survive. No-change detection still
        # skips the commit if the patched output is byte-for-byte equal.
        is_update = bool(existing_file.get("exists"))

        gen_mode = "update" if is_update else "create"
        gen_context = f"path={file_location.file_path} mode={gen_mode}"
        with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
            terragrunt_content = existing_file.get("content") if is_update else None
            if not terragrunt_content:
                # New file (or existing somehow empty) -> render from template.
                if (environment or "").lower() == "prod":
                    template_file = "terragrunt-prod.hcl"
                else:
                    template_file = "terragrunt.hcl"

                template_path = os.path.join(
                    os.path.dirname(__file__),
                    f"../../../../templates/terragrunt/sqs/{template_file}"
                )

                self.logger.info(f"Loading SQS template ({environment}): {template_file}")
                async with aiofiles.open(template_path, "r") as f:
                    terragrunt_content = await f.read()
            else:
                self.logger.info(
                    "SQS terragrunt.hcl already exists at %s — patching existing content",
                    file_location.file_path,
                )

            # Patch only the managed fields. A field absent from the config
            # leaves the existing line untouched; an explicit off patches the
            # line but never inserts or deletes one.
            if create_dlq_set:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content, "create_dlq", str(create_dlq).lower(),
                    anchors=["fifo_queue", "identifier"],
                )

            if fifo_queue_set:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content, "fifo_queue", str(fifo_queue).lower(),
                    anchors=["create_dlq", "identifier"],
                )

            if visibility_clear:
                terragrunt_content, _ = remove_scalar_field(
                    terragrunt_content, "visibility_timeout_seconds"
                )
            elif visibility_timeout_seconds is not None:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content,
                    "visibility_timeout_seconds", str(visibility_timeout_seconds),
                    anchors=["fifo_queue"],
                )

            if max_receive_clear:
                terragrunt_content, _ = remove_scalar_field(
                    terragrunt_content, "max_receive_count"
                )
            elif max_receive_count is not None:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content,
                    "max_receive_count", str(max_receive_count),
                    anchors=["visibility_timeout_seconds", "fifo_queue"],
                )

            if message_retention_clear:
                terragrunt_content, _ = remove_scalar_field(
                    terragrunt_content, "message_retention_seconds"
                )
            elif message_retention_seconds is not None:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content,
                    "message_retention_seconds", str(message_retention_seconds),
                    anchors=["max_receive_count", "visibility_timeout_seconds", "fifo_queue"],
                )

            if dlq_retention_clear:
                terragrunt_content, _ = remove_scalar_field(
                    terragrunt_content, "dlq_message_retention_seconds"
                )
            elif dlq_message_retention_seconds is not None:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content,
                    "dlq_message_retention_seconds", str(dlq_message_retention_seconds),
                    anchors=[
                        "message_retention_seconds", "max_receive_count",
                        "visibility_timeout_seconds", "fifo_queue",
                    ],
                )

            # The queue layer's variable is enable_cross_account_access;
            # cross_account_access_enabled (written until Aug 2026) is not a
            # layer variable and terragrunt silently ignored it. Rename the
            # stale line in place so old files heal on redeploy.
            terragrunt_content = re.sub(
                r"^(\s*)cross_account_access_enabled(\s*=)",
                r"\1enable_cross_account_access\2",
                terragrunt_content,
                flags=re.M,
            )

            if cross_account_clear:
                terragrunt_content, _ = remove_list_field(
                    terragrunt_content, "cross_account_ids"
                )
                terragrunt_content, _ = remove_scalar_field(
                    terragrunt_content, "enable_cross_account_access"
                )
            elif cross_account_ids:
                ids_formatted = ', '.join(f'"{aid}"' for aid in cross_account_ids)
                terragrunt_content = upsert_list_field(
                    terragrunt_content,
                    "cross_account_ids", f"[{ids_formatted}]",
                    anchors=[
                        "dlq_message_retention_seconds", "message_retention_seconds",
                        "max_receive_count", "visibility_timeout_seconds", "fifo_queue",
                    ],
                )
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content, "enable_cross_account_access", "true",
                    anchors=["cross_account_ids"],
                )
            elif cross_account_ids_set:
                # Explicitly cleared: patch existing lines, never insert them.
                terragrunt_content, _ = replace_list_field(
                    terragrunt_content, "cross_account_ids", "[]"
                )
                terragrunt_content, _ = replace_scalar_field(
                    terragrunt_content, "enable_cross_account_access", "false"
                )

            if fifo_queue_set and not fifo_queue:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content, "content_based_deduplication", "false",
                    anchors=[
                        "enable_cross_account_access", "dlq_message_retention_seconds",
                        "message_retention_seconds", "max_receive_count",
                        "visibility_timeout_seconds", "fifo_queue",
                    ],
                )

        self.logger.info(f"Template loaded and parameters replaced (create_dlq={create_dlq}, fifo_queue={fifo_queue}, visibility_timeout_seconds={visibility_timeout_seconds}, max_receive_count={max_receive_count}, message_retention_seconds={message_retention_seconds}, dlq_message_retention_seconds={dlq_message_retention_seconds}, cross_account_ids={cross_account_ids})")

        if not identifier and file_location and file_location.file_path:
            identifier = os.path.basename(os.path.dirname(file_location.file_path))

        # Generate preview HCL using preview_sqs_hcl method
        preview_hcl = self.preview_sqs_hcl(
            identifier=identifier,
            create_dlq=create_dlq,
            fifo_queue=fifo_queue,
            visibility_timeout_seconds=visibility_timeout_seconds,
            max_receive_count=max_receive_count,
            message_retention_seconds=message_retention_seconds,
            dlq_message_retention_seconds=dlq_message_retention_seconds,
            cross_account_ids=cross_account_ids
        )
        self.logger.info(f"Generated preview HCL for {identifier}")

        # Upload to S3 if requested
        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            # Validate identifier is provided for S3 upload
            if not identifier:
                raise ValueError("Parameter 'identifier' is required when upload_to_s3 is True")

            # Upload original HCL to S3 (bucket must already exist)
            try:
                original_s3_key = f"sqs/{identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=terragrunt_content,
                    content_type="text/plain"
                )
                self.logger.info(f"Uploaded original HCL to S3: {result['location']}")
            except Exception as e:
                self.logger.error(f"Failed to upload original HCL to S3: {str(e)}")
                raise

            # Upload preview HCL to S3 (non-blocking - failure shouldn't break original)
            try:
                preview_s3_key = f"preview/sqs/{identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_hcl,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": environment,
                        "identifier": identifier,
                        "generated_by": "aspora_sqs_script_gen_component"
                    }
                )
                self.logger.info(f"Uploaded preview HCL to S3: {result['location']}")
            except Exception as e:
                # Non-blocking error - preview upload failure shouldn't break original
                self.logger.warning(f"Failed to upload preview HCL to S3: {str(e)}")

            # Build artifact_s3_key JSON for database (include both keys)
            artifact_s3_key_json = {
                "original_s3_key": original_s3_key,
                "preview": preview_s3_key
            }

            # Save artifact_s3_key to database via repository
            if repository and queue_dict.get("code"):
                await repository.update_artifact_s3_key(queue_dict.get("code"), json.dumps(artifact_s3_key_json))
                self.logger.info(
                    "Saved artifact_s3_key to database for queue item %s: %s",
                    queue_dict.get("code"),
                    json.dumps(artifact_s3_key_json)
                )

        if tenant and file_location and workflow_context:
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
            # TODO: update the git commit sha response in the workflow context after commit

            if queue_dict.get("id"):
                script_gen_key = file_location.script_gen_key
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                    "original_content": terragrunt_content,
                    "preview_content": preview_hcl
                }

        return terragrunt_content
    
    def preview_sqs_hcl(
        self,
        identifier: str,
        create_dlq: bool = True,
        fifo_queue: bool = True,
        visibility_timeout_seconds: Optional[int] = None,
        max_receive_count: Optional[int] = None,
        message_retention_seconds: Optional[int] = None,
        dlq_message_retention_seconds: Optional[int] = None,
        cross_account_ids: Optional[list] = None
    ) -> str:
        """
        Render SQS Terragrunt preview for chat display.

        Returns a formatted HCL preview showing the SQS queue configuration
        with user-configured values highlighted and auto-configured values noted.

        Args:
            identifier: Queue identifier/name (e.g., "order-queue", "event-processor")
            create_dlq: Whether to create a dead letter queue (defaults to False)
            fifo_queue: Whether to create a FIFO queue (defaults to False for standard queue)
            visibility_timeout_seconds: Optional visibility timeout in seconds (0-43200)
            max_receive_count: Optional max receive count before moving to DLQ (1-1000)
            message_retention_seconds: Optional message retention in seconds (60-1209600, default: 345600 = 4 days)
            dlq_message_retention_seconds: Optional DLQ message retention in seconds (60-1209600, default: 1209600 = 14 days)
            cross_account_ids: Optional list of AWS account IDs for cross-account access

        Returns:
            str: Formatted HCL preview for chat display

        Raises:
            ValueError: If identifier is empty or invalid

        Example:
            >>> service = TemplateService()
            >>> # Standard queue without DLQ
            >>> preview = service.render_sqs_terragrunt("order-queue")
            >>> # FIFO queue with DLQ and optional parameters
            >>> preview = service.render_sqs_terragrunt(
            ...     "critical-events",
            ...     create_dlq=True,
            ...     fifo_queue=True,
            ...     visibility_timeout_seconds=300,
            ...     max_receive_count=3
            ... )
        """
        # Validate identifier
        if not identifier or not identifier.strip():
            raise ValueError("Identifier cannot be empty")

        # Clean identifier (remove spaces, lowercase, replace special chars)
        identifier = identifier.strip().lower().replace(" ", "-")

        # Convert Python booleans to HCL booleans (lowercase)
        create_dlq_str = str(create_dlq).lower()
        fifo_queue_str = str(fifo_queue).lower()

        # Build parameter lines conditionally
        param_lines = [
            f'  identifier   = "{identifier}"',
            f'  fifo_queue   = {fifo_queue_str}',
            f'  create_dlq   = {create_dlq_str}'
        ]

        # Add optional parameters only if provided
        if visibility_timeout_seconds is not None:
            param_lines.append(f'  visibility_timeout_seconds = {visibility_timeout_seconds}')
        if max_receive_count is not None:
            param_lines.append(f'  max_receive_count         = {max_receive_count}')
        if message_retention_seconds is not None:
            param_lines.append(f'  message_retention_seconds = {message_retention_seconds}')
        if dlq_message_retention_seconds is not None:
            param_lines.append(f'  dlq_message_retention_seconds = {dlq_message_retention_seconds}')
        if cross_account_ids:
            ids_formatted = ', '.join(f'"{aid}"' for aid in cross_account_ids)
            param_lines.append(f'  cross_account_ids            = [{ids_formatted}]')
            param_lines.append('  enable_cross_account_access  = true')

        # Return Terraform-style HCL preview
        return f"""SQS Queue Configuration:

inputs = {{
{chr(10).join(param_lines)}

  # Auto-configured from environment
  organization = include.env.locals.organization
  env          = include.env.locals.env
  region       = include.env.locals.region

  # Alarms
  alarms_sns_topic_arn = dependency.slack_ops.outputs.sns_topic_arn
}}"""
