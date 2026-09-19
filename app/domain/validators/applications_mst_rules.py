class ApplicationValidationError(ValueError):
    """Custom exception for application validation errors"""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


class ApplicationsMstValidator:
    """Validator for Applications Master business rules"""

    @staticmethod
    def validate_pagination(skip: int, limit: int) -> None:
        """
        Validate pagination parameters.

        Args:
            skip: Pagination offset
            limit: Page size

        Raises:
            ApplicationValidationError: If pagination parameters are invalid
        """
        errors: list[str] = []

        if skip < 0:
            errors.append("skip must be non-negative")

        if limit < 1:
            errors.append("limit must be at least 1")

        if limit > 500:
            errors.append("limit cannot exceed 500")

        if errors:
            raise ApplicationValidationError(errors)

    @staticmethod
    def validate_tenant_code(tenant_code: str) -> None:
        """
        Validate tenant code format.

        Args:
            tenant_code: Tenant code identifier

        Raises:
            ApplicationValidationError: If tenant code format is invalid
        """
        errors: list[str] = []

        if not tenant_code or not tenant_code.strip():
            errors.append("tenant_code cannot be empty")

        # Additional business rules can be added here:
        # - Length validation
        # - Format validation
        # Example:
        # if len(tenant_code) > 100:
        #     errors.append("tenant_code cannot exceed 100 characters")

        if errors:
            raise ApplicationValidationError(errors)

    @staticmethod
    def validate_application_code(application_code: str) -> None:
        """
        Validate application code format.

        Args:
            application_code: Application code identifier

        Raises:
            ApplicationValidationError: If application code format is invalid
        """
        errors: list[str] = []

        if not application_code or not application_code.strip():
            errors.append("application_code cannot be empty")

        # Additional business rules can be added here:
        # - Length validation
        # - Format validation (alphanumeric, underscore only)
        # - Reserved keywords check
        # Example:
        # if len(application_code) > 100:
        #     errors.append("application_code cannot exceed 100 characters")
        # if not re.match(r'^[a-z0-9_]+$', application_code):
        #     errors.append("application_code must contain only lowercase letters, numbers, and underscores")

        if errors:
            raise ApplicationValidationError(errors)

    @staticmethod
    def validate_get_all_applications_request(
        tenant_code: str,
        skip: int,
        limit: int
    ) -> None:
        """
        Validate complete get_all_applications request.

        Args:
            tenant_code: Tenant code
            skip: Pagination offset
            limit: Page size

        Raises:
            ApplicationValidationError: If any validation fails
        """
        errors: list[str] = []

        try:
            ApplicationsMstValidator.validate_tenant_code(tenant_code)
        except ApplicationValidationError as e:
            errors.extend(e.errors)

        try:
            ApplicationsMstValidator.validate_pagination(skip, limit)
        except ApplicationValidationError as e:
            errors.extend(e.errors)

        if errors:
            raise ApplicationValidationError(errors)

    @staticmethod
    def validate_create_application_request(
        tenant: str,
        application_name: str
    ) -> None:
        """
        Validate create application request.

        Args:
            tenant: Tenant subdomain (for tenant lookup)
            application_name: Application name

        Raises:
            ApplicationValidationError: If any validation fails
        """
        errors: list[str] = []

        # Validate tenant subdomain
        if not tenant or not tenant.strip():
            errors.append("tenant cannot be empty")

        # Validate application_name
        if not application_name or not application_name.strip():
            errors.append("application_name cannot be empty")

        if len(application_name) > 255:
            errors.append("application_name cannot exceed 255 characters")

        if errors:
            raise ApplicationValidationError(errors)
