"""
Aspora ECS Script Generation Component

Handles ECS terragrunt script generation for Aspora tenant.
"""

import json
import logging
import os
import re
import aiofiles
from typing import Dict, Any, Optional, List

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.hcl_patch import remove_scalar_field
from app.utils.timing import log_timing

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

# Tenant groups for branch logic
VANCE_ASPORA_TENANTS = {"vance", "aspora"}

# Map field names to anchor fields (fields in the same section to insert after)
# Order matters - we try each anchor in order until one is found
FIELD_ANCHOR_MAP = {
    # Container Config - insert after memory, cpu, or container_port
    "container_port": ["memory", "cpu"],
    "cpu": ["container_port", "memory"],
    "memory": ["cpu", "container_port"],
    # ALB Routing - insert after each other
    "service_path": ["listener_rule_priority"],
    "listener_rule_priority": ["service_path"],
    # Health Check - insert after health_check_path or before autoscaling fields
    "health_check_path": ["listener_rule_priority", "service_path", "memory"],
    # Autoscaling - insert after each other in order
    "enable_autoscaling": ["health_check_path", "memory"],
    "desired_count": ["enable_autoscaling"],
    "min_task_count": ["desired_count", "enable_autoscaling"],
    "max_task_count": ["min_task_count", "desired_count"],
    # Datadog Sidecar - insert after each other
    "enable_datadog_sidecar": ["max_task_count", "desired_count", "enable_autoscaling"],
    "datadog_secret_arn": ["enable_datadog_sidecar", "datadog_log_source"],
    "datadog_log_source": ["enable_datadog_sidecar"],
    "datadog_sidecar_cpu": ["datadog_log_source", "enable_datadog_sidecar"],
    "datadog_sidecar_memory": ["datadog_sidecar_cpu", "datadog_log_source"],
    "datadog_logs_enabled": ["datadog_sidecar_memory", "datadog_sidecar_cpu", "enable_datadog_sidecar"],
    # Feature Toggles - insert after other toggles
    "enable_ulimits": ["alb_attachment", "create_service", "create_alb"],
}

# Datadog dependency block template to insert when missing
DATADOG_DEPENDENCY_BLOCK = '''dependency "datadog_api_key" {
  config_path = "../../secrets/datadog-configs"
  mock_outputs = {
    secret_manager_arn = "arn:aws:secretsmanager:ap-south-1:111111122222:secret:datadog_api_key-123456"
  }
}'''

# Datadog parameters that trigger dependency infrastructure check
DATADOG_PARAMS = {
    "enable_datadog_sidecar",
    "datadog_log_source",
    "datadog_sidecar_cpu",
    "datadog_sidecar_memory",
    "datadog_secret_arn",
    "datadog_logs_enabled",
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


class AsporaEcsTerragruntScriptGenComponent:
    """
    Component for generating ECS service terragrunt scripts for Aspora tenant.

    Responsibilities:
    - Load ECS template from disk based on service type
    - Replace configuration placeholders with actual values
    - Apply environment-specific rules and dependency injections
    - Generate preview HCL for display
    - Upload to S3 (original and preview)
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
        Generate ECS terragrunt configuration.

        This function:
        1. Checks if a terragrunt file exists (staged entry or GitOps fetch).
        2. If file does NOT exist:
           - Load the ECS template based on service_type/alb_selection.
        3. If file exists:
           - Use existing content as base and update fields in place.
        4. Build field mappings from config snapshot.
        5. Inject Datadog dependency/path and set enable_otel_sidecar if needed.
        6. Apply field mappings to content.
        7. Update environment-specific paths and env file references.
        8. Apply ALB and environment rules (env rules only for new services).
        9. Generate preview HCL.
        10. Upload to S3 (original and preview) if requested.
        11. Return the final terragrunt content.

        Returns:
            Generated/updated terragrunt configuration content

        Raises:
            ValueError: If required parameters are missing
        """
        config_snapshot = queue_dict.get("config_snapshot") or {}

        # Extract required parameters
        service_name = config_snapshot.get('service_name')
        service_type = config_snapshot.get('service_type', 'API')
        alb_selection = config_snapshot.get('alb_selection', 'existing_alb')
        environment =config_snapshot.get('environment')or  queue_dict.get("environment")
        product_name = config_snapshot.get('product_name', '')

        # github_token = config_snapshot.get('github_token')
        # github_base_url = config_snapshot.get('github_base_url')

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # Extract identifier for S3
        identifier = config_snapshot.get('identifier') or service_name

        if not identifier and file_location and file_location.file_path:
            # Extract service name from file path if not provided
            identifier = os.path.basename(os.path.dirname(file_location.file_path))

        logger.info(f"Generating ECS configuration for: {file_location.file_path}")
        logger.info(f"  Service: {service_name} (type: {service_type})")

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
            fetch_context = f"repo={repo} branch={feature_branch} path={file_location.file_path}"
            with log_timing(logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db,
                    tenant=tenant,
                    owner=owner,
                    repo=repo,
                    file_path=file_location.file_path,
                    branch=feature_branch,
                    # github_base_url=github_base_url,
                    # github_token=github_token
                )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        terragrunt_content: str
        is_new_service = not existing_file["exists"]
        gen_mode = "create" if is_new_service else "update"
        gen_context = f"path={file_location.file_path} mode={gen_mode}"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            if not existing_file["exists"]:
                # File does NOT exist -> Load template from disk
                logger.info("File does not exist - loading template from disk")

                # Determine which template to use based on service type and alb_selection
                template_name = self._get_template_name(service_type, alb_selection)
                template_path = os.path.join(
                    os.path.dirname(__file__),
                    "../../../../templates/terragrunt/services",
                    template_name
                )

                logger.info(f"Loading ECS template from: {template_path}")
                async with aiofiles.open(template_path, "r") as f:
                    terragrunt_content = await f.read()

                logger.info("Template loaded successfully")
            else:
                # File exists -> Use existing content as base, then apply updates
                logger.info("File exists - using existing configuration as base")
                terragrunt_content = existing_file["content"]

            # Always apply field mapping updates (whether file is new or existing)
            # This ensures config_snapshot changes are applied
            sidecar_config = config_snapshot.get('sidecar_config')
            language_name = config_snapshot.get('language_name')
            is_no_alb = alb_selection == "no_alb"

            field_mapping = self._build_field_mapping(
                config=config_snapshot,
                sidecar_config=sidecar_config,
                language_name=language_name,
                is_no_alb=is_no_alb
            )

            # Handle Datadog dependencies
            if self._has_datadog_params(terragrunt_content, field_mapping):
                logger.info("Datadog parameters detected, ensuring dependency infrastructure...")
                terragrunt_content = self._ensure_datadog_dependency(terragrunt_content)
                terragrunt_content = self._ensure_datadog_in_dependencies_paths(terragrunt_content)
                if 'datadog_secret_arn' not in field_mapping and not re.search(r'\bdatadog_secret_arn\s*=', terragrunt_content):
                    field_mapping['datadog_secret_arn'] = 'dependency.datadog_api_key.outputs.secret_manager_arn'
                    logger.info("Added datadog_secret_arn to field_mapping")
                if 'enable_otel_sidecar' not in field_mapping:
                    field_mapping['enable_otel_sidecar'] = 'false'
                    logger.info("Setting enable_otel_sidecar = false (Datadog enabled)")

            # Apply field mapping with ANCHOR-BASED insertion
            for field_name, new_value in field_mapping.items():
                pattern = rf'(\b{field_name}\s*=\s*)([^\n]+)'
                if re.search(pattern, terragrunt_content):
                    # Update existing field
                    terragrunt_content = re.sub(pattern, lambda m: m.group(1) + new_value, terragrunt_content)
                else:
                    # Add new field after anchor
                    terragrunt_content = self._add_field_after_anchor(terragrunt_content, field_name, new_value)

            # ── Explicit clears ──────────────────────────────────────────────
            # The mapping above can only REPLACE or INSERT. A field the user
            # emptied never enters it (_build_field_mapping gates on truthiness),
            # so the old line survived a save that reported success — the form
            # and service_configs showed it cleared while the committed HCL kept
            # the value. The same clear/remove pair the SQS component already
            # uses: key PRESENT but empty means "the user emptied this box",
            # key ABSENT means "this change does not speak to that field".
            # remove_scalar_field is a no-op when the line is not there, so a
            # no-alb template (which carries no http_scaling line) is unaffected.
            for cleared in self._fields_to_clear(config_snapshot):
                terragrunt_content, removed = remove_scalar_field(terragrunt_content, cleared)
                if removed:
                    logger.info("Removed %s — emptied in the settings form", cleared)

            # Update environment-specific paths
            if environment:
                terragrunt_content = self._update_common_infra_paths(terragrunt_content, environment)
            terragrunt_content = self._uncomment_env_file_paths(
                terragrunt_content, service_name, environment, tenant,
                product_name, service_type
            )
            # Toggle ALB ARN based on http scaling settings.
            terragrunt_content = self._apply_http_scaling_alb_arn_rule(
                terragrunt_content, field_mapping
            )
            # Apply environment rules only for new services to avoid overwriting manual edits.
            if environment and is_new_service:
                terragrunt_content = self._apply_environment_rules(
                    terragrunt_content,
                    environment,
                    product_name=product_name,
                    tenant=tenant,
                    field_mapping=field_mapping,
                    service_type=service_type
                )
            # OPS_TOOLS always uses capacity_provider_name, even on updates.
            if service_type == "OPS_TOOLS" and not is_new_service:
                terragrunt_content = self._ensure_capacity_provider_uncommented(
                    terragrunt_content, tenant, environment, product_name
                )

            logger.info("Updated configuration with actual values")

            self.logger.info("ECS configuration generated successfully")

            # Generate preview HCL
            preview_hcl = self._preview_ecs_hcl(
                identifier=identifier,
                service_name=service_name,
                service_type=service_type,
                parameters=config_snapshot
            )
            self.logger.info("Generated ECS preview HCL")

        # Upload to S3 if requested
        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            if not identifier:
                raise ValueError("Parameter 'identifier' is required when upload_to_s3 is True")

            try:
                original_s3_key = f"ecs/{identifier}.hcl"
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
                preview_s3_key = f"preview/ecs/{identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_hcl,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": environment,
                        "identifier": identifier,
                        "service_name": service_name,
                        "service_type": service_type,
                        "generated_by": "aspora_ecs_script_gen_component"
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
            # TODO: update the git commit sha response in the workflow context here after commit

        if workflow_context is not None and queue_dict.get("id") and file_location is not None:
            script_gen_key = file_location.script_gen_key
            workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                "original_content": terragrunt_content,
                "preview_content": preview_hcl
            }

        return terragrunt_content

    def _get_template_name(self, service_type: str, alb_selection: str = "existing_alb") -> str:
        """
        Determine which template to use based on service type and alb_selection.
        Matches TerragruntSyncService._get_template_file() logic.

        Args:
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)
            alb_selection: ALB selection (existing_alb, create_new_alb, no_alb)

        Returns:
            Template filename
        """
        service_type = service_type.upper()

        # OPS_TOOLS services use dedicated template
        if service_type == "OPS_TOOLS":
            return "ops-tools.hcl"

        # BACKGROUND_SERVICE always uses no-alb template (workers don't need ALB)
        if service_type == "BACKGROUND_SERVICE":
            return "worker-no-alb.hcl"

        # Determine based on alb_selection for API services
        alb_selection = alb_selection.lower() if alb_selection else "existing_alb"

        if alb_selection == "no_alb":
            return "worker-no-alb.hcl"
        elif alb_selection == "create_new_alb":
            return "api-new-alb.hcl"
        else:
            # Default: existing_alb
            return "api-common-alb.hcl"

    #: config key -> terragrunt field whose LINE is deleted when the key arrives
    #: empty. Only fields the settings form always sends belong here: the rule
    #: is "present but empty = cleared", which is meaningless for a key the
    #: form may simply omit.
    CLEARABLE_FIELDS: Dict[str, str] = {
        "http_scaling_target_value": "http_scaling_target_value",
    }

    @staticmethod
    def _is_empty_value(value) -> bool:
        """Empty for clear purposes: None, blank string, empty list."""
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple, dict)):
            return not value
        return False

    def _fields_to_clear(self, config: Optional[Dict]) -> List[str]:
        """Terragrunt fields the config explicitly empties.

        Absent key -> not in play, left alone (JSON Merge Patch, the same rule
        diff_config_dicts and the SQS component follow). Present-but-empty ->
        the user cleared the box, so the line goes.
        """
        if not config:
            return []
        return [
            field
            for key, field in self.CLEARABLE_FIELDS.items()
            if key in config and self._is_empty_value(config[key])
        ]

    def _build_field_mapping(
        self,
        config: Optional[Dict],
        sidecar_config: Optional[list] = None,
        language_name: Optional[str] = None,
        is_no_alb: bool = False
    ) -> Dict[str, str]:
        """
        Build field mapping from config dict to Terragrunt field names.
        Copied from TerragruntSyncService._build_field_mapping().

        Handles:
        - Sidecar config (Datadog CPU, memory, logs)
        - Autoscaling config
        - Resource allocation (CPU/RAM conversion)
        - Service config (ALB paths, health checks)
        - Process limits

        Args:
            config: Service config dict from parameters
            sidecar_config: Sidecar config list [{name, enabled, cpu, ram, ...}]
            language_name: Language name (e.g., "java", "python")
            is_no_alb: Whether this is no-ALB config

        Returns:
            Dict mapping Terragrunt field names to values
        """
        field_mapping = {}

        # Datadog sidecar configuration from sidecar_config
        if sidecar_config:
            # Check if datadog sidecar is enabled and extract its cpu/ram
            datadog_enabled = False
            datadog_cpu = None
            datadog_ram = None
            datadog_logs_enabled = None
            for sidecar in sidecar_config:
                # Check both name and sidecar_config_code for "datadog"
                sidecar_name = (sidecar.get('name') or '').lower()
                sidecar_code = (sidecar.get('sidecar_config_code') or '').lower()
                is_datadog = 'datadog' in sidecar_name or 'datadog' in sidecar_code
                if is_datadog and sidecar.get('enabled', False):
                    datadog_enabled = True
                    datadog_cpu = sidecar.get('cpu')
                    datadog_ram = sidecar.get('ram')
                    datadog_logs_enabled = sidecar.get('datadog_logs_enabled')
                    break
            field_mapping["enable_datadog_sidecar"] = str(datadog_enabled).lower()
            # Add datadog sidecar cpu and memory if available
            if datadog_cpu:
                field_mapping["datadog_sidecar_cpu"] = str(datadog_cpu)
            if datadog_ram:
                field_mapping["datadog_sidecar_memory"] = str(datadog_ram)
            # Add datadog_logs_enabled if set
            if datadog_logs_enabled is not None:
                field_mapping["datadog_logs_enabled"] = str(datadog_logs_enabled).lower()
        else:
            field_mapping["enable_datadog_sidecar"] = "false"

        # Datadog log source from language_ref (base language only, no version)
        if language_name:
            # Extract base language name (e.g., "go 1.23" -> "go", "python 3.12" -> "python")
            base_language = language_name.split()[0].lower()
            field_mapping["datadog_log_source"] = f'"{base_language}"'

        if not config:
            return field_mapping

        # Auto Scaling Configuration - SKIP for no_alb (fields don't exist in no-alb template)
        if not is_no_alb:
            # Extract autoscaling config (nested structure)
            autoscaling = config.get("autoscaling", {})

            if "enabled" in autoscaling:
                field_mapping["enable_autoscaling"] = str(autoscaling["enabled"]).lower()

            if autoscaling.get("desired"):
                field_mapping["desired_count"] = autoscaling["desired"]

            if autoscaling.get("min"):
                field_mapping["min_task_count"] = autoscaling["min"]

            if autoscaling.get("max"):
                field_mapping["max_task_count"] = autoscaling["max"]
        else:
            logger.info("Skipping autoscaling fields for no-alb configuration")

        # Resource Allocation
        if config.get("port"):
            field_mapping["container_port"] = config["port"]

        if config.get("cpu"):
            # Convert CPU from vCPU to CPU units (1 vCPU = 1024 CPU units)
            # Database stores "2" (vCPU) -> Terragrunt needs 2048 (CPU units)
            cpu_value = config["cpu"]
            try:
                cpu_in_vcpu = float(cpu_value)
                cpu_in_units = int(cpu_in_vcpu * 1024)
                field_mapping["cpu"] = str(cpu_in_units)
            except (ValueError, TypeError):
                # If conversion fails, use the value as-is
                field_mapping["cpu"] = str(cpu_value)
                logger.warning(f"Could not convert CPU value '{cpu_value}' to units, using as-is")

        if config.get("ram"):
            # Convert RAM from GB to MB (1 GB = 1024 MB)
            # Database stores "4" (GB) -> Terragrunt needs 4096 (MB)
            ram_value = config["ram"]
            try:
                ram_in_gb = float(ram_value)
                ram_in_mb = int(ram_in_gb * 1024)
                field_mapping["memory"] = str(ram_in_mb)
            except (ValueError, TypeError):
                # If conversion fails, use the value as-is
                field_mapping["memory"] = str(ram_value)
                logger.warning(f"Could not convert RAM value '{ram_value}' to MB, using as-is")

        # Service Configuration - ALB only (service_path and listener_rule_priority don't exist in no-alb template)
        if not is_no_alb:
            if config.get("service_path"):
                # String values need quotes in HCL
                field_mapping["service_path"] = f'"{config["service_path"]}"'

            if config.get("listener_rule_priority"):
                field_mapping["listener_rule_priority"] = config["listener_rule_priority"]

            # Health Check - ALB only (commented out in no-alb template)
            if config.get("health"):
                # String values need quotes in HCL
                field_mapping["health_check_path"] = f'"{config["health"]}"'

            # HTTP Scaling - ALB only
            if "http_scaling_enabled" in config:
                field_mapping["http_scaling_enabled"] = str(config["http_scaling_enabled"]).lower()

            if config.get("http_scaling_target_value"):
                field_mapping["http_scaling_target_value"] = config["http_scaling_target_value"]
        else:
            logger.info("Skipping ALB-only fields (service_path, listener_rule_priority, health_check_path, http_scaling) for no-alb configuration")

        # Process Limits
        if "enable_ulimits" in config:
            field_mapping["enable_ulimits"] = str(config["enable_ulimits"]).lower()

        logger.info(f"Built field mapping with {len(field_mapping)} fields (no_alb={is_no_alb})")
        return field_mapping

    def _add_field_to_inputs_end(self, content: str, field_name: str, new_value: str) -> str:
        """
        Add field before the closing brace of inputs block.
        This is the fallback method when no anchor fields are found.

        Args:
            content: HCL content string
            field_name: Name of the field to add
            new_value: Value for the field

        Returns:
            Updated HCL content with the new field added at end of inputs block
        """
        # Find inputs = { and its matching }
        inputs_match = re.search(r'inputs\s*=\s*\{', content)
        if not inputs_match:
            logger.warning("Could not find 'inputs' block to add field")
            return content

        # Find matching closing brace by counting braces
        start_pos = inputs_match.end()
        brace_count = 1
        end_pos = start_pos

        for i, char in enumerate(content[start_pos:], start_pos):
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_pos = i
                    break

        # Find the start of the line containing the closing brace
        # to insert before it (preserving indentation of the brace)
        line_start = content.rfind('\n', 0, end_pos)
        if line_start == -1:
            line_start = 0
        else:
            line_start += 1  # Move past the newline

        new_field_line = f"  {field_name} = {new_value}\n"
        return content[:line_start] + new_field_line + content[line_start:]

    def _add_field_after_anchor(self, content: str, field_name: str, new_value: str) -> str:
        """
        Add a new field after an anchor field in HCL content.
        Looks for anchor fields (related fields in the same section) and inserts
        the new field after the last found anchor. Falls back to end of inputs block.

        Args:
            content: HCL content string
            field_name: Name of the field to add
            new_value: Value for the field

        Returns:
            Updated HCL content with the new field added
        """
        anchors = FIELD_ANCHOR_MAP.get(field_name, [])

        for anchor in anchors:
            # Pattern to find the anchor field and its value (entire line)
            anchor_pattern = rf'(\n\s*{anchor}\s*=\s*[^\n]+)'
            match = re.search(anchor_pattern, content)
            if match:
                # Insert after this anchor field's line
                insert_pos = match.end(1)
                new_field_line = f"\n  {field_name} = {new_value}"
                return content[:insert_pos] + new_field_line + content[insert_pos:]

        # Fallback: insert before the closing } of inputs block
        return self._add_field_to_inputs_end(content, field_name, new_value)

    def _has_datadog_params(self, content: str, field_mapping: Dict[str, str]) -> bool:
        """
        Check if any Datadog parameters are present in content or being inserted via field_mapping.

        Args:
            content: HCL content string
            field_mapping: Dict of field names to new values being inserted

        Returns:
            True if any Datadog parameters are found, False otherwise
        """
        # Check if any datadog params are in the field_mapping (being inserted)
        if any(param in field_mapping for param in DATADOG_PARAMS):
            return True

        # Check if any datadog params already exist in the content
        for param in DATADOG_PARAMS:
            if re.search(rf'\b{param}\s*=', content):
                return True

        return False

    def _ensure_datadog_dependency(self, content: str) -> str:
        """
        Ensure dependency "datadog_api_key" block exists in HCL content.
        If missing, insert it before the 'dependencies {' block.
        Uses simple pattern matching to avoid content corruption.

        Args:
            content: HCL content string

        Returns:
            Updated HCL content with datadog dependency block
        """
        # Check if datadog_api_key dependency already exists
        if re.search(r'dependency\s+"datadog_api_key"\s*\{', content):
            logger.info("datadog_api_key dependency already exists")
            return content  # Already exists

        logger.info("datadog_api_key dependency not found, inserting...")

        # Simple approach: find 'dependencies {' line and insert before it
        # This avoids complex nested brace matching which can corrupt content
        deps_block_pattern = r'(\n)(dependencies\s*\{)'
        match = re.search(deps_block_pattern, content)

        if match:
            insert_pos = match.start(1)
            logger.info("Inserting datadog dependency before dependencies block")
            return content[:insert_pos] + "\n\n" + DATADOG_DEPENDENCY_BLOCK + "\n" + content[insert_pos:]

        logger.warning("Could not find 'dependencies' block for insertion point")
        return content

    def _ensure_datadog_in_dependencies_paths(self, content: str) -> str:
        """
        Ensure '../../secrets/datadog-configs' is in the dependencies paths array.
        Uses simple pattern matching to avoid content corruption.

        Args:
            content: HCL content string

        Returns:
            Updated HCL content with datadog path in dependencies
        """
        datadog_path = '"../../secrets/datadog-configs"'

        # Check if already present in the paths array (not elsewhere in content)
        # Look specifically for it inside paths = [...]
        paths_array_match = re.search(r'paths\s*=\s*\[[^\]]*\]', content)
        if paths_array_match and '../../secrets/datadog-configs' in paths_array_match.group(0):
            logger.info("datadog-configs path already in dependencies paths array")
            return content

        logger.info("Adding datadog-configs to dependencies paths...")

        # Simple pattern: find paths array and insert before closing bracket
        # [^\]]+ matches everything inside the array (non-empty)
        pattern = r'(paths\s*=\s*\[[^\]]+)\]'

        def add_datadog_path(match):
            return match.group(1) + ', ' + datadog_path + ']'

        new_content = re.sub(pattern, add_datadog_path, content, count=1)

        if new_content == content:
            logger.warning("Could not find paths array to add datadog-configs")

        return new_content

    @staticmethod
    def _get_common_infra_path(environment: str) -> str:
        """
        Get common_infra dependency path based on environment.
        dev → ../common-dev-infra
        stage/staging/prod → ../common-infra

        Args:
            environment: Environment name (dev, staging, prod)

        Returns:
            Path for common_infra dependency
        """
        env_lower = environment.lower()
        if env_lower == "dev":
            return "../common-dev-infra"
        return "../common-infra"

    def _update_common_infra_paths(self, content: str, environment: str) -> str:
        """
        Update common_infra dependency paths based on environment.
        dev → ../common-dev-infra
        stage/prod → ../common-infra

        Args:
            content: HCL content
            environment: Environment name

        Returns:
            Updated HCL content
        """
        common_infra_path = self._get_common_infra_path(environment)

        # Pattern to match config_path inside dependency "common_infra" block
        # This specifically targets the common_infra dependency block
        common_infra_pattern = r'(dependency\s+"common_infra"\s*\{[^}]*config_path\s*=\s*)"[^"]*"'
        if re.search(common_infra_pattern, content, re.DOTALL):
            content = re.sub(
                common_infra_pattern,
                rf'\1"{common_infra_path}"',
                content,
                flags=re.DOTALL
            )
            logger.info(f"Updated common_infra config_path to: {common_infra_path}")
        else:
            logger.warning("Could not find common_infra dependency block to update config_path")

        # Also update the dependencies block paths
        # Replace "../common-infra" or "../common-dev-infra" with the correct path
        dependencies_pattern = r'(dependencies\s*\{[^}]*paths\s*=\s*\[[^\]]*)"\.\./(common-infra|common-dev-infra)"'
        if re.search(dependencies_pattern, content, re.DOTALL):
            content = re.sub(
                dependencies_pattern,
                rf'\1"{common_infra_path}"',
                content,
                flags=re.DOTALL
            )
            logger.info(f"Updated dependencies paths to use: {common_infra_path}")
        else:
            logger.debug("Dependencies block path pattern not found (may already be correct)")

        return content

    def _apply_priority_alarm_rules(self, content: str, environment: str, tenant: str) -> str:
        """
        Comment or uncomment priority alarm SNS topics based on environment.

        Prod: uncomment priority alarm SNS topics.
        Non-prod: comment out priority alarm SNS topics.
        """
        if not environment:
            logger.info("Skipping priority alarm rules - missing environment")
            return content

        env_lower = environment.lower()
        is_prod = env_lower == "prod"

        priority_alarm_fields = [
            "devops_p0_alarm_sns_topic_arn",
            "devops_p1_alarm_sns_topic_arn",
            "devs_p0_alarm_sns_topic_arn",
            "devs_p1_alarm_sns_topic_arn",
        ]

        if not is_prod:
            for field in priority_alarm_fields:
                pattern = rf'^(\s*)({field}\s*=\s*[^\n]+)$'
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(
                        pattern,
                        r'\1// \2',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.debug(f"Commented out {field}")

            logger.info(f"Commented out priority alarms (tenant={tenant}, env={environment})")
        else:
            for field in priority_alarm_fields:
                pattern = rf'^(\s*)(#|//)\s*({field}\s*=\s*[^\n]+)$'
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(
                        pattern,
                        r'\1\3',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.debug(f"Uncommented {field}")

            logger.info(f"Priority alarms enabled (tenant={tenant}, env={environment})")

        return content

    def _apply_environment_rules(
        self,
        content: str,
        environment: str,
        product_name: str = "",
        tenant: str = "",
        field_mapping: Optional[Dict[str, str]] = None,
        service_type: str = ""
    ) -> str:
        """
        Apply environment-based rules to HCL content.

        Rules applied (same for all tenants):
        - Priority alarms: prod enabled, non-prod commented.
        - capacity_provider_name: ops tools always enabled; stage/staging and prod+falcon commented.
        - create_alarms: prod true, non-prod false.
        - alb_arn_suffix: prod uncommented, non-prod commented.
        - enable_datadog_sidecar: dev false, stage/staging/prod true (skipped if explicit).
        """
        if not environment:
            logger.info("Skipping environment rules - missing environment")
            return content

        # Use raw environment for rules (normalization is only for path/branch logic).
        env_lower = environment.lower()
        product_lower = product_name.lower() if product_name else ""
        is_prod = env_lower == "prod"

        content = self._apply_priority_alarm_rules(content, environment, tenant)

        # capacity_provider_name handling.
        is_ops_tools = service_type == "OPS_TOOLS"
        should_comment_capacity_provider = (
            not is_ops_tools and (
                env_lower in ["stage", "staging"] or
                (env_lower == "prod" and product_lower == "falcon")
            )
        )

        if should_comment_capacity_provider:
            pattern = r'^(\s*)(capacity_provider_name\s*=\s*[^\n]+)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1// \2',
                    content,
                    flags=re.MULTILINE
                )
                logger.info(
                    "Commented out capacity_provider_name (tenant=%s, env=%s, product=%s)",
                    tenant, environment, product_name
                )
        else:
            pattern = r'^(\s*)(#|//)\s*(capacity_provider_name\s*=\s*[^\n]+)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1\3',
                    content,
                    flags=re.MULTILINE
                )
                logger.debug(
                    "Uncommented capacity_provider_name (tenant=%s, env=%s, product=%s, service_type=%s)",
                    tenant, environment, product_name, service_type
                )

        # create_alarms handling.
        if not is_prod:
            pattern = r'^(\s*)(create_alarms\s*=\s*)true(\s*)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1\2false\3',
                    content,
                    flags=re.MULTILINE
                )
                logger.info("Set create_alarms = false (env=%s)", environment)
        else:
            pattern = r'^(\s*)(create_alarms\s*=\s*)false(\s*)$'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(
                    pattern,
                    r'\1\2true\3',
                    content,
                    flags=re.MULTILINE
                )
                logger.debug("Set create_alarms = true (env=%s)", environment)

        # alb_arn_suffix handling for CloudWatch ALB metrics.
        if is_prod:
            alb_suffix_pattern = r'^(\s*)(//\s*)(alb_arn_suffix\s*=\s*[^\n]+)'
            if re.search(alb_suffix_pattern, content, re.MULTILINE):
                content = re.sub(alb_suffix_pattern, r'\1\3', content, flags=re.MULTILINE)
                logger.info("Uncommented alb_arn_suffix (env=%s, create_alarms=true)", environment)
        else:
            alb_suffix_uncommented = r'^(\s*)(alb_arn_suffix\s*=\s*[^\n]+)'
            match = re.search(alb_suffix_uncommented, content, re.MULTILINE)
            if match:
                line_start = content.rfind('\n', 0, match.start()) + 1
                line_prefix = content[line_start:match.start()]
                if '//' not in line_prefix:
                    content = re.sub(
                        alb_suffix_uncommented,
                        r'\1// \2',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.info("Commented out alb_arn_suffix (env=%s)", environment)

        # enable_datadog_sidecar handling when API does not provide a value.
        if not (field_mapping and "enable_datadog_sidecar" in field_mapping):
            is_dev = env_lower == "dev"

            if is_dev:
                pattern = r'^(\s*)(enable_datadog_sidecar\s*=\s*)true(\s*)$'
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(
                        pattern,
                        r'\1\2false\3',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.info("Set enable_datadog_sidecar = false (env=%s)", environment)
            else:
                pattern = r'^(\s*)(enable_datadog_sidecar\s*=\s*)false(\s*)$'
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(
                        pattern,
                        r'\1\2true\3',
                        content,
                        flags=re.MULTILINE
                    )
                    logger.info("Set enable_datadog_sidecar = true (env=%s)", environment)
        else:
            logger.info("Skipping enable_datadog_sidecar rule - API value provided")

        return content

    def _apply_http_scaling_alb_arn_rule(
        self,
        content: str,
        field_mapping: Dict[str, str]
    ) -> str:
        """
        Comment or uncomment existing_alb_arn based on http_scaling_enabled.
        """
        http_scaling_enabled = field_mapping.get("http_scaling_enabled") == "true"
        if http_scaling_enabled:
            alb_arn_commented_pattern = r'^(\s*)(//\s*)(existing_alb_arn\s*=\s*[^\n]+)'
            if re.search(alb_arn_commented_pattern, content, re.MULTILINE):
                content = re.sub(alb_arn_commented_pattern, r'\1\3', content, flags=re.MULTILINE)
                logger.info("Uncommented existing_alb_arn (http_scaling_enabled is true)")
        else:
            alb_arn_uncommented_pattern = r'^(\s*)(existing_alb_arn\s*=\s*[^\n]+)'
            if re.search(alb_arn_uncommented_pattern, content, re.MULTILINE):
                match = re.search(alb_arn_uncommented_pattern, content, re.MULTILINE)
                if match:
                    line_start = content.rfind('\n', 0, match.start()) + 1
                    line_prefix = content[line_start:match.start()]
                    if '//' not in line_prefix:
                        content = re.sub(
                            alb_arn_uncommented_pattern,
                            r'\1// \2',
                            content,
                            flags=re.MULTILINE
                        )
                        logger.info("Commented out existing_alb_arn (http_scaling_enabled is false)")

        return content

    def _ensure_capacity_provider_uncommented(
        self,
        content: str,
        tenant: str,
        environment: str,
        product_name: str
    ) -> str:
        """
        Ensure capacity_provider_name is uncommented for existing OPS_TOOLS services.
        """
        pattern = r'^(\s*)(#|//)\s*(capacity_provider_name\s*=\s*[^\n]+)$'
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(
                pattern,
                r'\1\3',
                content,
                flags=re.MULTILINE
            )
            logger.info(
                "Uncommented capacity_provider_name for existing OPS_TOOLS service "
                "(tenant=%s, env=%s, product=%s)",
                tenant, environment, product_name
            )

        return content

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """
        Sanitize name for file paths (replace consecutive spaces/underscores/hyphens with single hyphen).

        Args:
            name: Name to sanitize

        Returns:
            Sanitized name
        """
        import re
        return re.sub(r'[\s_-]+', '-', name).strip('-').lower()

    @staticmethod
    def _get_envs_folder_name(environment: str, tenant: str) -> str:
        """
        Get the envs folder name based on environment and tenant.

        For vance/aspora tenants in dev environment, uses 'envs-dev'.
        Otherwise uses 'envs'.

        Args:
            environment: Environment name (dev, staging, qa, prod)
            tenant: Tenant code

        Returns:
            Folder name ('envs' or 'envs-dev')
        """
        tenant_lower = tenant.lower() if tenant else ""
        env_lower = environment.lower()

        if tenant_lower in VANCE_ASPORA_TENANTS and env_lower == "dev":
            return "envs-dev"
        return "envs"

    @staticmethod
    def _get_service_name_for_env_files(service_name: str) -> str:
        """
        Get service name with -service suffix for env file paths.

        Args:
            service_name: Service name (sanitized)

        Returns:
            Service name with -service suffix
        """
        if service_name.endswith("-service"):
            return service_name
        return f"{service_name}-service"

    def _uncomment_env_file_paths(
        self,
        content: str,
        service_name: str,
        environment: str,
        tenant: str,
        product_name: str,
        service_type: str
    ) -> str:
        """
        Uncomment and update service_container_secrets and service_container_configs paths.
        Skip for Falcon services (tenant-specific rule).
        Handle OPS_TOOLS differently (dev-tools shared folder).

        Args:
            content: HCL content
            service_name: Service name
            environment: Environment name
            tenant: Tenant code
            product_name: Product/application name
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Returns:
            Updated HCL content with env file paths uncommented
        """
        # Skip env file uncommenting for Falcon services (aspora/vance tenants)
        service_lower = (service_name or "").lower()
        falcon_services = {
            "falcon-api", "falcon-consumer", "falcon-worker",
            "falcon-api-dev-service", "falcon-consumer-dev-service", "falcon-worker-dev-service"
        }
        skip_env_file_uncomment = (
            (product_name or "").lower() == "falcon" and
            service_lower in falcon_services and
            (tenant or "").lower() in VANCE_ASPORA_TENANTS
        )

        if skip_env_file_uncomment:
            logger.info(
                f"Skipping env file uncommenting for Falcon service: "
                f"product={product_name}, service={service_name}, tenant={tenant}"
            )
            return content

        if not environment or not service_name:
            logger.info("Skipping env file uncommenting - missing environment or service_name")
            return content

        service_sanitized = self._sanitize_name(service_name)
        service_for_env = self._get_service_name_for_env_files(service_sanitized)
        envs_folder = self._get_envs_folder_name(environment, tenant or "")

        # Build the correct paths based on service_type
        # OPS_TOOLS: use dev-tools shared folder (../../envs/dev-tools/secure/{service}-secrets.json)
        # Standard: use service-specific folder (../../envs/{service}-service/secure/{service}-service-secrets.json)
        if service_type == "OPS_TOOLS":
            secrets_path = f"../../{envs_folder}/dev-tools/secure/{service_for_env}-secrets.json"
            configs_path = f"../../{envs_folder}/dev-tools/non-secure/{service_for_env}-configs.json"
        else:
            secrets_path = f"../../{envs_folder}/{service_for_env}/secure/{service_for_env}-secrets.json"
            configs_path = f"../../{envs_folder}/{service_for_env}/non-secure/{service_for_env}-configs.json"

        # Uncomment and update service_container_secrets
        # Pattern matches: # or // service_container_secrets = jsondecode(file("..."))
        secrets_pattern = r'(#|//)\s*service_container_secrets\s*=\s*jsondecode\(file\("[^"]*"\)\)'
        secrets_replacement = f'service_container_secrets = jsondecode(file("{secrets_path}"))'
        if re.search(secrets_pattern, content):
            content = re.sub(secrets_pattern, secrets_replacement, content)
            logger.info(f"Uncommented and updated service_container_secrets path to: {secrets_path}")
        else:
            # Check if already uncommented, update the path
            secrets_uncommented_pattern = r'(service_container_secrets\s*=\s*jsondecode\(file\(")[^"]*("\)\))'
            if re.search(secrets_uncommented_pattern, content):
                content = re.sub(secrets_uncommented_pattern, rf'\1{secrets_path}\2', content)
                logger.info(f"Updated service_container_secrets path to: {secrets_path}")

        # Uncomment and update service_container_configs
        # Pattern matches: # or // service_container_configs = jsondecode(file("..."))
        configs_pattern = r'(#|//)\s*service_container_configs\s*=\s*jsondecode\(file\("[^"]*"\)\)'
        configs_replacement = f'service_container_configs = jsondecode(file("{configs_path}"))'
        if re.search(configs_pattern, content):
            content = re.sub(configs_pattern, configs_replacement, content)
            logger.info(f"Uncommented and updated service_container_configs path to: {configs_path}")
        else:
            # Check if already uncommented, update the path
            configs_uncommented_pattern = r'(service_container_configs\s*=\s*jsondecode\(file\(")[^"]*("\)\))'
            if re.search(configs_uncommented_pattern, content):
                content = re.sub(configs_uncommented_pattern, rf'\1{configs_path}\2', content)
                logger.info(f"Updated service_container_configs path to: {configs_path}")

        return content

    def _replace_placeholders(
        self,
        content: str,
        service_name: str,
        service_type: str,
        parameters: Dict[str, Any]
    ) -> str:
        """
        Replace placeholders in template with actual values.

        Args:
            content: Template content
            service_name: Service name
            service_type: Service type
            parameters: Configuration parameters

        Returns:
            Updated content with replaced values
        """
        # Container configuration
        container_port = parameters.get('container_port', 5000)
        cpu = parameters.get('cpu', 1024)
        memory = parameters.get('memory', 2048)

        content = re.sub(
            r'container_port\s*=\s*\d+',
            f'container_port = {container_port}',
            content
        )

        content = re.sub(
            r'cpu\s*=\s*\d+',
            f'cpu = {cpu}',
            content
        )

        content = re.sub(
            r'memory\s*=\s*\d+',
            f'memory = {memory}',
            content
        )

        # ALB configuration (only for API and OPS_TOOLS)
        if service_type in ["API", "OPS_TOOLS"]:
            service_path = parameters.get('service_path', '/*')
            listener_rule_priority = parameters.get('listener_rule_priority', 50000)

            content = re.sub(
                r'service_path\s*=\s*"[^"]*"',
                f'service_path = "{service_path}"',
                content
            )

            content = re.sub(
                r'listener_rule_priority\s*=\s*\d+',
                f'listener_rule_priority = {listener_rule_priority}',
                content
            )

            # Health check
            health_check_path = parameters.get('health_check_path', '/')
            content = re.sub(
                r'health_check_path\s*=\s*"[^"]*"',
                f'health_check_path = "{health_check_path}"',
                content
            )

        # Autoscaling configuration
        enable_autoscaling = parameters.get('enable_autoscaling', True)
        desired_count = parameters.get('desired_count', 1)
        min_task_count = parameters.get('min_task_count', 1)
        max_task_count = parameters.get('max_task_count', 1)

        content = re.sub(
            r'enable_autoscaling\s*=\s*\w+',
            f'enable_autoscaling = {str(enable_autoscaling).lower()}',
            content
        )

        content = re.sub(
            r'desired_count\s*=\s*\d+',
            f'desired_count = {desired_count}',
            content
        )

        content = re.sub(
            r'min_task_count\s*=\s*\d+',
            f'min_task_count = {min_task_count}',
            content
        )

        content = re.sub(
            r'max_task_count\s*=\s*\d+',
            f'max_task_count = {max_task_count}',
            content
        )

        # Datadog sidecar configuration
        enable_datadog = parameters.get('enable_datadog_sidecar', False)
        datadog_log_source = parameters.get('datadog_log_source', 'java')

        content = re.sub(
            r'enable_datadog_sidecar\s*=\s*\w+',
            f'enable_datadog_sidecar = {str(enable_datadog).lower()}',
            content
        )

        content = re.sub(
            r'datadog_log_source\s*=\s*"[^"]*"',
            f'datadog_log_source = "{datadog_log_source}"',
            content
        )

        # OTel sidecar configuration
        enable_otel = parameters.get('enable_otel_sidecar', False)

        content = re.sub(
            r'enable_otel_sidecar\s*=\s*\w+',
            f'enable_otel_sidecar = {str(enable_otel).lower()}',
            content
        )

        return content

    def _preview_ecs_hcl(
        self,
        identifier: str,
        service_name: str,
        service_type: str,
        parameters: Dict[str, Any]
    ) -> str:
        """
        Render ECS Terragrunt preview for chat display.

        Returns a formatted HCL preview showing the ECS service configuration
        with user-configured values highlighted and auto-configured values noted.
        """
        if not identifier or not identifier.strip():
            raise ValueError("Identifier cannot be empty")

        if not service_name or not service_name.strip():
            raise ValueError("Service name cannot be empty")

        identifier = identifier.strip().lower().replace(" ", "-")
        service_name = service_name.strip()
        service_type = service_type.upper()
        alb_selection = parameters.get('alb_selection', 'existing_alb')

        # Get configuration values - map from config_snapshot field names to Terragrunt field names
        # Port: config uses 'port', terragrunt uses 'container_port'
        container_port = parameters.get('port') or parameters.get('container_port', 5000)
        try:
            container_port = int(container_port)
        except (ValueError, TypeError):
            container_port = 5000

        # CPU: config uses 'cpu' in vCPU, terragrunt needs CPU units (vCPU * 1024)
        cpu_raw = parameters.get('cpu', 1)
        try:
            cpu = int(float(cpu_raw) * 1024)
        except (ValueError, TypeError):
            cpu = 1024

        # Memory: config uses 'ram' in GB, terragrunt needs MB (GB * 1024)
        ram_raw = parameters.get('ram') or parameters.get('memory', 2)
        try:
            memory = int(float(ram_raw) * 1024)
        except (ValueError, TypeError):
            memory = 2048

        # Autoscaling: config uses nested 'autoscaling' object
        autoscaling = parameters.get('autoscaling', {})
        if isinstance(autoscaling, dict):
            enable_autoscaling = autoscaling.get('enabled', False)
            desired_count = autoscaling.get('desired', 1)
            min_task_count = autoscaling.get('min', 1)
            max_task_count = autoscaling.get('max', 1)
        else:
            enable_autoscaling = parameters.get('enable_autoscaling', False)
            desired_count = parameters.get('desired_count', 1)
            min_task_count = parameters.get('min_task_count', 1)
            max_task_count = parameters.get('max_task_count', 1)

        # Convert to int safely
        try:
            desired_count = int(desired_count) if desired_count else 1
            min_task_count = int(min_task_count) if min_task_count else 1
            max_task_count = int(max_task_count) if max_task_count else 1
        except (ValueError, TypeError):
            desired_count = min_task_count = max_task_count = 1

        # Datadog sidecar: extract from sidecar_config array
        enable_datadog = False
        datadog_sidecar_cpu = 256
        datadog_sidecar_memory = 512
        datadog_logs_enabled = False
        sidecar_config = parameters.get('sidecar_config', [])
        if sidecar_config:
            for sidecar in sidecar_config:
                sidecar_name = (sidecar.get('name') or '').lower()
                sidecar_code = (sidecar.get('sidecar_config_code') or '').lower()
                is_datadog = 'datadog' in sidecar_name or 'datadog' in sidecar_code
                if is_datadog and sidecar.get('enabled', False):
                    enable_datadog = True
                    try:
                        datadog_sidecar_cpu = int(sidecar.get('cpu', 256))
                        datadog_sidecar_memory = int(sidecar.get('ram', 512))
                    except (ValueError, TypeError):
                        pass
                    datadog_logs_enabled = sidecar.get('datadog_logs_enabled', False)
                    break

        # Fallback to direct parameters if not in sidecar_config
        if not enable_datadog:
            enable_datadog = parameters.get('enable_datadog_sidecar', False)

        # Datadog log source: extract base language from language_name
        language_name = parameters.get('language_name', '')
        if language_name:
            datadog_log_source = language_name.split()[0].lower()
        else:
            datadog_log_source = parameters.get('datadog_log_source', 'java')

        enable_otel = parameters.get('enable_otel_sidecar', False)

        # Build preview based on service type and alb_selection
        if service_type == "BACKGROUND_SERVICE":
            return self._preview_worker_hcl(
                identifier=identifier,
                service_name=service_name,
                container_port=container_port,
                cpu=cpu,
                memory=memory,
                enable_autoscaling=enable_autoscaling,
                desired_count=desired_count,
                min_task_count=min_task_count,
                max_task_count=max_task_count,
                enable_datadog=enable_datadog,
                datadog_log_source=datadog_log_source,
                enable_otel=enable_otel
            )
        if service_type == "OPS_TOOLS":
            return self._preview_ops_tools_hcl(
                identifier=identifier,
                service_name=service_name,
                container_port=container_port,
                cpu=cpu,
                memory=memory,
                enable_datadog=enable_datadog,
                datadog_log_source=datadog_log_source,
                enable_otel=enable_otel,
                datadog_sidecar_cpu=datadog_sidecar_cpu,
                datadog_sidecar_memory=datadog_sidecar_memory,
                datadog_logs_enabled=datadog_logs_enabled
            )

        # Health check path: config uses 'health', terragrunt uses 'health_check_path'
        health_check_path = parameters.get('health') or parameters.get('health_check_path', '/')

        # Listener rule priority
        listener_rule_priority = parameters.get('listener_rule_priority', 50000)
        try:
            listener_rule_priority = int(listener_rule_priority)
        except (ValueError, TypeError):
            listener_rule_priority = 50000

        # Service path
        service_path = parameters.get('service_path', '/*')

        # API services - check alb_selection
        if alb_selection == "create_new_alb":
            return self._preview_api_new_alb_hcl(
                identifier=identifier,
                service_name=service_name,
                container_port=container_port,
                cpu=cpu,
                memory=memory,
                alb_identifier=parameters.get('alb_identifier', f'{identifier}-alb'),
                service_path=service_path,
                listener_rule_priority=listener_rule_priority,
                health_check_path=health_check_path,
                enable_autoscaling=enable_autoscaling,
                desired_count=desired_count,
                min_task_count=min_task_count,
                max_task_count=max_task_count,
                enable_datadog=enable_datadog,
                datadog_log_source=datadog_log_source,
                enable_otel=enable_otel
            )

        # Default: existing_alb
        return self._preview_api_hcl(
            identifier=identifier,
            service_name=service_name,
            container_port=container_port,
            cpu=cpu,
            memory=memory,
            service_path=service_path,
            listener_rule_priority=listener_rule_priority,
            health_check_path=health_check_path,
            enable_autoscaling=enable_autoscaling,
            desired_count=desired_count,
            min_task_count=min_task_count,
            max_task_count=max_task_count,
            enable_datadog=enable_datadog,
            datadog_log_source=datadog_log_source,
            enable_otel=enable_otel,
            datadog_sidecar_cpu=datadog_sidecar_cpu,
            datadog_sidecar_memory=datadog_sidecar_memory,
            datadog_logs_enabled=datadog_logs_enabled
        )

    def _preview_api_hcl(
        self,
        identifier: str,
        service_name: str,
        container_port: int,
        cpu: int,
        memory: int,
        service_path: str,
        listener_rule_priority: int,
        health_check_path: str,
        enable_autoscaling: bool,
        desired_count: int,
        min_task_count: int,
        max_task_count: int,
        enable_datadog: bool,
        datadog_log_source: str,
        enable_otel: bool,
        datadog_sidecar_cpu: int = 256,
        datadog_sidecar_memory: int = 512,
        datadog_logs_enabled: bool = False
    ) -> str:
        """Generate preview for API service with ALB."""
        autoscaling_enabled = "true" if enable_autoscaling else "false"
        datadog_enabled = "true" if enable_datadog else "false"
        otel_enabled = "true" if enable_otel else "false"
        datadog_logs = "true" if datadog_logs_enabled else "false"

        preview = f"""ECS API Service Configuration:

inputs = {{
  # General
  identifier          = "{identifier}"
  service_name        = "{service_name}"

  # Container Config
  container_port      = {container_port}
  cpu                 = {cpu}
  memory              = {memory}

  # ALB Routing
  service_path        = "{service_path}"
  listener_rule_priority = {listener_rule_priority}
  health_check_path   = "{health_check_path}"

  # Autoscaling
  enable_autoscaling  = {autoscaling_enabled}
  desired_count       = {desired_count}
  min_task_count      = {min_task_count}
  max_task_count      = {max_task_count}

  # Monitoring
  enable_datadog_sidecar = {datadog_enabled}"""

        if enable_datadog:
            preview += f"""
  datadog_sidecar_cpu    = {datadog_sidecar_cpu}
  datadog_sidecar_memory  = {datadog_sidecar_memory}
  datadog_log_source      = "{datadog_log_source}"
  datadog_logs_enabled    = {datadog_logs}"""

        preview += f"""
  enable_otel_sidecar    = {otel_enabled}

  # Auto-configured from environment
  organization        = include.env.locals.organization
  env                 = include.env.locals.env
  region              = include.env.locals.region
  index               = include.env.locals.index
  deletion_protection = include.env.locals.deletion_protection
  tags                = include.env.locals.tags
}}"""

        return preview

    def _preview_api_new_alb_hcl(
        self,
        identifier: str,
        service_name: str,
        container_port: int,
        cpu: int,
        memory: int,
        alb_identifier: str,
        service_path: str,
        listener_rule_priority: int,
        health_check_path: str,
        enable_autoscaling: bool,
        desired_count: int,
        min_task_count: int,
        max_task_count: int,
        enable_datadog: bool,
        datadog_log_source: str,
        enable_otel: bool,
        datadog_sidecar_cpu: int = 256,
        datadog_sidecar_memory: int = 512,
        datadog_logs_enabled: bool = False
    ) -> str:
        """Generate preview for API service with new dedicated ALB."""
        autoscaling_enabled = "true" if enable_autoscaling else "false"
        datadog_enabled = "true" if enable_datadog else "false"
        otel_enabled = "true" if enable_otel else "false"
        datadog_logs = "true" if datadog_logs_enabled else "false"

        preview = f"""ECS API Service Configuration (with new dedicated ALB):

inputs = {{
  # General
  identifier          = "{identifier}"
  service_name        = "{service_name}"

  # Container Config
  container_port      = {container_port}
  cpu                 = {cpu}
  memory              = {memory}

  # ALB Configuration (new dedicated ALB)
  create_alb          = true
  alb_identifier      = "{alb_identifier}"

  # ALB Routing
  service_path        = "{service_path}"
  listener_rule_priority = {listener_rule_priority}
  health_check_path   = "{health_check_path}"

  # Autoscaling
  enable_autoscaling  = {autoscaling_enabled}
  desired_count       = {desired_count}
  min_task_count      = {min_task_count}
  max_task_count      = {max_task_count}

  # Monitoring
  enable_datadog_sidecar = {datadog_enabled}"""

        if enable_datadog:
            preview += f"""
  datadog_sidecar_cpu    = {datadog_sidecar_cpu}
  datadog_sidecar_memory  = {datadog_sidecar_memory}
  datadog_log_source      = "{datadog_log_source}"
  datadog_logs_enabled    = {datadog_logs}"""

        preview += f"""
  enable_otel_sidecar    = {otel_enabled}

  # Auto-configured from environment
  organization        = include.env.locals.organization
  env                 = include.env.locals.env
  region              = include.env.locals.region
  index               = include.env.locals.index
  deletion_protection = include.env.locals.deletion_protection
  tags                = include.env.locals.tags
}}"""

        return preview

    def _preview_worker_hcl(
        self,
        identifier: str,
        service_name: str,
        container_port: int,
        cpu: int,
        memory: int,
        enable_autoscaling: bool,
        desired_count: int,
        min_task_count: int,
        max_task_count: int,
        enable_datadog: bool,
        datadog_log_source: str,
        enable_otel: bool,
        datadog_sidecar_cpu: int = 256,
        datadog_sidecar_memory: int = 512,
        datadog_logs_enabled: bool = False
    ) -> str:
        """Generate preview for background worker service."""
        autoscaling_enabled = "true" if enable_autoscaling else "false"
        datadog_enabled = "true" if enable_datadog else "false"
        otel_enabled = "true" if enable_otel else "false"
        datadog_logs = "true" if datadog_logs_enabled else "false"

        preview = f"""ECS Background Worker Service Configuration:

inputs = {{
  # General
  identifier          = "{identifier}"
  service_name        = "{service_name}"

  # Container Config
  container_port      = {container_port}
  cpu                 = {cpu}
  memory              = {memory}

  # No ALB for background workers

  # Autoscaling
  enable_autoscaling  = {autoscaling_enabled}
  desired_count       = {desired_count}
  min_task_count      = {min_task_count}
  max_task_count      = {max_task_count}

  # Monitoring
  enable_datadog_sidecar = {datadog_enabled}"""

        if enable_datadog:
            preview += f"""
  datadog_sidecar_cpu    = {datadog_sidecar_cpu}
  datadog_sidecar_memory  = {datadog_sidecar_memory}
  datadog_log_source      = "{datadog_log_source}"
  datadog_logs_enabled    = {datadog_logs}"""

        preview += f"""
  enable_otel_sidecar    = {otel_enabled}

  # Auto-configured from environment
  organization        = include.env.locals.organization
  env                 = include.env.locals.env
  region              = include.env.locals.region
  index               = include.env.locals.index
  deletion_protection = include.env.locals.deletion_protection
  tags                = include.env.locals.tags
}}"""

        return preview

    def _preview_ops_tools_hcl(
        self,
        identifier: str,
        service_name: str,
        container_port: int,
        cpu: int,
        memory: int,
        enable_datadog: bool = False,
        datadog_log_source: str = "java",
        enable_otel: bool = False,
        datadog_sidecar_cpu: int = 256,
        datadog_sidecar_memory: int = 512,
        datadog_logs_enabled: bool = False
    ) -> str:
        """Generate preview for ops tools service."""
        datadog_enabled = "true" if enable_datadog else "false"
        otel_enabled = "true" if enable_otel else "false"
        datadog_logs = "true" if datadog_logs_enabled else "false"

        preview = f"""ECS Ops Tools Service Configuration:

inputs = {{
  # General
  identifier          = "{identifier}"
  service_name        = "{service_name}"

  # Container Config
  container_port      = {container_port}
  cpu                 = {cpu}
  memory              = {memory}

  # ALB Routing
  service_path        = "/*"
  listener_rule_priority = 100
  health_check_path   = "/"

  # Alarms disabled for ops tools
  create_alarms       = false

  # Monitoring
  enable_datadog_sidecar = {datadog_enabled}"""

        if enable_datadog:
            preview += f"""
  datadog_sidecar_cpu    = {datadog_sidecar_cpu}
  datadog_sidecar_memory  = {datadog_sidecar_memory}
  datadog_log_source      = "{datadog_log_source}"
  datadog_logs_enabled    = {datadog_logs}"""

        preview += f"""
  enable_otel_sidecar    = {otel_enabled}

  # Auto-configured from environment
  organization        = include.env.locals.organization
  env                 = include.env.locals.env
  region              = include.env.locals.region
  index               = include.env.locals.index
  deletion_protection = include.env.locals.deletion_protection
  tags                = include.env.locals.tags
}}"""

        return preview
