"""
Pipeline Management Validation Rules
Contains business logic validation for pipeline operations
"""
from app.schemas.pipeline_schemas import CreatePipelineRequest


class PipelineValidationError(ValueError):
    """Custom exception for pipeline validation errors"""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


class PipelineValidator:
    """Validator for Pipeline Management business rules"""

    @staticmethod
    def validate_create_pipeline_request(data: CreatePipelineRequest) -> None:
        """
        Validate create pipeline request with business rules.

        Business logic validations only - format validations are handled by Pydantic schemas.

        Args:
            data: CreatePipelineRequest data

        Raises:
            PipelineValidationError: If validation fails
        """
        errors: list[str] = []

        # Business logic validations can be added here as needed
        # For example:
        # - Check if service is allowed to have pipelines
        # - Check if environment is supported
        # - Check if language is supported
        # - etc.

        # Currently, basic format validation is handled by Pydantic
        # This validator is ready for future business logic rules

        if errors:
            raise PipelineValidationError(errors)
