from typing import List, Optional

from app.core.enum import WorkspaceRoleEnum


class WorkspaceValidationError(ValueError):
    """Custom exception for workspace validation errors"""

    def __init__(self, errors: List[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


class WorkspaceValidator:
    """Validator for Workspace business rules"""

    @staticmethod
    def validate_pagination(skip: int, limit: int) -> None:
        errors: List[str] = []
        if skip < 0:
            errors.append("skip must be non-negative")
        if limit < 1:
            errors.append("limit must be at least 1")
        if limit > 500:
            errors.append("limit cannot exceed 500")
        if errors:
            raise WorkspaceValidationError(errors)

    @staticmethod
    def validate_tenant_code(tenant_code: str) -> None:
        if not tenant_code or not tenant_code.strip():
            raise WorkspaceValidationError(["tenant_code cannot be empty"])

    @staticmethod
    def validate_create_request(
        tenant_code: str,
        workspace_name: str,
        description: Optional[str],
    ) -> None:
        errors: List[str] = []
        if not tenant_code or not tenant_code.strip():
            errors.append("tenant_code cannot be empty")
        if not workspace_name or not workspace_name.strip():
            errors.append("workspace_name cannot be empty")
        elif len(workspace_name) > 255:
            errors.append("workspace_name cannot exceed 255 characters")
        if description is not None and len(description) > 500:
            errors.append("description cannot exceed 500 characters")
        if errors:
            raise WorkspaceValidationError(errors)

    @staticmethod
    def validate_add_users_request(
        workspace_code: str,
        users: list,
    ) -> None:
        """
        Validate the add-users payload.

        Rules:
        - workspace_code non-empty
        - users list non-empty
        - no duplicate user_code in payload
        - role of `owner` is rejected (only creator may be owner)
        """
        errors: List[str] = []

        if not workspace_code or not workspace_code.strip():
            errors.append("workspace_code cannot be empty")

        if not users:
            errors.append("users list cannot be empty")

        seen: set[str] = set()
        for entry in users or []:
            user_code = getattr(entry, "user_code", None)
            role = getattr(entry, "role", None)

            if not user_code:
                errors.append("each user entry must include a user_code")
                continue

            if user_code in seen:
                errors.append(f"duplicate user_code in payload: {user_code}")
            seen.add(user_code)

            if role == WorkspaceRoleEnum.owner:
                errors.append(
                    f"role 'owner' cannot be assigned via add-users (user_code={user_code})"
                )

        if errors:
            raise WorkspaceValidationError(errors)
