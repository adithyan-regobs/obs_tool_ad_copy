import httpx
import base64
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)

# Default timeout for GitHub API calls (in seconds)
DEFAULT_TIMEOUT = 300.0


class DiffStatus(str, Enum):
    """Outcome of a branch-diff fetch — lets callers distinguish an empty diff from a
    failure (auth/rate-limit/network/repo) instead of conflating both as an empty list."""
    OK = "ok"                 # succeeded, has file changes
    EMPTY = "empty"           # succeeded, no file changes
    AUTH = "auth"             # 401 — bad/expired token
    RATE_LIMIT = "rate_limit" # 403 — rate limited or insufficient permissions
    NETWORK = "network"       # request/transport error
    REPO_ERROR = "repo_error" # 404 / other non-2xx / unexpected response


@dataclass
class DiffResult:
    """Typed result of GitHubIntegration.get_branch_diff."""
    status: DiffStatus
    files: List[Dict[str, Any]] = field(default_factory=list)  # {path,status,additions,deletions,patch,previous_filename}


class GitHubIntegration:
    """Integration class for GitHub API"""

    @staticmethod
    async def fetch_user_repositories(
        token: str,
        base_url: str
    ) -> Dict[str, Any]:
        """
        Fetch all repositories for the authenticated GitHub user.

        Uses pagination to fetch all repositories (GitHub API returns max 100 per page).

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL (default: https://api.github.com)

        Returns:
            Dict with repositories list and metadata

        Raises:
            Exception: If API call fails
        """
        url = f"{base_url}/user/repos"

        # Prepare headers
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Fetch all repositories using pagination
        all_repositories = []
        page = 1
        per_page = 100  # GitHub's max per page

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while True:
                params = {
                    "page": page,
                    "per_page": per_page,
                    "sort": "updated",  # Sort by last updated
                    "direction": "desc"  # Most recent first
                }

                response = await client.get(url, headers=headers, params=params)

                if response.status_code == 200:
                    repositories = response.json()

                    if not repositories:
                        # No more repositories, exit loop
                        break

                    all_repositories.extend(repositories)
                    logger.info(f"Fetched page {page} with {len(repositories)} repositories")

                    # If we got fewer than per_page, we're on the last page
                    if len(repositories) < per_page:
                        break

                    page += 1
                elif response.status_code == 401:
                    raise Exception("GitHub authentication failed: Invalid or expired token")
                elif response.status_code == 403:
                    raise Exception("GitHub API rate limit exceeded or insufficient permissions")
                else:
                    error_msg = f"Failed to fetch GitHub repositories: {response.status_code} - {response.text}"
                    raise Exception(error_msg)

        logger.info(f"Total repositories fetched: {len(all_repositories)}")

        return {
            "repositories": all_repositories,
            "total_count": len(all_repositories)
        }

    @staticmethod
    async def fetch_installation_repositories(
        token: str,
        base_url: str
    ) -> Dict[str, Any]:
        """
        Fetch all repositories accessible to a GitHub App installation.

        Uses the /installation/repositories endpoint which works with
        installation access tokens (unlike /user/repos which requires a PAT).

        Args:
            token: GitHub App installation access token
            base_url: GitHub API base URL

        Returns:
            Dict with repositories list and total_count
        """
        url = f"{base_url}/installation/repositories"

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        all_repositories = []
        page = 1
        per_page = 100

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while True:
                params = {
                    "page": page,
                    "per_page": per_page,
                }

                response = await client.get(url, headers=headers, params=params)

                if response.status_code == 200:
                    data = response.json()
                    repos = data.get("repositories", [])

                    if not repos:
                        break

                    all_repositories.extend(repos)
                    logger.info(f"Fetched page {page} with {len(repos)} installation repositories")

                    if len(repos) < per_page:
                        break

                    page += 1
                elif response.status_code == 401:
                    raise Exception("GitHub authentication failed: Invalid or expired installation token")
                elif response.status_code == 403:
                    raise Exception("GitHub API rate limit exceeded or insufficient permissions")
                else:
                    error_msg = f"Failed to fetch installation repositories: {response.status_code} - {response.text}"
                    raise Exception(error_msg)

        logger.info(f"Total installation repositories fetched: {len(all_repositories)}")

        return {
            "repositories": all_repositories,
            "total_count": len(all_repositories)
        }

    @staticmethod
    async def fetch_repository_branches(
        token: str,
        base_url: str,
        owner: str,
        repo: str
    ) -> Dict[str, Any]:
        """
        Fetch all branches for a specific GitHub repository.

        Uses pagination to fetch all branches (GitHub API returns max 100 per page).

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL (default: https://api.github.com)
            owner: Repository owner (username or organization)
            repo: Repository name

        Returns:
            Dict with branches list and metadata

        Raises:
            Exception: If API call fails
        """
        url = f"{base_url}/repos/{owner}/{repo}/branches"

        # Prepare headers
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Fetch all branches using pagination
        all_branches = []
        page = 1
        per_page = 100  # GitHub's max per page

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while True:
                params = {
                    "page": page,
                    "per_page": per_page
                }

                response = await client.get(url, headers=headers, params=params)

                if response.status_code == 200:
                    branches = response.json()

                    if not branches:
                        # No more branches, exit loop
                        break

                    all_branches.extend(branches)
                    logger.info(f"Fetched page {page} with {len(branches)} branches for {owner}/{repo}")

                    # If we got fewer than per_page, we're on the last page
                    if len(branches) < per_page:
                        break

                    page += 1
                elif response.status_code == 401:
                    raise Exception("GitHub authentication failed: Invalid or expired token")
                elif response.status_code == 403:
                    raise Exception("GitHub API rate limit exceeded or insufficient permissions")
                elif response.status_code == 404:
                    raise Exception(f"Repository '{owner}/{repo}' not found or you don't have access")
                else:
                    error_msg = f"Failed to fetch branches for {owner}/{repo}: {response.status_code} - {response.text}"
                    raise Exception(error_msg)

        logger.info(f"Total branches fetched for {owner}/{repo}: {len(all_branches)}")

        return {
            "branches": all_branches,
            "total_count": len(all_branches)
        }

    @staticmethod
    async def commit_workflow_file(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
        file_path: str,
        content: str,
        message: str
    ) -> Dict[str, Any]:
        """
        Commit a workflow YAML file to the repository's .github/workflows/ directory.
        If the file already exists, creates a new versioned file.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL (default: https://api.github.com)
            owner: Repository owner (username or organization)
            repo: Repository name
            branch: Target branch name
            file_path: File path within repo (e.g., .github/workflows/deploy-service-prod.yml)
            content: YAML file content
            message: Commit message

        Returns:
            Dict with:
            - commit_sha: SHA of the commit
            - file_path: Final path where file was committed
            - commit_url: URL to view the commit
            - html_url: URL to view the file

        Raises:
            Exception: If API call fails
        """
        # Prepare headers
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Check if file exists and find available filename
        final_file_path = await GitHubIntegration._find_available_filename(
            token, base_url, owner, repo, branch, file_path, headers
        )

        # Encode content to base64
        content_encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')

        # Prepare request body
        url = f"{base_url}/repos/{owner}/{repo}/contents/{final_file_path}"
        body = {
            "message": message,
            "content": content_encoded,
            "branch": branch
        }

        # Create file
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.put(url, headers=headers, json=body)

        if response.status_code in [200, 201]:
            result = response.json()
            commit_data = result.get("commit", {})
            content_data = result.get("content", {})

            return {
                "commit_sha": commit_data.get("sha"),
                "file_path": final_file_path,
                "commit_url": commit_data.get("html_url"),
                "html_url": content_data.get("html_url"),
                "success": True
            }
        elif response.status_code == 401:
            raise Exception("GitHub authentication failed: Invalid or expired token")
        elif response.status_code == 403:
            raise Exception("GitHub API rate limit exceeded or insufficient permissions")
        elif response.status_code == 404:
            raise Exception(f"Repository '{owner}/{repo}' or branch '{branch}' not found")
        elif response.status_code == 409:
            raise Exception(f"Conflict: File '{final_file_path}' already exists (SHA mismatch)")
        else:
            error_msg = f"Failed to commit file to {owner}/{repo}: {response.status_code} - {response.text}"
            raise Exception(error_msg)

    @staticmethod
    async def update_or_create_file(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
        file_path: str,
        content: str,
        message: str
    ) -> Dict[str, Any]:
        """
        Update an existing file or create it if it doesn't exist.
        This method will overwrite the file if it exists.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Target branch name
            file_path: File path within repo
            content: File content
            message: Commit message

        Returns:
            Dict with commit info

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            # Check if file exists to get its SHA
            sha = None
            check_url = f"{base_url}/repos/{owner}/{repo}/contents/{file_path}?ref={branch}"
            check_response = await client.get(check_url, headers=headers)

            if check_response.status_code == 200:
                # File exists, get SHA for update
                sha = check_response.json().get("sha")
                logger.info(f"File {file_path} exists, updating with SHA: {sha}")
            elif check_response.status_code == 404:
                # File doesn't exist, will create new
                logger.info(f"File {file_path} doesn't exist, creating new file")
            else:
                raise Exception(f"Failed to check file existence: {check_response.status_code} - {check_response.text}")

            # Encode content to base64
            content_encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')

            # Prepare request body
            url = f"{base_url}/repos/{owner}/{repo}/contents/{file_path}"
            body = {
                "message": message,
                "content": content_encoded,
                "branch": branch
            }

            # Add SHA if updating existing file
            if sha:
                body["sha"] = sha

            # Create or update file
            response = await client.put(url, headers=headers, json=body)

        if response.status_code in [200, 201]:
            result = response.json()
            commit_data = result.get("commit", {})
            content_data = result.get("content", {})

            return {
                "commit_sha": commit_data.get("sha"),
                "file_path": file_path,
                "commit_url": commit_data.get("html_url"),
                "html_url": content_data.get("html_url"),
                "success": True
            }
        else:
            error_msg = f"Failed to update/create file {file_path}: {response.status_code} - {response.text}"
            raise Exception(error_msg)

    @staticmethod
    async def get_file_content(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        file_path: str,
        branch: str
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch file content from GitHub repository.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            file_path: File path within repo
            branch: Branch name

        Returns:
            Dict with file content and metadata, or None if file doesn't exist
            {
                "content": "decoded file content",
                "sha": "file sha",
                "encoding": "base64",
                "exists": True/False
            }

        Raises:
            Exception: If API call fails (except 404)
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Same as list_directory_contents: an absent branch means the repo's
        # default branch, not a ref named "None".
        url = f"{base_url}/repos/{owner}/{repo}/contents/{file_path}"
        if branch:
            url = f"{url}?ref={branch}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers)

        if response.status_code == 200:
            data = response.json()
            # Decode base64 content
            content_encoded = data.get("content", "")
            content_decoded = base64.b64decode(content_encoded).decode('utf-8')

            logger.info(f"Successfully fetched file {file_path} from {branch}")
            return {
                "content": content_decoded,
                "sha": data.get("sha"),
                "encoding": data.get("encoding"),
                "exists": True
            }
        elif response.status_code == 404:
            # File doesn't exist
            logger.info(f"File {file_path} not found in branch {branch}")
            return {
                "content": None,
                "sha": None,
                "encoding": None,
                "exists": False
            }
        else:
            raise Exception(f"Failed to fetch file: {response.status_code} - {response.text}")

    @staticmethod
    async def list_directory_contents(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        path: str,
        branch: str
    ) -> List[Dict[str, Any]]:
        """
        List contents of a directory in a GitHub repository.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            path: Directory path within repo (e.g., "tenant/acme/dev/01/ap-south-1/database")
            branch: Branch name

        Returns:
            List of directory entries with metadata:
            [
                {
                    "name": "subdirectory-name",
                    "path": "full/path/to/subdirectory-name",
                    "type": "dir" or "file",
                    "sha": "sha-hash"
                },
                ...
            ]

        Raises:
            Exception: If API call fails or path doesn't exist
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # No branch → no ref, and GitHub answers for the repo's default
        # branch. Callers that only know the repo (the deployments tab sends
        # no branch) would otherwise ask for a ref literally named "None".
        url = f"{base_url}/repos/{owner}/{repo}/contents/{path}"
        if branch:
            url = f"{url}?ref={branch}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers)

        if response.status_code == 200:
            contents = response.json()

            # GitHub returns a list for directories, single object for files
            if not isinstance(contents, list):
                logger.warning(f"Path {path} is a file, not a directory")
                return []

            result = []
            for item in contents:
                result.append({
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "type": item.get("type"),  # "dir" or "file"
                    "sha": item.get("sha")
                })

            logger.info(f"Listed {len(result)} items in {path}")
            return result
        elif response.status_code == 404:
            logger.warning(f"Directory {path} not found in branch {branch}")
            return []
        elif response.status_code == 401:
            raise Exception("GitHub authentication failed: Invalid or expired token")
        elif response.status_code == 403:
            raise Exception("GitHub API rate limit exceeded or insufficient permissions")
        else:
            raise Exception(f"Failed to list directory contents: {response.status_code} - {response.text}")

    @staticmethod
    async def get_repo_tree(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str = "main",
        exclude_paths: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        """
        Recursively fetch all files from a GitHub repo using the Git Trees API.

        Args:
            token: GitHub access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Branch to read from
            exclude_paths: List of path prefixes to skip (e.g. ["recommendations.md"])

        Returns:
            List of dicts with "path" and "content" for each file
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        exclude_paths = exclude_paths or []

        url = f"{base_url}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers)

        if response.status_code != 200:
            raise Exception(
                f"Failed to get repo tree for {owner}/{repo}: "
                f"{response.status_code} - {response.text}"
            )

        tree = response.json().get("tree", [])
        blob_items = [
            item for item in tree
            if item.get("type") == "blob"
            and not any(item["path"].startswith(ex) for ex in exclude_paths)
        ]

        files: List[Dict[str, str]] = []
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for item in blob_items:
                file_url = f"{base_url}/repos/{owner}/{repo}/contents/{item['path']}?ref={branch}"
                file_resp = await client.get(file_url, headers=headers)
                if file_resp.status_code != 200:
                    logger.warning(f"Skipping {item['path']}: {file_resp.status_code}")
                    continue
                data = file_resp.json()
                content_encoded = data.get("content", "")
                try:
                    content = base64.b64decode(content_encoded).decode("utf-8")
                except (UnicodeDecodeError, Exception):
                    logger.warning(f"Skipping binary file: {item['path']}")
                    continue
                files.append({"path": item["path"], "content": content})

        logger.info(f"Fetched {len(files)} files from {owner}/{repo}@{branch}")
        return files

    @staticmethod
    async def create_org_repository(
        token: str,
        base_url: str,
        org: str,
        repo_name: str,
        description: str = "",
        private: bool = True,
        auto_init: bool = True,
    ) -> Dict[str, Any]:
        """
        Create a new repository under a GitHub organization.

        Args:
            token: GitHub access token (personal or App installation)
            base_url: GitHub API base URL
            org: GitHub organization name
            repo_name: Repository name to create
            description: Repository description
            private: Whether the repo is private (default: True)
            auto_init: Initialize with a README (default: True, needed for first commit)

        Returns:
            Dict with repo info (full_name, html_url, clone_url, default_branch)

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        url = f"{base_url}/orgs/{org}/repos"
        body = {
            "name": repo_name,
            "description": description,
            "private": private,
            "auto_init": auto_init,
        }

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            # Check existence first so already-provisioned repos short-circuit
            # without needing create permission (avoids 403 on repeat calls).
            check = await client.get(f"{base_url}/repos/{org}/{repo_name}", headers=headers)
            if check.status_code == 200:
                logger.info(f"Repository {org}/{repo_name} already exists (pre-check)")
                data = check.json()
                return {
                    "full_name": data.get("full_name"),
                    "html_url": data.get("html_url"),
                    "clone_url": data.get("clone_url"),
                    "default_branch": data.get("default_branch", "main"),
                    "already_exists": True,
                }
            response = await client.post(url, headers=headers, json=body)

        if response.status_code in [200, 201]:
            data = response.json()
            logger.info(f"Created repository {org}/{repo_name}")
            return {
                "full_name": data.get("full_name"),
                "html_url": data.get("html_url"),
                "clone_url": data.get("clone_url"),
                "default_branch": data.get("default_branch", "main"),
                "already_exists": False,
            }
        elif response.status_code == 422:
            # Repo already exists
            logger.info(f"Repository {org}/{repo_name} already exists")
            return {
                "full_name": f"{org}/{repo_name}",
                "html_url": f"https://github.com/{org}/{repo_name}",
                "clone_url": f"https://github.com/{org}/{repo_name}.git",
                "default_branch": "main",
                "already_exists": True,
            }
        elif response.status_code == 401:
            raise Exception("GitHub authentication failed: Invalid or expired token")
        elif response.status_code == 403:
            raise Exception("Insufficient permissions to create repository in organization")
        else:
            raise Exception(
                f"Failed to create repository {org}/{repo_name}: "
                f"{response.status_code} - {response.text}"
            )

    @staticmethod
    async def repo_exists(
        token: str,
        base_url: str,
        org: str,
        repo_name: str,
    ) -> bool:
        """
        Quick existence check for a repo. True on 200, False on 404.
        Raises on 401/403 so perms issues surface instead of being papered over.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{base_url}/repos/{org}/{repo_name}"

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers)

        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        if response.status_code == 401:
            raise Exception("GitHub authentication failed: Invalid or expired token")
        if response.status_code == 403:
            raise Exception(f"Insufficient permissions to access {org}/{repo_name}")
        raise Exception(
            f"Failed to check repository {org}/{repo_name}: "
            f"{response.status_code} - {response.text}"
        )

    @staticmethod
    async def set_repo_secret(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        secret_name: str,
        secret_value: str,
    ) -> None:
        """
        Create or update a repository secret using GitHub Actions Secrets API.

        Uses libsodium sealed box encryption (required by GitHub).

        Args:
            token: GitHub access token
            base_url: GitHub API base URL
            owner: Repository owner (org or user)
            repo: Repository name
            secret_name: Name of the secret (e.g., PIPELINE_WEBHOOK_SECRET)
            secret_value: Plain text value to encrypt and store
        """
        from nacl import encoding, public
        import base64

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # Step 1: Get the repo's public key for secret encryption
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            key_resp = await client.get(
                f"{base_url}/repos/{owner}/{repo}/actions/secrets/public-key",
                headers=headers,
            )

        if key_resp.status_code != 200:
            raise Exception(
                f"Failed to get public key for {owner}/{repo}: "
                f"{key_resp.status_code} - {key_resp.text}"
            )

        key_data = key_resp.json()
        public_key = public.PublicKey(
            key_data["key"].encode("utf-8"), encoding.Base64Encoder
        )

        # Step 2: Encrypt the secret value using sealed box
        sealed_box = public.SealedBox(public_key)
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        encrypted_value = base64.b64encode(encrypted).decode("utf-8")

        # Step 3: Create or update the secret
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.put(
                f"{base_url}/repos/{owner}/{repo}/actions/secrets/{secret_name}",
                headers=headers,
                json={
                    "encrypted_value": encrypted_value,
                    "key_id": key_data["key_id"],
                },
            )

        if resp.status_code not in [201, 204]:
            raise Exception(
                f"Failed to set secret {secret_name} on {owner}/{repo}: "
                f"{resp.status_code} - {resp.text}"
            )

        logger.info(f"Set secret {secret_name} on {owner}/{repo}")

    @staticmethod
    async def create_branch(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch_name: str,
        from_branch: str = "main"
    ) -> Dict[str, Any]:
        """
        Create a new branch from an existing branch.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch_name: Name of the new branch to create
            from_branch: Source branch to create from (default: "main")

        Returns:
            Dict with branch information

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            # Get SHA of the source branch
            ref_url = f"{base_url}/repos/{owner}/{repo}/git/refs/heads/{from_branch}"
            ref_response = await client.get(ref_url, headers=headers)

            if ref_response.status_code != 200:
                raise Exception(f"Failed to get branch {from_branch}: {ref_response.status_code} - {ref_response.text}")

            ref_data = ref_response.json()
            sha = ref_data["object"]["sha"]

            # Create new branch from SHA
            create_ref_url = f"{base_url}/repos/{owner}/{repo}/git/refs"
            payload = {
                "ref": f"refs/heads/{branch_name}",
                "sha": sha
            }

            response = await client.post(create_ref_url, headers=headers, json=payload)

        if response.status_code == 201:
            result = response.json()
            logger.info(f"Successfully created branch {branch_name} from {from_branch}")
            return {
                "ref": result.get("ref"),
                "sha": result.get("object", {}).get("sha"),
                "success": True
            }
        elif response.status_code == 422:
            # Branch already exists
            logger.warning(f"Branch {branch_name} already exists")
            return {
                "ref": f"refs/heads/{branch_name}",
                "sha": sha,
                "success": True,
                "already_exists": True
            }
        else:
            raise Exception(f"Failed to create branch: {response.status_code} - {response.text}")

    @staticmethod
    async def find_open_pr(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        head: str,
        base: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find an existing open pull request for the given head and base branches.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            head: Source branch where changes are
            base: Target branch to merge into

        Returns:
            Dict with PR information if found, None otherwise
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Search for open PRs with matching head and base
        url = f"{base_url}/repos/{owner}/{repo}/pulls"
        params = {
            "state": "open",
            "head": f"{owner}:{head}",
            "base": base
        }

        logger.info(f"Searching for open PR: {head} -> {base} in {owner}/{repo}")

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers, params=params)

        if response.status_code == 200:
            prs = response.json()
            if prs and len(prs) > 0:
                pr = prs[0]  # Take the first matching PR
                logger.info(f"Found open PR #{pr.get('number')}: {pr.get('html_url')}")
                return {
                    "number": pr.get("number"),
                    "html_url": pr.get("html_url"),
                    "state": pr.get("state"),
                    "title": pr.get("title"),
                    "head": pr.get("head", {}).get("ref"),
                    "head_sha": pr.get("head", {}).get("sha"),
                    "base": pr.get("base", {}).get("ref"),
                    "draft": pr.get("draft"),
                    "exists": True
                }
            else:
                logger.info(f"No open PR found for {head} -> {base}")
                return None
        else:
            logger.warning(f"Failed to search PRs: {response.status_code} - {response.text}")
            return None

    @staticmethod
    async def create_pull_request(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str = None,
        draft: bool = False
    ) -> Dict[str, Any]:
        """
        Create a pull request from head branch to base branch.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            head: Source branch where changes are
            base: Target branch to merge into (e.g., "main")
            title: Pull request title
            body: Pull request description (optional)
            draft: Create as draft PR (default: False)

        Returns:
            Dict with PR information

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Prepare pull request payload
        url = f"{base_url}/repos/{owner}/{repo}/pulls"
        payload = {
            "title": title,
            "head": head,
            "base": base,
            "draft": draft
        }

        # Add body if provided
        if body:
            payload["body"] = body

        logger.info(
            f"Creating pull request: {head} -> {base} in {owner}/{repo} "
            f"(title_len={len(title or '')} body_len={len(body or '')} draft={draft})"
        )
        # GitHub rejects PR bodies over ~65536 chars — truncate defensively so an
        # oversized body can't 500 the request.
        if body and len(body) > 60000:
            logger.warning(f"PR body too long ({len(body)} chars) — truncating to 60000")
            payload["body"] = body[:60000] + "\n\n… (truncated)"

        # Create pull request
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)

        if response.status_code == 201:
            result = response.json()
            logger.info(f"Successfully created PR #{result.get('number')}: {result.get('html_url')}")

            return {
                "number": result.get("number"),
                "html_url": result.get("html_url"),
                "state": result.get("state"),
                "title": result.get("title"),
                "head": result.get("head", {}).get("ref"),
                "head_sha": result.get("head", {}).get("sha"),
                "base": result.get("base", {}).get("ref"),
                "draft": result.get("draft"),
                "success": True
            }
        elif response.status_code == 422:
            # Handle validation errors (e.g., PR already exists)
            error_data = response.json()
            error_msg = error_data.get("message", "Validation failed")
            errors = error_data.get("errors", [])

            # Check if PR already exists
            if any("pull request already exists" in str(err).lower() for err in errors):
                logger.warning(f"Pull request already exists for {head} -> {base}")
                raise Exception(f"Pull request already exists for branch {head}")
            else:
                logger.error(f"Failed to create PR: {error_msg}, errors: {errors}")
                raise Exception(f"Failed to create pull request: {error_msg}")
        else:
            # Capture GitHub's request id + body so an empty 500 is diagnosable.
            gh_req_id = response.headers.get("x-github-request-id", "?")
            error_msg = (
                f"Failed to create pull request: {response.status_code} - "
                f"{response.text or '<empty body>'} "
                f"[x-github-request-id={gh_req_id}, head={head}, base={base}, "
                f"title_len={len(title or '')}, body_len={len(payload.get('body') or '')}]"
            )
            logger.error(error_msg)
            raise Exception(error_msg)

    @staticmethod
    async def merge_pull_request(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pull_number: int,
        merge_method: str = "squash",
        commit_title: str = None,
        commit_message: str = None,
        expected_head_sha: str = None,
    ) -> Dict[str, Any]:
        """
        Merge a pull request.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            pull_number: PR number to merge
            merge_method: Merge method - "merge", "squash", or "rebase" (default: "squash")
            commit_title: Custom merge commit title (optional)
            commit_message: Custom merge commit message (optional)
            expected_head_sha: SHA-conditional merge — GitHub refuses with
                409 "Head branch was modified" if the PR head no longer
                matches this SHA. Closes the evaluate→merge race on the shared
                prod promotion PR: on that specific 409 the result is
                {"merged": False, "head_moved": True} so the caller
                re-evaluates instead of treating it as a merge conflict.

        Returns:
            Dict with merge result

        Raises:
            Exception: If merge fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        url = f"{base_url}/repos/{owner}/{repo}/pulls/{pull_number}/merge"
        payload = {"merge_method": merge_method}
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message
        if expected_head_sha:
            payload["sha"] = expected_head_sha

        logger.info(f"Merging PR #{pull_number} in {owner}/{repo} (method: {merge_method})")

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.put(url, headers=headers, json=payload)

        if response.status_code == 200:
            result = response.json()
            logger.info(f"Successfully merged PR #{pull_number}: {result.get('sha')}")
            return {
                "merged": True,
                "sha": result.get("sha"),
                "message": result.get("message"),
            }
        elif response.status_code == 405:
            error_msg = response.json().get("message", "PR not mergeable")
            logger.error(f"PR #{pull_number} not mergeable: {error_msg}")
            raise Exception(f"PR #{pull_number} cannot be merged: {error_msg}")
        elif response.status_code == 409:
            # 409 is ambiguous: sha-mismatch ("Head branch was modified.
            # Review and try again.") vs a genuine merge conflict. Only the
            # sha-mismatch is the compare-and-swap miss we retry on.
            try:
                error_msg = response.json().get("message", "")
            except Exception:
                error_msg = response.text or ""
            if expected_head_sha and "head branch was modified" in error_msg.lower():
                logger.warning(
                    f"PR #{pull_number} head moved past {expected_head_sha[:8]} — merge refused"
                )
                return {"merged": False, "head_moved": True, "message": error_msg}
            logger.error(f"PR #{pull_number} has merge conflicts: {error_msg}")
            raise Exception(f"PR #{pull_number} has merge conflicts")
        else:
            error_msg = f"Failed to merge PR #{pull_number}: {response.status_code} - {response.text}"
            logger.error(error_msg)
            raise Exception(error_msg)

    @staticmethod
    def _changed_paths(entries: List[Dict[str, Any]]) -> List[str]:
        """Extract every affected path from PR-files/compare entries.

        Keeps `filename` and, for renames, `previous_filename` too — a rename
        touches BOTH paths (the old location is deleted on merge), and the
        promotion merge gate must classify both. Everything else in the
        response (status, patch, counts) is discarded.
        """
        paths: List[str] = []
        for f in entries:
            if f.get("filename"):
                paths.append(f["filename"])
            if f.get("previous_filename"):
                paths.append(f["previous_filename"])
        return paths

    @staticmethod
    async def list_pull_request_files(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> List[str]:
        """
        List every changed file path in a PR (paginated — PRs cap at 3000 files).

        Used by the prod promotion merge gate to classify the shared
        stage→main PR's content dir-by-dir. Only paths are needed there —
        see _changed_paths for what is kept per entry.

        Returns: list of repo-relative file paths.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        files: List[str] = []
        page = 1
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while True:
                url = (
                    f"{base_url}/repos/{owner}/{repo}/pulls/{pull_number}/files"
                    f"?per_page=100&page={page}"
                )
                response = await client.get(url, headers=headers)
                if response.status_code != 200:
                    raise Exception(
                        f"Failed to list files for PR #{pull_number}: "
                        f"{response.status_code} - {response.text}"
                    )
                batch = response.json()
                files.extend(GitHubIntegration._changed_paths(batch))
                if len(batch) < 100:
                    break
                page += 1
        logger.info(f"PR #{pull_number} in {owner}/{repo}: {len(files)} changed path(s)")
        return files

    @staticmethod
    async def compare_commits(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        base_sha: str,
        head_sha: str,
    ) -> Dict[str, List[str]]:
        """
        Changed file paths AND commit authors between two commits (GitHub
        compare API, paginated).

        Used by the prod workflow's dir-scoped manual-commit check: which
        files landed on stage since our baseline, and WHO committed them.
        The compare API attributes files to the whole range (not to
        individual commits), so authors are returned as a range-level list —
        the caller applies the all-bot rule (every commit bot-authored →
        not manual, whatever it touched).

        Returns: {"files": [paths...], "authors": [github logins...]}
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        files: List[str] = []
        authors: List[str] = []
        page = 1
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while True:
                url = (
                    f"{base_url}/repos/{owner}/{repo}/compare/"
                    f"{base_sha}...{head_sha}?per_page=100&page={page}"
                )
                response = await client.get(url, headers=headers)
                if response.status_code != 200:
                    raise Exception(
                        f"Failed to compare {base_sha[:8]}...{head_sha[:8]}: "
                        f"{response.status_code} - {response.text}"
                    )
                data = response.json()
                batch = data.get("files", [])
                files.extend(GitHubIntegration._changed_paths(batch))
                for c in data.get("commits", []):
                    login = (c.get("author") or {}).get("login") or ""
                    if not login:
                        # No linked GitHub account — fall back to the git
                        # author name so the caller still sees a non-bot.
                        login = ((c.get("commit") or {}).get("author") or {}).get("name") or "unknown"
                    if login not in authors:
                        authors.append(login)
                if len(batch) < 100 and len(data.get("commits", [])) < 100:
                    break
                page += 1
        logger.info(
            f"compare {base_sha[:8]}...{head_sha[:8]} in {owner}/{repo}: "
            f"{len(files)} changed path(s), authors={authors}"
        )
        return {"files": files, "authors": authors}

    @staticmethod
    async def get_pr_reviews(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{base_url}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        logger.warning(f"get_pr_reviews PR #{pull_number}: {response.status_code} - {response.text}")
        return []

    @staticmethod
    async def approve_pull_request(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pull_number: int,
        body: str = "Approved by deployment workflow after successful plan.",
    ) -> Dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{base_url}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
        payload = {"event": "APPROVE", "body": body}

        logger.info(f"Approving PR #{pull_number} in {owner}/{repo}")

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)

        if response.status_code in (200, 201):
            result = response.json()
            logger.info(f"PR #{pull_number} approved (review id={result.get('id')})")
            return {"approved": True, "review_id": result.get("id")}

        error_msg = f"Failed to approve PR #{pull_number}: {response.status_code} - {response.text}"
        logger.error(error_msg)
        raise Exception(error_msg)

    @staticmethod
    async def _find_available_filename(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
        file_path: str,
        headers: Dict[str, str],
        max_attempts: int = 10
    ) -> str:
        """
        Find an available filename by checking if file exists and incrementing version.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Branch name
            file_path: Desired file path
            headers: Request headers
            max_attempts: Maximum versioning attempts

        Returns:
            Available file path (original or versioned)
        """
        # Try original filename first
        if not await GitHubIntegration._file_exists(token, base_url, owner, repo, branch, file_path, headers):
            return file_path

        # File exists, try versioned filenames
        base_path = file_path.rsplit('.', 1)[0]  # Remove extension
        extension = file_path.rsplit('.', 1)[1] if '.' in file_path else ''

        for version in range(2, max_attempts + 2):
            versioned_path = f"{base_path}-v{version}.{extension}" if extension else f"{base_path}-v{version}"

            if not await GitHubIntegration._file_exists(token, base_url, owner, repo, branch, versioned_path, headers):
                logger.info(f"Original file {file_path} exists, using versioned name: {versioned_path}")
                return versioned_path

        # If all versions exist, use timestamp-based name
        import time
        timestamp = int(time.time())
        timestamped_path = f"{base_path}-{timestamp}.{extension}" if extension else f"{base_path}-{timestamp}"
        logger.warning(f"All versions exist, using timestamp: {timestamped_path}")
        return timestamped_path

    @staticmethod
    async def _file_exists(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
        file_path: str,
        headers: Dict[str, str]
    ) -> bool:
        """
        Check if a file exists in the repository.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Branch name
            file_path: File path to check
            headers: Request headers

        Returns:
            True if file exists, False otherwise
        """
        url = f"{base_url}/repos/{owner}/{repo}/contents/{file_path}"
        params = {"ref": branch}

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(url, headers=headers, params=params)
            return response.status_code == 200
        except Exception as e:
            logger.debug(f"Error checking file existence: {e}")
            return False

    @staticmethod
    async def create_empty_commit(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
        message: str
    ) -> Dict[str, Any]:
        """
        Create an empty commit to trigger a workflow.

        An empty commit has the same tree as its parent but with a new commit message.
        This is useful for triggering CI/CD workflows without making actual code changes.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL (default: https://api.github.com)
            owner: Repository owner (username or organization)
            repo: Repository name
            branch: Target branch name
            message: Commit message

        Returns:
            Dict with:
            - commit_sha: SHA of the new commit
            - commit_url: URL to view the commit
            - tree_sha: SHA of the tree (unchanged from parent)

        Raises:
            Exception: If API call fails
        """
        # Prepare headers
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                # Step 1: Get the current branch reference to find the latest commit
                ref_url = f"{base_url}/repos/{owner}/{repo}/git/refs/heads/{branch}"
                ref_response = await client.get(ref_url, headers=headers)

                if ref_response.status_code != 200:
                    raise Exception(
                        f"Failed to get branch reference: {ref_response.status_code} - {ref_response.text}"
                    )

                current_commit_sha = ref_response.json()["object"]["sha"]
                logger.info(f"Current commit SHA for branch '{branch}': {current_commit_sha}")

                # Step 2: Get the current commit details to find its tree
                commit_url = f"{base_url}/repos/{owner}/{repo}/git/commits/{current_commit_sha}"
                commit_response = await client.get(commit_url, headers=headers)

                if commit_response.status_code != 200:
                    raise Exception(
                        f"Failed to get commit details: {commit_response.status_code} - {commit_response.text}"
                    )

                tree_sha = commit_response.json()["tree"]["sha"]
                logger.info(f"Tree SHA: {tree_sha}")

                # Step 3: Create a new commit with the same tree (empty commit)
                create_commit_url = f"{base_url}/repos/{owner}/{repo}/git/commits"
                commit_data = {
                    "message": message,
                    "tree": tree_sha,
                    "parents": [current_commit_sha]
                }

                create_response = await client.post(create_commit_url, headers=headers, json=commit_data)

                if create_response.status_code != 201:
                    raise Exception(
                        f"Failed to create commit: {create_response.status_code} - {create_response.text}"
                    )

                new_commit_sha = create_response.json()["sha"]
                commit_html_url = create_response.json()["html_url"]
                logger.info(f"Created new commit: {new_commit_sha}")

                # Step 4: Update the branch reference to point to the new commit
                update_ref_data = {
                    "sha": new_commit_sha,
                    "force": False  # Don't force push
                }

                update_response = await client.patch(ref_url, headers=headers, json=update_ref_data)

                if update_response.status_code != 200:
                    raise Exception(
                        f"Failed to update branch reference: {update_response.status_code} - {update_response.text}"
                    )

            logger.info(f"Updated branch '{branch}' to new commit {new_commit_sha}")

            return {
                "commit_sha": new_commit_sha,
                "commit_url": commit_html_url,
                "tree_sha": tree_sha,
                "success": True
            }

        except httpx.RequestError as e:
            logger.error(f"Network error creating empty commit: {str(e)}")
            raise Exception(f"Network error: {str(e)}")
        except KeyError as e:
            logger.error(f"Unexpected API response format: {str(e)}")
            raise Exception(f"Invalid API response: {str(e)}")
        except Exception as e:
            logger.error(f"Error creating empty commit: {str(e)}")
            raise

    @staticmethod
    async def trigger_workflow_dispatch(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        workflow_file: str,
        ref: str,
        inputs: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Trigger a GitHub Actions workflow using workflow_dispatch event.

        This method uses the GitHub API to trigger a specific workflow file.
        The workflow must have `workflow_dispatch` trigger enabled in its YAML.

        API: POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches

        Args:
            token: GitHub personal access token or GitHub App installation token
            base_url: GitHub API base URL (default: https://api.github.com)
            owner: Repository owner (username or organization)
            repo: Repository name
            workflow_file: Workflow file name (e.g., "deploy-payment-api-prod.yml")
                          Can be full path or just filename
            ref: Git reference (branch or tag) to run the workflow on
            inputs: Optional dict of workflow inputs (if workflow defines inputs)

        Returns:
            Dict with:
            - success: True if workflow was triggered
            - message: Descriptive message

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Extract just the filename if full path is provided
        # e.g., ".github/workflows/deploy-api.yml" -> "deploy-api.yml"
        if "/" in workflow_file:
            workflow_file = workflow_file.split("/")[-1]

        url = f"{base_url}/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches"

        payload = {
            "ref": ref
        }

        if inputs:
            payload["inputs"] = inputs

        try:
            logger.info(f"Triggering workflow dispatch: {owner}/{repo}/{workflow_file} on ref={ref}")

            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=payload)

            # 204 No Content = Success (GitHub returns no body on success)
            if response.status_code == 204:
                logger.info(f"Workflow dispatch triggered successfully for {workflow_file}")
                return {
                    "success": True,
                    "message": f"Workflow '{workflow_file}' triggered successfully on branch '{ref}'"
                }

            # Handle specific error codes
            if response.status_code == 404:
                error_msg = f"Workflow file '{workflow_file}' not found in {owner}/{repo}, or workflow_dispatch trigger not enabled"
                logger.error(error_msg)
                raise Exception(error_msg)

            if response.status_code == 422:
                error_data = response.json()
                error_msg = f"Invalid request: {error_data.get('message', 'Unknown validation error')}"
                logger.error(error_msg)
                raise Exception(error_msg)

            if response.status_code == 401:
                raise Exception("GitHub authentication failed: Invalid or expired token")

            if response.status_code == 403:
                raise Exception("Permission denied: Token lacks 'actions:write' permission")

            # Generic error
            raise Exception(
                f"Failed to trigger workflow dispatch: {response.status_code} - {response.text}"
            )

        except httpx.RequestError as e:
            logger.error(f"Network error triggering workflow dispatch: {str(e)}")
            raise Exception(f"Network error: {str(e)}")
        except Exception as e:
            logger.error(f"Error triggering workflow dispatch: {str(e)}")
            raise

    @staticmethod
    async def get_workflow_run_by_id(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        run_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get a specific workflow run by its ID.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            run_id: GitHub Actions workflow run ID

        Returns:
            Dict with workflow run details if found, None otherwise
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            url = f"{base_url}/repos/{owner}/{repo}/actions/runs/{run_id}"
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(url, headers=headers)

            if response.status_code != 200:
                logger.warning(f"Failed to get workflow run {run_id}: {response.status_code}")
                return None

            run = response.json()
            return {
                "run_id": str(run["id"]),
                "run_url": run["html_url"],
                "status": run["status"],
                "conclusion": run.get("conclusion"),
                "workflow_name": run["name"],
                "head_sha": run.get("head_sha"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at")
            }

        except Exception as e:
            logger.error(f"Error getting workflow run {run_id}: {str(e)}")
            return None

    @staticmethod
    async def get_latest_workflow_run(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        workflow_file: str,
        branch: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get the latest workflow run for a specific workflow file.

        Useful after triggering workflow_dispatch to find the triggered run.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            workflow_file: Workflow file name (e.g., "deploy-payment-api-prod.yml")
            branch: Optional branch filter

        Returns:
            Dict with workflow run details if found, None otherwise
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Extract just the filename if full path is provided
        if "/" in workflow_file:
            workflow_file = workflow_file.split("/")[-1]

        try:
            url = f"{base_url}/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs"
            params = {
                "per_page": 1
            }
            if branch:
                params["branch"] = branch

            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(url, headers=headers, params=params)

            if response.status_code != 200:
                logger.warning(f"Failed to get workflow runs: {response.status_code}")
                return None

            data = response.json()
            workflow_runs = data.get("workflow_runs", [])

            if not workflow_runs:
                logger.info(f"No workflow runs found for {workflow_file}")
                return None

            run = workflow_runs[0]
            return {
                "run_id": str(run["id"]),
                "run_url": run["html_url"],
                "status": run["status"],
                "conclusion": run.get("conclusion"),
                "workflow_name": run["name"],
                "head_sha": run.get("head_sha"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at")
            }

        except Exception as e:
            logger.error(f"Error getting latest workflow run: {str(e)}")
            return None

    @staticmethod
    async def list_workflow_runs(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        workflow_file: str = None,
        branch: str = None,
        status: str = "success",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        List workflow runs for a repo, optionally scoped to a workflow file and/or branch.
        When workflow_file is omitted, fetches all runs filtered by branch.
        Returns deployment history entries sorted newest-first.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        params: Dict[str, Any] = {"per_page": limit}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status

        try:
            if workflow_file:
                if "/" in workflow_file:
                    workflow_file = workflow_file.split("/")[-1]
                url = f"{base_url}/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs"
            else:
                url = f"{base_url}/repos/{owner}/{repo}/actions/runs"
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(url, headers=headers, params=params)

            if response.status_code != 200:
                logger.warning(f"Failed to list workflow runs: {response.status_code} {response.text}")
                return []

            runs = response.json().get("workflow_runs", [])
            result = []
            for run in runs:
                result.append({
                    "run_id": str(run["id"]),
                    "run_url": run["html_url"],
                    "status": run["status"],
                    "conclusion": run.get("conclusion"),
                    "workflow_name": run["name"],
                    "head_sha": run.get("head_sha", ""),
                    "head_branch": run.get("head_branch", ""),
                    "head_commit_message": (run.get("head_commit") or {}).get("message", ""),
                    "head_commit_author": (run.get("head_commit") or {}).get("author", {}).get("name", ""),
                    "triggering_actor": (run.get("triggering_actor") or {}).get("login", ""),
                    "created_at": run.get("created_at"),
                    "updated_at": run.get("updated_at"),
                    "run_started_at": run.get("run_started_at"),
                })
            return result

        except Exception as e:
            logger.error(f"Error listing workflow runs: {str(e)}")
            return []

    @staticmethod
    async def get_workflow_runs_by_commit(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        commit_sha: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get workflow runs triggered by a specific commit.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL (default: https://api.github.com)
            owner: Repository owner (username or organization)
            repo: Repository name
            commit_sha: Commit SHA to search for

        Returns:
            Dict with workflow run details if found, None otherwise:
            - run_id: GitHub Actions run ID
            - run_url: URL to view the workflow run
            - status: Workflow run status
            - conclusion: Workflow run conclusion

        Raises:
            Exception: If API call fails
        """
        # Prepare headers
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            # Get workflow runs for the specific commit
            url = f"{base_url}/repos/{owner}/{repo}/actions/runs"
            params = {
                "head_sha": commit_sha,
                "per_page": 1  # We only need the first run
            }

            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(url, headers=headers, params=params)

            if response.status_code != 200:
                logger.warning(
                    f"Failed to get workflow runs: {response.status_code} - {response.text}"
                )
                return None

            data = response.json()
            workflow_runs = data.get("workflow_runs", [])

            if not workflow_runs:
                logger.debug(f"No workflow runs found for commit {commit_sha}")
                return None

            # Get the first (most recent) workflow run
            run = workflow_runs[0]

            return {
                "run_id": str(run["id"]),
                "run_url": run["html_url"],
                "status": run["status"],
                "conclusion": run.get("conclusion"),
                "workflow_name": run["name"]
            }

        except httpx.RequestError as e:
            logger.error(f"Network error getting workflow runs: {str(e)}")
            return None
        except (KeyError, IndexError) as e:
            logger.error(f"Unexpected API response format: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Error getting workflow runs: {str(e)}")
            return None

    @staticmethod
    async def get_pull_request(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pr_number: int
    ) -> Dict[str, Any]:
        """
        Get details of a specific pull request by number.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number

        Returns:
            Dict with PR details:
            - number: PR number
            - state: PR state (open, closed)
            - merged: Whether PR was merged
            - title: PR title
            - html_url: URL to the PR
            - head_branch: Source branch
            - base_branch: Target branch
            - merged_at: Merge timestamp (if merged)
            - closed_at: Close timestamp (if closed)

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        url = f"{base_url}/repos/{owner}/{repo}/pulls/{pr_number}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers)

        if response.status_code == 200:
            pr = response.json()
            logger.info(f"Fetched PR #{pr_number}: state={pr.get('state')}, merged={pr.get('merged')}")

            return {
                "number": pr.get("number"),
                "state": pr.get("state"),  # "open" or "closed"
                "merged": pr.get("merged", False),
                "title": pr.get("title"),
                "html_url": pr.get("html_url"),
                "head_branch": pr.get("head", {}).get("ref"),
                "head_sha": pr.get("head", {}).get("sha"),
                "base_branch": pr.get("base", {}).get("ref"),
                "merged_at": pr.get("merged_at"),
                "closed_at": pr.get("closed_at"),
                "created_at": pr.get("created_at"),
                "updated_at": pr.get("updated_at")
            }
        elif response.status_code == 404:
            raise Exception(f"Pull request #{pr_number} not found in {owner}/{repo}")
        elif response.status_code == 401:
            raise Exception("GitHub authentication failed: Invalid or expired token")
        elif response.status_code == 403:
            raise Exception("GitHub API rate limit exceeded or insufficient permissions")
        else:
            raise Exception(f"Failed to fetch PR #{pr_number}: {response.status_code} - {response.text}")

    @staticmethod
    async def get_commit(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        sha: str,
    ) -> Dict[str, Any]:
        """
        Get details of a specific commit by SHA.
        Returns dict with 'sha', 'author' (GitHub user with 'login'), 'commit' (git metadata).
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{base_url}/repos/{owner}/{repo}/commits/{sha}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            raise Exception(f"Commit {sha} not found in {owner}/{repo}")
        else:
            raise Exception(f"Failed to fetch commit {sha}: {response.status_code} - {response.text}")

    @staticmethod
    async def update_pull_request(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pr_number: int,
        state: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Update a pull request (change state, title, or body).

        Used to reopen a PR that was auto-closed due to empty diff.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number
            state: New state ('open' or 'closed')
            title: New title (optional)
            body: New body (optional)

        Returns:
            Dict with updated PR details

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        url = f"{base_url}/repos/{owner}/{repo}/pulls/{pr_number}"

        # Build update payload with only provided fields
        data = {}
        if state is not None:
            data["state"] = state
        if title is not None:
            data["title"] = title
        if body is not None:
            data["body"] = body

        if not data:
            raise ValueError("At least one of state, title, or body must be provided")

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.patch(url, headers=headers, json=data)

        if response.status_code == 200:
            pr = response.json()
            logger.info(f"Updated PR #{pr_number}: state={pr.get('state')}")

            return {
                "number": pr.get("number"),
                "state": pr.get("state"),
                "merged": pr.get("merged", False),
                "title": pr.get("title"),
                "html_url": pr.get("html_url"),
                "head_branch": pr.get("head", {}).get("ref"),
                "base_branch": pr.get("base", {}).get("ref")
            }
        elif response.status_code == 404:
            raise Exception(f"Pull request #{pr_number} not found in {owner}/{repo}")
        elif response.status_code == 401:
            raise Exception("GitHub authentication failed: Invalid or expired token")
        elif response.status_code == 403:
            raise Exception("GitHub API rate limit exceeded or insufficient permissions")
        elif response.status_code == 422:
            # Validation failed - could be PR is merged and can't be reopened
            error_msg = response.json().get("message", "Validation failed")
            raise Exception(f"Cannot update PR #{pr_number}: {error_msg}")
        else:
            raise Exception(f"Failed to update PR #{pr_number}: {response.status_code} - {response.text}")

    @staticmethod
    async def get_pr_comments(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pr_number: int
    ) -> List[Dict[str, Any]]:
        """
        Get all comments on a pull request.

        Uses the issues API since PR comments are issue comments in GitHub.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number

        Returns:
            List of comment dicts with: id, body, created_at, updated_at, user

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        url = f"{base_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers, params={"per_page": 100})

        if response.status_code == 200:
            comments = response.json()
            logger.info(f"Fetched {len(comments)} comments for PR #{pr_number}")

            return [
                {
                    "id": comment.get("id"),
                    "body": comment.get("body", ""),
                    "created_at": comment.get("created_at"),
                    "updated_at": comment.get("updated_at"),
                    "user": comment.get("user", {}).get("login")
                }
                for comment in comments
            ]
        elif response.status_code == 404:
            raise Exception(f"Pull request #{pr_number} not found in {owner}/{repo}")
        elif response.status_code == 401:
            raise Exception("GitHub authentication failed: Invalid or expired token")
        elif response.status_code == 403:
            raise Exception("GitHub API rate limit exceeded or insufficient permissions")
        else:
            raise Exception(f"Failed to fetch PR comments: {response.status_code} - {response.text}")

    @staticmethod
    async def post_pr_comment(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "DevLift-Temporal-Worker",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{base_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json={"body": body})
        if resp.status_code not in (200, 201):
            raise Exception(f"Failed to post comment on PR #{pr_number}: {resp.status_code} — {resp.text}")

    @staticmethod
    async def close_pull_request(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        pr_number: int,
        comment: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Close a pull request with an optional comment.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number to close
            comment: Optional comment to add before closing

        Returns:
            Dict with:
            - number: PR number
            - state: New state (should be "closed")
            - html_url: URL to the PR
            - success: True if closed successfully

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                # Add comment first if provided
                if comment:
                    comment_url = f"{base_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
                    comment_response = await client.post(
                        comment_url,
                        headers=headers,
                        json={"body": comment}
                    )
                    if comment_response.status_code == 201:
                        logger.info(f"Added comment to PR #{pr_number}")
                    else:
                        logger.warning(f"Failed to add comment to PR #{pr_number}: {comment_response.status_code}")

                # Close the PR
                url = f"{base_url}/repos/{owner}/{repo}/pulls/{pr_number}"
                response = await client.patch(url, headers=headers, json={"state": "closed"})

            if response.status_code == 200:
                pr = response.json()
                logger.info(f"Successfully closed PR #{pr_number}")
                return {
                    "number": pr.get("number"),
                    "state": pr.get("state"),
                    "html_url": pr.get("html_url"),
                    "success": True
                }
            elif response.status_code == 404:
                raise Exception(f"Pull request #{pr_number} not found in {owner}/{repo}")
            elif response.status_code == 401:
                raise Exception("GitHub authentication failed: Invalid or expired token")
            elif response.status_code == 403:
                raise Exception("GitHub API rate limit exceeded or insufficient permissions")
            else:
                raise Exception(f"Failed to close PR #{pr_number}: {response.status_code} - {response.text}")

        except httpx.RequestError as e:
            logger.error(f"Network error closing PR #{pr_number}: {str(e)}")
            raise Exception(f"Network error: {str(e)}")

    @staticmethod
    async def delete_branch(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str
    ) -> Dict[str, Any]:
        """
        Delete a branch from the repository.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Branch name to delete

        Returns:
            Dict with:
            - branch: Branch name that was deleted
            - success: True if deleted successfully

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        url = f"{base_url}/repos/{owner}/{repo}/git/refs/heads/{branch}"

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.delete(url, headers=headers)

            if response.status_code == 204:
                logger.info(f"Successfully deleted branch '{branch}' from {owner}/{repo}")
                return {
                    "branch": branch,
                    "success": True
                }
            elif response.status_code == 404:
                # Branch doesn't exist - consider this a success (already deleted)
                logger.warning(f"Branch '{branch}' not found in {owner}/{repo} - may already be deleted")
                return {
                    "branch": branch,
                    "success": True,
                    "already_deleted": True
                }
            elif response.status_code == 401:
                raise Exception("GitHub authentication failed: Invalid or expired token")
            elif response.status_code == 403:
                raise Exception("GitHub API rate limit exceeded or insufficient permissions")
            elif response.status_code == 422:
                raise Exception(f"Cannot delete branch '{branch}': may be protected or is the default branch")
            else:
                raise Exception(f"Failed to delete branch '{branch}': {response.status_code} - {response.text}")

        except httpx.RequestError as e:
            logger.error(f"Network error deleting branch '{branch}': {str(e)}")
            raise Exception(f"Network error: {str(e)}")

    @staticmethod
    async def commit_multiple_files(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
        files: List[Dict[str, str]],
        message: str
    ) -> Dict[str, Any]:
        """
        Commit multiple files in a single commit using Git Tree API.

        This method creates a single commit with multiple file changes, which prevents
        triggering workflows multiple times.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Target branch name
            files: List of dicts with 'path' and 'content' keys
                   Example: [{'path': '.github/workflows/deploy.yml', 'content': '...'}]
            message: Commit message

        Returns:
            Dict with:
            - commit_sha: SHA of the commit
            - commit_url: URL to view the commit
            - tree_sha: SHA of the tree
            - files_committed: List of file paths committed

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                # Step 1: Get the current branch reference
                ref_url = f"{base_url}/repos/{owner}/{repo}/git/refs/heads/{branch}"
                ref_response = await client.get(ref_url, headers=headers)

                if ref_response.status_code != 200:
                    raise Exception(
                        f"Failed to get branch reference: {ref_response.status_code} - {ref_response.text}"
                    )

                current_commit_sha = ref_response.json()["object"]["sha"]
                logger.info(f"Current commit SHA for branch '{branch}': {current_commit_sha}")

                # Step 2: Get the current commit to find its tree
                commit_url = f"{base_url}/repos/{owner}/{repo}/git/commits/{current_commit_sha}"
                commit_response = await client.get(commit_url, headers=headers)

                if commit_response.status_code != 200:
                    raise Exception(
                        f"Failed to get commit details: {commit_response.status_code} - {commit_response.text}"
                    )

                base_tree_sha = commit_response.json()["tree"]["sha"]
                logger.info(f"Base tree SHA: {base_tree_sha}")

                # Step 3: Create blobs for each file
                blobs = []
                for file in files:
                    file_path = file['path']
                    file_content = file['content']

                    # Create blob for file content
                    blob_url = f"{base_url}/repos/{owner}/{repo}/git/blobs"
                    blob_data = {
                        "content": file_content,
                        "encoding": "utf-8"
                    }

                    blob_response = await client.post(blob_url, headers=headers, json=blob_data)

                    if blob_response.status_code != 201:
                        raise Exception(
                            f"Failed to create blob for {file_path}: {blob_response.status_code} - {blob_response.text}"
                        )

                    blob_sha = blob_response.json()["sha"]
                    blobs.append({
                        "path": file_path,
                        "mode": "100644",  # Regular file
                        "type": "blob",
                        "sha": blob_sha
                    })
                    logger.info(f"Created blob for {file_path}: {blob_sha}")

                # Step 4: Create a new tree with all the blobs
                tree_url = f"{base_url}/repos/{owner}/{repo}/git/trees"
                tree_data = {
                    "base_tree": base_tree_sha,
                    "tree": blobs
                }

                tree_response = await client.post(tree_url, headers=headers, json=tree_data)

                if tree_response.status_code != 201:
                    raise Exception(
                        f"Failed to create tree: {tree_response.status_code} - {tree_response.text}"
                    )

                new_tree_sha = tree_response.json()["sha"]
                logger.info(f"Created new tree: {new_tree_sha}")

                # Step 5: Create a new commit with the new tree
                create_commit_url = f"{base_url}/repos/{owner}/{repo}/git/commits"
                commit_data = {
                    "message": message,
                    "tree": new_tree_sha,
                    "parents": [current_commit_sha]
                }

                create_response = await client.post(create_commit_url, headers=headers, json=commit_data)

                if create_response.status_code != 201:
                    raise Exception(
                        f"Failed to create commit: {create_response.status_code} - {create_response.text}"
                    )

                new_commit_sha = create_response.json()["sha"]
                commit_html_url = create_response.json()["html_url"]
                logger.info(f"Created new commit: {new_commit_sha}")

                # Step 6: Update the branch reference to point to the new commit
                update_ref_data = {
                    "sha": new_commit_sha,
                    "force": False
                }

                update_response = await client.patch(ref_url, headers=headers, json=update_ref_data)

                if update_response.status_code != 200:
                    raise Exception(
                        f"Failed to update branch reference: {update_response.status_code} - {update_response.text}"
                    )

            logger.info(f"Updated branch '{branch}' to commit {new_commit_sha}")

            return {
                "commit_sha": new_commit_sha,
                "commit_url": commit_html_url,
                "tree_sha": new_tree_sha,
                "files_committed": [f["path"] for f in files],
                "success": True
            }

        except httpx.RequestError as e:
            logger.error(f"Network error committing multiple files: {str(e)}")
            raise Exception(f"Network error: {str(e)}")
        except KeyError as e:
            logger.error(f"Unexpected API response format: {str(e)}")
            raise Exception(f"Invalid API response: {str(e)}")
        except Exception as e:
            logger.error(f"Error committing multiple files: {str(e)}")
            raise

    @staticmethod
    async def reset_branch_to_ref(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
        target_sha: str,
        force: bool = True
    ) -> Dict[str, Any]:
        """
        Reset a branch to point to a specific SHA.

        Used for PR refresh - reset feature branch to latest base branch SHA
        before regenerating files and force-pushing.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Branch name to reset
            target_sha: SHA to reset the branch to
            force: Force update even if not fast-forward (default: True)

        Returns:
            Dict with:
            - ref: The updated ref
            - sha: The new SHA
            - success: True if reset successfully

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            ref_url = f"{base_url}/repos/{owner}/{repo}/git/refs/heads/{branch}"

            update_data = {
                "sha": target_sha,
                "force": force
            }

            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.patch(ref_url, headers=headers, json=update_data)

            if response.status_code == 200:
                result = response.json()
                logger.info(f"Reset branch '{branch}' to SHA {target_sha}")
                return {
                    "ref": result.get("ref"),
                    "sha": result.get("object", {}).get("sha"),
                    "success": True
                }
            elif response.status_code == 404:
                raise Exception(f"Branch '{branch}' not found in {owner}/{repo}")
            elif response.status_code == 401:
                raise Exception("GitHub authentication failed: Invalid or expired token")
            elif response.status_code == 403:
                raise Exception("GitHub API rate limit exceeded or insufficient permissions")
            elif response.status_code == 422:
                raise Exception(f"Cannot reset branch '{branch}': may be protected")
            else:
                raise Exception(
                    f"Failed to reset branch '{branch}': {response.status_code} - {response.text}"
                )

        except httpx.RequestError as e:
            logger.error(f"Network error resetting branch '{branch}': {str(e)}")
            raise Exception(f"Network error: {str(e)}")

    @staticmethod
    async def force_commit_from_parent(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
        parent_sha: str,
        files: List[Dict[str, str]],
        message: str,
    ) -> Dict[str, Any]:
        """
        Rebases a feature branch onto parent_sha by creating a new commit
        whose parent is parent_sha (current base branch HEAD) and whose tree
        starts from parent_sha's tree (stage tree) with the given files overlaid.

        Using the parent's tree as base_tree preserves all files currently on the
        base branch (including any added after the feature branch was created),
        preventing spurious file deletions in the PR diff.

        Steps:
          1. GET parent commit → extract tree SHA
          2. POST blob for each file in `files`
          3. POST tree: base_tree=parent_tree_sha + new blobs
          4. POST commit: parents=[parent_sha]  ← base branch HEAD, fixes merge base
          5. PATCH ref with force=True  ← PR stays open, Atlantis lock held
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                # Step 1: Get parent (base branch) tree SHA — use as base_tree
                # so all current stage files are inherited (no spurious deletions)
                commit_url = f"{base_url}/repos/{owner}/{repo}/git/commits/{parent_sha}"
                commit_response = await client.get(commit_url, headers=headers)
                if commit_response.status_code != 200:
                    raise Exception(
                        f"Failed to get parent commit {parent_sha}: "
                        f"{commit_response.status_code} - {commit_response.text}"
                    )
                parent_tree_sha = commit_response.json()["tree"]["sha"]
                logger.info(f"force_commit_from_parent: parent tree={parent_tree_sha}")

                # Step 2: Create blobs for each changed file
                blobs = []
                for file in files:
                    blob_response = await client.post(
                        f"{base_url}/repos/{owner}/{repo}/git/blobs",
                        headers=headers,
                        json={"content": file["content"], "encoding": "utf-8"},
                    )
                    if blob_response.status_code != 201:
                        raise Exception(
                            f"Failed to create blob for {file['path']}: "
                            f"{blob_response.status_code} - {blob_response.text}"
                        )
                    blob_sha = blob_response.json()["sha"]
                    blobs.append({"path": file["path"], "mode": "100644", "type": "blob", "sha": blob_sha})
                    logger.info(f"force_commit_from_parent: blob {file['path']}={blob_sha}")

                # Step 3: Create tree — inherit all stage files, overlay our generated files
                tree_response = await client.post(
                    f"{base_url}/repos/{owner}/{repo}/git/trees",
                    headers=headers,
                    json={"base_tree": parent_tree_sha, "tree": blobs},
                )
                if tree_response.status_code != 201:
                    raise Exception(
                        f"Failed to create tree: {tree_response.status_code} - {tree_response.text}"
                    )
                new_tree_sha = tree_response.json()["sha"]
                logger.info(f"force_commit_from_parent: new tree={new_tree_sha}")

                # Step 4: Create commit whose parent is the base branch HEAD
                create_response = await client.post(
                    f"{base_url}/repos/{owner}/{repo}/git/commits",
                    headers=headers,
                    json={"message": message, "tree": new_tree_sha, "parents": [parent_sha]},
                )
                if create_response.status_code != 201:
                    raise Exception(
                        f"Failed to create commit: {create_response.status_code} - {create_response.text}"
                    )
                new_commit_sha = create_response.json()["sha"]
                logger.info(f"force_commit_from_parent: new commit={new_commit_sha} parent={parent_sha[:8]}")

                # Step 5: Force-push feature branch
                ref_response = await client.patch(
                    f"{base_url}/repos/{owner}/{repo}/git/refs/heads/{branch}",
                    headers=headers,
                    json={"sha": new_commit_sha, "force": True},
                )
                if ref_response.status_code != 200:
                    raise Exception(
                        f"Failed to force-update branch '{branch}': "
                        f"{ref_response.status_code} - {ref_response.text}"
                    )

                logger.info(f"force_commit_from_parent: '{branch}' → {new_commit_sha}")
                return {
                    "commit_sha": new_commit_sha,
                    "tree_sha": new_tree_sha,
                    "parent_sha": parent_sha,
                    "files_committed": [f["path"] for f in files],
                    "success": True,
                }

        except httpx.RequestError as e:
            logger.error(f"Network error in force_commit_from_parent: {str(e)}")
            raise Exception(f"Network error: {str(e)}")
        except Exception:
            raise

    @staticmethod
    async def compare_branches(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        base: str,
        head: str
    ) -> Dict[str, Any]:
        """
        Compare two branches to check if head is behind or ahead of base.

        Useful for detecting stale PRs that need refresh.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            base: Base branch (e.g., "main")
            head: Head branch (e.g., "feature-branch")

        Returns:
            Dict with:
            - status: "ahead", "behind", "diverged", or "identical"
            - ahead_by: Number of commits head is ahead
            - behind_by: Number of commits head is behind
            - total_commits: Total number of commits in comparison
            - base_commit: SHA of base branch head
            - head_commit: SHA of head branch head
            - is_stale: True if head is behind base (needs refresh)

        Raises:
            Exception: If API call fails
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            # GitHub compare API: GET /repos/{owner}/{repo}/compare/{base}...{head}
            url = f"{base_url}/repos/{owner}/{repo}/compare/{base}...{head}"
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(url, headers=headers)

            if response.status_code == 200:
                data = response.json()

                status = data.get("status")  # "ahead", "behind", "diverged", or "identical"
                ahead_by = data.get("ahead_by", 0)
                behind_by = data.get("behind_by", 0)

                logger.info(
                    f"Branch comparison {base}...{head}: status={status}, "
                    f"ahead_by={ahead_by}, behind_by={behind_by}"
                )

                return {
                    "status": status,
                    "ahead_by": ahead_by,
                    "behind_by": behind_by,
                    "total_commits": data.get("total_commits", 0),
                    "base_commit": data.get("base_commit", {}).get("sha"),
                    "head_commit": data.get("merge_base_commit", {}).get("sha"),
                    "is_stale": behind_by > 0,
                    "success": True
                }
            elif response.status_code == 404:
                raise Exception(f"One or both branches not found: {base}, {head}")
            elif response.status_code == 401:
                raise Exception("GitHub authentication failed: Invalid or expired token")
            elif response.status_code == 403:
                raise Exception("GitHub API rate limit exceeded or insufficient permissions")
            else:
                raise Exception(
                    f"Failed to compare branches: {response.status_code} - {response.text}"
                )

        except httpx.RequestError as e:
            logger.error(f"Network error comparing branches: {str(e)}")
            raise Exception(f"Network error: {str(e)}")

    @staticmethod
    async def get_branch_diff(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        base: str,
        head: str
    ) -> "DiffResult":
        """
        Get the per-file diff between a base branch and a head branch.

        Uses the GitHub compare API (`/compare/{base}...{head}`, which diffs from the
        merge-base, so it returns only the head branch's own changes). Each file carries
        its real unified diff (`patch`). Returns a typed DiffResult so callers can tell a
        genuinely empty diff from an auth/rate-limit/network/repo failure (never raises).
        GitHub omits `patch` for binary/very large files — those come back with patch=None.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            url = f"{base_url}/repos/{owner}/{repo}/compare/{base}...{head}"
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(url, headers=headers)
        except httpx.RequestError as e:
            logger.warning(f"get_branch_diff network error for {base}...{head}: {e}")
            return DiffResult(status=DiffStatus.NETWORK)
        except Exception as e:
            logger.warning(f"get_branch_diff unexpected error for {base}...{head}: {e}")
            return DiffResult(status=DiffStatus.REPO_ERROR)

        if response.status_code == 401:
            logger.warning(f"get_branch_diff auth failure for {base}...{head}")
            return DiffResult(status=DiffStatus.AUTH)
        if response.status_code == 403:
            logger.warning(f"get_branch_diff rate-limited/forbidden for {base}...{head}")
            return DiffResult(status=DiffStatus.RATE_LIMIT)
        if response.status_code != 200:
            logger.warning(
                f"get_branch_diff {base}...{head} returned {response.status_code}: "
                f"{response.text[:200]}"
            )
            return DiffResult(status=DiffStatus.REPO_ERROR)

        files = response.json().get("files", []) or []
        diff: List[Dict[str, Any]] = []
        for f in files:
            diff.append({
                "path": f.get("filename", "unknown"),
                "status": f.get("status", "modified"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                # `patch` is absent for binary/large files — preserve None (don't coerce to "")
                "patch": f.get("patch"),
                "previous_filename": f.get("previous_filename"),  # set for renames
            })
        logger.info(f"get_branch_diff {base}...{head}: {len(diff)} changed file(s)")
        return DiffResult(status=DiffStatus.OK if diff else DiffStatus.EMPTY, files=diff)

    @staticmethod
    async def get_branch_sha(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str
    ) -> Optional[str]:
        """
        Get the current SHA of a branch.

        Args:
            token: GitHub personal access token
            base_url: GitHub API base URL
            owner: Repository owner
            repo: Repository name
            branch: Branch name

        Returns:
            SHA of the branch head, or None if branch not found

        Raises:
            Exception: If API call fails (except 404)
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        try:
            ref_url = f"{base_url}/repos/{owner}/{repo}/git/refs/heads/{branch}"
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(ref_url, headers=headers)

            if response.status_code == 200:
                sha = response.json()["object"]["sha"]
                logger.info(f"Branch '{branch}' SHA: {sha}")
                return sha
            elif response.status_code == 404:
                logger.warning(f"Branch '{branch}' not found")
                return None
            elif response.status_code == 401:
                raise Exception("GitHub authentication failed: Invalid or expired token")
            elif response.status_code == 403:
                raise Exception("GitHub API rate limit exceeded or insufficient permissions")
            else:
                raise Exception(
                    f"Failed to get branch SHA: {response.status_code} - {response.text}"
                )

        except httpx.RequestError as e:
            logger.error(f"Network error getting branch SHA: {str(e)}")
            raise Exception(f"Network error: {str(e)}")

    @staticmethod
    async def get_branch_latest_commit_time(
        token: str,
        base_url: str,
        owner: str,
        repo: str,
        branch: str,
    ) -> Optional[str]:
        """
        Returns the committer date (ISO 8601) of the latest commit on a branch.
        Returns None if the branch has no commits or the request fails.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "RegObs-App",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{base_url}/repos/{owner}/{repo}/commits?sha={branch}&per_page=1"
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(url, headers=headers)

            if response.status_code == 200:
                commits = response.json()
                if commits:
                    commit_date = (
                        commits[0].get("commit", {}).get("committer", {}).get("date")
                        or commits[0].get("commit", {}).get("author", {}).get("date")
                    )
                    return commit_date
                return None
            elif response.status_code == 404:
                logger.warning(f"Branch '{branch}' not found in {owner}/{repo}")
                return None
            else:
                raise Exception(
                    f"Failed to get branch commits: {response.status_code} - {response.text}"
                )
        except httpx.RequestError as e:
            logger.error(f"Network error getting branch latest commit time: {str(e)}")
            raise Exception(f"Network error: {str(e)}")
