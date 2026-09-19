"""
Config Builder

Build and merge service configs from user input and reference configs.
Single responsibility: config construction and merging.
"""
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from copy import deepcopy


@dataclass
class ConfigBuildResult:
    """Result of building a config."""
    config: Dict[str, Any]
    fields_from_user: List[str] = field(default_factory=list)
    fields_from_reference: List[str] = field(default_factory=list)
    missing_required_fields: List[str] = field(default_factory=list)


class ConfigBuilder:
    """
    Build service configs from user input and references.

    Single responsibility: config construction.
    Does NOT validate - use ConfigValidator for that.
    Does NOT interact with database.
    """

    # Fields that can be configured
    CONFIGURABLE_FIELDS = {
        "cpu", "ram", "port", "health", "service_path",
        "listener_rule_priority", "alb_selection",
        "xms", "xmx",  # JVM memory settings
        "http_scaling_target_value",
    }

    # Autoscaling sub-fields
    AUTOSCALING_FIELDS = {"enabled", "min", "max", "desired"}

    # EBS sub-fields
    EBS_FIELDS = {"volume", "size", "type"}

    # Required fields for a complete config
    REQUIRED_FIELDS = {"cpu", "ram", "port"}

    def build_from_reference(
        self,
        reference_config: Optional[Dict[str, Any]],
        user_values: Optional[Dict[str, Any]] = None,
    ) -> ConfigBuildResult:
        """
        Build config by merging user values with reference.

        Priority: user_values > reference_config > defaults

        Args:
            reference_config: Config from another environment as base
            user_values: Values provided by user

        Returns:
            ConfigBuildResult with merged config and field sources
        """
        result = ConfigBuildResult(config={})

        # Start with reference if available
        base = deepcopy(reference_config) if reference_config else {}
        user = user_values or {}

        # Track field sources
        for field_name in self.CONFIGURABLE_FIELDS:
            if field_name in user and user[field_name] is not None:
                result.config[field_name] = user[field_name]
                result.fields_from_user.append(field_name)
            elif field_name in base and base[field_name] is not None:
                result.config[field_name] = base[field_name]
                result.fields_from_reference.append(field_name)

        # Handle autoscaling
        self._merge_autoscaling(base, user, result)

        # Handle EBS
        self._merge_ebs(base, user, result)

        # Check required fields
        for field_name in self.REQUIRED_FIELDS:
            if field_name not in result.config or result.config[field_name] is None:
                result.missing_required_fields.append(field_name)

        return result

    def _merge_autoscaling(
        self,
        base: Dict[str, Any],
        user: Dict[str, Any],
        result: ConfigBuildResult,
    ):
        """Merge autoscaling settings."""
        base_autoscaling = base.get("autoscaling", {}) or {}
        user_autoscaling = user.get("autoscaling", {}) or {}

        if not base_autoscaling and not user_autoscaling:
            return

        merged = {}
        for field_name in self.AUTOSCALING_FIELDS:
            if field_name in user_autoscaling and user_autoscaling[field_name] is not None:
                merged[field_name] = user_autoscaling[field_name]
                if "autoscaling" not in result.fields_from_user:
                    result.fields_from_user.append("autoscaling")
            elif field_name in base_autoscaling and base_autoscaling[field_name] is not None:
                merged[field_name] = base_autoscaling[field_name]
                if "autoscaling" not in result.fields_from_reference:
                    result.fields_from_reference.append("autoscaling")

        if merged:
            result.config["autoscaling"] = merged

    def _merge_ebs(
        self,
        base: Dict[str, Any],
        user: Dict[str, Any],
        result: ConfigBuildResult,
    ):
        """Merge EBS settings."""
        base_ebs = base.get("ebs", {}) or {}
        user_ebs = user.get("ebs", {}) or {}

        if not base_ebs and not user_ebs:
            return

        merged = {}
        for field_name in self.EBS_FIELDS:
            if field_name in user_ebs and user_ebs[field_name] is not None:
                merged[field_name] = user_ebs[field_name]
                if "ebs" not in result.fields_from_user:
                    result.fields_from_user.append("ebs")
            elif field_name in base_ebs and base_ebs[field_name] is not None:
                merged[field_name] = base_ebs[field_name]
                if "ebs" not in result.fields_from_reference:
                    result.fields_from_reference.append("ebs")

        if merged:
            result.config["ebs"] = merged

    def update_config(
        self,
        current_config: Dict[str, Any],
        updates: Dict[str, Any],
    ) -> ConfigBuildResult:
        """
        Update existing config with new values.

        Args:
            current_config: Current config state
            updates: New values to apply

        Returns:
            ConfigBuildResult with updated config
        """
        result = ConfigBuildResult(config=deepcopy(current_config))

        # Apply top-level updates
        for field_name in self.CONFIGURABLE_FIELDS:
            if field_name in updates and updates[field_name] is not None:
                result.config[field_name] = updates[field_name]
                result.fields_from_user.append(field_name)

        # Handle autoscaling updates
        if "autoscaling" in updates and updates["autoscaling"]:
            if "autoscaling" not in result.config:
                result.config["autoscaling"] = {}
            for field_name in self.AUTOSCALING_FIELDS:
                if field_name in updates["autoscaling"]:
                    result.config["autoscaling"][field_name] = updates["autoscaling"][field_name]
            result.fields_from_user.append("autoscaling")

        # Handle EBS updates
        if "ebs" in updates and updates["ebs"]:
            if "ebs" not in result.config:
                result.config["ebs"] = {}
            for field_name in self.EBS_FIELDS:
                if field_name in updates["ebs"]:
                    result.config["ebs"][field_name] = updates["ebs"][field_name]
            result.fields_from_user.append("ebs")

        # Check required fields
        for field_name in self.REQUIRED_FIELDS:
            if field_name not in result.config or result.config[field_name] is None:
                result.missing_required_fields.append(field_name)

        return result

    def extract_values_from_llm_response(
        self,
        llm_extracted: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Clean and normalize values extracted by LLM.

        LLM might return strings where we need ints, etc.
        This normalizes the output.

        Args:
            llm_extracted: Raw dict from LLM extraction

        Returns:
            Normalized dict with proper types
        """
        normalized = {}

        # Numeric fields
        numeric_fields = {"cpu", "ram", "port", "xms", "xmx", "listener_rule_priority", "http_scaling_target_value"}
        for field_name in numeric_fields:
            if field_name in llm_extracted and llm_extracted[field_name] is not None:
                try:
                    normalized[field_name] = int(float(str(llm_extracted[field_name])))
                except (ValueError, TypeError):
                    pass  # Skip invalid values

        # String fields
        string_fields = {"health", "service_path", "alb_selection"}
        for field_name in string_fields:
            if field_name in llm_extracted and llm_extracted[field_name]:
                normalized[field_name] = str(llm_extracted[field_name])

        # Autoscaling
        if "autoscaling" in llm_extracted and llm_extracted["autoscaling"]:
            autoscaling = llm_extracted["autoscaling"]
            normalized_autoscaling = {}

            if "enabled" in autoscaling:
                normalized_autoscaling["enabled"] = bool(autoscaling["enabled"])

            for num_field in ["min", "max", "desired"]:
                if num_field in autoscaling and autoscaling[num_field] is not None:
                    try:
                        normalized_autoscaling[num_field] = int(float(str(autoscaling[num_field])))
                    except (ValueError, TypeError):
                        pass

            if normalized_autoscaling:
                normalized["autoscaling"] = normalized_autoscaling

        # EBS
        if "ebs" in llm_extracted and llm_extracted["ebs"]:
            ebs = llm_extracted["ebs"]
            normalized_ebs = {}

            if "volume" in ebs and ebs["volume"]:
                normalized_ebs["volume"] = str(ebs["volume"])
            if "type" in ebs and ebs["type"]:
                normalized_ebs["type"] = str(ebs["type"])
            if "size" in ebs and ebs["size"] is not None:
                try:
                    normalized_ebs["size"] = int(float(str(ebs["size"])))
                except (ValueError, TypeError):
                    pass

            if normalized_ebs:
                normalized["ebs"] = normalized_ebs

        return normalized

    def is_config_complete(self, config: Dict[str, Any]) -> bool:
        """
        Check if config has all required fields.

        Args:
            config: Config to check

        Returns:
            True if all required fields are present
        """
        for field_name in self.REQUIRED_FIELDS:
            if field_name not in config or config[field_name] is None:
                return False
        return True

    def get_missing_fields(self, config: Dict[str, Any]) -> List[str]:
        """
        Get list of missing required fields.

        Args:
            config: Config to check

        Returns:
            List of missing field names
        """
        missing = []
        for field_name in self.REQUIRED_FIELDS:
            if field_name not in config or config[field_name] is None:
                missing.append(field_name)
        return missing

    def build_response_config_json(self, config_model) -> Dict[str, Any]:
        """
        Build config JSON for API response / form prefill.

        Args:
            config_model: ServiceConfigModel instance

        Returns:
            Dict matching ServiceConfigResponse structure
        """
        return {
            "config": config_model.config or {},
            "sidecar_config": config_model.sidecar_config or [],
            "language_ref_code": config_model.language_ref_code,
            "alb_selection": config_model.alb_selection,
            "infrastructuretype_ref_code": config_model.infrastructuretype_ref_code,
            "infra_vendor_enum": config_model.infra_vendor_enum.value if config_model.infra_vendor_enum else None,
            "infrastructure_mst_code": config_model.infrastructure_mst_code,
        }

    def format_config_summary(self, config: Dict[str, Any]) -> str:
        """
        Format config as human-readable summary.

        Args:
            config: Config dict

        Returns:
            Formatted string summary
        """
        lines = []

        # Core fields
        if "cpu" in config:
            lines.append(f"CPU: {config['cpu']}")
        if "ram" in config:
            lines.append(f"RAM: {config['ram']} MB")
        if "port" in config:
            lines.append(f"Port: {config['port']}")
        if "health" in config:
            lines.append(f"Health Check: {config['health']}")
        if "service_path" in config:
            lines.append(f"Service Path: {config['service_path']}")

        # ALB
        if "alb_selection" in config:
            alb_display = {
                "no_alb": "No ALB",
                "existing_alb": "Use Existing ALB",
                "create_new_alb": "Create New ALB",
            }.get(config["alb_selection"], config["alb_selection"])
            lines.append(f"ALB: {alb_display}")

        # Autoscaling
        autoscaling = config.get("autoscaling", {})
        if autoscaling:
            if autoscaling.get("enabled"):
                lines.append(f"Autoscaling: Enabled (Min: {autoscaling.get('min', 'N/A')}, "
                           f"Max: {autoscaling.get('max', 'N/A')}, "
                           f"Desired: {autoscaling.get('desired', 'N/A')})")
            else:
                lines.append("Autoscaling: Disabled")

        # EBS
        ebs = config.get("ebs", {})
        if ebs:
            lines.append(f"EBS: {ebs.get('size', 'N/A')} GB ({ebs.get('type', 'gp3')})")

        # JVM
        if "xms" in config or "xmx" in config:
            lines.append(f"JVM: -Xms{config.get('xms', 'N/A')}m -Xmx{config.get('xmx', 'N/A')}m")

        return "\n".join(lines) if lines else "No configuration set"
