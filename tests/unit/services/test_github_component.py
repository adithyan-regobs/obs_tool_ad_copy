"""
Unit tests for GitHubGitOpsHandler.
"""

from unittest.mock import patch

from app.services.gitops.github_component import GitHubGitOpsHandler


def test_create_commit_skip_if_exists():
    handler = GitHubGitOpsHandler()
    files = [{"path": "README.md", "content": "new content"}]

    with patch.object(handler, "_get_github_token", return_value="token"):
        with patch("app.services.gitops.github_component.GitHubIntegration.get_file_content") as get_file:
            with patch("app.services.gitops.github_component.GitHubIntegration.create_branch") as create_branch:
                with patch("app.services.gitops.github_component.GitHubIntegration.commit_multiple_files") as commit:
                    get_file.return_value = {"exists": True, "content": "existing"}

                    result = handler.create_commit(
                        owner="owner",
                        repo="repo",
                        base_branch="main",
                        feature_branch="feature",
                        files=files,
                        commit_message="msg",
                        skip_if_exists=True
                    )

                    assert result["status"] == "skipped"
                    assert result["existing_files"] == ["README.md"]
                    create_branch.assert_not_called()
                    commit.assert_not_called()


def test_create_commit_no_changes():
    handler = GitHubGitOpsHandler()
    files = [{"path": "README.md", "content": "same"}]

    with patch.object(handler, "_get_github_token", return_value="token"):
        with patch("app.services.gitops.github_component.GitHubIntegration.get_file_content") as get_file:
            with patch("app.services.gitops.github_component.GitHubIntegration.create_branch") as create_branch:
                with patch("app.services.gitops.github_component.GitHubIntegration.commit_multiple_files") as commit:
                    get_file.return_value = {"exists": True, "content": "same"}

                    result = handler.create_commit(
                        owner="owner",
                        repo="repo",
                        base_branch="main",
                        feature_branch="feature",
                        files=files,
                        commit_message="msg"
                    )

                    assert result["status"] == "no_changes"
                    assert result["unchanged_files"] == ["README.md"]
                    create_branch.assert_not_called()
                    commit.assert_not_called()


def test_create_commit_success():
    handler = GitHubGitOpsHandler()
    files = [{"path": "README.md", "content": "new content"}]

    with patch.object(handler, "_get_github_token", return_value="token"):
        with patch("app.services.gitops.github_component.GitHubIntegration.get_file_content") as get_file:
            with patch("app.services.gitops.github_component.GitHubIntegration.create_branch") as create_branch:
                with patch("app.services.gitops.github_component.GitHubIntegration.commit_multiple_files") as commit:
                    get_file.return_value = {"exists": False}
                    create_branch.return_value = {"already_exists": False}
                    commit.return_value = {
                        "commit_sha": "abc123",
                        "commit_url": "https://github.com/owner/repo/commit/abc123",
                        "files_committed": ["README.md"]
                    }

                    result = handler.create_commit(
                        owner="owner",
                        repo="repo",
                        base_branch="main",
                        feature_branch="feature",
                        files=files,
                        commit_message="msg"
                    )

                    assert result["status"] == "success"
                    assert result["commit_sha"] == "abc123"
                    assert result["files_committed"] == ["README.md"]
                    assert result["branch_already_exists"] is False
                    create_branch.assert_called_once()
                    commit.assert_called_once()


def test_create_pr_existing():
    handler = GitHubGitOpsHandler()

    with patch.object(handler, "_get_github_token", return_value="token"):
        with patch("app.services.gitops.github_component.GitHubIntegration.find_open_pr") as find_pr:
            with patch("app.services.gitops.github_component.GitHubIntegration.create_pull_request") as create_pr:
                find_pr.return_value = {
                    "number": 12,
                    "html_url": "https://github.com/owner/repo/pull/12"
                }

                result = handler.create_pr(
                    owner="owner",
                    repo="repo",
                    base_branch="main",
                    feature_branch="feature",
                    pr_title="title"
                )

                assert result["status"] == "success"
                assert result["pr_number"] == 12
                create_pr.assert_not_called()


def test_create_pr_new():
    handler = GitHubGitOpsHandler()

    with patch.object(handler, "_get_github_token", return_value="token"):
        with patch("app.services.gitops.github_component.GitHubIntegration.find_open_pr") as find_pr:
            with patch("app.services.gitops.github_component.GitHubIntegration.create_pull_request") as create_pr:
                find_pr.return_value = None
                create_pr.return_value = {
                    "number": 33,
                    "html_url": "https://github.com/owner/repo/pull/33"
                }

                result = handler.create_pr(
                    owner="owner",
                    repo="repo",
                    base_branch="main",
                    feature_branch="feature",
                    pr_title="title"
                )

                assert result["status"] == "success"
                assert result["pr_number"] == 33
                create_pr.assert_called_once()
