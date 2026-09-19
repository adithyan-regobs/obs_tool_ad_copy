"""
Dockerfile Sync Service

Handles Dockerfile modifications for Datadog APM integration.
Creates deterministic PRs for enabling/disabling Datadog in service Dockerfiles.

Smart PR Replacement Strategy:
- For each branch, check if an existing open PR exists (via database lookup)
- If existing PR found: compare content from that PR's branch
- If no changes: skip silently, return existing PR info
- If changes: create new PR, return old PR info for cleanup
"""

import logging
from typing import Dict, Optional, List

from app.db.models.service_config_model import ServiceConfigModel
from app.utils.naming_helpers import build_dockerfile_feature_branch
from app.utils.dockerfile_helpers import (
    check_dockerfile_eligibility,
    get_datadog_advanced_options,
    build_pr_body,
    determine_overall_status
)
from app.utils.github_sync_helpers import (
    create_or_get_feature_branch,
    get_file_from_appropriate_branch,
    should_skip_commit,
    normalize_file_content
)
from app.services.dockerfile_fetch_service import DockerfileFetchService

# Aspora Client Plugin - PR Strategy
from app.strategies.aspora.pr_strategy import AsporaPRStrategy

logger = logging.getLogger(__name__)


class DockerfileSyncService:
    """Service for syncing Dockerfile changes to GitHub via PRs."""

    def should_modify_dockerfile(
        self,
        service_config: ServiceConfigModel,
        field_mapping: Dict[str, str]
    ) -> bool:
        """Check if Dockerfile should be modified for Datadog."""
        return check_dockerfile_eligibility(service_config, field_mapping)

    async def modify_dockerfile_for_datadog(
        self,
        service_config: ServiceConfigModel,
        service,
        github_token: str,
        github_base_url: str,
        service_sanitized: str,
        env_normalized: str,
        geo_loc_sanitized: str,
        user_email: str = None,
        enable_datadog: bool = True,
        existing_prs_by_branch: Optional[Dict[str, Dict]] = None,
        create_secondary_pr: bool = False,
        tenant_code: str = None
    ) -> Dict[str, any]:
        """
        Modify Dockerfile in service repository to add/remove Datadog configuration.

        Args:
            existing_prs_by_branch: Optional dict mapping branch name to existing PR info:
                {
                    "main": {
                        "git_branch": "datadog/service-main-123456",
                        "pr_number": 937,
                        "workflow_id": 123
                    }
                }
                If provided, the service will compare content with the existing PR's branch
                and skip if no changes, or replace if changes exist.
        """
        from app.integrations.github_integration import GitHubIntegration
        from app.utils.dockerfile_transformer import (
            transform_dockerfile_for_datadog,
            transform_dockerfile_remove_datadog
        )
        from app.services.dockerfile_fetch_service import DockerfileFetchService

        config = service_config.config
        if not config:
            return {"status": "skipped", "branches": [], "message": "No config object"}

        repository = config.get("repository")
        branches = config.get("branches", [])

        # Get dockerfile_path - compute it if generate_dockerfile is enabled and path not set
        dockerfile_path = config.get("dockerfile_path")
        if not dockerfile_path and config.get("generate_dockerfile", False):
            # Compute the expected dockerfile_path based on build_path and service name
            from app.utils.language_helpers import get_dockerfile_path, sanitize_service_name_for_path
            build_path = config.get("build_path")
            if build_path:
                # When build_path is set, Dockerfile is at docker/{service-name}/Dockerfile
                sanitized_name = sanitize_service_name_for_path(service.name)
                dockerfile_path = f"docker/{sanitized_name}/Dockerfile"
                logger.info(f"Computed dockerfile_path for generate_dockerfile: {dockerfile_path}")
        dockerfile_path = dockerfile_path or "Dockerfile"

        logger.info(f"=== DOCKERFILE SYNC: BRANCHES DEBUG ===")
        logger.info(f"Repository: {repository}")
        logger.info(f"Branches from config: {branches}")
        logger.info(f"Number of branches: {len(branches) if branches else 0}")

        if not repository or "/" not in repository or not branches:
            logger.warning(f"Invalid config - repository: {repository}, branches: {branches}")
            return {"status": "skipped", "branches": [], "message": "Invalid config"}

        owner, repo = repository.split("/", 1)
        service_name = service.name
        environment = service_config.environment.value
        xms_mb = int(config.get("xms")) if config.get("xms") else None
        xmx_mb = int(config.get("xmx")) if config.get("xmx") else None
        build_args = config.get("build_args")  # Custom Docker build arguments
        advanced_options = get_datadog_advanced_options(service_config)

        action = "Enable" if enable_datadog else "Disable"
        logger.info(f"Starting Dockerfile modification: {action} Datadog for {service_name}")
        logger.info(f"Datadog config: xms_mb={xms_mb}, xmx_mb={xmx_mb}, advanced_options count={len(advanced_options) if advanced_options else 0}, build_args count={len(build_args) if build_args else 0}")

        branch_results = []
        success_count = 0
        error_count = 0

        # Initialize existing_prs_by_branch to empty dict if not provided
        existing_prs_by_branch = existing_prs_by_branch or {}

        for idx, branch in enumerate(branches):
            logger.info(f"=== Processing branch {idx + 1}/{len(branches)}: {branch} ===")
            # Sanitize branch name (simple inline)
            branch_sanitized = branch.replace("/", "-").replace("_", "-").lower()

            # Get existing PR info for this branch if available
            existing_pr_info = existing_prs_by_branch.get(branch)

            result = await self._process_branch(
                GitHubIntegration, github_token, github_base_url, owner, repo,
                branch, dockerfile_path, service_name, environment,
                service_sanitized, branch_sanitized, env_normalized, geo_loc_sanitized,
                enable_datadog, xms_mb, xmx_mb, advanced_options, build_args, user_email,
                transform_dockerfile_for_datadog, transform_dockerfile_remove_datadog,
                existing_pr_info,
                create_secondary_pr,
                tenant_code
            )

            logger.info(f"Branch '{branch}' result: status={result.get('status')}, pr_number={result.get('pr_number')}, error={result.get('error')}")

            if result["status"] == "success":
                success_count += 1
            elif result["status"] == "error":
                error_count += 1

            branch_results.append(result)

        logger.info(f"=== DOCKERFILE SYNC COMPLETE ===")
        logger.info(f"Total branches: {len(branches)}, Success: {success_count}, Errors: {error_count}")
        logger.info(f"Branch results: {branch_results}")

        return determine_overall_status(branch_results, success_count, error_count, len(branches))

    async def _process_branch(
        self, GitHubIntegration, github_token, github_base_url, owner, repo,
        branch, dockerfile_path, service_name, environment,
        service_sanitized, branch_sanitized, env_normalized, geo_loc_sanitized,
        enable_datadog, xms_mb, xmx_mb, advanced_options, build_args, user_email,
        transform_add, transform_remove,
        existing_pr_info: Optional[Dict] = None,
        create_secondary_pr: bool = False,
        tenant_code: str = None
    ) -> Dict:
        """
        Process a single branch for Dockerfile modification with smart PR replacement.

        Uses Aspora PR Strategy:
        - Protected branches (main/master/pre-prod/qa/sandbox) → PR to pre-prod (fallback stage-env)
        - stage-env/stage-env-copy → PR to stage-env
        - Feature branches → PR to same branch
        - Optional secondary PR for qa/sandbox/stage-env-copy

        Args:
            existing_pr_info: Optional dict with existing open PR info:
                {
                    "git_branch": "datadog/service-main-123456",
                    "pr_number": 937,
                    "workflow_id": 123
                }
            create_secondary_pr: Whether to create secondary PR for eligible branches

        Returns:
            Dict with result including:
            - old_pr_to_close: Info about old PR to close (if replacing)
            - old_branch_to_delete: Branch name to delete (if replacing)
        """
        result = {
            "branch": branch,
            "feature_branch": None,
            "commit_sha": None,
            "status": "error",
            "error": None,
            "pr_url": None,
            "pr_number": None,
            "old_pr_to_close": None,  # Info about old PR that should be closed
            "old_branch_to_delete": None,  # Branch that should be deleted
            "old_workflow_id": None,  # DB workflow ID to update status
            "secondary_pr": None  # Secondary PR info if created
        }

        try:
            # SMART PR REPLACEMENT LOGIC
            # If we have an existing open PR, compare content before creating new branch
            # If PR was manually closed (was_closed=True), skip comparison and create new PR

            if existing_pr_info:
                existing_branch = existing_pr_info.get("git_branch")
                existing_pr_number = existing_pr_info.get("pr_number")
                existing_workflow_id = existing_pr_info.get("workflow_id")
                pr_was_closed = existing_pr_info.get("was_closed", False)

                if pr_was_closed:
                    # PR was manually closed on GitHub - need to create new PR
                    logger.info(f"Previous PR #{existing_pr_number} was closed - creating replacement PR")
                    result["old_workflow_id"] = existing_workflow_id  # Track for DB update
                else:
                    logger.info(f"Found existing open PR #{existing_pr_number} on branch {existing_branch}")

                    # Step 1: Fetch Dockerfile from the EXISTING PR's branch
                    existing_file = await GitHubIntegration.get_file_content(
                        token=github_token, base_url=github_base_url,
                        owner=owner, repo=repo, file_path=dockerfile_path, branch=existing_branch
                    )

                    if existing_file and existing_file.get("exists"):
                        existing_content = existing_file.get("content", "")

                        # Step 2: Also get the base branch content to transform from
                        base_file = await GitHubIntegration.get_file_content(
                            token=github_token, base_url=github_base_url,
                            owner=owner, repo=repo, file_path=dockerfile_path, branch=branch
                        )

                        if not base_file or not base_file.get("exists"):
                            result["status"] = "skipped"
                            result["error"] = f"Dockerfile not found in base branch {branch}"
                            return result

                        base_content = base_file.get("content", "")

                        # Step 3: Transform the base content to get new content
                        # First apply Datadog transformation if enabled
                        new_content = transform_add(
                            dockerfile_content=base_content, service_name=service_name,
                            xms_mb=xms_mb, xmx_mb=xmx_mb, advanced_options=advanced_options
                        ) if enable_datadog else base_content

                        # Then apply build_args and xms/xmx
                        dockerfile_service = DockerfileFetchService()
                        if build_args:
                            new_content = dockerfile_service.inject_build_args(new_content, build_args)
                        if not enable_datadog and (xms_mb or xmx_mb):
                            new_content = dockerfile_service.update_java_memory_settings(
                                new_content, xms_mb=xms_mb, xmx_mb=xmx_mb
                            )

                        # Step 4: Compare existing PR content with new content
                        # Debug: Log comparison details to help diagnose issues
                        normalized_existing = normalize_file_content(existing_content)
                        normalized_new = normalize_file_content(new_content)
                        logger.debug(f"Content comparison for PR #{existing_pr_number}:")
                        logger.debug(f"  Existing content length: {len(normalized_existing)}")
                        logger.debug(f"  New content length: {len(normalized_new)}")
                        logger.debug(f"  xms_mb: {xms_mb}, xmx_mb: {xmx_mb}")
                        logger.debug(f"  advanced_options: {advanced_options}")

                        if should_skip_commit(existing_content, new_content):
                            logger.info(f"No changes compared to existing PR #{existing_pr_number} - skipping")
                            result["status"] = "success"
                            result["feature_branch"] = existing_branch
                            result["pr_number"] = existing_pr_number
                            result["pr_url"] = f"https://github.com/{owner}/{repo}/pull/{existing_pr_number}"
                            result["commit_sha"] = existing_pr_info.get("commit_sha")
                            result["error"] = "No changes needed, existing PR is up to date"
                            return result
                        else:
                            logger.info(f"Content differs from existing PR #{existing_pr_number} - will replace")

                        # Content has changed - we need to create a new PR and close the old one
                        logger.info(f"Content changed - replacing PR #{existing_pr_number} with new PR")
                        result["old_pr_to_close"] = existing_pr_number
                        result["old_branch_to_delete"] = existing_branch
                        result["old_workflow_id"] = existing_workflow_id

            # === NORMAL FLOW (no existing PR, or existing PR has changes) ===

            # === Aspora PR Strategy (only for aspora tenant) ===
            is_aspora_tenant = tenant_code and tenant_code.lower() == "aspora"

            if is_aspora_tenant:
                # Fetch available branches for strategy decision
                available_branches = await self._get_repo_branch_names(
                    GitHubIntegration, github_token, github_base_url, owner, repo
                )

                # Apply Aspora PR strategy
                strategy = AsporaPRStrategy()
                routing_info = strategy.get_pr_routing_info(
                    selected_branch=branch,
                    available_branches=available_branches,
                    create_secondary_pr=create_secondary_pr
                )

                # Get the actual PR target and feature branch source from strategy
                pr_target_branch = routing_info["pr_target"]
                feature_branch_source = routing_info["feature_branch_source"]

                logger.info(f"Aspora PR Strategy: selected={branch}, pr_target={pr_target_branch}, "
                           f"feature_source={feature_branch_source}, secondary_pr={routing_info['secondary_pr_enabled']}")
            else:
                # Default behavior for non-aspora tenants: PR to same branch
                pr_target_branch = branch
                feature_branch_source = branch
                routing_info = {
                    "pr_target": branch,
                    "feature_branch_source": branch,
                    "is_protected": False,
                    "secondary_pr_target": None,
                    "secondary_pr_enabled": False
                }
                logger.info(f"Default PR Strategy (non-aspora): branch={branch}")

            # Step 1: Create new branch name with timestamp
            feature_branch = build_dockerfile_feature_branch(
                service_sanitized, branch_sanitized, env_normalized, geo_loc_sanitized
            )
            result["feature_branch"] = feature_branch
            logger.info(f"Using new branch: {feature_branch}")

            # Step 2: Create feature branch (from strategy-determined source, not original branch)
            branch_already_exists, error = create_or_get_feature_branch(
                GitHubIntegration, github_token, github_base_url,
                owner, repo, feature_branch, feature_branch_source  # Use strategy source
            )
            if error:
                result["error"] = error
                return result

            # Step 3: Fetch Dockerfile from base branch (fresh start)
            # Use strategy source for file fetching
            original, error = get_file_from_appropriate_branch(
                GitHubIntegration, github_token, github_base_url,
                owner, repo, dockerfile_path, feature_branch, feature_branch_source, branch_already_exists
            )
            if error:
                result["status"] = "skipped"
                result["error"] = error
                return result

            # Step 4: Transform the content
            # First apply Datadog transformation if enabled
            new_content = transform_add(
                dockerfile_content=original, service_name=service_name,
                xms_mb=xms_mb, xmx_mb=xmx_mb, advanced_options=advanced_options
            ) if enable_datadog else original  # Don't remove Datadog, just start with original

            # Then apply build_args and xms/xmx (these apply regardless of Datadog status)
            dockerfile_service = DockerfileFetchService()

            # Inject build args if provided (handles duplicates automatically)
            if build_args:
                new_content = dockerfile_service.inject_build_args(new_content, build_args)

            # Update xms/xmx in JAVA_TOOL_OPTIONS (if not already handled by Datadog transform)
            if not enable_datadog and (xms_mb or xmx_mb):
                new_content = dockerfile_service.update_java_memory_settings(
                    new_content, xms_mb=xms_mb, xmx_mb=xmx_mb
                )

            # Step 5: Check if content changed from base (first time PR)
            if should_skip_commit(original, new_content) and not existing_pr_info:
                logger.info(f"No changes needed for {dockerfile_path} on branch {branch}")
                result["status"] = "skipped"
                result["error"] = "No changes needed"
                return result

            # Step 6: Commit the changes
            # Build commit message based on what changed
            changes = []
            if enable_datadog:
                changes.append("Add Datadog configuration")
            if build_args:
                changes.append("Update Docker build arguments")
            if (xms_mb or xmx_mb) and not enable_datadog:
                changes.append("Update Java memory settings")
            commit_msg = f"{', '.join(changes) if changes else 'Update Dockerfile'} for {service_name}"
            commit_result = await GitHubIntegration.update_or_create_file(
                token=github_token, base_url=github_base_url,
                owner=owner, repo=repo, branch=feature_branch,
                file_path=dockerfile_path, content=new_content, message=commit_msg
            )
            result["commit_sha"] = commit_result.get("commit_sha")

            # Step 7: Create PR (to strategy-determined target, not original branch)
            # Build PR title based on what changed
            if enable_datadog:
                pr_title = f"[Datadog] Enable Datadog for {service_name} - {branch}"
            elif build_args or xms_mb or xmx_mb:
                pr_title = f"[Dockerfile] Update configuration for {service_name} - {branch}"
            else:
                pr_title = f"[Dockerfile] Update for {service_name} - {branch}"

            # Add supersedes note if replacing old PR
            pr_body = build_pr_body(service_name, branch, environment, enable_datadog, xms_mb, xmx_mb, user_email, build_args)
            if result.get("old_pr_to_close"):
                pr_body += f"\n\n---\n*Supersedes PR #{result['old_pr_to_close']}*"

            # Add note if PR target differs from selected branch (Aspora strategy)
            if pr_target_branch != branch:
                pr_body += f"\n\n---\n*Note: PR target is `{pr_target_branch}` (workflow trigger branch: `{branch}`)*"

            pr_result = await GitHubIntegration.create_pull_request(
                token=github_token, base_url=github_base_url,
                owner=owner, repo=repo, head=feature_branch, base=pr_target_branch,  # Use strategy target
                title=pr_title, body=pr_body, draft=False
            )
            result["status"] = "success"
            result["pr_url"] = pr_result.get("html_url")
            result["pr_number"] = pr_result.get("number")
            result["pr_target"] = pr_target_branch  # Track actual PR target

            # Step 8: Close old PR and delete old branch (if replacing)
            if result.get("old_pr_to_close"):
                try:
                    # Close old PR with comment
                    await GitHubIntegration.close_pull_request(
                        token=github_token, base_url=github_base_url,
                        owner=owner, repo=repo,
                        pr_number=result["old_pr_to_close"],
                        comment=f"Superseded by PR #{result['pr_number']}"
                    )
                    logger.info(f"Closed old PR #{result['old_pr_to_close']}")
                except Exception as e:
                    logger.warning(f"Failed to close old PR #{result['old_pr_to_close']}: {e}")

                try:
                    # Delete old branch
                    await GitHubIntegration.delete_branch(
                        token=github_token, base_url=github_base_url,
                        owner=owner, repo=repo, branch=result["old_branch_to_delete"]
                    )
                    logger.info(f"Deleted old branch {result['old_branch_to_delete']}")
                except Exception as e:
                    logger.warning(f"Failed to delete old branch {result['old_branch_to_delete']}: {e}")

            # === Secondary PR Creation (Aspora Strategy) ===
            # Create secondary PR for qa/sandbox/stage-env-copy if flag enabled
            if routing_info["secondary_pr_enabled"] and routing_info["secondary_pr_target"]:
                secondary_target = routing_info["secondary_pr_target"]
                logger.info(f"Creating secondary PR to {secondary_target} for Dockerfile")

                secondary_pr_result = await self._create_secondary_dockerfile_pr(
                    GitHubIntegration, github_token, github_base_url, owner, repo,
                    secondary_target, dockerfile_path, new_content, service_name, environment,
                    service_sanitized, env_normalized, enable_datadog, result["pr_number"]
                )

                if secondary_pr_result:
                    result["secondary_pr"] = secondary_pr_result
                    logger.info(f"Secondary PR created: #{secondary_pr_result.get('pr_number')}")
                else:
                    logger.warning(f"Failed to create secondary PR to {secondary_target}")

        except Exception as e:
            result["error"] = str(e)
            logger.warning(f"Failed to modify Dockerfile for branch {branch}: {e}")

        return result

    async def _get_repo_branch_names(
        self,
        GitHubIntegration,
        github_token: str,
        github_base_url: str,
        owner: str,
        repo: str
    ) -> List[str]:
        """
        Fetch all branch names from repository for strategy decision.

        Args:
            GitHubIntegration: GitHub integration class
            github_token: GitHub token
            github_base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name

        Returns:
            List of branch names
        """
        try:
            branches_result = await GitHubIntegration.fetch_repository_branches(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo
            )

            branches = branches_result.get("branches", [])
            branch_names = [b.get("name", "") for b in branches]
            logger.info(f"Fetched {len(branch_names)} branches for PR strategy decision")
            return branch_names

        except Exception as e:
            logger.warning(f"Failed to fetch branches for strategy: {e}, returning empty list")
            return []

    async def _create_secondary_dockerfile_pr(
        self,
        GitHubIntegration,
        github_token: str,
        github_base_url: str,
        owner: str,
        repo: str,
        secondary_target: str,
        dockerfile_path: str,
        new_content: str,
        service_name: str,
        environment: str,
        service_sanitized: str,
        env_normalized: str,
        enable_datadog: bool,
        primary_pr_number: int
    ) -> Optional[Dict]:
        """
        Create secondary PR to qa/sandbox/stage-env-copy for Dockerfile.

        Args:
            secondary_target: Target branch (qa, sandbox, or stage-env-copy)
            dockerfile_path: Path to Dockerfile
            new_content: New Dockerfile content
            service_name: Service name
            primary_pr_number: Primary PR number for reference

        Returns:
            Dict with secondary PR info or None if failed
        """
        import time

        try:
            # Create unique feature branch for secondary PR
            timestamp = int(time.time())
            secondary_branch_name = f"datadog/{service_sanitized}-{secondary_target}-{timestamp}"

            logger.info(f"Creating secondary feature branch: {secondary_branch_name} from {secondary_target}")

            # Create feature branch from secondary target
            branch_result = await GitHubIntegration.create_branch(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo,
                branch_name=secondary_branch_name,
                from_branch=secondary_target
            )

            if not branch_result:
                logger.error(f"Failed to create secondary feature branch from {secondary_target}")
                return None

            # Commit file to secondary feature branch
            commit_message = f"[Secondary] {'Add' if enable_datadog else 'Remove'} Datadog for {service_name} - refs PR #{primary_pr_number}"
            commit_result = await GitHubIntegration.update_or_create_file(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo,
                branch=secondary_branch_name,
                file_path=dockerfile_path,
                content=new_content,
                message=commit_message
            )

            if not commit_result:
                logger.error("Failed to commit file to secondary feature branch")
                return None

            # Create secondary PR
            pr_title = f"[Secondary] Datadog {'Enable' if enable_datadog else 'Disable'} for {service_name} - {secondary_target} (refs #{primary_pr_number})"
            pr_body = f"""## Secondary PR - Dockerfile Datadog Configuration

**Service:** `{service_name}`
**Environment:** {environment}
**Target Branch:** {secondary_target}
**Action:** {'Enable' if enable_datadog else 'Disable'} Datadog APM

> **Note:** This is a secondary PR. The primary PR is #{primary_pr_number} targeting pre-prod/stage-env.

---
*Generated automatically*"""

            pr_result = await GitHubIntegration.create_pull_request(
                token=github_token,
                base_url=github_base_url,
                owner=owner,
                repo=repo,
                head=secondary_branch_name,
                base=secondary_target,
                title=pr_title,
                body=pr_body,
                draft=False
            )

            logger.info(f"Secondary Dockerfile PR created: #{pr_result.get('number')}")

            return {
                "pr_number": pr_result.get("number"),
                "pr_url": pr_result.get("html_url"),
                "feature_branch": secondary_branch_name,
                "base_branch": secondary_target,
                "commit_sha": commit_result.get("commit_sha"),
                "success": True
            }

        except Exception as e:
            logger.error(f"Failed to create secondary Dockerfile PR: {str(e)}", exc_info=True)
            return None
