"""
Validators for Listener Priority Rules in Service Configuration

OPS_TOOLS and regular services (API/BACKGROUND_SERVICE) use different ALBs:
- OPS_TOOLS: attaches to ops-tools/common-infra ALB
- API/BACKGROUND_SERVICE: attaches to services/common-infra ALB

Since they use different ALBs, priority uniqueness is validated within
the same service_type group (OPS_TOOLS services don't conflict with regular services).
Both support the same AWS ALB priority range: 1-50000.
"""

from typing import List, Optional


class ListenerPriorityValidationError(Exception):
    """Custom exception for listener priority validation errors"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"Validation failed: {', '.join(errors)}")


class ListenerPriorityValidator:
    """Validator for listener priority business rules"""

    @staticmethod
    def validate_priority_uniqueness(
        priority: str,
        existing_configs: List[dict],
        current_service_code: Optional[str] = None,
        service_type: Optional[str] = None
    ) -> None:
        """
        Validate that the listener_rule_priority is not already in use
        by another service in the same scope (tenant/environment/geo_loc/alb).

        OPS_TOOLS services use a different ALB than API/BACKGROUND_SERVICE,
        so priority uniqueness is validated within the same service_type group.

        Args:
            priority: The listener rule priority to validate
            existing_configs: List of existing service configs with their priorities
                             Each dict should have: service_code, listener_rule_priority, service_type
            current_service_code: The service code being updated (to exclude from check)
            service_type: The service type (API, BACKGROUND_SERVICE, OPS_TOOLS)
                         Used to filter configs - OPS_TOOLS only checks against other OPS_TOOLS

        Raises:
            ListenerPriorityValidationError: If priority is already in use
        """
        errors: list[str] = []

        if not priority or not str(priority).strip():
            return

        priority_value = str(priority).strip()

        # Determine which service types share the same ALB
        # OPS_TOOLS uses ops-tools/common-infra ALB
        # API and BACKGROUND_SERVICE use services/common-infra ALB
        if service_type == "OPS_TOOLS":
            conflicting_types = {"OPS_TOOLS"}
            alb_name = "ops-tools ALB"
        else:
            # API and BACKGROUND_SERVICE share the same ALB
            conflicting_types = {"API", "BACKGROUND_SERVICE"}
            alb_name = "services ALB"

        for config in existing_configs:
            config_priority = config.get("listener_rule_priority")
            config_service = config.get("service_code")
            config_service_type = config.get("service_type")

            if not config_priority:
                continue

            if current_service_code and config_service == current_service_code:
                continue

            # Only check configs that share the same ALB (based on service_type)
            if service_type and config_service_type not in conflicting_types:
                continue

            if str(config_priority).strip() == priority_value:
                errors.append(
                    f"Listener rule priority '{priority_value}' is already in use "
                    f"by service '{config_service}'. Each service must have a unique "
                    f"listener priority within the same environment, geo location, and {alb_name}."
                )
                break

        if errors:
            raise ListenerPriorityValidationError(errors)

    @staticmethod
    def validate_priority_range(priority: Optional[str]) -> None:
        """
        Validate that the listener rule priority is within valid range (1-50000).

        Args:
            priority: The listener rule priority to validate

        Raises:
            ListenerPriorityValidationError: If priority is out of range
        """
        errors: list[str] = []

        if priority is None or not str(priority).strip():
            return

        try:
            priority_int = int(priority)
            if priority_int < 1 or priority_int > 50000:
                errors.append(
                    f"Listener rule priority must be between 1 and 50000, got: {priority_int}"
                )
        except ValueError:
            errors.append(
                f"Listener rule priority must be a valid integer, got: '{priority}'"
            )

        if errors:
            raise ListenerPriorityValidationError(errors)

    @staticmethod
    def validate_priority_for_alb_type(
        priority: Optional[str],
        alb_selection: str
    ) -> None:
        """
        Validate that listener priority is only set for ALB-enabled configurations.

        Args:
            priority: The listener rule priority
            alb_selection: ALB type (no_alb/existing_alb/create_new_alb)

        Raises:
            ListenerPriorityValidationError: If priority set for no-alb config
        """
        errors: list[str] = []

        if priority and str(priority).strip() and alb_selection == "no_alb":
            errors.append(
                "Listener rule priority cannot be set for 'no_alb' configurations. "
                "Remove listener_rule_priority or change alb_selection."
            )

        if errors:
            raise ListenerPriorityValidationError(errors)
