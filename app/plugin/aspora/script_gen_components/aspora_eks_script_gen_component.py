"""
EKS/Helm Script Generation Component

Generates Kubernetes/EKS configuration files:
- config.yaml (service configuration in configs/{env}/)
- workflow YAML files (GitHub Actions workflows in .github/workflows/)

Uploads to S3 and updates database with S3 keys.
"""

import os
import json
import logging
import aiofiles
from typing import Dict, Any, Optional
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.repository.language_ref_repository import LanguageRefRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.timing import log_timing

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


class AsporaEksScriptGenComponent:
    """
    Component for generating EKS/Helm scripts for Aspora tenant.

    Responsibilities:
    - Load EKS config.yaml template
    - Replace placeholders with actual values
    - Generate preview YAML
    - Upload to S3 (original and preview)
    - Update database with S3 keys
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
        Generate EKS config.yaml configuration.

        This function:
        1. Checks if file exists in GitHub
        2. Load template and replace placeholders
        3. Generate preview YAML
        4. Upload to S3 if requested
        5. Return the final YAML content

        Args:
            parameters: Dictionary containing:
                - service_name: Service name (required)
                - language: Language (java, golang, nodejs, python)
                - java_version/go_version/nodejs_version/python_version: Language version
                - dockerfile_path: Dockerfile path
                - build_type: Build type (gradle, maven, docker-only)
                - skip_tests: Skip tests (default: true)
                - skip_checks: Skip checks (default: true)
                - cpu_requested/memory_requested: Resource allocation
                - cpu_limit/memory_limit: Resource limits
                - alb_schema: ALB schema (internet-facing/internal)
                - service_path: Service path
                - health: Health check path
                - secrets_enabled: Enable secrets
                - secret_keys: Secret key list
                - ebs_enabled: Enable EBS storage
                - ebs_size: EBS size in GB
                - deployment_strategy: Deployment strategy (rolling, recreate)
                - github_token: GitHub API token (required)
                - github_base_url: GitHub API base URL (required)
                - owner: GitHub repository owner (required)
                - repo: GitHub repository name (required)
                - base_branch: Base branch (required)
        Returns:
            Generated/updated YAML configuration content

        Raises:
            ValueError: If required parameters are missing
        """
        config_snapshot = queue_dict.get("config_snapshot") or {}

        # Extract required parameters
        service_name = config_snapshot.get('service_name')
        service_code = config_snapshot.get("services_mst_code") or config_snapshot.get("service_mst_code")
        repo_for_service = repository or self.repository
        if not service_name and service_code and repo_for_service and getattr(repo_for_service, "session", None):
            try:
                services_repo = ServicesMstRepository(repo_for_service.session)
                service = await services_repo.get_by_code(service_code)
                if service and service.name:
                    service_name = service.name
            except Exception as exc:
                logger.warning("Failed to resolve service name from service API: %s", exc)

        if service_name and not service_name.endswith('-service'):
            service_name = f"{service_name}-service"

        language_name = config_snapshot.get("language_name") or config_snapshot.get("language") or "java"
        language = config_snapshot.get("language") or language_name
        language_version = None
        language_ref_code = config_snapshot.get("language_ref_code")
        if language_ref_code and repo_for_service and getattr(repo_for_service, "session", None):
            try:
                language_repo = LanguageRefRepository(repo_for_service.session)
                language_ref = await language_repo.get_by_code(language_ref_code)
                if language_ref:
                    language_name = language_ref.name.lower()
                    language_version = language_ref.version
            except Exception as exc:
                logger.warning("Failed to resolve language ref for config generation: %s", exc)

        # Extract identifier for S3
        identifier = config_snapshot.get('identifier') or service_name
        environment = config_snapshot.get('environment') or queue_dict.get("environment") or ""

        if not identifier and file_location and file_location.file_path:
            identifier = os.path.basename(os.path.dirname(file_location.file_path))

        if not identifier:
            raise ValueError("Identifier cannot be empty")

        logger.info(f"Generating EKS configuration for: {file_location.file_path}")
        logger.info(f"  Service: {service_name} (language: {language})")

        parameters = dict(config_snapshot)
        if service_name:
            parameters["service_name"] = service_name
        if language_name:
            parameters["language_name"] = language_name
        if language_version:
            parameters["language_version"] = language_version

        # github_token = config_snapshot.get('github_token')
        # github_base_url = config_snapshot.get('github_base_url')

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

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

        gen_mode = "create" if not existing_file["exists"] else "update"
        gen_context = f"path={file_location.file_path} mode={gen_mode}"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            template_path = os.path.join(
                os.path.dirname(__file__),
                "../../../../templates/eks/config",
                "config.yaml"
            )

            try:
                async with aiofiles.open(template_path, "r") as f:
                    content = await f.read()
                logger.info(f"Loaded EKS template from: {template_path}")
            except FileNotFoundError:
                logger.error(f"Template not found: {template_path}")
                content = self._get_config_template_fallback()

            # Replace placeholders
            content = self._replace_placeholders(
                content,
                parameters,
                environment=environment,
                language_name=language_name,
                language_version=language_version
            )

            logger.info("EKS configuration generated successfully")

            # Generate preview YAML
            preview_yaml = self._preview_eks_yaml(
                service_name=service_name,
                parameters=parameters
            )
            logger.info("Generated EKS preview YAML")

        # Upload to S3 if requested
        original_s3_key = None
        preview_s3_key = None

        if upload_to_s3:
            try:
                # Upload original config.yaml
                original_s3_key = f"eks/{identifier}.yaml"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=content,
                    content_type="text/plain"
                )
                logger.info(f"Uploaded original YAML to S3: {result['location']}")

                # Upload preview YAML
                preview_s3_key = f"preview/eks/{identifier}.yaml"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_yaml,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "environment": environment,
                        "identifier": identifier,
                        "service_name": service_name,
                        "language": language,
                        "generated_by": "aspora_eks_script_gen_component"
                    }
                )
                logger.info(f"Uploaded preview YAML to S3: {result['location']}")

                # Update database with S3 keys (JSON format)
                if repository and queue_dict.get("code"):
                    artifact_s3_key_json = {
                        "original_s3_key": original_s3_key,
                        "preview": preview_s3_key
                    }
                    update_payload = json.dumps(artifact_s3_key_json)
                    await repository.update_artifact_s3_key(queue_dict.get("code"), update_payload)
                    self.logger.info(
                        "Saved artifact_s3_key to database for queue item %s: %s",
                        queue_dict.get("code"),
                        update_payload
                    )

            except Exception as e:
                logger.error(f"Failed to upload to S3: {e}", exc_info=True)
                raise

        if tenant and file_location and workflow_context:
            # Skip commit if in preview mode
            if not workflow_context.skip_commit:
                skip_commit = False
                existing_content = existing_file.get("content")
                if existing_file.get("exists") and existing_content:
                    skip_commit = should_skip_commit(existing_content, content)
                    if skip_commit:
                        logger.info(
                            "Config file unchanged on feature branch %s, skipping commit for %s",
                            feature_branch,
                            file_location.file_path
                        )

                if not skip_commit:
                    _upsert_staged_entry(
                        workflow_context=workflow_context,
                        repo=file_location.repo,
                        base_branch=base_branch,
                        feature_branch=file_location.feature_branch,
                        file_path=file_location.file_path,
                        content=content,
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

            if queue_dict.get("id"):
                script_gen_key = file_location.script_gen_key
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                    "original_content": content,
                    "preview_content": preview_yaml
                }

        return content

    def _replace_placeholders(
        self,
        content: str,
        parameters: Dict[str, Any],
        environment: str = "",
        language_name: str = "",
        language_version: Optional[str] = None
    ) -> str:
        """
        Replace placeholders in template with actual values.

        Args:
            content: Template content
            parameters: Parameters dictionary

        Returns:
            Content with placeholders replaced
        """
        service_name = parameters.get('service_name', '')
        config = parameters.get("config")
        if not isinstance(config, dict):
            config = parameters
        eks_build_config = config.get("eks_build_config") or {}

        if not language_name:
            language_name = parameters.get("language_name") or parameters.get("language") or "java"
        base_language, normalized_language, parsed_version = self._parse_language_name(language_name)

        # Version line based on language precedence
        version_line = self._get_version_line(
            config=config,
            normalized_language=normalized_language,
            parsed_version=parsed_version,
            language_version=language_version
        )

        # Dockerfile path
        dockerfile_path = config.get('dockerfile_path') or 'Dockerfile'

        # Build configuration
        build_type = eks_build_config.get("type") or self._get_default_build_type(base_language)
        env_lower = (environment or "").lower()
        is_go = normalized_language == "golang"
        is_prod = env_lower == "prod"
        if is_go and is_prod:
            skip_tests = str(eks_build_config.get("skip_tests", False)).lower()
            skip_checks = str(eks_build_config.get("skip_checks", False)).lower()
        else:
            skip_tests = str(eks_build_config.get("skip_tests", True)).lower()
            skip_checks = str(eks_build_config.get("skip_checks", True)).lower()

        # Generate Gradle/Maven config
        gradle_config = ""
        maven_config = ""
        if build_type == "gradle":
            gradle_config = self._generate_gradle_config(eks_build_config, config, env_lower)
        elif build_type == "maven":
            maven_config = self._generate_maven_config(eks_build_config, config, env_lower)

        # Replace all placeholders
        content = content.replace("{{SERVICE_NAME}}", service_name)
        content = content.replace("{{LANGUAGE}}", normalized_language)
        content = content.replace("{{VERSION_LINE}}", version_line)
        content = content.replace("{{DOCKERFILE_PATH}}", dockerfile_path)
        content = content.replace("{{BUILD_TYPE}}", build_type)
        content = content.replace("{{SKIP_TESTS}}", skip_tests)
        content = content.replace("{{SKIP_CHECKS}}", skip_checks)
        content = content.replace("{{GRADLE_CONFIG}}", gradle_config)
        content = content.replace("{{MAVEN_CONFIG}}", maven_config)

        return content

    def _parse_language_name(self, language_name: str) -> tuple[str, str, str]:
        """
        Parse language name to extract base language, normalized name, and version.
        """
        lang_lower = (language_name or "").lower().strip()

        if lang_lower.startswith("go") or lang_lower.startswith("golang"):
            import re
            version_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', lang_lower)
            version = version_match.group(1) if version_match else "1.24.0"
            return ("go", "golang", version)

        if "java" in lang_lower:
            import re
            if "maven" in lang_lower:
                base_lang = "java-maven"
            elif "gradle" in lang_lower:
                base_lang = "java-gradle"
            else:
                base_lang = "java"

            version_match = re.search(r'(\d+)', lang_lower)
            version = version_match.group(1) if version_match else "17"
            return (base_lang, "java", version)

        if "node" in lang_lower:
            import re
            version_match = re.search(r'(\d+)', lang_lower)
            version = version_match.group(1) if version_match else "20"
            return ("nodejs", "nodejs", version)

        if "python" in lang_lower:
            import re
            version_match = re.search(r'(\d+\.\d+)', lang_lower)
            version = version_match.group(1) if version_match else "3.11"
            return ("python", "python", version)

        return (lang_lower, lang_lower, "")

    def _get_default_build_type(self, base_language: str) -> str:
        """Get default build type based on language."""
        if base_language in ["go", "golang", "nodejs", "python"]:
            return "docker-only"
        if base_language == "java-maven":
            return "maven"
        if base_language in ["java", "java-gradle"]:
            return "gradle"
        return "docker-only"

    def _get_version_line(
        self,
        config: Dict[str, Any],
        normalized_language: str,
        parsed_version: str,
        language_version: Optional[str] = None
    ) -> str:
        """Generate version line based on precedence."""
        if normalized_language == "java":
            java_version = language_version or config.get("java_version") or parsed_version or "17"
            return f'java_version: "{java_version}"'
        if normalized_language == "golang":
            go_version = language_version or config.get("go_version") or parsed_version or "1.24"
            go_version = f"{go_version}.0"
            return f'go_version: "{go_version}"'
        if normalized_language == "nodejs":
            nodejs_version = language_version or config.get("nodejs_version") or parsed_version or "20"
            return f'nodejs_version: "{nodejs_version}"'
        if normalized_language == "python":
            python_version = language_version or config.get("python_version") or parsed_version or "3.11"
            return f'python_version: "{python_version}"'
        return ""

    def _generate_gradle_config(
        self,
        eks_build_config: dict,
        config: dict = None,
        environment: str = None
    ) -> str:
        """Generate Gradle build configuration block."""
        config = config or {}

        build_path = config.get("build_path") or ""
        default_jar = f"{build_path}/build/libs/*.jar" if build_path else "build/libs/*.jar"

        jar_file = (eks_build_config.get("gradle_jar_file") or eks_build_config.get("jar_file") or
                   config.get("gradle_jar_file") or config.get("jar_file") or default_jar)
        tasks = (eks_build_config.get("gradle_tasks") or eks_build_config.get("tasks") or
                config.get("gradle_tasks") or config.get("tasks") or "assemble")
        jvm_args = (eks_build_config.get("gradle_jvm_args") or eks_build_config.get("jvm_args") or
                   config.get("gradle_jvm_args") or config.get("jvm_args") or "-Xmx4g -XX:+UseG1GC")
        workers_max = (eks_build_config.get("gradle_workers_max") or eks_build_config.get("workers_max") or
                      config.get("gradle_workers_max") or config.get("workers_max") or 4)

        xms_raw = (eks_build_config.get("gradle_xms") or eks_build_config.get("xms") or
                  config.get("gradle_xms") or config.get("xms"))
        xmx_raw = (eks_build_config.get("gradle_xmx") or eks_build_config.get("xmx") or
                  config.get("gradle_xmx") or config.get("xmx"))

        if environment in ["staging", "stage", "qa"]:
            profile = "stg"
        elif environment == "dev":
            profile = "dev"
        else:
            profile = "prod"

        config_lines = [
            "  gradle:",
            f"    jar_file: {jar_file}",
            f"    tasks: {tasks}",
            f'    jvm_args: "{jvm_args}"',
            f"    workers_max: {workers_max}",
            f"    profile: {profile}"
        ]

        if xms_raw:
            xms = xms_raw if xms_raw.endswith('m') else f"{xms_raw}m"
            config_lines.append(f"    xms: {xms}")
        if xmx_raw:
            xmx = xmx_raw if xmx_raw.endswith('m') else f"{xmx_raw}m"
            config_lines.append(f"    xmx: {xmx}")

        return "\n".join(config_lines) + "\n"

    def _generate_maven_config(
        self,
        eks_build_config: dict,
        config: dict = None,
        environment: str = None
    ) -> str:
        """Generate Maven build configuration block."""
        config = config or {}

        build_path = config.get("build_path") or ""
        default_jar = f"{build_path}/target/*.jar" if build_path else "target/*.jar"

        jar_file = (eks_build_config.get("maven_jar_file") or eks_build_config.get("jar_file") or
                   config.get("maven_jar_file") or config.get("jar_file") or default_jar)
        goals = (eks_build_config.get("maven_goals") or eks_build_config.get("goals") or
                config.get("maven_goals") or config.get("goals") or "clean package")

        repo_id = "github"
        repo_name = "java-commons"
        repo_url = "https://maven.pkg.github.com/Vance-Club/java-commons"

        if environment in ["staging", "stage", "qa"]:
            profile = "stg"
        elif environment == "dev":
            profile = "dev"
        else:
            profile = "prod"

        xms_raw = (eks_build_config.get("maven_xms") or eks_build_config.get("xms") or
                  config.get("maven_xms") or config.get("xms"))
        xmx_raw = (eks_build_config.get("maven_xmx") or eks_build_config.get("xmx") or
                  config.get("maven_xmx") or config.get("xmx"))

        config_lines = [
            "  maven:",
            f"    jar_file: {jar_file}",
            f'    goals: "{goals}"',
            f"    repo_id: {repo_id}",
            f"    repo_name: {repo_name}",
            f"    repo_url: {repo_url}",
            f"    profile: {profile}"
        ]

        if xms_raw:
            xms = xms_raw if xms_raw.endswith('m') else f"{xms_raw}m"
            config_lines.append(f"    xms: {xms}")
        if xmx_raw:
            xmx = xmx_raw if xmx_raw.endswith('m') else f"{xmx_raw}m"
            config_lines.append(f"    xmx: {xmx}")

        return "\n".join(config_lines) + "\n"

    def _generate_resource_allocation_config(self, config: Dict[str, Any]) -> str:
        """Generate resource allocation configuration block."""
        config_lines = ["resources:"]

        cpu_requested = config.get("cpu_requested") or config.get("cpu")
        cpu_limit = config.get("cpu_limit") or config.get("cpu")
        if cpu_requested:
            cpu_req_str = str(cpu_requested)
            cpu_req = cpu_req_str if cpu_req_str.endswith("m") else f"{int(float(cpu_req_str) * 1000)}m"
            config_lines.append(f"  cpu_requested: \"{cpu_req}\"")
        if cpu_limit:
            cpu_lim_str = str(cpu_limit)
            cpu_lim = cpu_lim_str if cpu_lim_str.endswith("m") else f"{int(float(cpu_lim_str) * 1000)}m"
            config_lines.append(f"  cpu_limit: \"{cpu_lim}\"")

        memory_requested = config.get("memory_requested") or config.get("ram")
        memory_limit = config.get("memory_limit") or config.get("ram")
        if memory_requested:
            mem_req_str = str(memory_requested)
            mem_req = mem_req_str if mem_req_str.endswith(('Mi', 'Gi', 'MB', 'GB')) else f"{int(float(mem_req_str) * 1024)}Mi"
            config_lines.append(f"  memory_requested: \"{mem_req}\"")
        if memory_limit:
            mem_lim_str = str(memory_limit)
            mem_lim = mem_lim_str if mem_lim_str.endswith(('Mi', 'Gi', 'MB', 'GB')) else f"{int(float(mem_lim_str) * 1024)}Mi"
            config_lines.append(f"  memory_limit: \"{mem_lim}\"")

        container_port = config.get("container_port") or config.get("port")
        if container_port:
            config_lines.append(f"  container_port: {container_port}")

        replica_count = config.get("replica_count")
        if replica_count:
            config_lines.append(f"  replica_count: {replica_count}")

        hpa_config = config.get("hpa")
        if hpa_config and hpa_config.get("enabled"):
            config_lines.append("  hpa:")
            config_lines.append("    enabled: true")
            if hpa_config.get("min_replicas"):
                config_lines.append(f"    min_replicas: {hpa_config.get('min_replicas')}")
            if hpa_config.get("max_replicas"):
                config_lines.append(f"    max_replicas: {hpa_config.get('max_replicas')}")
        else:
            config_lines.append("  hpa:")
            config_lines.append("    enabled: false")

        ebs_enabled = config.get("ebs_enabled") or config.get("ebs_storage_enabled")
        if ebs_enabled:
            config_lines.append("  ebs:")
            config_lines.append("    enabled: true")
            ebs_size = config.get("ebs_size") or config.get("ebs_storage_size")
            ebs_mount_path = config.get("ebs_mount_path") or config.get("ebs_storage_mount_path")
            if ebs_size:
                config_lines.append(f"    size: \"{ebs_size}\"")
            if ebs_mount_path:
                config_lines.append(f"    mount_path: \"{ebs_mount_path}\"")
        else:
            config_lines.append("  ebs:")
            config_lines.append("    enabled: false")

        return "\n".join(config_lines) + "\n"

    def _generate_service_configuration_config(self, config: Dict[str, Any]) -> str:
        """Generate service configuration block."""
        config_lines = ["service:"]

        alb_schema = config.get("alb_schema")
        if alb_schema:
            config_lines.append(f"  alb_schema: \"{alb_schema}\"")

        health_endpoint = config.get("health_endpoint") or config.get("health_check_path")
        if health_endpoint:
            config_lines.append(f"  health_endpoint: \"{health_endpoint}\"")

        service_path = config.get("service_path") or config.get("path_pattern")
        if service_path:
            config_lines.append(f"  service_path: \"{service_path}\"")

        secrets_enabled = config.get("secrets_enabled")
        if secrets_enabled:
            config_lines.append("  secrets:")
            config_lines.append("    enabled: true")
            secret_keys = config.get("secret_keys")
            if secret_keys:
                if isinstance(secret_keys, str):
                    keys_list = [k.strip() for k in secret_keys.split(",") if k.strip()]
                else:
                    keys_list = secret_keys
                if keys_list:
                    config_lines.append("    keys:")
                    for key in keys_list:
                        config_lines.append(f"      - \"{key}\"")
        else:
            config_lines.append("  secrets:")
            config_lines.append("    enabled: false")

        return "\n".join(config_lines) + "\n"

    def _generate_deployment_strategy_config(self, deployment_strategy: dict = None) -> str:
        """Generate deployment strategy configuration block."""
        if not deployment_strategy:
            return "deployment:\n  strategy: rolling\n  rolling:\n    maxSurge: \"25%\"\n    maxUnavailable: \"25%\"\n"

        strategy = deployment_strategy.get("strategy", "rolling")
        config_lines = ["deployment:", f"  strategy: {strategy}"]

        if strategy == "canary":
            canary_config = deployment_strategy.get("canary", {})
            config_lines.append("  canary:")
            config_lines.append(f"    canaryService: {canary_config.get('canaryService', 'service-canary')}")
            config_lines.append(f"    stableService: {canary_config.get('stableService', 'service-stable')}")
            config_lines.append(f"    analysisEnabled: {str(canary_config.get('analysisEnabled', False)).lower()}")

            steps = canary_config.get("steps", [])
            if steps:
                config_lines.append("    steps:")
                for step in steps:
                    config_lines.append(f"      - weight: {step.get('weight', 0)}")
                    config_lines.append(f"        pauseDuration: {step.get('pauseDuration', 30)}")
                    config_lines.append(f"        pauseType: {step.get('pauseType', 'duration')}")

        elif strategy == "bluegreen":
            bluegreen_config = deployment_strategy.get("blueGreen", {})
            config_lines.append("  blueGreen:")
            config_lines.append(f"    activeService: {bluegreen_config.get('activeService', 'service-active')}")
            config_lines.append(f"    previewService: {bluegreen_config.get('previewService', 'service-preview')}")
            config_lines.append(f"    autoPromote: {str(bluegreen_config.get('autoPromote', False)).lower()}")
            config_lines.append(f"    scaleDownDelay: {bluegreen_config.get('scaleDownDelay', 30)}")
            config_lines.append(f"    previewReplicas: {bluegreen_config.get('previewReplicas', 1)}")

        elif strategy == "rolling":
            rolling_config = deployment_strategy.get("rolling", {})
            config_lines.append("  rolling:")
            config_lines.append(f"    maxSurge: \"{rolling_config.get('maxSurge', '25%')}\"")
            config_lines.append(f"    maxUnavailable: \"{rolling_config.get('maxUnavailable', '25%')}\"")

        elif strategy == "recreate":
            config_lines.append("  recreate:")
            config_lines.append("    # Recreate strategy: terminates all pods before creating new ones")
            config_lines.append("    terminationGracePeriodSeconds: 30")

        return "\n".join(config_lines) + "\n"

    def _get_config_template_fallback(self) -> str:
        """Fallback config.yaml template if file not found."""
        return """# Service name (identifier) - used to derive ECR repo, EKS deployment names, etc.
service_name: {{SERVICE_NAME}}

language: {{LANGUAGE}}
{{VERSION_LINE}}
# aws_region: [ ap-south-1, eu-west-2, us-east-1 ]

# Path to Dockerfile (relative to repo root)
dockerfile_path: {{DOCKERFILE_PATH}}

# Build configuration
build:
  type: {{BUILD_TYPE}}
  skip_tests: {{SKIP_TESTS}}
  skip_checks: {{SKIP_CHECKS}}
{{GRADLE_CONFIG}}{{MAVEN_CONFIG}}"""

    def _preview_eks_yaml(
        self,
        service_name: str,
        parameters: Dict[str, Any]
    ) -> str:
        """
        Generate preview YAML for display.

        Args:
            service_name: Service name
            parameters: Configuration parameters

        Returns:
            Preview YAML content
        """
        language = parameters.get("language") or "java"
        language_name = parameters.get("language_name") or language
        config = parameters.get("config")
        if not isinstance(config, dict):
            config = parameters
        eks_build_config = config.get("eks_build_config") or {}

        preview_lines = []
        preview_lines.append("# EKS Service Configuration Preview")
        preview_lines.append(f"# Service: {service_name}")
        preview_lines.append(f"# Language: {language}")
        preview_lines.append("")
        preview_lines.append("inputs = {")
        preview_lines.append(f'  service_name     = "{service_name}"')
        preview_lines.append(f'  language         = "{language}"')

        # Add version
        _, normalized_language, parsed_version = self._parse_language_name(language_name)
        version_line = self._get_version_line(
            config=config,
            normalized_language=normalized_language,
            parsed_version=parsed_version,
            language_version=parameters.get("language_version")
        )
        if version_line:
            # Remove quotes from version line for preview
            version_line_clean = version_line.replace('"', '').replace(': ', ' = ')
            preview_lines.append(f'  {version_line_clean}')

        # Add dockerfile path
        dockerfile_path = config.get('dockerfile_path') or 'Dockerfile'
        preview_lines.append(f'  dockerfile_path  = "{dockerfile_path}"')

        # Add build type
        base_language, _, _ = self._parse_language_name(language_name)
        build_type = eks_build_config.get("type") or self._get_default_build_type(base_language)
        preview_lines.append(f'  build_type       = "{build_type}"')

        preview_lines.append("}")

        return "\n".join(preview_lines)
