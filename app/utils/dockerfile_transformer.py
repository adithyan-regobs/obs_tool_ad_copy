"""
Dockerfile Transformer Utility

Pure utility functions for transforming Dockerfiles to add/remove Datadog configuration.
Used when enabling/disabling Datadog sidecar for Java services.
"""

import re
from typing import Tuple, Optional, List


# Constants
DEFAULT_DD_AGENT_VERSION = "v1.56.1"
DD_AGENT_JAR_URL = f"https://github.com/DataDog/dd-trace-java/releases/download/{DEFAULT_DD_AGENT_VERSION}/dd-java-agent-1.56.1.jar"

# OTel patterns to identify OTel-related lines
OTEL_PATTERNS = [
    r'opentelemetry-javaagent\.jar',
    r'OTEL_SERVICE_NAME',
    r'OTEL_EXPORTER',
    r'OTEL_PROPAGATORS',
    r'OTEL_TRACES',
    r'OTEL_METRICS',
    r'OTEL_LOGS',
    r'OTEL_RESOURCE',
    r'otel\.sdk',
    r'otel\.instrumentation',
]

# Datadog patterns to identify Datadog-related lines
DATADOG_PATTERNS = [
    r'dd-java-agent.*\.jar',
    r'DD_SERVICE',
    r'DD_VERSION',
    r'DD_AGENT_HOST',
    r'DD_TRACE',
    r'DD_DOGSTATSD',
    r'DD_LOGS',
    r'DD_RUNTIME',
    r'DD_PROFILING',
    r'DD_APPSEC',
    r'DD_IAST',
    r'DD_DBM',
]

# Default Datadog advanced options (used when no custom options provided)
DEFAULT_DATADOG_ADVANCED_OPTIONS = [
    {"key": "DD_LOGS_INJECTION", "value": "true"},
    {"key": "DD_TRACE_ENABLED", "value": "true"},
    {"key": "DD_RUNTIME_METRICS_ENABLED", "value": "true"},
    {"key": "DD_PROFILING_ENABLED", "value": "true"},
    {"key": "DD_PROFILING_DDPROF_ENABLED", "value": "true"},
    {"key": "DD_PROFILING_CPU_ENABLED", "value": "true"},
    {"key": "DD_PROFILING_WALLCLOCK_ENABLED", "value": "true"},
    {"key": "DD_PROFILING_ALLOCATION_ENABLED", "value": "true"},
    {"key": "DD_PROFILING_ALLOCATION_SAMPLE_LIMIT", "value": "10000"},
    {"key": "DD_PROFILING_HEAP_ENABLED", "value": "true"},
    {"key": "DD_PROFILING_HEAP_SAMPLE_LIMIT", "value": "100"},
    {"key": "DD_TRACE_AGENT_MAX_QUEUE_SIZE", "value": "1000"},
    {"key": "DD_PROFILING_UPLOAD_PERIOD", "value": "60"},
    {"key": "DD_PROFILING_STACKDEPTH", "value": "128"},
]


def calculate_memory_settings(ram_gb: float) -> Tuple[int, int]:
    """
    Calculate JVM memory settings based on RAM allocation.

    Formula:
        Xms = 25% of RAM (capped at 512MB)
        Xmx = 50% of RAM (capped at 2GB)

    Args:
        ram_gb: RAM in GB (e.g., 2, 4, 8)

    Returns:
        Tuple of (xms_mb, xmx_mb)

    Example:
        ram_gb=2 -> (512, 1024)
        ram_gb=4 -> (512, 2048)  # Xms capped at 512
        ram_gb=8 -> (512, 2048)  # Both capped
    """
    ram_mb = int(ram_gb * 1024)
    xms = max(128, int(ram_mb * 0.25))  # Minimum 128MB
    xmx = max(256, int(ram_mb * 0.50))  # Minimum 256MB

    # Apply caps
    xms = min(xms, 512)   # Cap Xms at 512MB
    xmx = min(xmx, 2048)  # Cap Xmx at 2GB

    return (xms, xmx)


def is_java_language(language_name: Optional[str]) -> bool:
    """
    Check if language is a Java variant.

    Handles: "Java", "java", "JAVA", "Java 17", "java 21", "Java (OpenJDK 17)"

    Args:
        language_name: Language name from language_ref table

    Returns:
        True if Java, False otherwise
    """
    if not language_name:
        return False
    return language_name.lower().strip().split()[0] == "java"


def _is_otel_line(line: str) -> bool:
    """Check if a line is OTel-related."""
    for pattern in OTEL_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return True
    return False


def _is_datadog_line(line: str) -> bool:
    """Check if a line is Datadog-related."""
    for pattern in DATADOG_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return True
    return False


def _is_otel_header(line: str) -> bool:
    """Check if line is OTel section header."""
    return bool(re.match(r'^\s*##?\s*[Oo]tel\s*[Cc]onfig', line))


def _is_datadog_header(line: str) -> bool:
    """Check if line is Datadog section header."""
    return bool(re.match(r'^\s*##?\s*[Dd]atadog\s*[Cc]onfig', line))


def _is_already_commented(line: str) -> bool:
    """Check if line is already commented out."""
    return line.strip().startswith('#')


def comment_out_otel_block(dockerfile_content: str) -> str:
    """
    Comment out OTel configuration lines in Dockerfile.

    Identifies OTel block by:
    - Lines containing "## Otel" header
    - ADD/ENV lines with otel-related content
    - JAVA_TOOL_OPTIONS with opentelemetry agent

    Args:
        dockerfile_content: Original Dockerfile content

    Returns:
        Modified Dockerfile with OTel lines commented out
    """
    lines = dockerfile_content.split('\n')
    result_lines = []
    in_otel_block = False

    for line in lines:
        # Check for OTel header
        if _is_otel_header(line):
            in_otel_block = True
            if not _is_already_commented(line):
                # Update header to indicate it's commented out
                result_lines.append("## Otel Configs (commented out - using Datadog)")
            else:
                result_lines.append(line)
            continue

        # Check for other section headers (end of OTel block)
        if in_otel_block and re.match(r'^\s*##\s*\w+', line) and not _is_otel_header(line):
            in_otel_block = False

        # Comment out OTel-related lines
        if _is_otel_line(line) and not _is_already_commented(line):
            result_lines.append(f"# {line}")
        elif in_otel_block and line.strip() and not _is_already_commented(line):
            # Comment out lines within OTel block (ENV, ADD, etc.)
            if line.strip().startswith(('ENV', 'ADD', 'RUN')):
                result_lines.append(f"# {line}")
            else:
                result_lines.append(line)
        else:
            result_lines.append(line)

    return '\n'.join(result_lines)


def _extract_existing_java_tool_options(dockerfile_content: str) -> Optional[str]:
    """
    Extract existing JAVA_TOOL_OPTIONS value from Dockerfile.

    Returns the value (without quotes) or None if not found.
    """
    for line in dockerfile_content.split('\n'):
        # Match ENV JAVA_TOOL_OPTIONS="..." or ENV JAVA_TOOL_OPTIONS=...
        match = re.match(r'^\s*ENV\s+JAVA_TOOL_OPTIONS\s*=\s*["\']?([^"\']*)["\']?\s*$', line)
        if match:
            return match.group(1).strip()
    return None


def _merge_java_tool_options(
    existing_opts: Optional[str],
    xms_mb: Optional[int],
    xmx_mb: Optional[int]
) -> str:
    """
    Merge existing JAVA_TOOL_OPTIONS with Datadog settings.

    Strategy:
    - If xms/xmx provided from frontend, use those values
    - If not provided, preserve existing -Xms/-Xmx from Dockerfile
    - Remove any existing -javaagent (will add Datadog agent)
    - Remove any existing OTel-related options
    - Keep other custom JVM options
    - Add Datadog agent and required options

    Args:
        existing_opts: Existing JAVA_TOOL_OPTIONS value
        xms_mb: New Xms value in MB (optional - if None, preserve existing)
        xmx_mb: New Xmx value in MB (optional - if None, preserve existing)

    Returns:
        Merged JAVA_TOOL_OPTIONS value
    """
    # Start with existing options or empty
    opts = existing_opts or ""

    # Extract existing memory settings before removing them
    existing_xms = None
    existing_xmx = None
    xms_match = re.search(r'-Xms(\d+)([mgMG])?', opts)
    xmx_match = re.search(r'-Xmx(\d+)([mgMG])?', opts)
    if xms_match:
        existing_xms = xms_match.group(0)  # Keep full string like "-Xms768m"
    if xmx_match:
        existing_xmx = xmx_match.group(0)  # Keep full string like "-Xmx1536m"

    # Patterns to remove from existing options
    remove_patterns = [
        r'-Xms\d+[mgMG]?',           # Existing Xms
        r'-Xmx\d+[mgMG]?',           # Existing Xmx
        r'-javaagent:[^\s]+',         # Any javaagent
        r'-Dotel\.[^\s]+',            # OTel system properties
        r'-Ddd\.[^\s]+',              # Datadog system properties (DD_* env vars handle this)
    ]

    # Remove patterns we'll replace
    for pattern in remove_patterns:
        opts = re.sub(pattern, '', opts, flags=re.IGNORECASE)

    # Clean up multiple spaces
    opts = ' '.join(opts.split())

    # Check if existing options have a GC collector
    gc_collectors = [
        'UseG1GC', 'UseShenandoahGC', 'UseZGC', 'UseParallelGC',
        'UseConcMarkSweepGC', 'UseSerialGC'
    ]
    has_gc_collector = any(gc in opts for gc in gc_collectors)

    # Build new options
    new_opts_parts = []

    # Add memory settings: use frontend values if provided, otherwise preserve existing
    # Handle xms: prefer new value, fallback to existing
    if xms_mb:
        new_opts_parts.append(f"-Xms{xms_mb}m")
    elif existing_xms:
        new_opts_parts.append(existing_xms)

    # Handle xmx: prefer new value, fallback to existing
    if xmx_mb:
        new_opts_parts.append(f"-Xmx{xmx_mb}m")
    elif existing_xmx:
        new_opts_parts.append(existing_xmx)

    # Add GC and container options (only if not already present)
    # Skip G1GC if user already has a GC collector configured
    if not has_gc_collector:
        new_opts_parts.append("-XX:+UseG1GC")
        # Only add G1GC-specific options if we're adding G1GC
        if "-XX:MaxGCPauseMillis" not in opts:
            new_opts_parts.append("-XX:MaxGCPauseMillis=100")

    # Add other container/performance options if not present
    if "-XX:+UseStringDeduplication" not in opts:
        new_opts_parts.append("-XX:+UseStringDeduplication")
    if "-XX:+UseContainerSupport" not in opts:
        new_opts_parts.append("-XX:+UseContainerSupport")

    # Add Datadog agent near the end (before logging level)
    # Note: DD_* env vars are already set above, no need for -Ddd.* system properties
    new_opts_parts.append("-javaagent:/app/dd-java-agent.jar")

    # Add logging level at the end
    if "-Dlogging.level.root=" not in opts:
        new_opts_parts.append("-Dlogging.level.root=info")

    # Combine: JVM options first, then javaagent and logging at the end
    # This preserves the conventional order: GC opts -> container opts -> agent -> logging
    result_parts = new_opts_parts
    if opts:
        # Insert remaining existing options before javaagent (second to last position)
        # Find where javaagent is and insert before it
        agent_idx = next((i for i, p in enumerate(result_parts) if '-javaagent:' in p), len(result_parts))
        result_parts.insert(agent_idx, opts)

    return ' '.join(result_parts)


def _comment_out_java_tool_options_line(dockerfile_content: str) -> str:
    """Comment out existing JAVA_TOOL_OPTIONS line (preserve for later restoration)."""
    lines = dockerfile_content.split('\n')
    result_lines = []
    for line in lines:
        # Match uncommented JAVA_TOOL_OPTIONS line (not already commented)
        if re.match(r'^\s*ENV\s+JAVA_TOOL_OPTIONS\s*=', line) and not line.strip().startswith('#'):
            result_lines.append(f'# {line}  # Commented by Datadog integration')
        else:
            result_lines.append(line)
    return '\n'.join(result_lines)


def _uncomment_java_tool_options_line(dockerfile_content: str) -> str:
    """Uncomment previously commented JAVA_TOOL_OPTIONS line."""
    lines = dockerfile_content.split('\n')
    result_lines = []
    for line in lines:
        # Match commented JAVA_TOOL_OPTIONS line with our marker
        if re.match(r'^\s*#\s*ENV\s+JAVA_TOOL_OPTIONS\s*=', line):
            # Remove the comment prefix and trailing marker
            uncommented = re.sub(r'^\s*#\s*', '', line)
            uncommented = re.sub(r'\s*#\s*Commented by Datadog integration\s*$', '', uncommented)
            result_lines.append(uncommented)
        else:
            result_lines.append(line)
    return '\n'.join(result_lines)


def generate_datadog_block(
    service_name: str,
    xms_mb: Optional[int] = None,
    xmx_mb: Optional[int] = None,
    dd_agent_version: str = DEFAULT_DD_AGENT_VERSION,
    existing_java_tool_options: Optional[str] = None,
    advanced_options: Optional[List[dict]] = None
) -> str:
    """
    Generate Datadog configuration block for Dockerfile.

    Args:
        service_name: DD_SERVICE value (from service.name in DB)
        xms_mb: -Xms value in MB (optional, omitted if not provided)
        xmx_mb: -Xmx value in MB (optional, omitted if not provided)
        dd_agent_version: Datadog Java agent version
        existing_java_tool_options: Existing JAVA_TOOL_OPTIONS to merge with
        advanced_options: List of {"key": "DD_*", "value": "..."} dicts for custom ENV vars

    Returns:
        Dockerfile snippet with Datadog configuration
    """
    # Extract version number without 'v' prefix for JAR filename
    version_short = dd_agent_version.lstrip('v')
    agent_url = f"https://github.com/DataDog/dd-trace-java/releases/download/{dd_agent_version}/dd-java-agent-{version_short}.jar"

    # Merge existing JAVA_TOOL_OPTIONS with Datadog settings
    merged_java_opts = _merge_java_tool_options(existing_java_tool_options, xms_mb, xmx_mb)

    # Use provided advanced_options or fall back to defaults
    options_to_use = advanced_options if advanced_options else DEFAULT_DATADOG_ADVANCED_OPTIONS

    # Generate dynamic ENV lines from advanced options
    env_lines = []
    for opt in options_to_use:
        key = opt.get('key', '')
        value = opt.get('value', '')
        if key and value:
            env_lines.append(f'ENV {key}="{value}"')

    advanced_env_block = '\n'.join(env_lines)

    return f"""## Datadog Configs
ADD {agent_url} /app/dd-java-agent.jar

# Set environment variables for Datadog
ENV DD_SERVICE="{service_name}"
ENV DD_VERSION="latest"
ENV DD_AGENT_HOST="localhost"
ENV DD_TRACE_AGENT_PORT="8126"
ENV DD_DOGSTATSD_PORT="8125"
# Datadog advanced options
{advanced_env_block}

ENV JAVA_TOOL_OPTIONS="{merged_java_opts}"
"""


def remove_datadog_block(dockerfile_content: str) -> str:
    """
    Remove Datadog configuration block from Dockerfile.

    Identifies and removes:
    - Lines containing "## Datadog" header
    - ADD lines with dd-java-agent
    - ENV lines with DD_* variables
    - JAVA_TOOL_OPTIONS with Datadog agent

    Args:
        dockerfile_content: Original Dockerfile content

    Returns:
        Modified Dockerfile with Datadog block removed
    """
    lines = dockerfile_content.split('\n')
    result_lines = []
    in_datadog_block = False
    skip_blank_after_block = False

    for i, line in enumerate(lines):
        # Check for Datadog header
        if _is_datadog_header(line):
            in_datadog_block = True
            skip_blank_after_block = True
            continue  # Skip this line

        # Check for other section headers (end of Datadog block)
        if in_datadog_block and re.match(r'^\s*##\s*\w+', line) and not _is_datadog_header(line):
            in_datadog_block = False
            skip_blank_after_block = False

        # Skip Datadog-related lines
        if _is_datadog_line(line):
            continue

        # Skip lines within Datadog block
        if in_datadog_block:
            # Skip ENV, ADD, RUN, and comment lines within the block
            stripped = line.strip()
            if stripped.startswith(('ENV', 'ADD', 'RUN', '#')) or not stripped:
                continue
            else:
                # Non-Datadog line, end of block
                in_datadog_block = False
                skip_blank_after_block = False

        # Skip extra blank lines after removing the block
        if skip_blank_after_block and not line.strip():
            continue
        else:
            skip_blank_after_block = False

        result_lines.append(line)

    # Clean up multiple consecutive blank lines
    cleaned_lines = []
    prev_blank = False
    for line in result_lines:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        cleaned_lines.append(line)
        prev_blank = is_blank

    return '\n'.join(cleaned_lines)


def _find_insertion_point(lines: List[str]) -> int:
    """
    Find the best insertion point for Datadog block.

    Strategy:
    1. After commented OTel block
    2. Before ENTRYPOINT/CMD
    3. At end of file
    """
    entrypoint_idx = None
    otel_end_idx = None
    in_otel_block = False

    for i, line in enumerate(lines):
        # Track OTel block
        if _is_otel_header(line) or 'commented out' in line.lower() and 'otel' in line.lower():
            in_otel_block = True

        if in_otel_block and line.strip() and not line.strip().startswith('#'):
            if not _is_otel_line(line):
                otel_end_idx = i
                in_otel_block = False

        # Track ENTRYPOINT/CMD
        if re.match(r'^\s*(ENTRYPOINT|CMD)\s', line):
            entrypoint_idx = i

    # Return appropriate insertion point
    if otel_end_idx is not None:
        return otel_end_idx
    if entrypoint_idx is not None:
        return entrypoint_idx
    return len(lines)


def transform_dockerfile_for_datadog(
    dockerfile_content: str,
    service_name: str,
    xms_mb: Optional[int] = None,
    xmx_mb: Optional[int] = None,
    dd_agent_version: str = DEFAULT_DD_AGENT_VERSION,
    advanced_options: Optional[List[dict]] = None
) -> str:
    """
    Full Dockerfile transformation to add Datadog configuration.

    Steps:
    1. Extract existing JAVA_TOOL_OPTIONS (to merge custom settings)
    2. Comment out OTel block if present
    3. Remove any existing Datadog block (to avoid duplicates)
    4. Comment out existing JAVA_TOOL_OPTIONS line (preserve for later restoration when Datadog is removed)
    5. Generate and insert new Datadog block with merged JAVA_TOOL_OPTIONS

    Args:
        dockerfile_content: Original Dockerfile content
        service_name: Service name for DD_SERVICE
        xms_mb: -Xms value in MB (optional, from frontend)
        xmx_mb: -Xmx value in MB (optional, from frontend)
        dd_agent_version: Datadog agent version
        advanced_options: List of {"key": "DD_*", "value": "..."} dicts for custom ENV vars

    Returns:
        Transformed Dockerfile content
    """
    # Step 1: Extract existing JAVA_TOOL_OPTIONS before any modifications
    existing_java_opts = _extract_existing_java_tool_options(dockerfile_content)

    # Step 2: Comment out OTel block
    content = comment_out_otel_block(dockerfile_content)

    # Step 3: Remove any existing Datadog block (avoid duplicates)
    content = remove_datadog_block(content)

    # Step 4: Comment out existing JAVA_TOOL_OPTIONS line (preserve for later restoration)
    content = _comment_out_java_tool_options_line(content)

    # Step 5: Generate Datadog block with merged JAVA_TOOL_OPTIONS and advanced options
    datadog_block = generate_datadog_block(
        service_name, xms_mb, xmx_mb, dd_agent_version, existing_java_opts, advanced_options
    )

    # Step 6: Find insertion point and insert
    lines = content.split('\n')
    insertion_point = _find_insertion_point(lines)

    # Insert Datadog block
    lines.insert(insertion_point, datadog_block)

    return '\n'.join(lines)


def transform_dockerfile_remove_datadog(dockerfile_content: str) -> str:
    """
    Remove Datadog configuration from Dockerfile.

    Steps:
    1. Remove Datadog block entirely (including Datadog's JAVA_TOOL_OPTIONS)
    2. Uncomment the original JAVA_TOOL_OPTIONS line (if it was commented by Datadog integration)
    3. Leave OTel configs commented (no restoration)

    Args:
        dockerfile_content: Original Dockerfile content

    Returns:
        Dockerfile with Datadog removed and original JAVA_TOOL_OPTIONS restored
    """
    # Step 1: Remove Datadog block
    content = remove_datadog_block(dockerfile_content)

    # Step 2: Uncomment original JAVA_TOOL_OPTIONS line
    content = _uncomment_java_tool_options_line(content)

    return content
