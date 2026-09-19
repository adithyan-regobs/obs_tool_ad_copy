"""
Markdown Table Formatter for Service Config Agent.

Pre-formats comparison results as markdown tables to prevent LLM
from generating inconsistent table formats.
"""

from typing import Any, Dict, List, Optional


def format_comparison_table(
    differences: Dict[str, Dict[str, Any]],
    label_a: str,
    label_b: str,
    only_in_a: Optional[Dict[str, Any]] = None,
    only_in_b: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Format config comparison as a markdown table.

    Args:
        differences: Dict of {param: {a: val, b: val}}
        label_a: Label for first service/env
        label_b: Label for second service/env
        only_in_a: Optional params only in first config
        only_in_b: Optional params only in second config

    Returns:
        Markdown formatted table string
    """
    if not differences and not only_in_a and not only_in_b:
        return "**No differences found** - configurations are identical."

    lines = []

    # Main differences table
    if differences:
        lines.append("### Parameter Differences")
        lines.append("")
        lines.append(f"| Parameter | {label_a} | {label_b} |")
        lines.append("|-----------|--------|--------|")

        for param, values in sorted(differences.items()):
            val_a = _format_value(values.get("a") or values.get("env_a", ""))
            val_b = _format_value(values.get("b") or values.get("env_b", ""))
            lines.append(f"| {param} | {val_a} | {val_b} |")

        lines.append("")

    # Only in A
    if only_in_a:
        lines.append(f"### Only in {label_a}")
        lines.append("")
        lines.append("| Parameter | Value |")
        lines.append("|-----------|-------|")
        for param, value in sorted(only_in_a.items()):
            lines.append(f"| {param} | {_format_value(value)} |")
        lines.append("")

    # Only in B
    if only_in_b:
        lines.append(f"### Only in {label_b}")
        lines.append("")
        lines.append("| Parameter | Value |")
        lines.append("|-----------|-------|")
        for param, value in sorted(only_in_b.items()):
            lines.append(f"| {param} | {_format_value(value)} |")
        lines.append("")

    return "\n".join(lines)


def format_service_comparison_table(
    service_a_name: str,
    service_b_name: str,
    differences: Dict[str, Dict[str, Any]],
    only_in_a: Dict[str, Any],
    only_in_b: Dict[str, Any],
    identical_count: int = 0,
) -> str:
    """
    Format service-to-service comparison.

    Args:
        service_a_name: Name of first service
        service_b_name: Name of second service
        differences: Parameter differences
        only_in_a: Params only in first service
        only_in_b: Params only in second service
        identical_count: Count of identical parameters

    Returns:
        Pre-formatted markdown table
    """
    table = format_comparison_table(
        differences=differences,
        label_a=service_a_name,
        label_b=service_b_name,
        only_in_a=only_in_a,
        only_in_b=only_in_b,
    )

    # Add summary
    summary_parts = []
    if differences:
        summary_parts.append(f"{len(differences)} different")
    if only_in_a:
        summary_parts.append(f"{len(only_in_a)} unique to {service_a_name}")
    if only_in_b:
        summary_parts.append(f"{len(only_in_b)} unique to {service_b_name}")
    if identical_count:
        summary_parts.append(f"{identical_count} identical")

    summary = f"**Summary:** {', '.join(summary_parts)}"

    return f"{summary}\n\n{table}"


def format_environment_comparison_table(
    service_name: str,
    env_a: str,
    env_b: str,
    differences: Dict[str, Dict[str, Any]],
    only_in_a: Dict[str, Any],
    only_in_b: Dict[str, Any],
    identical_count: int = 0,
) -> str:
    """
    Format environment-to-environment comparison.

    Args:
        service_name: Name of the service
        env_a: First environment
        env_b: Second environment
        differences: Parameter differences
        only_in_a: Params only in first env
        only_in_b: Params only in second env
        identical_count: Count of identical parameters

    Returns:
        Pre-formatted markdown table
    """
    table = format_comparison_table(
        differences=differences,
        label_a=f"{env_a}",
        label_b=f"{env_b}",
        only_in_a=only_in_a,
        only_in_b=only_in_b,
    )

    # Add summary
    summary_parts = []
    if differences:
        summary_parts.append(f"{len(differences)} different")
    if only_in_a:
        summary_parts.append(f"{len(only_in_a)} only in {env_a}")
    if only_in_b:
        summary_parts.append(f"{len(only_in_b)} only in {env_b}")
    if identical_count:
        summary_parts.append(f"{identical_count} identical")

    summary = f"**{service_name}** comparison ({env_a} vs {env_b}): {', '.join(summary_parts)}"

    return f"{summary}\n\n{table}"


def format_parameter_list_table(
    parameter: str,
    services: List[Dict[str, Any]],
    environment: str,
) -> str:
    """
    Format parameter values across services as a table.

    Args:
        parameter: Parameter name
        services: List of {value, count} or {service_name, value}
        environment: Environment name

    Returns:
        Pre-formatted markdown table
    """
    if not services:
        return f"No services found with parameter '{parameter}' in {environment}."

    lines = [
        f"### {parameter} values in {environment}",
        "",
        "| Value | Count |",
        "|-------|-------|",
    ]

    for item in services:
        value = _format_value(item.get("value", ""))
        count = item.get("count", 1)
        lines.append(f"| {value} | {count} |")

    return "\n".join(lines)


def _format_value(value: Any) -> str:
    """Format a value for table display."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, dict)):
        # Truncate complex values
        s = str(value)
        return s[:50] + "..." if len(s) > 50 else s
    return str(value)
