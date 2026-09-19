"""
Validators for Monitoring Policy Overrides
"""

from typing import Optional


class MonitoringPolicyOverrideValidationError(Exception):
    """Custom exception for monitoring policy override validation errors"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"Validation failed: {', '.join(errors)}")


class MonitoringPolicyOverrideValidator:
    """Validator for monitoring policy override business rules"""

    @staticmethod
    def validate_override_data(override_data: dict) -> None:
        """
        Validate business rules for creating a policy override using factory output.

        Args:
            override_data: Dictionary from factory containing all override fields

        Raises:
            MonitoringPolicyOverrideValidationError: If validation fails
        """
        errors: list[str] = []

        # Name validation
        name = override_data.get("name", "")
        if not name or not str(name).strip():
            errors.append("name cannot be empty or whitespace")

        if len(str(name)) > 255:
            errors.append("name cannot exceed 255 characters")

        # Infrastructure type validation
        infrastructuretype_ref_code = override_data.get("infrastructuretype_ref_code")
        if not infrastructuretype_ref_code or not str(infrastructuretype_ref_code).strip():
            errors.append("infrastructuretype_ref_code is required")

        # Alert type validation
        alerttype_ref_code = override_data.get("alerttype_ref_code")
        if not alerttype_ref_code or not str(alerttype_ref_code).strip():
            errors.append("alerttype_ref_code is required")

        # Get scope fields
        tenants_mst_code = override_data.get("tenants_mst_code")
        applications_mst_code = override_data.get("applications_mst_code")
        resource_group_mst_code = override_data.get("resource_group_mst_code")

        # If resource_group is specified, application must also be specified
        if resource_group_mst_code and not applications_mst_code:
            errors.append(
                "applications_mst_code is required when resource_group_mst_code is specified"
            )

        # If application is specified, tenant must also be specified
        if applications_mst_code and not tenants_mst_code:
            errors.append(
                "tenants_mst_code is required when applications_mst_code is specified"
            )

        # Threshold validation
        threshold_value = float(override_data.get("threshold_value", 0))
        if threshold_value < 0:
            errors.append("threshold_value must be greater than or equal to 0")

        if threshold_value > 10000:
            errors.append("threshold_value cannot exceed 10000")

        # Evaluation window validation
        eval_window = int(override_data.get("eval_window", 0))
        if eval_window <= 0:
            errors.append("eval_window must be greater than 0")

        # Duration validation
        for_duration = int(override_data.get("for_duration", 0))
        if for_duration <= 0:
            errors.append("for_duration must be greater than 0")

        # Raise exception if there are errors
        if errors:
            raise MonitoringPolicyOverrideValidationError(errors)

    @staticmethod
    def validate_scope_hierarchy(
        tenants_mst_code: Optional[str],
        applications_mst_code: Optional[str],
        resource_group_mst_code: Optional[str]
    ) -> None:
        """
        Validate that scope hierarchy is valid.

        Rules:
        - Resource Group requires Application
        - Application requires Tenant
        - Tenant can be standalone

        Args:
            tenants_mst_code: Optional tenant scope
            applications_mst_code: Optional application scope
            resource_group_mst_code: Optional resource group scope

        Raises:
            MonitoringPolicyOverrideValidationError: If hierarchy is invalid
        """
        errors: list[str] = []

        if resource_group_mst_code and not applications_mst_code:
            errors.append(
                "Invalid scope hierarchy: resource_group_mst_code requires applications_mst_code"
            )

        if applications_mst_code and not tenants_mst_code:
            errors.append(
                "Invalid scope hierarchy: applications_mst_code requires tenants_mst_code"
            )

        if errors:
            raise MonitoringPolicyOverrideValidationError(errors)