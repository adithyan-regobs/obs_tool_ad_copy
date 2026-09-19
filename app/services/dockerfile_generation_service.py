"""
Dockerfile Generation Service

Handles standardized Dockerfile generation for Java and Go services.
Creates Dockerfiles at docker/{service-name}/Dockerfile with optional Datadog support (Java)
or AWS Secrets Manager support (Go).
"""

import os
import logging
import aiofiles
from typing import Dict, Any, Optional, List

from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.language_ref_model import LanguageRefModel
from app.utils.language_helpers import (
    is_java_language,
    is_go_language,
    is_python_language,
    get_dockerfile_path,
    sanitize_service_name_for_path
)
from app.utils.dockerfile_helpers import (
    get_datadog_advanced_options,
    build_pr_body,
    determine_overall_status
)
from app.utils.github_sync_helpers import create_or_get_feature_branch
from app.utils.naming_helpers import build_dockerfile_feature_branch
from app.utils.dockerfile_transformer import (
    generate_datadog_block,
    DEFAULT_DD_AGENT_VERSION
)
from app.core.config import settings

logger = logging.getLogger(__name__)


class DockerfileGenerationService:
    """Service for generating standardized Dockerfiles for Java and Go services."""

    def __init__(self):
        self.java_template_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "templates", "dockerfiles", "java-standard.Dockerfile"
        )
        self.go_template_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "templates", "dockerfiles", "go-standard.Dockerfile"
        )
        self.python_template_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "templates", "dockerfiles", "python-standard.Dockerfile"
        )

    def should_generate_dockerfile(
        self,
        service_config: ServiceConfigModel,
        language_ref: Optional[LanguageRefModel]
    ) -> bool:
        """
        Check if Dockerfile should be generated.

        Returns True if:
        - generate_dockerfile flag is True in config
        - Language is Java or Go
        - Repository is configured
        - Branches list is non-empty
        """
        config = service_config.config or {}

        if not config.get("generate_dockerfile", False):
            logger.info("DOCKERFILE GENERATION CHECK: generate_dockerfile flag is False")
            return False

        if not language_ref:
            logger.warning("DOCKERFILE GENERATION CHECK FAILED: No language reference")
            return False

        is_java = is_java_language(language_ref.name)
        is_go = is_go_language(language_ref.name)
        is_python = is_python_language(language_ref.name)

        if not is_java and not is_go and not is_python:
            logger.warning(f"DOCKERFILE GENERATION CHECK FAILED: Language '{language_ref.name}' not supported (Java, Go, or Python required)")
            return False

        repository = config.get("repository")
        if not repository or "/" not in repository:
            logger.warning("DOCKERFILE GENERATION CHECK FAILED: No valid repository")
            return False

        branches = config.get("branches", [])
        if not branches:
            logger.warning("DOCKERFILE GENERATION CHECK FAILED: No branches")
            return False

        lang_name = 'Java' if is_java else ('Go' if is_go else 'Python')
        logger.info(f"DOCKERFILE GENERATION CHECK PASSED for {lang_name}")
        return True

    async def _load_java_template(self) -> str:
        """Load the Java Dockerfile template."""
        async with aiofiles.open(self.java_template_path, 'r') as f:
            return await f.read()

    async def _load_go_template(self) -> str:
        """Load the Go Dockerfile template."""
        async with aiofiles.open(self.go_template_path, 'r') as f:
            return await f.read()

    async def _load_python_template(self) -> str:
        """Load the Python Dockerfile template."""
        async with aiofiles.open(self.python_template_path, 'r') as f:
            return await f.read()

    def _is_datadog_enabled(self, service_config: ServiceConfigModel) -> bool:
        """Check if Datadog sidecar is enabled."""
        for sidecar in (service_config.sidecar_config or []):
            if not isinstance(sidecar, dict):
                continue
            name = sidecar.get("name", "").lower()
            code = sidecar.get("sidecar_config_code", "").lower()
            if ("datadog" in name or "datadog" in code) and sidecar.get("enabled"):
                return True
        return False

    def _generate_build_args_block(self, build_args: Optional[List[Dict[str, str]]]) -> str:
        """Generate ARG declarations for custom build arguments."""
        if not build_args:
            return ""

        lines = []
        for arg in build_args:
            name = arg.get("name", "").strip()
            if name:
                # Only add ARG declaration (values are passed at build time via --build-arg)
                lines.append(f"ARG {name}")

        return "\n".join(lines) + "\n" if lines else ""

    async def _generate_java_dockerfile_content(
        self,
        service_name: str,
        jdk_version: str,
        enable_datadog: bool = False,
        xms_mb: Optional[int] = None,
        xmx_mb: Optional[int] = None,
        advanced_options: Optional[List[Dict]] = None,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """Generate Java Dockerfile content from template."""
        template = await self._load_java_template()
        content = template.replace("{{JDK_VERSION}}", jdk_version)

        # Handle custom build args
        build_args_block = self._generate_build_args_block(build_args)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

        # Base JAVA_TOOL_OPTIONS for logging (will be merged with Datadog if enabled)
        base_java_tool_options = "-Dlogging.level.root=info"

        if enable_datadog:
            # Pass base options to merge with Datadog agent
            datadog_block = generate_datadog_block(
                service_name=service_name,
                xms_mb=xms_mb,
                xmx_mb=xmx_mb,
                dd_agent_version=DEFAULT_DD_AGENT_VERSION,
                existing_java_tool_options=base_java_tool_options,
                advanced_options=advanced_options
            )
            # Add chmod after ADD line
            lines = datadog_block.strip().split('\n')
            new_lines = []
            for line in lines:
                new_lines.append(line)
                if line.startswith("ADD ") and "dd-java-agent" in line:
                    new_lines.append("RUN chmod 644 /app/dd-java-agent.jar")
            datadog_block = '\n'.join(new_lines) + '\n\n'
            content = content.replace("{{DATADOG_BLOCK}}\n", datadog_block)
        else:
            # No Datadog - add base JAVA_TOOL_OPTIONS for logging
            java_tool_options_line = f'ENV JAVA_TOOL_OPTIONS="{base_java_tool_options}"\n\n'
            content = content.replace("{{DATADOG_BLOCK}}\n", java_tool_options_line)

        return content

    async def _generate_go_dockerfile_content(
        self,
        service_name: str,
        port: str,
        go_config_path: Optional[str] = None,
        use_aws_secrets: bool = False,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """Generate Go Dockerfile content from template."""
        template = await self._load_go_template()
        content = template

        # Handle AWS Secrets block
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

        # Handle custom build args
        build_args_block = self._generate_build_args_block(build_args)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

        # Handle config path - frontend passes full config path directly
        if go_config_path:
            # Determine config type from file extension
            config_type = "json" if go_config_path.endswith(".json") else "yaml"
            content = content.replace("{{CONFIG_PATH}}", f"/app/{go_config_path}")
            content = content.replace("{{CONFIG_TYPE}}", config_type)
            # Use the exact path provided by frontend
            config_copy_line = f"COPY {go_config_path} /app/{go_config_path}"
            content = content.replace("{{CONFIG_COPY_LINE}}", config_copy_line)
        else:
            # Default config path
            content = content.replace("{{CONFIG_PATH}}", "/app/configs/config.json")
            content = content.replace("{{CONFIG_TYPE}}", "json")
            content = content.replace("{{CONFIG_COPY_LINE}}", "")

        # Handle port
        content = content.replace("{{PORT}}", port or "8080")

        return content

    async def _generate_python_dockerfile_content(
        self,
        service_name: str,
        python_version: str,
        port: str = "8000",
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """Generate Python Dockerfile content from template."""
        template = await self._load_python_template()
        content = template.replace("{{PYTHON_VERSION}}", python_version)
        content = content.replace("{{PORT}}", port)

        # Handle custom build args
        build_args_block = self._generate_build_args_block(build_args)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

        return content

    async def generate_and_commit_dockerfile(
        self,
        service_config: ServiceConfigModel,
        service: ServicesMstModel,
        language_ref: LanguageRefModel,
        github_token: str,
        github_base_url: str,
        user_email: Optional[str] = None
    ) -> Dict[str, Any]:
        """Generate and commit standardized Dockerfile to repository."""
        from app.integrations.github_integration import GitHubIntegration

        config = service_config.config or {}
        repository = config.get("repository")
        branches = config.get("branches", [])
        build_path = config.get("build_path")

        if not repository or "/" not in repository:
            return {"status": "skipped", "message": "No valid repository", "branches": []}

        owner, repo = repository.split("/", 1)
        service_name = service.name

        # Determine language type
        is_java = is_java_language(language_ref.name)
        is_go = is_go_language(language_ref.name)
        is_python = is_python_language(language_ref.name)

        # Determine Dockerfile path
        # Java with build_path uses docker/{service-name}/Dockerfile
        # Go/Python always uses root Dockerfile (build_path is only for go build command)
        dockerfile_path = get_dockerfile_path(service_name, build_path, is_java)

        lang_name = 'Java' if is_java else ('Go' if is_go else 'Python')
        logger.info(f"=== DOCKERFILE GENERATION: {service_name} ===")
        logger.info(f"Path: {dockerfile_path}, Language: {lang_name}")

        # Get build_args from config (applies to all languages)
        build_args = config.get("build_args")

        # Generate content based on language
        if is_java:
            jdk_version = language_ref.version or "21"
            logger.info(f"Generating Java Dockerfile with JDK: {jdk_version}")

            # Check Datadog and get config
            enable_datadog = self._is_datadog_enabled(service_config)
            xms_mb = int(config.get("xms")) if config.get("xms") else None
            xmx_mb = int(config.get("xmx")) if config.get("xmx") else None
            advanced_options = get_datadog_advanced_options(service_config) if enable_datadog else None

            dockerfile_content = await self._generate_java_dockerfile_content(
                service_name, jdk_version, enable_datadog, xms_mb, xmx_mb, advanced_options, build_args
            )
        elif is_go:
            port = config.get("port", "8080")
            go_config_path = config.get("go_config_path")
            use_aws_secrets = config.get("go_use_aws_secrets", False)
            logger.info(f"Generating Go Dockerfile with port: {port}, config_path: {go_config_path}, aws_secrets: {use_aws_secrets}")

            dockerfile_content = await self._generate_go_dockerfile_content(
                service_name, port, go_config_path, use_aws_secrets, build_args
            )
        elif is_python:
            port = config.get("port", "8000")
            python_version = language_ref.version or "3.12"
            logger.info(f"Generating Python Dockerfile with version: {python_version}, port: {port}")

            dockerfile_content = await self._generate_python_dockerfile_content(
                service_name, python_version, port, build_args
            )
        else:
            return {"status": "skipped", "message": "Unsupported language", "branches": []}

        branch_results = []
        success_count = 0
        error_count = 0

        for branch in branches:
            result = await self._process_branch(
                GitHubIntegration, github_token, github_base_url,
                owner, repo, branch, dockerfile_path, dockerfile_content,
                service_name, user_email, language_type=lang_name.lower()
            )
            branch_results.append(result)
            if result["status"] == "success":
                success_count += 1
            elif result["status"] == "error":
                error_count += 1

        overall = determine_overall_status(branch_results, success_count, error_count, len(branches))
        overall["dockerfile_path"] = dockerfile_path
        return overall

    async def _process_branch(
        self,
        GitHubIntegration,
        github_token: str,
        github_base_url: str,
        owner: str,
        repo: str,
        branch: str,
        dockerfile_path: str,
        dockerfile_content: str,
        service_name: str,
        user_email: Optional[str],
        language_type: str = "java"
    ) -> Dict[str, Any]:
        """Process single branch for Dockerfile generation."""
        result = {
            "branch": branch,
            "status": "error",
            "error": None,
            "pr_url": None,
            "pr_number": None,
            "commit_sha": None
        }

        try:
            # Check if Dockerfile already exists on base branch
            try:
                existing = await GitHubIntegration.get_file_content(
                    token=github_token, base_url=github_base_url,
                    owner=owner, repo=repo, file_path=dockerfile_path, branch=branch
                )
                if existing and existing.get("content"):
                    logger.info(f"Dockerfile exists at {dockerfile_path} on {branch}")
                    result["status"] = "skipped"
                    result["error"] = f"Dockerfile already exists at {dockerfile_path}"
                    return result
            except Exception as e:
                if "404" not in str(e) and "Not Found" not in str(e):
                    raise

            # Build feature branch name (uses datadog/ prefix with timestamp)
            service_sanitized = sanitize_service_name_for_path(service_name)
            branch_sanitized = branch.replace("/", "-").replace("_", "-").lower()
            feature_branch = build_dockerfile_feature_branch(service_sanitized, branch_sanitized)

            # Create feature branch using existing helper
            branch_exists, error = create_or_get_feature_branch(
                GitHubIntegration, github_token, github_base_url,
                owner, repo, feature_branch, branch
            )
            if error:
                result["error"] = error
                return result

            # Create/update the file
            commit_msg = f"Add standardized Dockerfile for {service_name}"
            commit_result = await GitHubIntegration.update_or_create_file(
                token=github_token, base_url=github_base_url,
                owner=owner, repo=repo, branch=feature_branch,
                file_path=dockerfile_path, content=dockerfile_content, message=commit_msg
            )
            result["commit_sha"] = commit_result.get("commit_sha")

            # Check/create PR
            existing_pr = await GitHubIntegration.find_open_pr(
                token=github_token, base_url=github_base_url,
                owner=owner, repo=repo, head=feature_branch, base=branch
            )

            if existing_pr:
                result["status"] = "success"
                result["pr_url"] = existing_pr.get("html_url")
                result["pr_number"] = existing_pr.get("number")
            else:
                pr_title = f"[Dockerfile] Add standardized Dockerfile for {service_name}"
                pr_body = self._build_pr_body(service_name, branch, dockerfile_path, user_email, language_type)
                pr_result = await GitHubIntegration.create_pull_request(
                    token=github_token, base_url=github_base_url,
                    owner=owner, repo=repo, head=feature_branch, base=branch,
                    title=pr_title, body=pr_body, draft=False
                )
                result["status"] = "success"
                result["pr_url"] = pr_result.get("html_url")
                result["pr_number"] = pr_result.get("number")

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"Failed on branch {branch}: {e}")

        return result

    def _build_pr_body(
        self,
        service_name: str,
        base_branch: str,
        dockerfile_path: str,
        user_email: Optional[str],
        language_type: str = "java"
    ) -> str:
        """Build PR body for Dockerfile creation."""
        if language_type == "java":
            return f"""## Standardized Dockerfile Generation (Java)

**Service:** `{service_name}`
**Branch:** `{base_branch}`
**Dockerfile Path:** `{dockerfile_path}`

### What's Included
- Eclipse Temurin JDK base image
- JAR_FILE build argument for flexible JAR path
- PROFILE build argument for Spring profiles
- Datadog configuration (if sidecar enabled)

---
*Generated by {settings.app_name}*
*Requested by: {user_email or 'system'}*"""
        elif language_type == "go":
            return f"""## Standardized Dockerfile Generation (Go)

**Service:** `{service_name}`
**Branch:** `{base_branch}`
**Dockerfile Path:** `{dockerfile_path}`

### What's Included
- Debian Bullseye Slim base image
- AWS Secrets Manager support (if enabled)
- Config file path configuration
- CA certificates for HTTPS

---
*Generated by {settings.app_name}*
*Requested by: {user_email or 'system'}*"""
        else:  # python
            return f"""## Standardized Dockerfile Generation (Python)

**Service:** `{service_name}`
**Branch:** `{base_branch}`
**Dockerfile Path:** `{dockerfile_path}`

### What's Included
- Python slim base image
- pip install from requirements.txt
- Generic python main.py entrypoint
- Configurable port

---
*Generated by {settings.app_name}*
*Requested by: {user_email or 'system'}*"""
