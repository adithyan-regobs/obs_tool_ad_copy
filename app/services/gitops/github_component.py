"""
GitHub GitOps Handler

Orchestrates GitHub operations for GitOps workflows:
- Optional create-only skip
- No-change detection
- Commit multiple files
- Create or reuse PR

Branch strategy and input validation should be handled by the caller.
"""

from typing import Dict, Any, List, Optional
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.integrations.github_integration import GitHubIntegration
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.github_app_token import get_token_for_org

logger = logging.getLogger(__name__)


class GithubComponent:
    """Handler for GitHub GitOps operations (commit + PR)."""

    def __init__(self, db: AsyncSession) -> None:
        self.logger = logging.getLogger(__name__)
        self.db = db

    async def _get_github_token(self, owner: str) -> str:
        """Get GitHub App installation token for the given org."""
        return await get_token_for_org(owner, self.db)

    async def create_commit(
        self,
        owner: str,
        repo: str,
        base_branch: str,
        feature_branch: str,
        files: List[Dict[str, str]],
        commit_message: str,
        comparison_branch: Optional[str] = None,
        skip_if_exists: bool = False,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute GitHub GitOps workflow: compare and commit.

        Args:
            owner: GitHub repo owner (org name — used to resolve installation token)
            repo: GitHub repo name
            base_branch: PR base branch
            feature_branch: Feature branch for commit/PR
            files: List of dicts with "path" and "content"
            commit_message: Commit message for file changes
            comparison_branch: Optional branch to compare against for no-change detection
            skip_if_exists: If True, short-circuit when files already exist
            github_base_url: Optional override for GitHub API base URL

        Returns:
            Dict with status and commit info
        """
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            if not files:
                return {"status": "error", "error": "No files provided for commit"}

            comparison_branch = comparison_branch or base_branch

            # Create-only skip check (instruction-driven)
            if skip_if_exists:
                existing_paths: List[str] = []
                for file_item in files:
                    existing = await GitHubIntegration.get_file_content(
                        token=token,
                        base_url=base_url,
                        owner=owner,
                        repo=repo,
                        file_path=file_item["path"],
                        branch=comparison_branch
                    )
                    if existing and existing.get("exists"):
                        existing_paths.append(file_item["path"])

                if existing_paths:
                    return {
                        "status": "skipped",
                        "message": "One or more files already exist",
                        "existing_files": existing_paths,
                        "feature_branch": feature_branch,
                        "base_branch": base_branch
                    }

            # No-change detection
            changed_files: List[Dict[str, str]] = []
            unchanged_paths: List[str] = []

            for file_item in files:
                existing_content = None
                try:
                    existing = await GitHubIntegration.get_file_content(
                        token=token,
                        base_url=base_url,
                        owner=owner,
                        repo=repo,
                        file_path=file_item["path"],
                        branch=comparison_branch
                    )
                    if existing and existing.get("exists"):
                        existing_content = existing.get("content")
                except Exception as e:
                    self.logger.warning(
                        f"Failed to fetch {file_item['path']} from {comparison_branch}: {e}"
                    )

                if existing_content is not None and should_skip_commit(
                    existing_content,
                    file_item["content"]
                ):
                    unchanged_paths.append(file_item["path"])
                    continue

                changed_files.append(file_item)

            if not changed_files:
                return {
                    "status": "no_changes",
                    "message": "No changes detected",
                    "unchanged_files": unchanged_paths,
                    "feature_branch": feature_branch,
                    "base_branch": base_branch
                }

            # Commit all files in a single commit
            commit_result = await GitHubIntegration.commit_multiple_files(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                branch=feature_branch,
                files=changed_files,
                message=commit_message
            )

            return {
                "status": "success",
                "commit_sha": commit_result.get("commit_sha"),
                "commit_url": commit_result.get("commit_url"),
                "files_committed": commit_result.get("files_committed", []),
                "feature_branch": feature_branch,
                "base_branch": base_branch
            }

        except Exception as e:
            self.logger.error(f"GitHub GitOps create_commit failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def create_repo(
        self,
        org: str,
        repo_name: str,
        description: str = "",
        private: bool = True,
        auto_init: bool = True,
        github_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new repository under a GitHub organization."""
        token = await self._get_github_token(org)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            result = await GitHubIntegration.create_org_repository(
                token=token,
                base_url=base_url,
                org=org,
                repo_name=repo_name,
                description=description,
                private=private,
                auto_init=auto_init,
            )
            return {"status": "success", **result}
        except Exception as e:
            self.logger.error(f"GitHub GitOps create_repo failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def create_branch(
        self,
        owner: str,
        repo: str,
        base_branch: str,
        feature_branch: str,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a feature branch from a base branch."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            # If the infra repo is missing, run the same full provisioning
            # flow we use at org creation (scaffold + env files + secrets +
            # infra_mst record) — not a bare "create repo" API call.
            from app.services.org_infrastructure_service import OrgInfrastructureService
            ensure_result = await OrgInfrastructureService(self.db).ensure_infra_repo_exists(
                owner=owner,
                repo_name=repo,
            )
            if ensure_result.get("auto_provisioned"):
                self.logger.info(
                    f"Auto-provisioned missing infrastructure repo {owner}/{repo}: "
                    f"status={ensure_result.get('status')}"
                )

            branch_result = await GitHubIntegration.create_branch(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                branch_name=feature_branch,
                from_branch=base_branch
            )

            return {
                "status": "success",
                "feature_branch": feature_branch,
                "base_branch": base_branch,
                "branch_already_exists": branch_result.get("already_exists", False)
            }
        except Exception as e:
            self.logger.error(f"GitHub GitOps create_branch failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def create_pr(
        self,
        owner: str,
        repo: str,
        base_branch: str,
        feature_branch: str,
        pr_title: str,
        pr_body: Optional[str] = None,
        draft: bool = False,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a pull request from feature branch to base branch."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            existing_pr = await GitHubIntegration.find_open_pr(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                head=feature_branch,
                base=base_branch
            )

            if existing_pr:
                return {
                    "status": "success",
                    "message": f"Existing PR #{existing_pr.get('number')} found",
                    "pr_number": existing_pr.get("number"),
                    "pr_url": existing_pr.get("html_url"),
                    "head_sha": existing_pr.get("head_sha"),
                    "feature_branch": feature_branch,
                    "base_branch": base_branch
                }

            pr_result = await GitHubIntegration.create_pull_request(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                head=feature_branch,
                base=base_branch,
                title=pr_title,
                body=pr_body,
                draft=draft
            )

            return {
                "status": "success",
                "pr_number": pr_result.get("number"),
                "pr_url": pr_result.get("html_url"),
                "head_sha": pr_result.get("head_sha"),
                "feature_branch": feature_branch,
                "base_branch": base_branch
            }
        except Exception as e:
            self.logger.error(f"GitHub GitOps create_pr failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def reset_branch_to_ref(
        self,
        owner: str,
        repo: str,
        branch: str,
        target_sha: str,
        force: bool = True,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Reset a branch to point to a specific SHA."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            result = await GitHubIntegration.reset_branch_to_ref(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                branch=branch,
                target_sha=target_sha,
                force=force
            )
            return {"status": "success", **result}
        except Exception as e:
            self.logger.error(f"GitHub GitOps reset_branch_to_ref failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get details of a specific pull request."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            result = await GitHubIntegration.get_pull_request(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                pr_number=pr_number
            )
            return {"status": "success", **result}
        except Exception as e:
            self.logger.error(f"GitHub GitOps get_pull_request failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def get_commit(
        self,
        owner: str,
        repo: str,
        sha: str,
        github_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get details of a specific commit by SHA."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        try:
            result = await GitHubIntegration.get_commit(
                token=token, base_url=base_url, owner=owner, repo=repo, sha=sha,
            )
            return {"status": "success", **result}
        except Exception as e:
            self.logger.error(f"GitHub GitOps get_commit failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def get_branch_sha(
        self,
        owner: str,
        repo: str,
        branch: str,
        github_base_url: Optional[str] = None
    ) -> Optional[str]:
        """Get the current SHA of a branch."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            return await GitHubIntegration.get_branch_sha(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                branch=branch
            )
        except Exception as e:
            self.logger.error(f"GitHub GitOps get_branch_sha failed: {e}", exc_info=True)
            return None

    async def get_branch_latest_commit_time(
        self,
        owner: str,
        repo: str,
        branch: str,
        github_base_url: Optional[str] = None
    ) -> Optional[str]:
        """Get the committer date (ISO 8601) of the latest commit on a branch."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            return await GitHubIntegration.get_branch_latest_commit_time(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                branch=branch,
            )
        except Exception as e:
            self.logger.error(f"GitHub GitOps get_branch_latest_commit_time failed: {e}", exc_info=True)
            return None

    async def update_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        state: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update a pull request (change state, title, or body)."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            result = await GitHubIntegration.update_pull_request(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                state=state,
                title=title,
                body=body
            )
            return {"status": "success", **result}
        except Exception as e:
            self.logger.error(f"GitHub GitOps update_pull_request failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def get_repo_tree(
        self,
        owner: str,
        repo: str,
        branch: str = "main",
        exclude_paths: Optional[List[str]] = None,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Recursively fetch all files from a GitHub repository."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            files = await GitHubIntegration.get_repo_tree(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                branch=branch,
                exclude_paths=exclude_paths,
            )
            return {"status": "success", "files": files}
        except Exception as e:
            self.logger.error(f"GitHub GitOps get_repo_tree failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def list_directory(
        self,
        owner: str,
        repo: str,
        path: str,
        branch: str,
        github_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List one directory's entries. Never raises — callers use this to
        discover optional files, so a missing path or an API hiccup must read
        as 'nothing found', not as a failed deploy."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            entries = await GitHubIntegration.list_directory_contents(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                path=path,
                branch=branch,
            )
            return {"status": "success", "entries": entries}
        except Exception as e:
            self.logger.error(f"GitHub GitOps list_directory failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e), "entries": []}

    async def get_content(
        self,
        owner: str,
        repo: str,
        file_path: str,
        branch: str,
        github_base_url: Optional[str] = None,
        github_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch file content from GitHub using the integration layer."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")

        try:
            return await GitHubIntegration.get_file_content(
                token=token,
                base_url=base_url,
                owner=owner,
                repo=repo,
                file_path=file_path,
                branch=branch
            ) or {
                "content": None,
                "sha": None,
                "encoding": None,
                "exists": False
            }
        except Exception as e:
            self.logger.error(f"GitHub GitOps get_content failed: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "content": None,
                "sha": None,
                "encoding": None,
                "exists": False
            }

    async def get_pr_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        github_base_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        return await GitHubIntegration.get_pr_comments(
            token=token, base_url=base_url, owner=owner, repo=repo, pr_number=pr_number,
        )

    async def get_pr_reviews(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        github_base_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        return await GitHubIntegration.get_pr_reviews(
            token=token, base_url=base_url, owner=owner, repo=repo, pull_number=pr_number,
        )

    async def post_pr_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        github_base_url: Optional[str] = None,
    ) -> None:
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        await GitHubIntegration.post_pr_comment(
            token=token, base_url=base_url, owner=owner, repo=repo, pr_number=pr_number, body=body,
        )

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        merge_method: str = "squash",
        commit_title: str = None,
        commit_message: str = None,
        github_base_url: Optional[str] = None,
        expected_head_sha: str = None,
    ) -> Dict[str, Any]:
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        return await GitHubIntegration.merge_pull_request(
            token=token, base_url=base_url, owner=owner, repo=repo,
            pull_number=pull_number, merge_method=merge_method,
            commit_title=commit_title, commit_message=commit_message,
            expected_head_sha=expected_head_sha,
        )

    async def list_pr_files(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        github_base_url: Optional[str] = None,
    ) -> List[str]:
        """All changed file paths in a PR (paginated; renames yield both paths)."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        return await GitHubIntegration.list_pull_request_files(
            token=token, base_url=base_url, owner=owner, repo=repo,
            pull_number=pull_number,
        )

    async def compare_commits(
        self,
        owner: str,
        repo: str,
        base_sha: str,
        head_sha: str,
        github_base_url: Optional[str] = None,
    ) -> Dict[str, List[str]]:
        """Changed file paths + commit authors between two commits
        (compare API): {"files": [...], "authors": [...]}."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        return await GitHubIntegration.compare_commits(
            token=token, base_url=base_url, owner=owner, repo=repo,
            base_sha=base_sha, head_sha=head_sha,
        )

    async def close_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comment: Optional[str] = None,
        github_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        return await GitHubIntegration.close_pull_request(
            token=token, base_url=base_url, owner=owner, repo=repo,
            pr_number=pr_number, comment=comment,
        )

    async def find_open_pr_by_branch(
        self,
        owner: str,
        repo: str,
        feature_branch: str,
        base_branch: str,
        github_base_url: Optional[str] = None,
    ) -> Optional[int]:
        """Return the PR number of the open PR from feature_branch into base_branch, or None."""
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        pr_info = await GitHubIntegration.find_open_pr(
            token=token,
            base_url=base_url,
            owner=owner,
            repo=repo,
            head=feature_branch,
            base=base_branch,
        )
        return pr_info.get("number") if pr_info else None

    async def force_commit_from_parent(
        self,
        owner: str,
        repo: str,
        branch: str,
        parent_sha: str,
        files: List[Dict[str, str]],
        message: str,
        github_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        return await GitHubIntegration.force_commit_from_parent(
            token=token, base_url=base_url, owner=owner, repo=repo,
            branch=branch, parent_sha=parent_sha, files=files, message=message,
        )

    async def list_deployment_history(
        self,
        owner: str,
        repo: str,
        branch: Optional[str] = None,
        workflow_file: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        github_base_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return workflow runs for a service, newest first.
        Filters by branch and status when provided; workflow_file is optional for further scoping.
        """
        token = await self._get_github_token(owner)
        base_url = (github_base_url or settings.github_base_url).rstrip("/")
        return await GitHubIntegration.list_workflow_runs(
            token=token,
            base_url=base_url,
            owner=owner,
            repo=repo,
            workflow_file=workflow_file,
            branch=branch,
            status=status,
            limit=limit,
        )

