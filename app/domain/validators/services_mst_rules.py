from typing import Optional
from app.core.enum import ServiceTypeEnum


class ServiceValidationError(ValueError):
    """Custom exception for service validation errors"""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


class ServicesMstValidator:
    """Validator for Services Master business rules"""

    @staticmethod
    def validate_pagination(skip: int, limit: int) -> None:
        """
        Validate pagination parameters.

        Args:
            skip: Pagination offset
            limit: Page size

        Raises:
            ServiceValidationError: If pagination parameters are invalid
        """
        errors: list[str] = []

        if skip < 0:
            errors.append("skip must be non-negative")

        if limit < 1:
            errors.append("limit must be at least 1")

        if limit > 500:
            errors.append("limit cannot exceed 500")

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_service_code(service_code: str) -> None:
        """
        Validate service code format.

        Args:
            service_code: Service code identifier

        Raises:
            ServiceValidationError: If service code format is invalid
        """
        errors: list[str] = []

        if not service_code or not service_code.strip():
            errors.append("service_code cannot be empty")

        # Additional business rules can be added here:
        # - Length validation
        # - Format validation (alphanumeric, underscore only)
        # - Reserved keywords check
        # Example:
        # if len(service_code) > 100:
        #     errors.append("service_code cannot exceed 100 characters")

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_tenant_code(tenant_code: str) -> None:
        """
        Validate tenant code format.

        Args:
            tenant_code: Tenant code identifier

        Raises:
            ServiceValidationError: If tenant code format is invalid
        """
        errors: list[str] = []

        if not tenant_code or not tenant_code.strip():
            errors.append("tenant_code cannot be empty")

        # Additional business rules can be added here
        # Example:
        # if len(tenant_code) > 100:
        #     errors.append("tenant_code cannot exceed 100 characters")

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_application_code(application_code: Optional[str]) -> None:
        """
        Validate application code format (if provided).

        Args:
            application_code: Application code identifier (optional)

        Raises:
            ServiceValidationError: If application code format is invalid
        """
        errors: list[str] = []

        if application_code is not None:
            if not application_code.strip():
                errors.append("application_code cannot be empty string")

            # Additional business rules can be added here

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_get_all_services_request(
        tenant_code: str,
        application_code: Optional[str],
        skip: int,
        limit: int
    ) -> None:
        """
        Validate complete get_all_services request.

        Args:
            tenant_code: Tenant code
            application_code: Application code (optional)
            skip: Pagination offset
            limit: Page size

        Raises:
            ServiceValidationError: If any validation fails
        """
        errors: list[str] = []

        try:
            ServicesMstValidator.validate_tenant_code(tenant_code)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        try:
            ServicesMstValidator.validate_application_code(application_code)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        try:
            ServicesMstValidator.validate_pagination(skip, limit)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_get_service_detail_request(
        tenant_code: str,
        service_code: str
    ) -> None:
        """
        Validate get_service_detail request.

        Args:
            tenant_code: Tenant code
            service_code: Service code

        Raises:
            ServiceValidationError: If any validation fails
        """
        errors: list[str] = []

        try:
            ServicesMstValidator.validate_tenant_code(tenant_code)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        try:
            ServicesMstValidator.validate_service_code(service_code)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_service_name(service_name: str) -> None:
        """
        Validate service name.

        Args:
            service_name: Service name

        Raises:
            ServiceValidationError: If service name is invalid
        """
        errors: list[str] = []

        if not service_name or not service_name.strip():
            errors.append("service_name cannot be empty")

        if len(service_name) > 255:
            errors.append("service_name cannot exceed 255 characters")

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_resource_group_code(resource_group_code: str) -> None:
        """
        Validate resource group code.

        Args:
            resource_group_code: Resource group code

        Raises:
            ServiceValidationError: If resource group code is invalid
        """
        errors: list[str] = []

        if not resource_group_code or not resource_group_code.strip():
            errors.append("resource_group_code cannot be empty")

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_service_type(service_type: ServiceTypeEnum) -> None:
        """
        Validate service type.

        Args:
            service_type: Service type enum value

        Raises:
            ServiceValidationError: If service type is invalid
        """
        errors: list[str] = []

        valid_types = [ServiceTypeEnum.API, ServiceTypeEnum.BACKGROUND_SERVICE, ServiceTypeEnum.OPS_TOOLS, ServiceTypeEnum.MODEL_SERVING]
        if service_type not in valid_types:
            errors.append("service_type must be 'API', 'Background Service', 'Ops Tools', or 'Model Serving'")

        if errors:
            raise ServiceValidationError(errors)

    @staticmethod
    def validate_create_service_request(
        tenant_code: str,
        application_code: str,
        resource_group_code: str,
        service_name: str,
        service_type: ServiceTypeEnum
    ) -> None:
        """
        Validate create_service request.

        Args:
            tenant_code: Tenant code
            application_code: Application code
            resource_group_code: Resource group code
            service_name: Service name
            service_type: Service type

        Raises:
            ServiceValidationError: If any validation fails
        """
        errors: list[str] = []

        try:
            ServicesMstValidator.validate_tenant_code(tenant_code)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        try:
            ServicesMstValidator.validate_application_code(application_code)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        try:
            ServicesMstValidator.validate_resource_group_code(resource_group_code)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        try:
            ServicesMstValidator.validate_service_name(service_name)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        try:
            ServicesMstValidator.validate_service_type(service_type)
        except ServiceValidationError as e:
            errors.extend(e.errors)

        if errors:
            raise ServiceValidationError(errors)
