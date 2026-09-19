
import os
import json
import logging
import aiofiles
from typing import Optional
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.repository.transaction_queue_repository import TransactionQueueRepository
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing
from app.utils.hcl_patch import remove_scalar_field, replace_scalar_field, upsert_scalar_field


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


class AsporaS3ScriptGenComponent:
    """
    Component responsible for generating S3 bucket terragrunt script content.
    This component focuses ONLY on script generation - no database operations or file path construction.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)

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
        repository:TransactionQueueRepository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = True,
        db=None,
    ) -> str:
        """
        Generate S3 bucket terragrunt.hcl content.

        Args:
            parameters: Dictionary containing S3 configuration parameters:
                - identifier: Bucket identifier/name (optional; inferred from file_path if missing)
                - versioning: Enable versioning (optional, default: False)
                - enable_s3_replication: Enable S3 replication (optional, default: False)
                - cross_account_account_id: Cross-account AWS account ID (optional, required if replication enabled)
            upload_to_s3 (bool): Upload generated HCL to S3 (default: False)
            s3_bucket (str): S3 bucket name (default: from S3_UPLOAD_BUCKET env variable)
            queue_item_code (str): Queue item code for updating s3_key in database

        Returns:
            str: Generated terragrunt.hcl content

        Note:
            The bucket identifier is determined by the directory structure (basename of terragrunt dir),
            not from the script content, so it's not needed as a parameter here.
        """
        # Extract parameters
        config_snapshot = queue_dict.get("config_snapshot") or {}
        identifier = config_snapshot.get('identifier') or queue_dict.get("identifier")

        # Track whether each managed field was actually provided: an absent
        # field must leave the existing line untouched, not reset it to the
        # template default.
        versioning_set = 'versioning' in config_snapshot or 'versioning' in queue_dict
        if 'versioning' in config_snapshot:
            versioning = config_snapshot.get('versioning')
        else:
            versioning = queue_dict.get("versioning", False)

        replication_set = (
            'enable_s3_replication' in config_snapshot
            or 'enable_s3_replication' in queue_dict
        )
        if 'enable_s3_replication' in config_snapshot:
            enable_s3_replication = config_snapshot.get('enable_s3_replication')
        else:
            enable_s3_replication = queue_dict.get("enable_s3_replication", False)

        # Explicit null = the user cleared the field. versioning always exists
        # in the template, so null just leaves its line alone; a null
        # replication removes the optional line entirely.
        if versioning_set and versioning is None:
            versioning_set = False
        replication_clear = replication_set and enable_s3_replication is None

        versioning = self._coerce_bool(versioning)
        enable_s3_replication = self._coerce_bool(enable_s3_replication)

        cross_account_keys_present = (
            'cross_account_id' in config_snapshot
            or 'cross_account_account_id' in config_snapshot
        )
        cross_account_account_id = config_snapshot.get('cross_account_id')
        if cross_account_account_id is None:
            cross_account_account_id = config_snapshot.get('cross_account_account_id')
        if cross_account_account_id is None and not cross_account_keys_present:
            cross_account_account_id = queue_dict.get("cross_account_id")
        # Explicit null on either key = remove the cross-account line.
        cross_account_clear = cross_account_keys_present and cross_account_account_id is None

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

        # File may already exist (re-deploy of the same bucket). When it does,
        # the existing content is the base and only the managed fields below are
        # patched, so hand-added inputs (lifecycle_rules, CORS, kms_key_arn, ...)
        # survive. No-change detection still skips the commit if the patched
        # output is byte-for-byte equal.
        is_update = bool(existing_file.get("exists"))

        gen_mode = "update" if is_update else "create"
        gen_context = f"path={file_location.file_path} mode={gen_mode}"
        with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
            terragrunt_content = existing_file.get("content") if is_update else None
            if not terragrunt_content:
                # New file (or existing somehow empty) -> render from template.
                template_path = os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/terragrunt/s3/terragrunt.hcl"
                )
                self.logger.info(f"Loading S3 template from: {template_path}")
                async with aiofiles.open(template_path, "r") as f:
                    terragrunt_content = await f.read()
            else:
                self.logger.info(
                    "S3 terragrunt.hcl already exists at %s — patching existing content",
                    file_location.file_path,
                )

            # Patch only the managed fields. A field absent from the config
            # leaves the existing line untouched; an explicit off patches the
            # line to false but never inserts or deletes one.
            if versioning_set:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content,
                    "versioning",
                    str(versioning).lower(),
                    anchors=["identifier"],
                )
                self.logger.info(f"Set versioning = {versioning}")

            if replication_set:
                if replication_clear:
                    terragrunt_content, _ = remove_scalar_field(
                        terragrunt_content, "enable_s3_replication"
                    )
                elif enable_s3_replication:
                    terragrunt_content = upsert_scalar_field(
                        terragrunt_content,
                        "enable_s3_replication",
                        "true",
                        anchors=["versioning"],
                    )
                else:
                    terragrunt_content, _ = replace_scalar_field(
                        terragrunt_content, "enable_s3_replication", "false"
                    )
                self.logger.info(f"Set enable_s3_replication = {enable_s3_replication}")

            if cross_account_clear:
                terragrunt_content, _ = remove_scalar_field(
                    terragrunt_content, "cross_account_account_id"
                )
            elif cross_account_account_id:
                terragrunt_content = upsert_scalar_field(
                    terragrunt_content,
                    "cross_account_account_id",
                    f'"{cross_account_account_id}"',
                    anchors=["enable_s3_replication", "versioning"],
                )
                self.logger.info(f"Set cross_account_account_id = {cross_account_account_id}")

        if not identifier and file_location and file_location.file_path:
            identifier = os.path.basename(os.path.dirname(file_location.file_path))

        preview_hcl = self.preview_s3_hcl(
            identifier=identifier,
            versioning=versioning,
            enable_s3_replication=enable_s3_replication,
            cross_account_account_id=cross_account_account_id
        )
        self.logger.info(f"Generated preview HCL for {identifier}")

        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            if not identifier:
                raise ValueError("Parameter 'identifier' is required when upload_to_s3 is True")

            try:
                original_s3_key = f"s3/{identifier}.hcl"
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
                preview_s3_key = f"preview/s3/{identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_hcl,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": config_snapshot.get('environment') or queue_dict.get("environment"),
                        "identifier": identifier,
                        "generated_by": "aspora_s3_script_gen_component"
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
                await repository.update_artifact_s3_key(queue_dict.get("code") or config_snapshot.get('code'), update_payload)

                self.logger.info(
                    "Saved artifact_s3_key to database for queue item %s: %s",
                    queue_dict.get("code") or config_snapshot.get('code'),
                    update_payload
                )

        if workflow_context and not workflow_context.skip_commit:
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
        # Store in workflow context
        if queue_dict["id"]:
            script_gen_key = file_location.script_gen_key
            workflow_context.script_gen_responses[queue_dict["id"]][script_gen_key] = {
                "original_content": terragrunt_content,
                "preview_content": preview_hcl
            }

        return terragrunt_content

    def preview_s3_hcl(
        self,
        identifier: str,
        versioning: bool = False,
        enable_s3_replication: bool = False,
        cross_account_account_id: Optional[str] = None
    ) -> str:
        """
        Render S3 Terragrunt preview for chat display.

        Returns a formatted HCL preview showing the S3 bucket configuration
        with user-configured values highlighted and auto-configured values noted.
        """
        if not identifier or not identifier.strip():
            raise ValueError("Identifier cannot be empty")

        identifier = identifier.strip().lower().replace(" ", "-")

        optional_lines = []
        if versioning:
            optional_lines.append('  versioning               = true')
        if enable_s3_replication:
            optional_lines.append('  enable_s3_replication    = true')
        if cross_account_account_id:
            optional_lines.append(f'  cross_account_account_id = "{cross_account_account_id}"')

        optional_section = ""
        if optional_lines:
            optional_section = "\n" + "\n".join(optional_lines)

        return f"""S3 Bucket Configuration:

inputs = {{
  identifier               = "{identifier}"{optional_section}

  # Auto-configured from environment
  organization = include.env.locals.organization
  env          = include.env.locals.env
  region       = include.env.locals.region
  index        = include.env.locals.index

  # Dependencies
  s3_access_logs_bucket_id = dependency.regional_bootstrap.outputs.s3_access_logs_bucket_id
  tags         = include.env.locals.tags
}}"""
