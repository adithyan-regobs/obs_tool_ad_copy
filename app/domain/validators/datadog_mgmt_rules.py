"""
Datadog Management Domain Validation Rules

This module contains validation logic and custom exceptions for Datadog management operations.
Follows the established pattern used by services_mst_rules.py and applications_mst_rules.py.
"""


class DatadogValidationError(ValueError):
    """
    Custom exception for Datadog management validation errors.

    Extends ValueError to maintain consistency with domain exceptions pattern.
    Stores a list of validation errors that can be displayed to the user.

    Example:
        >>> errors = ["Code already exists", "Invalid signal kind"]
        >>> raise DatadogValidationError(errors)
    """
    def __init__(self, errors: list[str]):
        """
        Initialize validation error with list of error messages.

        Args:
            errors: List of validation error messages
        """
        super().__init__("\n".join(errors))
        self.errors = errors
