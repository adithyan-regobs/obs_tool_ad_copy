"""
GitOps Handler

Orchestrator layer for GitOps operations.
Routes to tenant-specific GitOps component implementations:

- Default -> app/services/gitops/github_component.py (GithubComponent)
- Aspora -> app/services/gitops/github_component.py (GithubComponent)

This handler does NOT contain business logic for GitOps operations.
It only routes to the appropriate tenant-specific component.

Tenant-specific components implement:
- Commit creation (multi-file)
- PR creation (or reuse existing)
- No-change detection
- Branch management

Future extensions:
- GitLab component support
- Bitbucket component support
"""

import logging
import time
from typing import Dict, Any, List, Optional, Type
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.gitops.github_component import GithubComponent
from app.schemas.pr_workflow_context import PRWorkflowContext
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


class GitOpsHandler:
    """
    Handler for orchestrating GitOps operations.

    Routes to tenant-specific GitOps components (GitHub, GitLab, etc.).
    Currently only supports GitHub component.
    """

    _tenant_gitops_map: Dict[str, Type] = {
        "default": GithubComponent
    }

    @classmethod
    def get_component(cls, tenant: str, db: AsyncSession) -> GithubComponent:
        """
        Resolve concrete GitOps component.
        Falls back to default (GithubComponent) if tenant is not configured.

        Args:
            tenant: Tenant code
            db: Database session (needed for installation token resolution)

        Returns:
            GitOps component instance
        """
        try:
            component_cls = cls._tenant_gitops_map[tenant]
        except KeyError:
            logger.warning(f"No specific GitOps component for tenant '{tenant}', using default")
            component_cls = GithubComponent

        return component_cls(db)

    @classmethod
    async def create_commit(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        base_branch: str,
        feature_branch: str,
        files: List[Dict[str, str]],
        commit_message: str,
        workflow_context: PRWorkflowContext,
        db: AsyncSession,
        comparison_branch: Optional[str] = None,
        skip_if_exists: bool = False,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create commit using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.create_commit(
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            feature_branch=feature_branch,
            files=files,
            commit_message=commit_message,
            comparison_branch=comparison_branch,
            skip_if_exists=skip_if_exists,
            github_base_url=github_base_url
        )

        # Store commit result in workflow context
        repo_key = f"{repo}|||{base_branch}"
        if repo_key not in workflow_context.gitops_responses:
            workflow_context.gitops_responses[repo_key] = {}
        workflow_context.gitops_responses[repo_key]["commit"] = result
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "GitOps commit repo=%s base=%s feature=%s files=%s status=%s ms=%s",
            repo,
            base_branch,
            feature_branch,
            len(files),
            result.get("status"),
            elapsed_ms
        )

        return result

    @classmethod
    async def create_pr(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        base_branch: str,
        feature_branch: str,
        pr_title: str,
        workflow_context: PRWorkflowContext,
        db: AsyncSession,
        pr_body: Optional[str] = None,
        draft: bool = False,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create pull request using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.create_pr(
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            feature_branch=feature_branch,
            pr_title=pr_title,
            pr_body=pr_body,
            draft=draft,
            github_base_url=github_base_url
        )

        repo_key = f"{repo}|||{base_branch}"
        if repo_key not in workflow_context.gitops_responses:
            workflow_context.gitops_responses[repo_key] = {}
        workflow_context.gitops_responses[repo_key]["pr"] = result
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "GitOps PR repo=%s base=%s feature=%s status=%s pr_number=%s ms=%s",
            repo,
            base_branch,
            feature_branch,
            result.get("status"),
            result.get("pr_number"),
            elapsed_ms
        )

        return result

    @classmethod
    async def reset_branch_to_ref(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        branch: str,
        target_sha: str,
        db: AsyncSession,
        force: bool = True,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Reset a branch to a specific SHA using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.reset_branch_to_ref(
            owner=owner,
            repo=repo,
            branch=branch,
            target_sha=target_sha,
            force=force,
            github_base_url=github_base_url
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "GitOps reset_branch repo=%s branch=%s target_sha=%s status=%s ms=%s",
            repo, branch, target_sha[:8] if target_sha else None,
            result.get("status"), elapsed_ms
        )
        return result

    @classmethod
    async def get_pull_request(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        pr_number: int,
        db: AsyncSession,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get pull request details using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.get_pull_request(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            github_base_url=github_base_url
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "GitOps get_pr repo=%s pr_number=%s state=%s ms=%s",
            repo, pr_number, result.get("state"), elapsed_ms
        )
        return result

    @classmethod
    async def get_commit(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        sha: str,
        db: AsyncSession,
        github_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get commit details by SHA."""
        component = cls.get_component(tenant, db)
        return await component.get_commit(owner=owner, repo=repo, sha=sha, github_base_url=github_base_url)

    @classmethod
    async def get_branch_sha(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        branch: str,
        db: AsyncSession,
        github_base_url: Optional[str] = None
    ) -> Optional[str]:
        """Get the current SHA of a branch using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.get_branch_sha(
            owner=owner,
            repo=repo,
            branch=branch,
            github_base_url=github_base_url
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "GitOps get_branch_sha repo=%s branch=%s sha=%s ms=%s",
            repo, branch, result[:8] if result else None, elapsed_ms
        )
        return result

    @classmethod
    async def get_branch_latest_commit_time(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        branch: str,
        db: AsyncSession,
        github_base_url: Optional[str] = None
    ) -> Optional[str]:
        """Get the committer date (ISO 8601) of the latest commit on a branch."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.get_branch_latest_commit_time(
            owner=owner,
            repo=repo,
            branch=branch,
            github_base_url=github_base_url,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "GitOps get_branch_latest_commit_time repo=%s branch=%s date=%s ms=%s",
            repo, branch, result, elapsed_ms
        )
        return result

    @classmethod
    async def update_pull_request(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        pr_number: int,
        db: AsyncSession,
        state: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        github_base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update a pull request using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.update_pull_request(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            state=state,
            title=title,
            body=body,
            github_base_url=github_base_url
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "GitOps update_pr repo=%s pr_number=%s state=%s status=%s ms=%s",
            repo, pr_number, state, result.get("status"), elapsed_ms
        )
        return result

    @classmethod
    async def get_content(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        file_path: str,
        branch: str,
        db: AsyncSession = None,
        github_base_url: Optional[str] = None,
        github_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch file content using tenant-specific GitOps component."""
        start = time.monotonic()
        if db is None:
            async with AsyncSessionLocal() as db:
                return await cls.get_content(
                    tenant=tenant, owner=owner, repo=repo,
                    file_path=file_path, branch=branch, db=db,
                    github_base_url=github_base_url, github_token=github_token
                )
        component = cls.get_component(tenant, db)
        result = await component.get_content(
            owner=owner,
            repo=repo,
            file_path=file_path,
            branch=branch,
            github_base_url=github_base_url,
            github_token=github_token
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        status = result.get("status") if isinstance(result, dict) else None
        exists = result.get("exists") if isinstance(result, dict) else None
        logger.info(
            "GitOps fetch repo=%s branch=%s path=%s status=%s exists=%s ms=%s",
            repo,
            branch,
            file_path,
            status,
            exists,
            elapsed_ms
        )
        return result

    @classmethod
    async def list_directory(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        path: str,
        branch: str,
        db: AsyncSession = None,
        github_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List a directory's entries using the tenant-specific GitOps component."""
        if db is None:
            async with AsyncSessionLocal() as db:
                return await cls.list_directory(
                    tenant=tenant, owner=owner, repo=repo, path=path,
                    branch=branch, db=db, github_base_url=github_base_url,
                )
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.list_directory(
            owner=owner,
            repo=repo,
            path=path,
            branch=branch,
            github_base_url=github_base_url,
        )
        logger.info(
            "GitOps list repo=%s branch=%s path=%s status=%s entries=%s ms=%s",
            repo,
            branch,
            path,
            result.get("status"),
            len(result.get("entries") or []),
            int((time.monotonic() - start) * 1000),
        )
        return result

    @classmethod
    async def get_pr_comments(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        pr_number: int,
        db: AsyncSession,
    ) -> List[Dict[str, Any]]:
        """Get all comments on a PR using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.get_pr_comments(owner=owner, repo=repo, pr_number=pr_number)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("GitOps get_pr_comments repo=%s pr=%s count=%s ms=%s", repo, pr_number, len(result), elapsed_ms)
        return result

    @classmethod
    async def get_pr_reviews(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        pr_number: int,
        db: AsyncSession,
    ) -> List[Dict[str, Any]]:
        """Get all reviews on a PR using tenant-specific GitOps component."""
        component = cls.get_component(tenant, db)
        return await component.get_pr_reviews(owner=owner, repo=repo, pr_number=pr_number)

    @classmethod
    async def post_pr_comment(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        db: AsyncSession,
    ) -> None:
        """Post a comment on a PR using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        await component.post_pr_comment(owner=owner, repo=repo, pr_number=pr_number, body=body)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("GitOps post_pr_comment repo=%s pr=%s ms=%s", repo, pr_number, elapsed_ms)

    @classmethod
    async def merge_pull_request(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        pull_number: int,
        db: AsyncSession,
        merge_method: str = "squash",
        commit_title: str = None,
        commit_message: str = None,
        expected_head_sha: str = None,
    ) -> Dict[str, Any]:
        """Merge a PR using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.merge_pull_request(
            owner=owner, repo=repo, pull_number=pull_number,
            merge_method=merge_method, commit_title=commit_title, commit_message=commit_message,
            expected_head_sha=expected_head_sha,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("GitOps merge_pr repo=%s pr=%s ms=%s", repo, pull_number, elapsed_ms)
        return result

    @classmethod
    async def list_pr_files(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        pull_number: int,
        db: AsyncSession,
    ) -> List[str]:
        """All changed file paths in a PR (paginated; renames yield both paths)."""
        component = cls.get_component(tenant, db)
        return await component.list_pr_files(owner=owner, repo=repo, pull_number=pull_number)

    @classmethod
    async def compare_commits(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        base_sha: str,
        head_sha: str,
        db: AsyncSession,
    ) -> Dict[str, List[str]]:
        """Changed file paths + commit authors between two commits
        (compare API): {"files": [...], "authors": [...]}."""
        component = cls.get_component(tenant, db)
        return await component.compare_commits(
            owner=owner, repo=repo, base_sha=base_sha, head_sha=head_sha,
        )

    @classmethod
    async def close_pull_request(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        pr_number: int,
        db: AsyncSession,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close a PR using tenant-specific GitOps component."""
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.close_pull_request(
            owner=owner, repo=repo, pr_number=pr_number, comment=comment,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("GitOps close_pr repo=%s pr=%s ms=%s", repo, pr_number, elapsed_ms)
        return result

    @classmethod
    async def find_open_pr_by_branch(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        feature_branch: str,
        base_branch: str,
        db: AsyncSession,
    ) -> Optional[int]:
        """Return the PR number of the open DevLift PR (feature_branch → base_branch), or None."""
        component = cls.get_component(tenant, db)
        return await component.find_open_pr_by_branch(
            owner=owner,
            repo=repo,
            feature_branch=feature_branch,
            base_branch=base_branch,
        )

    @classmethod
    async def force_commit_from_parent(
        cls,
        tenant: str,
        owner: str,
        repo: str,
        branch: str,
        parent_sha: str,
        files: List[Dict[str, str]],
        message: str,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """
        Creates a new commit on the feature branch with parent=parent_sha (base HEAD).
        Uses parent's tree as base_tree so all stage files are preserved.
        Force-pushes the branch ref — PR stays open, Atlantis lock held.
        """
        start = time.monotonic()
        component = cls.get_component(tenant, db)
        result = await component.force_commit_from_parent(
            owner=owner, repo=repo, branch=branch,
            parent_sha=parent_sha, files=files, message=message,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "GitOps force_commit_from_parent repo=%s branch=%s parent=%s commit=%s ms=%s",
            repo, branch, parent_sha[:8] if parent_sha else None,
            result.get("commit_sha", "")[:8], elapsed_ms,
        )
        return result
