"""
Dockerfile Generation Service

Handles standardized Dockerfile generation for Java services.
Creates Dockerfiles at docker/{service-name}/Dockerfile with optional Datadog support.
"""

import os
import logging
from typing import Dict, Any, Optional, List

from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.language_ref_model import LanguageRefModel
from app.utils.language_helpers import (
    is_java_language,
    get_dockerfile_path,
    sanitize_service_name_for_path
)
from app.utils.dockerfile_helpers import (
    get_datadog_advanced_options,
    build_pr_body,
    determine_overall_status
)
from app.utils.github_sync_helpers import create_or_get_feature_branch
from app.utils.dockerfile_transformer import (
    generate_datadog_block,
    DEFAULT_DD_AGENT_VERSION
)
from app.core.config import settings

logger = logging.getLogger(__name__)


def build_dockerfile_generation_feature_branch(
    service_sanitized: str,
    branch_sanitized: str
) -> str:
    """Build feature branch name for Dockerfile generation."""
    return f"dockerfile/{service_sanitized}-{branch_sanitized}"


class DockerfileGenerationService:
    """Service for generating standardized Dockerfiles for Java services."""

    def __init__(self):
        self.template_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "templates", "dockerfiles", "java-standard.Dockerfile"
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
        - Language is Java
        - Repository is configured
        - Branches list is non-empty
        """
        config = service_config.config or {}

        if not config.get("generate_dockerfile", False):
            logger.info("DOCKERFILE GENERATION CHECK: generate_dockerfile flag is False")
            return False

        if not language_ref or not is_java_language(language_ref.name):
            logger.warning(f"DOCKERFILE GENERATION CHECK FAILED: Not Java language")
            return False

        repository = config.get("repository")
        if not repository or "/" not in repository:
            logger.warning("DOCKERFILE GENERATION CHECK FAILED: No valid repository")
            return False

        branches = config.get("branches", [])
        if not branches:
            logger.warning("DOCKERFILE GENERATION CHECK FAILED: No branches")
            return False

        logger.info("DOCKERFILE GENERATION CHECK PASSED")
        return True

    def _load_template(self) -> str:
        """Load the Dockerfile template."""
        with open(self.template_path, 'r') as f:
            return f.read()

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

    def _generate_dockerfile_content(
        self,
        service_name: str,
        jdk_version: str,
        enable_datadog: bool = False,
        xms_mb: Optional[int] = None,
        xmx_mb: Optional[int] = None,
        advanced_options: Optional[List[Dict]] = None
    ) -> str:
        """Generate Dockerfile content from template."""
        template = self._load_template()
        content = template.replace("{{JDK_VERSION}}", jdk_version)

        if enable_datadog:
            datadog_block = generate_datadog_block(
                service_name=service_name,
                xms_mb=xms_mb,
                xmx_mb=xmx_mb,
                dd_agent_version=DEFAULT_DD_AGENT_VERSION,
                existing_java_tool_options=None,
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
            content = content.replace("{{DATADOG_BLOCK}}\n", "")

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
        jdk_version = language_ref.version or "21"

        # Determine Dockerfile path
        is_java = is_java_language(language_ref.name)
        dockerfile_path = get_dockerfile_path(service_name, build_path, is_java)

        logger.info(f"=== DOCKERFILE GENERATION: {service_name} ===")
        logger.info(f"Path: {dockerfile_path}, JDK: {jdk_version}")

        # Check Datadog and get config
        enable_datadog = self._is_datadog_enabled(service_config)
        xms_mb = int(config.get("xms")) if config.get("xms") else None
        xmx_mb = int(config.get("xmx")) if config.get("xmx") else None
        advanced_options = get_datadog_advanced_options(service_config) if enable_datadog else None

        # Generate content
        dockerfile_content = self._generate_dockerfile_content(
            service_name, jdk_version, enable_datadog, xms_mb, xmx_mb, advanced_options
        )

        branch_results = []
        success_count = 0
        error_count = 0

        for branch in branches:
            result = self._process_branch(
                GitHubIntegration, github_token, github_base_url,
                owner, repo, branch, dockerfile_path, dockerfile_content,
                service_name, user_email
            )
            branch_results.append(result)
            if result["status"] == "success":
                success_count += 1
            elif result["status"] == "error":
                error_count += 1

        overall = determine_overall_status(branch_results, success_count, error_count, len(branches))
        overall["dockerfile_path"] = dockerfile_path
        return overall

    def _process_branch(
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
        user_email: Optional[str]
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
                existing = GitHubIntegration.get_file_content(
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

            # Build feature branch name
            service_sanitized = sanitize_service_name_for_path(service_name)
            branch_sanitized = branch.replace("/", "-").replace("_", "-").lower()
            feature_branch = build_dockerfile_generation_feature_branch(service_sanitized, branch_sanitized)

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
            commit_result = GitHubIntegration.update_or_create_file(
                token=github_token, base_url=github_base_url,
                owner=owner, repo=repo, branch=feature_branch,
                file_path=dockerfile_path, content=dockerfile_content, message=commit_msg
            )
            result["commit_sha"] = commit_result.get("commit_sha")

            # Check/create PR
            existing_pr = GitHubIntegration.find_open_pr(
                token=github_token, base_url=github_base_url,
                owner=owner, repo=repo, head=feature_branch, base=branch
            )

            if existing_pr:
                result["status"] = "success"
                result["pr_url"] = existing_pr.get("html_url")
                result["pr_number"] = existing_pr.get("number")
            else:
                pr_title = f"[Dockerfile] Add standardized Dockerfile for {service_name}"
                pr_body = self._build_pr_body(service_name, branch, dockerfile_path, user_email)
                pr_result = GitHubIntegration.create_pull_request(
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
        user_email: Optional[str]
    ) -> str:
        """Build PR body for Dockerfile creation."""
        return f"""## Standardized Dockerfile Generation

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
