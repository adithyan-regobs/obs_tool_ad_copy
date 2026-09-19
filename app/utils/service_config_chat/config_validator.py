"""
Config Validator

Validation logic for service config values.
Single responsibility: validate config fields.
"""
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field


@dataclass
class ValidationWarning:
    """Single validation warning."""
    field: str
    message: str
    severity: str = "warning"  # "warning" or "error"


@dataclass
class ValidationResult:
    """Result of validating a config."""
    is_valid: bool
    warnings: List[ValidationWarning] = field(default_factory=list)
    errors: List[ValidationWarning] = field(default_factory=list)

    def add_warning(self, field_name: str, message: str):
        self.warnings.append(ValidationWarning(field=field_name, message=message, severity="warning"))

    def add_error(self, field_name: str, message: str):
        self.errors.append(ValidationWarning(field=field_name, message=message, severity="error"))
        self.is_valid = False


class ConfigValidator:
    """
    Validate service config values.

    Single responsibility: field validation.
    Does NOT interact with database.
    """

    # Valid EBS volume types
    VALID_EBS_TYPES = {"gp3", "gp2", "io2", "io1", "st1", "sc1"}

    # Valid ALB selections
    VALID_ALB_SELECTIONS = {"no_alb", "existing_alb", "create_new_alb"}

    # Common CPU values for ECS Fargate
    RECOMMENDED_CPU_VALUES = {256, 512, 1024, 2048, 4096}

    # Common RAM values for ECS Fargate
    RECOMMENDED_RAM_VALUES = {512, 1024, 2048, 4096, 8192, 16384}

    def validate(self, config: Dict[str, Any]) -> ValidationResult:
        """
        Validate entire config dict.

        Args:
            config: Config dictionary to validate

        Returns:
            ValidationResult with is_valid flag and any warnings/errors
        """
        result = ValidationResult(is_valid=True)

        if not config:
            return result

        # Validate individual fields
        for field_name, value in config.items():
            error = self.validate_field(field_name, value)
            if error:
                result.add_error(field_name, error)

        # Cross-field validations
        self._validate_autoscaling(config, result)
        self._validate_recommendations(config, result)

        return result

    def validate_field(self, field_name: str, value: Any) -> Optional[str]:
        """
        Validate a single field.

        Args:
            field_name: Name of the field
            value: Value to validate

        Returns:
            Error message if invalid, None if valid
        """
        if value is None or value == "":
            return None  # Empty values are allowed

        validators = {
            "cpu": self._validate_cpu,
            "ram": self._validate_ram,
            "port": self._validate_port,
            "health": self._validate_health_path,
            "service_path": self._validate_service_path,
            "listener_rule_priority": self._validate_priority,
            "alb_selection": self._validate_alb_selection,
            "xms": self._validate_jvm_memory,
            "xmx": self._validate_jvm_memory,
            "http_scaling_target_value": self._validate_positive_integer,
        }

        validator = validators.get(field_name)
        if validator:
            return validator(value)

        return None

    def _validate_cpu(self, value: Any) -> Optional[str]:
        """Validate CPU is a positive number."""
        try:
            cpu = float(str(value))
            if cpu <= 0:
                return "CPU must be a positive number"
        except (ValueError, TypeError):
            return "CPU must be a valid number"
        return None

    def _validate_ram(self, value: Any) -> Optional[str]:
        """Validate RAM is a positive number."""
        try:
            ram = float(str(value))
            if ram <= 0:
                return "RAM must be a positive number"
        except (ValueError, TypeError):
            return "RAM must be a valid number"
        return None

    def _validate_port(self, value: Any) -> Optional[str]:
        """Validate port is between 1 and 65535."""
        try:
            port = int(str(value))
            if port < 1 or port > 65535:
                return "Port must be between 1 and 65535"
        except (ValueError, TypeError):
            return "Port must be a valid integer"
        return None

    def _validate_health_path(self, value: Any) -> Optional[str]:
        """Validate health check path starts with /."""
        path = str(value)
        if not path.startswith("/"):
            return "Health path must start with /"
        return None

    def _validate_service_path(self, value: Any) -> Optional[str]:
        """Validate service path starts with /."""
        path = str(value)
        if not path.startswith("/"):
            return "Service path must start with /"
        return None

    def _validate_priority(self, value: Any) -> Optional[str]:
        """Validate listener rule priority is between 1 and 50000."""
        try:
            priority = int(str(value))
            if priority < 1 or priority > 50000:
                return "Priority must be between 1 and 50000"
        except (ValueError, TypeError):
            return "Priority must be a valid integer"
        return None

    def _validate_alb_selection(self, value: Any) -> Optional[str]:
        """Validate ALB selection is one of allowed values."""
        if str(value) not in self.VALID_ALB_SELECTIONS:
            return f"ALB selection must be one of: {', '.join(self.VALID_ALB_SELECTIONS)}"
        return None

    def _validate_jvm_memory(self, value: Any) -> Optional[str]:
        """Validate JVM memory is a positive integer."""
        try:
            mem = int(str(value))
            if mem <= 0:
                return "JVM memory must be a positive integer"
        except (ValueError, TypeError):
            return "JVM memory must be a valid integer"
        return None

    def _validate_positive_integer(self, value: Any) -> Optional[str]:
        """Validate value is a positive integer."""
        try:
            num = int(str(value))
            if num <= 0:
                return "Value must be a positive integer"
        except (ValueError, TypeError):
            return "Value must be a valid integer"
        return None

    def _validate_autoscaling(self, config: Dict[str, Any], result: ValidationResult):
        """Validate autoscaling constraints: min <= desired <= max."""
        autoscaling = config.get("autoscaling", {})
        if not autoscaling or not autoscaling.get("enabled"):
            return

        try:
            min_val = int(str(autoscaling.get("min", 0)))
            max_val = int(str(autoscaling.get("max", 0)))
            desired = int(str(autoscaling.get("desired", 0)))

            if min_val > max_val:
                result.add_error("autoscaling", "Min cannot be greater than Max")
            if desired < min_val:
                result.add_error("autoscaling", "Desired cannot be less than Min")
            if desired > max_val:
                result.add_error("autoscaling", "Desired cannot be greater than Max")
        except (ValueError, TypeError):
            pass  # Already validated individual fields

    def _validate_recommendations(self, config: Dict[str, Any], result: ValidationResult):
        """Add warnings for non-standard values (not errors)."""
        # CPU recommendation
        cpu = config.get("cpu")
        if cpu:
            try:
                cpu_int = int(float(str(cpu)))
                if cpu_int not in self.RECOMMENDED_CPU_VALUES:
                    result.add_warning(
                        "cpu",
                        f"CPU {cpu_int} is non-standard. Recommended: {sorted(self.RECOMMENDED_CPU_VALUES)}"
                    )
            except (ValueError, TypeError):
                pass

        # RAM recommendation
        ram = config.get("ram")
        if ram:
            try:
                ram_int = int(float(str(ram)))
                if ram_int not in self.RECOMMENDED_RAM_VALUES:
                    result.add_warning(
                        "ram",
                        f"RAM {ram_int} is non-standard. Recommended: {sorted(self.RECOMMENDED_RAM_VALUES)}"
                    )
            except (ValueError, TypeError):
                pass
