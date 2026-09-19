"""
Dockerfile Fetch Service

Handles fetching Dockerfiles from GitHub repositories and local templates.
"""
import os
import re
import logging
import aiofiles
from typing import Dict, Any, Optional, List, Set
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.github_integration import GitHubIntegration
from app.db.models.language_ref_model import LanguageRefModel
from app.db.models.service_config_model import ServiceConfigModel
from app.repository.service_config_repository import ServiceConfigRepository
from app.utils.language_helpers import is_java_language, is_go_language, is_python_language
from app.utils.dockerfile_transformer import generate_datadog_block, DEFAULT_DD_AGENT_VERSION

logger = logging.getLogger(__name__)


class DockerfileFetchService:
    """Service for fetching Dockerfiles from repositories and templates."""

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

    async def fetch_from_repository(
        self,
        repository: str,
        branch: str,
        dockerfile_path: str,
        github_token: str,
        github_base_url: str = "https://api.github.com"
    ) -> Dict[str, Any]:
        """
        Fetch Dockerfile content from a GitHub repository.

        Args:
            repository: Repository in format 'owner/repo'
            branch: Branch name
            dockerfile_path: Path to Dockerfile in repository
            github_token: GitHub authentication token
            github_base_url: GitHub API base URL

        Returns:
            Dict with content, metadata, and GitHub info

        Raises:
            ValueError: If repository format is invalid
            Exception: If file not found or API error
        """
        if not repository or "/" not in repository:
            raise ValueError("Repository must be in format 'owner/repo'")

        owner, repo = repository.split("/", 1)

        logger.info(f"Fetching Dockerfile from {repository}:{branch} at {dockerfile_path}")

        # Fetch file from GitHub
        file_data = await GitHubIntegration.get_file_content(
            token=github_token,
            base_url=github_base_url,
            owner=owner,
            repo=repo,
            file_path=dockerfile_path,
            branch=branch
        )

        if not file_data or not file_data.get("exists"):
            raise Exception(f"Dockerfile not found at {dockerfile_path} in {repository}:{branch}")

        return {
            "content": file_data.get("content", ""),
            "repository": repository,
            "branch": branch,
            "path": dockerfile_path,
            "sha": file_data.get("sha"),
            "url": file_data.get("url")
        }

    async def fetch_template(
        self,
        language_ref: LanguageRefModel
    ) -> Dict[str, Any]:
        """
        Fetch Dockerfile template from local templates directory.

        Args:
            language_ref: LanguageRefModel instance with name and version

        Returns:
            Dict with template content and metadata

        Raises:
            ValueError: If language not supported (only Java and Go)
            FileNotFoundError: If template file not found
        """
        language_name = language_ref.name
        language_version = language_ref.version

        # Determine language type
        is_java = is_java_language(language_name)
        is_go = is_go_language(language_name)
        is_python = is_python_language(language_name)

        if is_java:
            template_path = self.java_template_path
            template_name = "java-standard.Dockerfile"
            language_type = "java"
        elif is_go:
            template_path = self.go_template_path
            template_name = "go-standard.Dockerfile"
            language_type = "go"
        elif is_python:
            template_path = self.python_template_path
            template_name = "python-standard.Dockerfile"
            language_type = "python"
        else:
            raise ValueError(
                f"Unsupported language: {language_name}. "
                f"Only Java, Go, and Python templates are available."
            )

        logger.info(f"Loading {language_type} template from {template_path}")

        # Load template file
        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Template not found at {template_path}")

        async with aiofiles.open(template_path, 'r') as f:
            content = await f.read()

        return {
            "content": content,
            "language": language_type,
            "version": language_version,
            "template_name": template_name,
            "template_path": template_path
        }

    # Reserved ARG names that exist in standard templates (to prevent duplicates)
    RESERVED_JAVA_ARGS = {"JAR_FILE", "PROFILE"}
    RESERVED_GO_ARGS = {"AWS_SECRETS_MANAGER_NAME", "CONFIG_ENV"}
    RESERVED_PYTHON_ARGS = set()  # No reserved args for Python template

    def _generate_build_args_block(
        self,
        build_args: Optional[List[Dict[str, str]]],
        reserved_args: Optional[set] = None
    ) -> str:
        """
        Generate ARG declarations for custom build arguments.

        Args:
            build_args: List of dicts with 'name' (or 'key' for backward compat) and 'value' keys
            reserved_args: Set of ARG names already in the template (to skip duplicates)

        Returns:
            String with ARG declarations, or empty string if no build args
        """
        if not build_args:
            return ""

        reserved = reserved_args or set()
        lines = []
        seen_args = set()  # Track args we've already added (case-insensitive dedup)

        for arg in build_args:
            # Support both 'name' and 'key' for backward compatibility
            name = arg.get("name", "") or arg.get("key", "")
            if name:
                name = name.strip().upper()
                # Skip if already in template or already added
                if name in reserved or name in seen_args:
                    logger.warning(f"Skipping duplicate ARG: {name}")
                    continue
                seen_args.add(name)
                # Only add ARG declaration (values are passed at build time via --build-arg)
                lines.append(f"ARG {name}")

        return "\n".join(lines) + "\n" if lines else ""

    def _extract_existing_args(self, dockerfile_content: str) -> Set[str]:
        """
        Extract all existing ARG names from a Dockerfile.

        Args:
            dockerfile_content: Dockerfile content as string

        Returns:
            Set of ARG names (uppercase)
        """
        args = set()
        for line in dockerfile_content.split('\n'):
            # Match ARG declarations: ARG NAME or ARG NAME=value
            match = re.match(r'^\s*ARG\s+([A-Z_][A-Z0-9_]*)', line, re.IGNORECASE)
            if match:
                args.add(match.group(1).upper())
        return args

    def inject_build_args(
        self,
        dockerfile_content: str,
        build_args: Optional[List[Dict[str, str]]]
    ) -> str:
        """
        Inject custom build arguments into an existing Dockerfile.

        This method:
        1. Extracts all existing ARG declarations to avoid duplicates
        2. Finds the best insertion point (after FROM and existing ARGs)
        3. Injects new ARG declarations

        Args:
            dockerfile_content: Existing Dockerfile content
            build_args: List of build args to inject

        Returns:
            Modified Dockerfile content with new ARGs injected
        """
        if not build_args:
            return dockerfile_content

        # Extract existing ARGs to avoid duplicates
        existing_args = self._extract_existing_args(dockerfile_content)

        # Generate new ARG declarations (excluding duplicates)
        new_args_block = self._generate_build_args_block(build_args, existing_args)
        if not new_args_block:
            return dockerfile_content  # No new args to add

        # Find insertion point: after the last ARG or after FROM
        lines = dockerfile_content.split('\n')
        insertion_idx = 0
        last_arg_idx = -1
        from_idx = -1

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.upper().startswith('FROM '):
                from_idx = i
            elif stripped.upper().startswith('ARG '):
                last_arg_idx = i

        # Insert after last ARG if exists, otherwise after FROM
        if last_arg_idx >= 0:
            insertion_idx = last_arg_idx + 1
        elif from_idx >= 0:
            insertion_idx = from_idx + 1
        else:
            insertion_idx = 0  # Fallback to beginning

        # Insert the new ARGs block
        lines.insert(insertion_idx, new_args_block.rstrip())
        return '\n'.join(lines)

    def update_java_memory_settings(
        self,
        dockerfile_content: str,
        xms_mb: Optional[int] = None,
        xmx_mb: Optional[int] = None
    ) -> str:
        """
        Update or add xms/xmx values in an existing Dockerfile's JAVA_TOOL_OPTIONS.

        This method:
        1. Finds existing JAVA_TOOL_OPTIONS ENV line
        2. Updates existing -Xms/-Xmx values or adds them if not present
        3. If no JAVA_TOOL_OPTIONS exists, adds a new ENV line

        Args:
            dockerfile_content: Existing Dockerfile content
            xms_mb: Initial heap size in MB (e.g., 512)
            xmx_mb: Maximum heap size in MB (e.g., 1024)

        Returns:
            Modified Dockerfile content with updated memory settings
        """
        if not xms_mb and not xmx_mb:
            return dockerfile_content

        lines = dockerfile_content.split('\n')
        java_tool_options_idx = -1
        java_tool_options_line = ""

        # Find existing JAVA_TOOL_OPTIONS line
        for i, line in enumerate(lines):
            if re.match(r'^\s*ENV\s+JAVA_TOOL_OPTIONS\s*=', line, re.IGNORECASE):
                java_tool_options_idx = i
                java_tool_options_line = line
                break

        if java_tool_options_idx >= 0:
            # Update existing JAVA_TOOL_OPTIONS
            # Extract the value part
            match = re.match(r'^(\s*ENV\s+JAVA_TOOL_OPTIONS\s*=\s*)["\']?([^"\']*)["\']?\s*$', java_tool_options_line)
            if match:
                prefix = match.group(1)
                value = match.group(2)

                # Update or add -Xms
                if xms_mb:
                    if re.search(r'-Xms\d+[mgMG]?', value):
                        value = re.sub(r'-Xms\d+[mgMG]?', f'-Xms{xms_mb}m', value)
                    else:
                        value = f'-Xms{xms_mb}m {value}'

                # Update or add -Xmx
                if xmx_mb:
                    if re.search(r'-Xmx\d+[mgMG]?', value):
                        value = re.sub(r'-Xmx\d+[mgMG]?', f'-Xmx{xmx_mb}m', value)
                    else:
                        # Insert after -Xms if present, otherwise at beginning
                        if '-Xms' in value:
                            value = re.sub(r'(-Xms\d+[mgMG]?)', f'\\1 -Xmx{xmx_mb}m', value)
                        else:
                            value = f'-Xmx{xmx_mb}m {value}'

                # Clean up multiple spaces
                value = ' '.join(value.split())
                lines[java_tool_options_idx] = f'{prefix}"{value}"'
        else:
            # No JAVA_TOOL_OPTIONS found, add a new one
            # Find insertion point: before ENTRYPOINT/CMD or at end
            insertion_idx = len(lines)
            for i, line in enumerate(lines):
                stripped = line.strip().upper()
                if stripped.startswith('ENTRYPOINT') or stripped.startswith('CMD'):
                    insertion_idx = i
                    break

            # Build new JAVA_TOOL_OPTIONS
            opts_parts = []
            if xms_mb:
                opts_parts.append(f'-Xms{xms_mb}m')
            if xmx_mb:
                opts_parts.append(f'-Xmx{xmx_mb}m')
            opts_parts.append('-Dlogging.level.root=info')

            new_line = f'ENV JAVA_TOOL_OPTIONS="{" ".join(opts_parts)}"'
            lines.insert(insertion_idx, new_line)
            lines.insert(insertion_idx, '')  # Add blank line before

        return '\n'.join(lines)

    def update_existing_dockerfile(
        self,
        dockerfile_content: str,
        build_args: Optional[List[Dict[str, str]]] = None,
        xms_mb: Optional[int] = None,
        xmx_mb: Optional[int] = None
    ) -> str:
        """
        Update an existing Dockerfile with build args and memory settings.

        This is a convenience method that combines:
        - inject_build_args: Add new ARG declarations
        - update_java_memory_settings: Update -Xms/-Xmx in JAVA_TOOL_OPTIONS

        Args:
            dockerfile_content: Existing Dockerfile content
            build_args: Build arguments to inject
            xms_mb: Initial heap size in MB
            xmx_mb: Maximum heap size in MB

        Returns:
            Modified Dockerfile content
        """
        content = dockerfile_content

        # Inject build args (if any)
        if build_args:
            content = self.inject_build_args(content, build_args)

        # Update memory settings (if any)
        if xms_mb or xmx_mb:
            content = self.update_java_memory_settings(content, xms_mb, xmx_mb)

        return content

    async def generate_dockerfile(
        self,
        language_ref: LanguageRefModel,
        service_name: str,
        enable_datadog: bool = False,
        xms: Optional[int] = None,
        xmx: Optional[int] = None,
        advanced_options: Optional[List[Dict]] = None,
        port: Optional[str] = None,
        go_config_path: Optional[str] = None,
        use_aws_secrets: bool = False,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Generate Dockerfile with placeholders replaced.

        Args:
            language_ref: LanguageRefModel instance
            service_name: Service name for Datadog/documentation
            enable_datadog: Enable Datadog APM (Java only)
            xms: Java heap min in MB (Java only)
            xmx: Java heap max in MB (Java only)
            advanced_options: Datadog advanced options (Java only)
            port: Application port (Go only)
            go_config_path: Config file path (Go only)
            use_aws_secrets: Enable AWS Secrets Manager (Go only)
            build_args: Custom Docker build arguments (all languages)

        Returns:
            Dict with generated content and metadata

        Raises:
            ValueError: If language not supported
        """
        language_name = language_ref.name
        language_version = language_ref.version

        # Determine language type
        is_java = is_java_language(language_name)
        is_go = is_go_language(language_name)
        is_python = is_python_language(language_name)

        parameters_used = {
            "service_name": service_name,
            "language_version": language_version
        }

        if is_java:
            content = await self._generate_java_dockerfile(
                service_name=service_name,
                jdk_version=language_version or "21",
                enable_datadog=enable_datadog,
                xms_mb=xms,
                xmx_mb=xmx,
                advanced_options=advanced_options,
                build_args=build_args
            )
            parameters_used.update({
                "enable_datadog": enable_datadog,
                "xms": xms,
                "xmx": xmx,
                "advanced_options_count": len(advanced_options) if advanced_options else 0,
                "build_args_count": len(build_args) if build_args else 0
            })
            language_type = "java"

        elif is_go:
            content = await self._generate_go_dockerfile(
                service_name=service_name,
                port=port or "8080",
                go_config_path=go_config_path,
                use_aws_secrets=use_aws_secrets,
                build_args=build_args
            )
            parameters_used.update({
                "port": port or "8080",
                "go_config_path": go_config_path,
                "use_aws_secrets": use_aws_secrets,
                "build_args_count": len(build_args) if build_args else 0
            })
            language_type = "go"

        elif is_python:
            content = await self._generate_python_dockerfile(
                service_name=service_name,
                python_version=language_version or "3.12",
                port=port or "8000",
                build_args=build_args
            )
            parameters_used.update({
                "port": port or "8000",
                "build_args_count": len(build_args) if build_args else 0
            })
            language_type = "python"

        else:
            raise ValueError(
                f"Unsupported language: {language_name}. "
                f"Only Java, Go, and Python are supported for Dockerfile generation."
            )

        return {
            "content": content,
            "language": language_type,
            "version": language_version,
            "service_name": service_name,
            "parameters_used": parameters_used
        }

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
        """Generate Java Dockerfile content from template."""
        async with aiofiles.open(self.java_template_path, 'r') as f:
            template = await f.read()

        content = template.replace("{{JDK_VERSION}}", jdk_version)

        # Handle custom build arguments (skip reserved args already in Java template)
        build_args_block = self._generate_build_args_block(build_args, self.RESERVED_JAVA_ARGS)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

        # Base JAVA_TOOL_OPTIONS for logging
        base_java_tool_options = "-Dlogging.level.root=info"

        if enable_datadog:
            # Generate Datadog block
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
            # No Datadog - add JAVA_TOOL_OPTIONS with xms/xmx if provided
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
        """Generate Go Dockerfile content from template."""
        async with aiofiles.open(self.go_template_path, 'r') as f:
            template = await f.read()

        content = template

        # Handle custom build arguments (skip reserved args if AWS secrets enabled)
        reserved_args = self.RESERVED_GO_ARGS if use_aws_secrets else set()
        build_args_block = self._generate_build_args_block(build_args, reserved_args)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

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

        # Handle config path
        if go_config_path:
            config_type = "json" if go_config_path.endswith(".json") else "yaml"
            content = content.replace("{{CONFIG_PATH}}", f"/app/{go_config_path}")
            content = content.replace("{{CONFIG_TYPE}}", config_type)
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

    async def _generate_python_dockerfile(
        self,
        service_name: str,
        python_version: str,
        port: str = "8000",
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """Generate Python Dockerfile content from template."""
        async with aiofiles.open(self.python_template_path, 'r') as f:
            template = await f.read()

        content = template.replace("{{PYTHON_VERSION}}", python_version)
        content = content.replace("{{PORT}}", port)

        # Handle custom build arguments
        build_args_block = self._generate_build_args_block(build_args, self.RESERVED_PYTHON_ARGS)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", build_args_block)

        return content
