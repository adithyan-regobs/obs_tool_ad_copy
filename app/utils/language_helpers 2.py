"""
Language Helper Utilities

Helper functions for parsing and working with language reference data.
"""

from typing import Optional, Dict
import re


def parse_java_language(language_name: str) -> Dict[str, Optional[str]]:
    """
    Parse Java language name to extract build tool.

    Handles formats like:
    - "Java Maven 21 LTS" -> {"language": "java", "build_tool": "maven"}
    - "Java Maven 17 LTS" -> {"language": "java", "build_tool": "maven"}
    - "Java 21 LTS" -> {"language": "java", "build_tool": "gradle"} (default to gradle)
    - "Java 17 LTS" -> {"language": "java", "build_tool": "gradle"} (default to gradle)
    - "Java" -> {"language": "java", "build_tool": "gradle"} (default to gradle)

    Note: Default is Gradle because existing "Java X LTS" entries use java-gradle.yml

    Args:
        language_name: Language name from language_ref.name

    Returns:
        Dict with "language" and "build_tool" keys
    """
    if not language_name:
        return {"language": None, "build_tool": None}

    # Normalize and split
    parts = language_name.lower().strip().split()

    if not parts:
        return {"language": None, "build_tool": None}

    language = parts[0]

    # Default build tool is Gradle (existing Java entries use java-gradle.yml)
    build_tool = "gradle"

    # Check if second word is a build tool
    if len(parts) > 1:
        second_word = parts[1]
        if second_word in ("maven", "gradle"):
            build_tool = second_word

    return {
        "language": language,
        "build_tool": build_tool
    }


def parse_java_build_tool(language_name: str) -> str:
    """
    Extract build tool from Java language name.

    Convenience function that returns just the build tool string.

    Args:
        language_name: Language name from language_ref.name

    Returns:
        "maven" or "gradle" (defaults to "gradle" if not specified)
    """
    result = parse_java_language(language_name)
    return result.get("build_tool") or "gradle"


def is_java_language(language_name: Optional[str]) -> bool:
    """
    Check if language is a Java variant.

    Handles: "Java", "java", "JAVA", "Java Maven 17 LTS", "Java 21 LTS", etc.

    Args:
        language_name: Language name from language_ref.name

    Returns:
        True if Java, False otherwise
    """
    if not language_name:
        return False
    return language_name.lower().strip().split()[0] == "java"


def get_jar_directory(build_tool: str) -> str:
    """
    Get the JAR output directory based on build tool.

    Args:
        build_tool: "maven" or "gradle"

    Returns:
        JAR directory path ("target" for Maven, "build/libs" for Gradle)
    """
    if build_tool.lower() == "gradle":
        return "build/libs"
    return "target"


def get_jar_path(build_path: Optional[str], build_tool: str) -> str:
    """
    Get full JAR path for docker build --build-arg JAR_FILE=

    Args:
        build_path: Optional build path (e.g., "services/user-api")
        build_tool: "maven" or "gradle"

    Returns:
        JAR path like "target/*.jar" or "services/user-api/build/libs/*.jar"
    """
    jar_dir = get_jar_directory(build_tool)

    if build_path:
        # Remove trailing slash if present
        build_path = build_path.rstrip("/")
        return f"{build_path}/{jar_dir}/*.jar"

    return f"{jar_dir}/*.jar"


def sanitize_service_name_for_path(service_name: str) -> str:
    """
    Sanitize service name for use in file paths.

    Converts "My Service Name" -> "my-service-name"

    Args:
        service_name: Service name from services_mst.name

    Returns:
        Sanitized name safe for file paths
    """
    if not service_name:
        return "service"

    # Convert to lowercase
    name = service_name.lower()

    # Replace spaces and underscores with hyphens
    name = re.sub(r'[\s_]+', '-', name)

    # Remove any characters that aren't alphanumeric or hyphens
    name = re.sub(r'[^a-z0-9-]', '', name)

    # Remove consecutive hyphens
    name = re.sub(r'-+', '-', name)

    # Remove leading/trailing hyphens
    name = name.strip('-')

    return name or "service"


def get_dockerfile_path(
    service_name: str,
    build_path: Optional[str],
    is_java: bool
) -> str:
    """
    Get the Dockerfile path based on service configuration.

    Rules:
    - If build_path is provided AND language is Java: docker/{service-name}/Dockerfile
    - Otherwise: Dockerfile (root)

    Args:
        service_name: Service name from services_mst.name
        build_path: Build path from service_config.config.build_path
        is_java: Whether the language is Java

    Returns:
        Dockerfile path (e.g., "docker/my-service/Dockerfile" or "Dockerfile")
    """
    if build_path and is_java:
        sanitized_name = sanitize_service_name_for_path(service_name)
        return f"docker/{sanitized_name}/Dockerfile"

    return "Dockerfile"
