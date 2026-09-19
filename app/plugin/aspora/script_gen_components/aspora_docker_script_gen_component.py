"""
Aspora Docker Script Generation Component

Handles Dockerfile creation and updates for Aspora:
- generate_dockerfile=true: Creates new Dockerfile from template (warns if Dockerfile exists)
- generate_dockerfile=false: Updates existing Dockerfile with configuration changes
"""

import json
import logging
import os
import re
import aiofiles
from typing import Dict, List, Optional

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing
from app.utils.dockerfile_transformer import (
    generate_datadog_block,
    DEFAULT_DD_AGENT_VERSION,
    transform_dockerfile_for_datadog,
    transform_dockerfile_remove_datadog,
    comment_out_otel_block
)
from app.utils.language_helpers import is_java_language, is_go_language, is_python_language


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


class AsporaDockerScriptGenComponent:
    """
    Component responsible for generating and updating Dockerfile content.

    Behavior Logic:
    1. generate_dockerfile=true:
       - Check repository for existing Dockerfile
       - Warn if it exists (creation continues and overwrites)
       - Generate from template

    2. generate_dockerfile=false (I have a Dockerfile):
       - Fetch existing Dockerfile from repository
       - Error if the file is missing or empty
       - Apply transformations (Datadog, build_args, xms/xmx)
       - Update the Dockerfile with changes
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository
        self.java_template_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "templates", "dockerfiles", "java-standard.Dockerfile"
        )
        self.go_template_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "templates", "dockerfiles", "go-standard.Dockerfile"
        )
        self.python_template_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "templates", "dockerfiles", "python-standard.Dockerfile"
        )

        self._reserved_java_args = {"JAR_FILE", "PROFILE"}
        self._reserved_go_args = {"AWS_SECRETS_MANAGER_NAME", "CONFIG_ENV"}
        self._reserved_python_args: set = set()

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
        Generate or update Dockerfile content based on generate_dockerfile flag.

        Logic:
        1. generate_dockerfile=true: Create new Dockerfile from template (warn if it exists)
        2. generate_dockerfile=false: Update existing Dockerfile with transformations

        Returns:
            str: Dockerfile content
        """
        config_snapshot = queue_dict.get("config_snapshot")
        file_path = file_location.file_path

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        base_branch = file_location.base_branch or file_location.target_branch or ""
        feature_branch = file_location.feature_branch
        component_name = self.__class__.__name__

        # Extract all configuration
        generate_dockerfile = config_snapshot.get("generate_dockerfile", False)
        language_name = config_snapshot.get("language_name")
        language_version = config_snapshot.get("language_version")
        service_name = config_snapshot.get("service_name")
        enable_datadog = config_snapshot.get("enable_datadog", False)
        xms = config_snapshot.get("xms")
        xmx = config_snapshot.get("xmx")
        advanced_options = config_snapshot.get("advanced_options")
        port = config_snapshot.get("port")
        go_config_path = config_snapshot.get("go_config_path")
        use_aws_secrets = config_snapshot.get("use_aws_secrets", False)
        build_args = config_snapshot.get("build_args")

        self.logger.info(f"=== DOCKERFILE PROCESSING ===")
        self.logger.info(f"Mode: {'CREATE (generate_dockerfile=true)' if generate_dockerfile else 'UPDATE (I have a Dockerfile)'}")
        self.logger.info(f"Path:{file_path}")
        self.logger.info(f"Service: {service_name}, Language: {language_name}")

        dockerfile_exists = False
        warning_message = None

        # ===================================================================
        # SCENARIO 1: generate_dockerfile=true (Create new Dockerfile)
        # ===================================================================
        if generate_dockerfile:
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
                existing_file = await fetch_existing_content(
                    db=db,
                    tenant=tenant,
                    owner=owner,
                    repo=repo,
                    file_path=file_path,
                    base_branch=base_branch,
                    feature_branch=feature_branch,
                    workflow_context=workflow_context,
                    logger=self.logger,
                    component_name=component_name,
                )

            # if existing_file.get("status") == "error":
            #     raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

            dockerfile_exists = existing_file.get("exists", False)
            self.logger.error("%%"*40)
            self.logger.error(f"Dockerfile exists: {dockerfile_exists}")
            self.logger.error("%%"*40)

            if dockerfile_exists:
                warning_message = (
                    "Dockerfile already exists. Continuing with 'Create Dockerfile' will "
                    "replace the existing file."
                )
                self.logger.error(warning_message)

            # Generate new Dockerfile from template
            self.logger.info("✓ Generating new Dockerfile from template")
            gen_context = f"path={file_path} mode=create"
            with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
                dockerfile_content = await self._generate_dockerfile_content(
                    language_name=language_name,
                    language_version=language_version,
                    service_name=service_name,
                    enable_datadog=enable_datadog,
                    xms=xms,
                    xmx=xmx,
                    advanced_options=advanced_options,
                    port=port,
                    go_config_path=go_config_path,
                    use_aws_secrets=use_aws_secrets,
                    build_args=build_args
                )

        # ===================================================================
        # SCENARIO 2: generate_dockerfile=false (Update existing Dockerfile)
        # ===================================================================
        else:
            
            # Check if Dockerfile exists in repository
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
                existing_file = await fetch_existing_content(
                    db=db,
                    tenant=tenant,
                    owner=owner,
                    repo=repo,
                    file_path=file_path,
                    base_branch=base_branch,
                    feature_branch=feature_branch,
                    workflow_context=workflow_context,
                    logger=self.logger,
                    component_name=component_name,
                )

            if existing_file.get("status") == "error":
                raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

            dockerfile_exists = existing_file.get("exists", False)
            self.logger.info(f"Dockerfile exists: {dockerfile_exists}")
            self.logger.error("%%"*40)
            self.logger.error(f"Dockerfile exists: {dockerfile_exists}")
            self.logger.error("%%"*40)
            self.logger.info("✓ Fetching existing Dockerfile for update")
            existing_content = existing_file.get("content")
            if not dockerfile_exists or not existing_content or not existing_content.strip():
                error_message = (
                    f"No Dockerfile content found at {file_path}. "
                    f"Enable 'Create Dockerfile' to generate a new one, or check the dockerfile_path."
                )
                if workflow_context and workflow_context.skip_commit:
                    warning_message = error_message
                    self.logger.error(error_message)
                    if queue_dict.get("id"):
                        script_gen_key = file_location.script_gen_key
                        workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key][file_location.base_branch] = {
                            "original_content": "",
                            "preview_content": warning_message,
                            "warning_message": warning_message
                        }
                    return ""

                raise ValueError(error_message)

            # Apply transformations to existing Dockerfile
            self.logger.info(f"✓ Applying transformations to existing Dockerfile")
            gen_context = f"path={file_path} mode=update"
            with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
                dockerfile_content = self._apply_transformations_to_existing_dockerfile(
                    existing_content=existing_content,
                    language_name=language_name,
                    language_version=language_version,
                    service_name=service_name,
                    enable_datadog=enable_datadog,
                    xms=xms,
                    xmx=xmx,
                    advanced_options=advanced_options,
                    port=port,
                    go_config_path=go_config_path,
                    use_aws_secrets=use_aws_secrets,
                    build_args=build_args
                )

        # ===================================================================
        # COMMON: S3 Upload, Commit, and Workflow Context
        # ===================================================================
        if upload_to_s3:
            identifier = queue_dict.get("code") or config_snapshot.get("service_name") or "dockerfile"
            original_s3_key = await self._upload_original_to_s3(dockerfile_content, identifier)

            if repository and queue_dict.get("code"):
                artifact_s3_key_json = {
                    "original_s3_key": original_s3_key,
                    "preview": original_s3_key
                }
                await repository.update_artifact_s3_key(queue_dict.get("code") or config_snapshot.get('code'), json.dumps(artifact_s3_key_json))
                self.logger.info(
                    "Saved artifact_s3_key to database for queue item %s: %s",
                    queue_dict.get("code"),
                    json.dumps(artifact_s3_key_json)
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
                    content=dockerfile_content,
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
                response_payload = {
                    "original_content": dockerfile_content,
                    "preview_content": dockerfile_content
                }
                if warning_message:
                    response_payload["warning_message"] = warning_message
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key][file_location.base_branch] = response_payload

        self.logger.info(f"=== DOCKERFILE PROCESSING COMPLETE ===")
        return dockerfile_content

    async def _upload_original_to_s3(self, content: str, identifier: str) -> str:
        """Upload Dockerfile content to S3 and return the key."""
        original_s3_key = f"dockerfiles/{identifier}.Dockerfile"
        await self._upload_to_s3(
            content=content,
            key=original_s3_key
        )
        return original_s3_key

    async def _upload_to_s3(
        self,
        content: str,
        key: str,
        #TODO None Not acceptable for s3 api
        metadata: Optional[Dict[str, str]] = None
    ) -> None:
        """Upload content to S3 via FileManagerHandler."""
        try:
            result = await FileManagerHandler.upload_file(
                key=key,
                content=content,
                content_type="text/plain",
                metadata=metadata
            )
            self.logger.info(f"Uploaded content to S3: {result['location']}")
        except Exception as e:
            self.logger.error(f"Failed to upload content to S3: {str(e)}")
            raise

    def _apply_transformations_to_existing_dockerfile(
        self,
        existing_content: str,
        language_name: str,
        language_version: Optional[str],
        service_name: str,
        enable_datadog: bool = False,
        xms: Optional[int] = None,
        xmx: Optional[int] = None,
        advanced_options: Optional[List[Dict]] = None,
        port: Optional[str] = None,
        go_config_path: Optional[str] = None,
        use_aws_secrets: bool = False,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """
        Apply transformations to an existing Dockerfile.

        This method modifies existing Dockerfiles inline by:
        1. Injecting/updating build arguments (ARG declarations)
        2. Updating Java memory settings (xms/xmx) in JAVA_TOOL_OPTIONS
        3. Updating Datadog configuration (for Java)

        Note: For Go/Python, this primarily handles build_args since they don't have
        JVM memory settings or Datadog agent integration in the same way.

        Args:
            existing_content: The current Dockerfile content
            language_name: Language name (java, go, python)
            language_version: Language version
            service_name: Service name for Datadog
            enable_datadog: Whether to enable Datadog (Java only)
            xms: Java heap min in MB
            xmx: Java heap max in MB
            advanced_options: Datadog advanced options
            port: Port (Go/Python)
            go_config_path: Go config path
            use_aws_secrets: Go AWS secrets flag
            build_args: Build arguments to inject

        Returns:
            str: Transformed Dockerfile content
        """
        content = existing_content

        # Step 1: Inject or update build arguments
        if build_args:
            self.logger.info(f"  → Injecting build_args: {[arg.get('name') for arg in build_args]}")
            content = self._inject_build_args_into_dockerfile(content, build_args, language_name)

        # Step 2: Language-specific transformations
        if is_java_language(language_name):
            self.logger.info(f"  → Applying Java-specific transformations")
            content = self._apply_java_transformations(
                content=content,
                service_name=service_name,
                enable_datadog=enable_datadog,
                xms_mb=xms,
                xmx_mb=xmx,
                advanced_options=advanced_options
            )
        elif is_go_language(language_name):
            # For Go, build_args are the main transformation
            # Port and config_path are typically fixed in the template
            self.logger.info(f"  → Go Dockerfile: build_args applied")
        elif is_python_language(language_name):
            # For Python, build_args are the main transformation
            self.logger.info(f"  → Python Dockerfile: build_args applied")

        return content

    def _inject_build_args_into_dockerfile(
        self,
        dockerfile_content: str,
        build_args: List[Dict[str, str]],
        language_name: str
    ) -> str:
        """
        Inject build ARG declarations into existing Dockerfile.

        Strategy:
        1. Parse existing ARG declarations
        2. Add new ARGs (skip duplicates and reserved args)
        3. Insert after FROM line but before other instructions
        """
        if not build_args:
            return dockerfile_content

        # Get reserved args for the language
        reserved_args = set()
        if is_java_language(language_name):
            reserved_args = self._reserved_java_args
        elif is_go_language(language_name):
            reserved_args = self._reserved_go_args
        elif is_python_language(language_name):
            reserved_args = self._reserved_python_args

        # Extract ARG names from build_args
        new_arg_names = []
        for arg in build_args:
            name = arg.get("name", "") or arg.get("key", "")
            if name:
                name = name.strip().upper()
                if name not in reserved_args:
                    new_arg_names.append(name)

        if not new_arg_names:
            return dockerfile_content

        # Parse existing ARGs in Dockerfile and find last ARG position
        existing_args = set()
        last_arg_match = None
        arg_pattern = re.compile(r'^ARG\s+([A-Z_][A-Z0-9_]*)', re.MULTILINE | re.IGNORECASE)
        for match in arg_pattern.finditer(dockerfile_content):
            existing_args.add(match.group(1).upper())
            last_arg_match = match  # Keep track of last ARG for insertion

        # Filter out args that already exist (prevents duplicates)
        args_to_add = [name for name in new_arg_names if name not in existing_args]

        if not args_to_add:
            self.logger.info(f"    All build args already exist in Dockerfile")
            return dockerfile_content

        self.logger.info(f"    Adding new ARGs: {args_to_add}")

        # Build the new ARGs block
        new_args_block = "\n".join([f"ARG {name}" for name in args_to_add])

        # Strategy: Insert after last existing ARG if present, otherwise after FROM
        if last_arg_match:
            # Insert after last existing ARG
            insert_pos = last_arg_match.end()
            new_args_block_with_newline = "\n" + new_args_block
            return dockerfile_content[:insert_pos] + new_args_block_with_newline + dockerfile_content[insert_pos:]
        else:
            # No existing ARGs - insert after FROM line
            from_pattern = re.compile(r'^FROM\s+.+$', re.MULTILINE)
            from_match = from_pattern.search(dockerfile_content)

            if not from_match:
                # No FROM found, add at the beginning
                return new_args_block + "\n\n" + dockerfile_content

            # Insert after FROM line
            insert_pos = from_match.end()
            return dockerfile_content[:insert_pos] + "\n" + new_args_block + "\n" + dockerfile_content[insert_pos:]

    def _apply_java_transformations(
        self,
        content: str,
        service_name: str,
        enable_datadog: bool,
        xms_mb: Optional[int],
        xmx_mb: Optional[int],
        advanced_options: Optional[List[Dict]]
    ) -> str:
        """
        Apply Java-specific transformations to existing Dockerfile.

        Handles:
        1. Datadog agent injection/update/removal (if enable_datadog changes)
        2. OTEL commenting (when Datadog is enabled)
        3. Java memory settings (xms/xmx) in JAVA_TOOL_OPTIONS

        Behavior matches DockerfileSyncService:
        - enable_datadog=true + no Datadog → Add Datadog + comment out OTEL
        - enable_datadog=true + has Datadog → Update Datadog + ensure OTEL commented
        - enable_datadog=false + has Datadog → Remove Datadog + restore original JAVA_TOOL_OPTIONS
        - enable_datadog=false + no Datadog → Just update memory settings
        """
        # Check if Datadog is already present
        has_datadog = "dd-java-agent" in content

        if enable_datadog:
            # ============================================================
            # SCENARIO: Datadog ENABLED
            # ============================================================
            if not has_datadog:
                # Add Datadog from scratch using the utility function
                self.logger.info(f"    Adding Datadog configuration and commenting out OTEL")
                content = transform_dockerfile_for_datadog(
                    dockerfile_content=content,
                    service_name=service_name,
                    xms_mb=xms_mb,
                    xmx_mb=xmx_mb,
                    advanced_options=advanced_options
                )
                # transform_dockerfile_for_datadog already handles OTEL commenting
            else:
                # Update existing Datadog configuration
                self.logger.info(f"    Updating existing Datadog configuration")
                content = self._update_java_tool_options(content, xms_mb, xmx_mb)
                content = self._update_datadog_env_vars(content, service_name, advanced_options)

                # Ensure OTEL is commented out (in case it wasn't before)
                content = comment_out_otel_block(content)

        else:
            # ============================================================
            # SCENARIO: Datadog DISABLED
            # ============================================================
            if has_datadog:
                # Remove Datadog configuration using the utility function
                self.logger.info(f"    Removing Datadog configuration and restoring original JAVA_TOOL_OPTIONS")
                content = transform_dockerfile_remove_datadog(content)

                # Update memory settings after removal (if specified)
                if xms_mb or xmx_mb:
                    from app.services.dockerfile_fetch_service import DockerfileFetchService
                    dockerfile_service = DockerfileFetchService()
                    content = dockerfile_service.update_java_memory_settings(
                        content, xms_mb=xms_mb, xmx_mb=xmx_mb
                    )
            else:
                # Just update memory settings without Datadog
                if xms_mb or xmx_mb:
                    self.logger.info(f"    Updating Java memory settings (xms={xms_mb}, xmx={xmx_mb})")
                    content = self._update_java_tool_options(content, xms_mb, xmx_mb, include_datadog=False)

        return content

    def _update_java_tool_options(
        self,
        content: str,
        xms_mb: Optional[int],
        xmx_mb: Optional[int],
        include_datadog: bool = True
    ) -> str:
        """
        Update JAVA_TOOL_OPTIONS in Dockerfile with new memory settings.

        Finds existing ENV JAVA_TOOL_OPTIONS line and updates it.
        """
        # Pattern to match uncommented ENV JAVA_TOOL_OPTIONS line (^ excludes commented lines)
        pattern = re.compile(r'^ENV\s+JAVA_TOOL_OPTIONS\s*=\s*"([^"]+)"', re.MULTILINE)
        match = pattern.search(content)

        if not match:
            self.logger.warning("    Could not find ENV JAVA_TOOL_OPTIONS in Dockerfile")
            return content

        existing_options = match.group(1)
        self.logger.debug(f"    Current JAVA_TOOL_OPTIONS: {existing_options}")

        # Update xms/xmx values
        new_options = existing_options

        # Update -Xms
        if xms_mb:
            xms_pattern = re.compile(r'-Xms\d+m')
            if xms_pattern.search(new_options):
                new_options = xms_pattern.sub(f'-Xms{xms_mb}m', new_options)
            else:
                new_options = f'-Xms{xms_mb}m {new_options}'

        # Update -Xmx
        if xmx_mb:
            xmx_pattern = re.compile(r'-Xmx\d+m')
            if xmx_pattern.search(new_options):
                new_options = xmx_pattern.sub(f'-Xmx{xmx_mb}m', new_options)
            else:
                new_options = f'-Xmx{xmx_mb}m {new_options}'

        # Replace in content
        new_line = f'ENV JAVA_TOOL_OPTIONS="{new_options}"'
        content = pattern.sub(new_line, content)

        self.logger.debug(f"    Updated JAVA_TOOL_OPTIONS: {new_options}")
        return content

    def _update_datadog_env_vars(
        self,
        content: str,
        service_name: str,
        advanced_options: Optional[List[Dict]]
    ) -> str:
        """
        Update Datadog environment variables in existing Dockerfile.

        Updates:
        - ENV DD_SERVICE (if service name changed)
        - ENV DD_TAGS (if advanced options changed)
        - Other DD_* variables from advanced_options
        """
        # Update DD_SERVICE
        dd_service_pattern = re.compile(r'ENV\s+DD_SERVICE\s*=\s*"([^"]*)"', re.MULTILINE)
        dd_service_match = dd_service_pattern.search(content)
        if dd_service_match:
            current_service = dd_service_match.group(1)
            if current_service != service_name:
                self.logger.info(f"    Updating DD_SERVICE: {current_service} → {service_name}")
                content = dd_service_pattern.sub(f'ENV DD_SERVICE="{service_name}"', content)

        # Update advanced options (DD_TAGS and other DD_* variables)
        if advanced_options:
            for option in advanced_options:
                key = option.get("key", "")
                value = option.get("value", "")
                if not key or not value:
                    continue

                # Normalize key to uppercase with DD_ prefix
                if not key.startswith("DD_"):
                    key = f"DD_{key.upper()}"
                else:
                    key = key.upper()

                # Update or add the ENV variable
                env_pattern = re.compile(rf'ENV\s+{re.escape(key)}\s*=\s*"([^"]*)"', re.MULTILINE)
                env_match = env_pattern.search(content)

                if env_match:
                    current_value = env_match.group(1)
                    if current_value != value:
                        self.logger.info(f"    Updating {key}: {current_value} → {value}")
                        content = env_pattern.sub(f'ENV {key}="{value}"', content)
                else:
                    # Add new ENV variable after DD_SERVICE
                    self.logger.info(f"    Adding new {key}={value}")
                    dd_service_match = dd_service_pattern.search(content)
                    if dd_service_match:
                        insert_pos = dd_service_match.end()
                        content = content[:insert_pos] + f'\nENV {key}="{value}"' + content[insert_pos:]

        return content

    async def _generate_dockerfile_content(
        self,
        language_name: str,
        language_version: Optional[str],
        service_name: str,
        enable_datadog: bool = False,
        xms: Optional[int] = None,
        xmx: Optional[int] = None,
        advanced_options: Optional[List[Dict]] = None,
        port: Optional[str] = None,
        go_config_path: Optional[str] = None,
        use_aws_secrets: bool = False,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        if is_java_language(language_name):
            return await self._generate_java_dockerfile(
                service_name=service_name,
                jdk_version=language_version or "21",
                enable_datadog=enable_datadog,
                xms_mb=xms,
                xmx_mb=xmx,
                advanced_options=advanced_options,
                build_args=build_args
            )

        if is_go_language(language_name):
            return await self._generate_go_dockerfile(
                service_name=service_name,
                port=port or "8080",
                go_config_path=go_config_path,
                use_aws_secrets=use_aws_secrets,
                build_args=build_args
            )

        if is_python_language(language_name):
            return await self._generate_python_dockerfile(
                service_name=service_name,
                python_version=language_version or "3.12",
                port=port or "8000",
                build_args=build_args
            )

        raise ValueError(f"Unsupported language: {language_name}")

    def _generate_build_args_block(
        self,
        build_args: Optional[List[Dict[str, str]]],
        reserved_args: Optional[set] = None
    ) -> str:
        if not build_args:
            return ""

        reserved = reserved_args or set()
        lines = []
        seen_args = set()

        for arg in build_args:
            name = arg.get("name", "") or arg.get("key", "")
            if name:
                name = name.strip().upper()
                if name in reserved or name in seen_args:
                    self.logger.warning(f"Skipping duplicate ARG: {name}")
                    continue
                seen_args.add(name)
                lines.append(f"ARG {name}")

        return "\n".join(lines) + "\n" if lines else ""

    async def _generate_java_dockerfile(
        self,
        service_name: str,
        jdk_version: str,
        enable_datadog: bool = False,
        xms_mb: Optional[int] = None,
        xmx_mb: Optional[int] = None,
        advanced_options: Optional[List[Dict]] = None,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        async with aiofiles.open(self.java_template_path, "r") as f:
            template = await f.read()

        content = template.replace("{{JDK_VERSION}}", jdk_version)

        build_args_block = self._generate_build_args_block(build_args, self._reserved_java_args)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

        base_java_tool_options = "-Dlogging.level.root=info"

        if enable_datadog:
            datadog_block = generate_datadog_block(
                service_name=service_name,
                xms_mb=xms_mb,
                xmx_mb=xmx_mb,
                dd_agent_version=DEFAULT_DD_AGENT_VERSION,
                existing_java_tool_options=base_java_tool_options,
                advanced_options=advanced_options
            )
            lines = datadog_block.strip().split("\n")
            new_lines = []
            for line in lines:
                new_lines.append(line)
                if line.startswith("ADD ") and "dd-java-agent" in line:
                    new_lines.append("RUN chmod 644 /app/dd-java-agent.jar")
            datadog_block = "\n".join(new_lines) + "\n\n"
            content = content.replace("{{DATADOG_BLOCK}}\n", datadog_block)
        else:
            java_opts_parts = []
            if xms_mb:
                java_opts_parts.append(f"-Xms{xms_mb}m")
            if xmx_mb:
                java_opts_parts.append(f"-Xmx{xmx_mb}m")
            java_opts_parts.append(base_java_tool_options)
            java_tool_options = " ".join(java_opts_parts)
            java_tool_options_line = f'ENV JAVA_TOOL_OPTIONS="{java_tool_options}"\n\n'
            content = content.replace("{{DATADOG_BLOCK}}\n", java_tool_options_line)

        return content

    async def _generate_go_dockerfile(
        self,
        service_name: str,
        port: str,
        go_config_path: Optional[str] = None,
        use_aws_secrets: bool = False,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        async with aiofiles.open(self.go_template_path, "r") as f:
            template = await f.read()

        content = template

        reserved_args = self._reserved_go_args if use_aws_secrets else set()
        build_args_block = self._generate_build_args_block(build_args, reserved_args)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

        if use_aws_secrets:
            aws_block = """ARG AWS_SECRETS_MANAGER_NAME
ARG CONFIG_ENV
RUN if [ -z "$CONFIG_ENV" ]; then echo "ERROR: CONFIG_ENV build arg is required" && exit 1; fi
ENV AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
ENV AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
ENV AWS_REGION=$AWS_REGION
ENV AWS_SECRETS_MANAGER_NAME=$AWS_SECRETS_MANAGER_NAME
ENV CONFIG_ENV=$CONFIG_ENV
"""
            content = content.replace("{{AWS_SECRETS_BLOCK}}\n", aws_block)
        else:
            content = content.replace("{{AWS_SECRETS_BLOCK}}\n", "")

        if go_config_path:
            config_type = "json" if go_config_path.endswith(".json") else "yaml"
            content = content.replace("{{CONFIG_PATH}}", f"/app/{go_config_path}")
            content = content.replace("{{CONFIG_TYPE}}", config_type)
            config_copy_line = f"COPY {go_config_path} /app/{go_config_path}"
            content = content.replace("{{CONFIG_COPY_LINE}}", config_copy_line)
        else:
            content = content.replace("{{CONFIG_PATH}}", "/app/configs/config.json")
            content = content.replace("{{CONFIG_TYPE}}", "json")
            content = content.replace("{{CONFIG_COPY_LINE}}", "")

        content = content.replace("{{PORT}}", port or "8080")

        return content

    async def _generate_python_dockerfile(
        self,
        service_name: str,
        python_version: str,
        port: str = "8000",
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        async with aiofiles.open(self.python_template_path, "r") as f:
            template = await f.read()

        content = template.replace("{{PYTHON_VERSION}}", python_version)
        content = content.replace("{{PORT}}", port)

        build_args_block = self._generate_build_args_block(build_args, self._reserved_python_args)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

        return content
