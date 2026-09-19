"""
CI/CD Workflow Steps Configuration
Based on existing GitHub Actions workflow templates
"""
from typing import List, Dict, Any, Optional


# Language-specific workflow configurations based on actual GitHub Actions templates
LANGUAGE_WORKFLOWS: Dict[str, Dict[str, Any]] = {
    "go": {
        "language": "go",
        "steps": [
            {
                "id": "code-checkout",
                "name": "Code Checkout",
                "order": 1,
                "description": "Checkout source code from repository",
                "mandatory": True,
                "enabled": True,
                "category": "setup",
            },
            {
                "id": "pre-security-validation",
                "name": "Pre-Security Validation",
                "order": 2,
                "description": "Validate code and configuration before build",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout"],
                "category": "quality",
            },
            {
                "id": "env-validation",
                "name": "Environment Validation",
                "order": 3,
                "description": "Validate environment configuration and variables",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout"],
                "category": "setup",
            },
            {
                "id": "code-quality-check",
                "name": "Code Quality Check",
                "order": 4,
                "description": "Run linting, formatting, and static analysis",
                "mandatory": False,
                "enabled": True,
                "dependsOn": ["code-checkout"],
                "category": "quality",
            },
            {
                "id": "build",
                "name": "Build Application",
                "order": 5,
                "description": "Compile/build the application binary",
                "mandatory": True,
                "enabled": True,
                "dependsOn": ["code-checkout", "pre-security-validation", "env-validation", "code-quality-check"],
                "category": "build",
            },
            {
                "id": "post-security",
                "name": "Post-Security Scan",
                "order": 6,
                "description": "Run security scans after build",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["build"],
                "category": "quality",
            },
            {
                "id": "docker-build",
                "name": "Docker Build",
                "order": 7,
                "description": "Build Docker container image",
                "mandatory": True,
                "enabled": True,
                "dependsOn": ["build", "post-security"],
                "category": "build",
            },
            {
                "id": "generate-sbom",
                "name": "Generate SBOM",
                "order": 8,
                "description": "Generate Software Bill of Materials",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["docker-build"],
                "category": "build",
            },
            {
                "id": "image-security-check",
                "name": "Image Security Check",
                "order": 9,
                "description": "Scan container image for vulnerabilities",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["docker-build"],
                "category": "quality",
            },
            {
                "id": "slack-notification",
                "name": "Send Slack Notification",
                "order": 10,
                "description": "Send build and deployment status to Slack",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout", "code-quality-check", "build", "docker-build"],
                "category": "notification",
            },
        ],
    },
    "java": {
        "language": "java",
        "steps": [
            {
                "id": "code-checkout",
                "name": "Code Checkout",
                "order": 1,
                "description": "Checkout source code from repository",
                "mandatory": True,
                "enabled": True,
                "category": "setup",
            },
            {
                "id": "pre-security-validation",
                "name": "Pre-Security Validation",
                "order": 2,
                "description": "Validate code and configuration before build",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout"],
                "category": "quality",
            },
            {
                "id": "env-validation",
                "name": "Environment Validation",
                "order": 3,
                "description": "Validate environment configuration and variables",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout"],
                "category": "setup",
            },
            {
                "id": "code-quality-check",
                "name": "Code Quality Check",
                "order": 4,
                "description": "Run SonarQube analysis and Checkstyle",
                "mandatory": False,
                "enabled": True,
                "dependsOn": ["code-checkout"],
                "category": "quality",
            },
            {
                "id": "build",
                "name": "Build Application",
                "order": 5,
                "description": "Build application with Maven/Gradle",
                "mandatory": True,
                "enabled": True,
                "dependsOn": ["code-checkout", "pre-security-validation", "env-validation", "code-quality-check"],
                "category": "build",
            },
            {
                "id": "post-security",
                "name": "Post-Security Scan",
                "order": 6,
                "description": "Run security scans after build",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["build"],
                "category": "quality",
            },
            {
                "id": "docker-build",
                "name": "Docker Build",
                "order": 7,
                "description": "Build Docker container image",
                "mandatory": True,
                "enabled": True,
                "dependsOn": ["build", "post-security"],
                "category": "build",
            },
            {
                "id": "generate-sbom",
                "name": "Generate SBOM",
                "order": 8,
                "description": "Generate Software Bill of Materials",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["docker-build"],
                "category": "build",
            },
            {
                "id": "image-security-check",
                "name": "Image Security Check",
                "order": 9,
                "description": "Scan container image for vulnerabilities",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["docker-build"],
                "category": "quality",
            },
            {
                "id": "slack-notification",
                "name": "Send Slack Notification",
                "order": 10,
                "description": "Send build and deployment status to Slack",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout", "code-quality-check", "build", "docker-build"],
                "category": "notification",
            },
        ],
    },
    "nodejs": {
        "language": "nodejs",
        "steps": [
            {
                "id": "code-checkout",
                "name": "Code Checkout",
                "order": 1,
                "description": "Checkout source code from repository",
                "mandatory": True,
                "enabled": True,
                "category": "setup",
            },
            {
                "id": "pre-security-validation",
                "name": "Pre-Security Validation",
                "order": 2,
                "description": "Validate code and configuration before build",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout"],
                "category": "quality",
            },
            {
                "id": "env-validation",
                "name": "Environment Validation",
                "order": 3,
                "description": "Validate environment configuration and variables",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout"],
                "category": "setup",
            },
            {
                "id": "code-quality-check",
                "name": "Code Quality Check",
                "order": 4,
                "description": "Run ESLint, Prettier, and TSC",
                "mandatory": False,
                "enabled": True,
                "dependsOn": ["code-checkout"],
                "category": "quality",
            },
            {
                "id": "build",
                "name": "Build Application",
                "order": 5,
                "description": "Install dependencies and build",
                "mandatory": True,
                "enabled": True,
                "dependsOn": ["code-checkout", "pre-security-validation", "env-validation", "code-quality-check"],
                "category": "build",
            },
            {
                "id": "post-security",
                "name": "Post-Security Scan",
                "order": 6,
                "description": "Run security scans after build",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["build"],
                "category": "quality",
            },
            {
                "id": "docker-build",
                "name": "Docker Build",
                "order": 7,
                "description": "Build Docker container image",
                "mandatory": True,
                "enabled": True,
                "dependsOn": ["build", "post-security"],
                "category": "build",
            },
            {
                "id": "generate-sbom",
                "name": "Generate SBOM",
                "order": 8,
                "description": "Generate Software Bill of Materials",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["docker-build"],
                "category": "build",
            },
            {
                "id": "image-security-check",
                "name": "Image Security Check",
                "order": 9,
                "description": "Scan container image for vulnerabilities",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["docker-build"],
                "category": "quality",
            },
            {
                "id": "slack-notification",
                "name": "Send Slack Notification",
                "order": 10,
                "description": "Send build and deployment status to Slack",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout", "code-quality-check", "build", "docker-build"],
                "category": "notification",
            },
        ],
    },
    "python": {
        "language": "python",
        "steps": [
            {
                "id": "code-checkout",
                "name": "Code Checkout",
                "order": 1,
                "description": "Checkout source code from repository",
                "mandatory": True,
                "enabled": True,
                "category": "setup",
            },
            {
                "id": "pre-security-validation",
                "name": "Pre-Security Validation",
                "order": 2,
                "description": "Validate code and configuration before build",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout"],
                "category": "quality",
            },
            {
                "id": "env-validation",
                "name": "Environment Validation",
                "order": 3,
                "description": "Validate environment configuration and variables",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout"],
                "category": "setup",
            },
            {
                "id": "code-quality-check",
                "name": "Code Quality Check",
                "order": 4,
                "description": "Run pylint, black, and mypy",
                "mandatory": False,
                "enabled": True,
                "dependsOn": ["code-checkout"],
                "category": "quality",
            },
            {
                "id": "build",
                "name": "Build Application",
                "order": 5,
                "description": "Build Python application",
                "mandatory": True,
                "enabled": True,
                "dependsOn": ["code-checkout", "pre-security-validation", "env-validation", "code-quality-check"],
                "category": "build",
            },
            {
                "id": "post-security",
                "name": "Post-Security Scan",
                "order": 6,
                "description": "Run security scans after build",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["build"],
                "category": "quality",
            },
            {
                "id": "docker-build",
                "name": "Docker Build",
                "order": 7,
                "description": "Build Docker container image",
                "mandatory": True,
                "enabled": True,
                "dependsOn": ["build", "post-security"],
                "category": "build",
            },
            {
                "id": "generate-sbom",
                "name": "Generate SBOM",
                "order": 8,
                "description": "Generate Software Bill of Materials",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["docker-build"],
                "category": "build",
            },
            {
                "id": "image-security-check",
                "name": "Image Security Check",
                "order": 9,
                "description": "Scan container image for vulnerabilities",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["docker-build"],
                "category": "quality",
            },
            {
                "id": "slack-notification",
                "name": "Send Slack Notification",
                "order": 10,
                "description": "Send build and deployment status to Slack",
                "mandatory": False,
                "enabled": False,
                "dependsOn": ["code-checkout", "code-quality-check", "build", "docker-build"],
                "category": "notification",
            },
        ],
    },
}


def get_workflow_steps(language: str) -> List[Dict[str, Any]]:
    """
    Get workflow steps for a specific language.

    Args:
        language: Programming language

    Returns:
        List of workflow step configurations
    """
    normalized_lang = language.lower()

    # Map common language variations
    lang_map: Dict[str, str] = {
        "go": "go",
        "golang": "go",
        "java": "java",
        "java gradle": "java",
        "java maven": "java",
        "maven": "java",
        "gradle": "java",
        "nodejs": "nodejs",
        "node": "nodejs",
        "javascript": "nodejs",
        "typescript": "nodejs",
        "python": "python",
        "python3": "python",
    }

    mapped_lang = lang_map.get(normalized_lang, normalized_lang)
    workflow = LANGUAGE_WORKFLOWS.get(mapped_lang, LANGUAGE_WORKFLOWS["go"])

    # Return a deep copy to avoid modifying the original
    return [step.copy() for step in workflow["steps"]]


def toggle_workflow_step(steps: List[Dict[str, Any]], step_id: str, enabled: bool) -> List[Dict[str, Any]]:
    """
    Toggle a workflow step on/off.

    Args:
        steps: List of workflow steps
        step_id: ID of step to toggle
        enabled: New enabled state

    Returns:
        Updated steps list
    """
    return [
        {**step, "enabled": enabled} if step["id"] == step_id else step
        for step in steps
    ]


def validate_workflow_steps(steps: List[Dict[str, Any]]) -> Optional[str]:
    """
    Validate workflow configuration.

    Args:
        steps: List of workflow steps

    Returns:
        Error message if invalid, None if valid
    """
    enabled_steps = [s for s in steps if s.get("enabled", False)]

    # Check if all mandatory steps are enabled
    mandatory_steps = [s for s in steps if s.get("mandatory", False)]
    disabled_mandatory = [s for s in mandatory_steps if not s.get("enabled", False)]

    if disabled_mandatory:
        mandatory_names = ", ".join([s["name"] for s in disabled_mandatory])
        return f"Mandatory steps must be enabled: {mandatory_names}"

    # Check dependencies
    for step in enabled_steps:
        depends_on = step.get("dependsOn", [])
        if depends_on:
            for dep_id in depends_on:
                dep = next((s for s in steps if s["id"] == dep_id), None)
                if dep and not dep.get("enabled", False):
                    return f'"{step["name"]}" requires "{dep["name"]}" to be enabled'

    return None
