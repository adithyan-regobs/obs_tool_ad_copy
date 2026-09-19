class GitHubValidationError(ValueError):
    """Custom exception for GitHub validation errors"""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


class GitHubValidator:
    """Validator for GitHub integration business rules"""

    @staticmethod
    def validate_repository_params(owner: str, repo: str) -> None:
        """
        Validate repository owner and name parameters.

        Args:
            owner: Repository owner (username or organization)
            repo: Repository name

        Raises:
            GitHubValidationError: If parameters are invalid
        """
        errors: list[str] = []

        if not owner or not owner.strip():
            errors.append("owner cannot be empty")

        if not repo or not repo.strip():
            errors.append("repo cannot be empty")

        # GitHub username/org name rules
        if owner and len(owner) > 39:
            errors.append("owner cannot exceed 39 characters")

        # GitHub repo name rules
        if repo and len(repo) > 100:
            errors.append("repo name cannot exceed 100 characters")

        if errors:
            raise GitHubValidationError(errors)

    @staticmethod
    def validate_pagination(page: int, per_page: int) -> None:
        """
        Validate pagination parameters for GitHub API.

        Args:
            page: Page number (minimum 1)
            per_page: Items per page (1-100)

        Raises:
            GitHubValidationError: If pagination parameters are invalid
        """
        errors: list[str] = []

        if page < 1:
            errors.append("page must be at least 1")

        if per_page < 1:
            errors.append("per_page must be at least 1")

        if per_page > 100:
            errors.append("per_page cannot exceed 100 (GitHub API limit)")

        if errors:
            raise GitHubValidationError(errors)

    @staticmethod
    def validate_token_format(token: str) -> None:
        """
        Basic validation for GitHub token format.

        Args:
            token: GitHub personal access token

        Raises:
            GitHubValidationError: If token format is invalid
        """
        errors: list[str] = []

        if not token or not token.strip():
            errors.append("GitHub token cannot be empty")

        # GitHub tokens typically start with specific prefixes
        # ghp_ = Personal Access Token
        # gho_ = OAuth token
        # ghs_ = Server-to-server token
        # ghr_ = Refresh token
        if token and not any(token.startswith(prefix) for prefix in ['ghp_', 'gho_', 'ghs_', 'ghr_']):
            errors.append("GitHub token format appears invalid (should start with ghp_, gho_, ghs_, or ghr_)")

        if errors:
            raise GitHubValidationError(errors)
