"""
Dockerfile Helper Utilities

Helper functions for Dockerfile modification operations.
"""

import logging
from typing import Dict, List, Optional

from app.db.models.service_config_model import ServiceConfigModel
from app.core.config import settings

logger = logging.getLogger(__name__)


def check_dockerfile_eligibility(
    service_config: ServiceConfigModel,
    field_mapping: Dict[str, str]
) -> bool:
    """
    Check if Dockerfile should be modified.

    Returns True if:
    - Language is Java (from language_ref)
    - Repository is configured in service_config.config
    - Branches list is non-empty
    - AND any of:
      - Datadog sidecar is enabled/disabled
      - build_args are configured
      - xms or xmx memory settings are configured
    """
    from app.utils.dockerfile_transformer import is_java_language

    logger.info("=== Dockerfile Modification Check Started ===")
    logger.info(f"service_config.id: {service_config.id}")
    logger.info(f"field_mapping.enable_datadog_sidecar: {field_mapping.get('enable_datadog_sidecar')}")

    # Check if language is Java
    if not service_config.language_ref:
        logger.warning("DOCKERFILE CHECK FAILED: No language_ref relationship loaded")
        return False

    if not is_java_language(service_config.language_ref.name):
        logger.warning(f"DOCKERFILE CHECK FAILED: Language '{service_config.language_ref.name}' is not Java")
        return False

    logger.info("CHECK PASSED: Language is Java")

    # Check if config exists with repository and branches
    config = service_config.config
    if not config:
        logger.warning("DOCKERFILE CHECK FAILED: No config object in service_config")
        return False

    repository = config.get("repository")
    if not repository:
        logger.warning("DOCKERFILE CHECK FAILED: No repository configured")
        return False

    logger.info("CHECK PASSED: Repository is configured")

    branches = config.get("branches")
    if not branches or not isinstance(branches, list) or len(branches) == 0:
        logger.warning(f"DOCKERFILE CHECK FAILED: No valid branches. branches={branches}")
        return False

    logger.info(f"CHECK PASSED: Branches configured - {branches}")

    # Check if there's a reason to modify the Dockerfile
    has_datadog = field_mapping.get("enable_datadog_sidecar") == "true"
    has_build_args = bool(config.get("build_args"))
    has_xms = bool(config.get("xms"))
    has_xmx = bool(config.get("xmx"))

    if has_datadog or has_build_args or has_xms or has_xmx:
        logger.info(f"CHECK PASSED: Modification needed - datadog={has_datadog}, build_args={has_build_args}, xms={has_xms}, xmx={has_xmx}")
        return True

    logger.info("DOCKERFILE CHECK: No modifications needed (no datadog, build_args, xms, or xmx)")
    return False


def get_datadog_advanced_options(service_config: ServiceConfigModel) -> Optional[List[Dict]]:
    """Extract Datadog advanced_options from sidecar config."""
    sidecar_config = service_config.sidecar_config or []
    for sidecar in sidecar_config:
        if not isinstance(sidecar, dict):
            continue
        sidecar_name = sidecar.get("name", "").lower()
        sidecar_code = sidecar.get("sidecar_config_code", "").lower()
        is_datadog = "datadog" in sidecar_name or "datadog" in sidecar_code
        if is_datadog and sidecar.get("enabled"):
            options = sidecar.get("advanced_options")
            if options:
                logger.info(f"Found {len(options)} Datadog advanced options")
            return options
    return None


def build_pr_body(
    service_name: str,
    base_branch: str,
    environment: str,
    enable_datadog: bool,
    xms_mb: Optional[int],
    xmx_mb: Optional[int],
    user_email: Optional[str],
    build_args: Optional[List[Dict[str, str]]] = None
) -> str:
    """Build PR body for Dockerfile changes."""
    # Determine what changes are being made
    changes_list = []
    if enable_datadog:
        changes_list.append("Added Datadog Java agent configuration")
        changes_list.append("Commented out OTel configuration (if present)")
    if build_args:
        arg_names = [arg.get("name", "") for arg in build_args if arg.get("name", "").strip()]
        if arg_names:
            changes_list.append(f"Added Docker build arguments: {', '.join(arg_names)}")
    if xms_mb or xmx_mb:
        changes_list.append("Updated JAVA_TOOL_OPTIONS with memory settings")

    # Build changes section
    changes_section = "\n".join(f"- {change}" for change in changes_list) if changes_list else "- No specific changes"

    # Determine PR type
    if enable_datadog:
        pr_type = "Datadog Configuration Addition"
        action = "Enable Datadog APM"
    elif build_args or xms_mb or xmx_mb:
        pr_type = "Dockerfile Configuration Update"
        action = "Update Dockerfile configuration"
    else:
        pr_type = "Dockerfile Update"
        action = "Update Dockerfile"

    # Build memory section
    memory_lines = []
    if xms_mb:
        memory_lines.append(f"- Xms: {xms_mb}m")
    if xmx_mb:
        memory_lines.append(f"- Xmx: {xmx_mb}m")
    memory_section = "\n".join(memory_lines) if memory_lines else "- No memory settings configured"

    # Build build args section
    build_args_section = ""
    if build_args:
        valid_args = [arg for arg in build_args if arg.get("name", "").strip()]
        if valid_args:
            args_lines = [f"- `{arg['name']}`: {arg.get('value', '(from secrets)')}" for arg in valid_args]
            build_args_section = f"""

### Docker Build Arguments
{chr(10).join(args_lines)}"""

    return f"""## {pr_type}

**Service:** `{service_name}`
**Branch:** `{base_branch}`
**Environment:** `{environment}`
**Action:** {action}

### Changes
{changes_section}

### Memory Configuration
{memory_section}{build_args_section}

---
*Generated by {settings.app_name}*
*Requested by: {user_email or 'system'}*"""


def determine_overall_status(
    branch_results: List[Dict],
    success_count: int,
    error_count: int,
    total_branches: int
) -> Dict:
    """Determine overall status based on branch results."""
    result = {"status": "skipped", "branches": branch_results, "message": ""}

    if success_count == total_branches:
        result["status"] = "success"
        result["message"] = f"Successfully created {success_count} PR(s)"
    elif success_count > 0:
        result["status"] = "partial"
        result["message"] = f"Created {success_count} PR(s), {error_count} failed"
    elif error_count > 0:
        result["status"] = "error"
        result["message"] = f"All {error_count} branches failed"
    else:
        result["status"] = "skipped"
        result["message"] = "No changes needed for any branch"

    return result
