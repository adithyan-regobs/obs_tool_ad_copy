"""
Infrastructure Transaction Validation Rules

Validators for infrastructure transactions API requests.
"""
from typing import List


class InfrastructureTransactionValidationError(ValueError):
    """Custom exception for infrastructure transaction validation errors"""

    def __init__(self, errors: List[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


class InfrastructureTransactionValidator:
    """Validator for infrastructure transaction requests"""

    @staticmethod
    def validate_pagination(skip: int, limit: int) -> None:
        """
        Validate pagination parameters.

        Args:
            skip: Pagination offset
            limit: Page size

        Raises:
            InfrastructureTransactionValidationError: If validation fails
        """
        errors: List[str] = []

        if skip < 0:
            errors.append("skip must be non-negative")

        if limit < 1:
            errors.append("limit must be at least 1")

        if limit > 100:
            errors.append("limit cannot exceed 100")

        if errors:
            raise InfrastructureTransactionValidationError(errors)

    @staticmethod
    def validate_sort_order(sort_order: str) -> None:
        """
        Validate sort order parameter.

        Args:
            sort_order: Sort order ('asc' or 'desc')

        Raises:
            InfrastructureTransactionValidationError: If validation fails
        """
        if sort_order not in ["asc", "desc"]:
            raise InfrastructureTransactionValidationError(
                ["sort_order must be 'asc' or 'desc'"]
            )

    @staticmethod
    def validate_tenant_code(tenant_code: str) -> None:
        """
        Validate tenant code.

        Args:
            tenant_code: Tenant code from JWT

        Raises:
            InfrastructureTransactionValidationError: If validation fails
        """
        if not tenant_code or not tenant_code.strip():
            raise InfrastructureTransactionValidationError(
                ["tenant_code cannot be empty"]
            )

    @staticmethod
    def validate_request(
        tenant_code: str,
        skip: int,
        limit: int,
        sort_order: str
    ) -> None:
        """
        Validate complete request parameters.

        Collects all validation errors before raising.

        Args:
            tenant_code: Tenant code from JWT
            skip: Pagination offset
            limit: Page size
            sort_order: Sort order

        Raises:
            InfrastructureTransactionValidationError: If any validation fails
        """
        errors: List[str] = []

        # Validate tenant code
        try:
            InfrastructureTransactionValidator.validate_tenant_code(tenant_code)
        except InfrastructureTransactionValidationError as e:
            errors.extend(e.errors)

        # Validate pagination
        try:
            InfrastructureTransactionValidator.validate_pagination(skip, limit)
        except InfrastructureTransactionValidationError as e:
            errors.extend(e.errors)

        # Validate sort order
        try:
            InfrastructureTransactionValidator.validate_sort_order(sort_order)
        except InfrastructureTransactionValidationError as e:
            errors.extend(e.errors)

        if errors:
            raise InfrastructureTransactionValidationError(errors)
