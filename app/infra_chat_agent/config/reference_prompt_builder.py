"""
Prompt builder for REFERENCE workflow intent detection.

Provides configurable prompt generation for REFERENCE tools.
Fetches tools dynamically from MCP server (industry standard pattern).
"""
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import logging

from app.infra_chat_agent.config.mcp_tool_provider import (
    get_reference_mcp_tools_sync,
    format_tools_for_prompt,
)

logger = logging.getLogger(__name__)


@dataclass
class ReferenceToolConfig:
    """Configuration for a reference tool."""
    name: str
    description: str
    required_params: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)


@dataclass
class ReferenceConfig:
    """Configuration for REFERENCE workflow prompts."""
    tools: List[ReferenceToolConfig] = field(default_factory=list)
    services: List[str] = field(default_factory=list)
    parameters: List[str] = field(default_factory=list)


# Fallback configuration - used only if MCP server is unavailable
FALLBACK_REFERENCE_CONFIG = ReferenceConfig(
    tools=[
        ReferenceToolConfig(
            name="list_services",
            description="List all services for the tenant",
            required_params=[],
            examples=[
                "list services",
                "show me all services",
                "what services do we have",
                "show services",
            ],
        ),
    ],
    services=[],
    parameters=[],
)


class ReferencePromptBuilder:
    """
    Configurable prompt builder for REFERENCE workflow.

    Fetches tool definitions dynamically from MCP server.
    Falls back to static config if MCP server is unavailable.
    """

    def __init__(
        self,
        config: Optional[ReferenceConfig] = None,
        api_base_url: str = "http://localhost:8080",
        use_mcp: bool = True,
    ):
        self.config = config or FALLBACK_REFERENCE_CONFIG
        self.api_base_url = api_base_url
        self.use_mcp = use_mcp
        self._mcp_tools: Optional[List[Dict[str, Any]]] = None

    def _get_mcp_tools(self) -> List[Dict[str, Any]]:
        """Get REFERENCE tools (cached)."""
        if self._mcp_tools is None and self.use_mcp:
            try:
                self._mcp_tools = get_reference_mcp_tools_sync()
            except Exception as e:
                logger.warning(f"Failed to get MCP tools, using fallback: {e}")
                self._mcp_tools = []
        return self._mcp_tools or []

    def build_intent_detection_prompt_section(self) -> str:
        """
        Build the REFERENCE section for intent detection system prompt.

        Dynamically fetches tools from MCP server if use_mcp=True.
        """
        lines = [
            "### 3. REFERENCE",
            "The user wants to query information about existing microservices.",
            "",
            "REQUIRED FIELD: tool_name",
            "",
            "Available tools:",
        ]

        # Try MCP tools first (industry standard)
        mcp_tools = self._get_mcp_tools()

        if mcp_tools:
            # Use tools from MCP server
            lines.append(format_tools_for_prompt(mcp_tools))
        else:
            # Fallback to static config
            for tool in self.config.tools:
                if tool.required_params:
                    lines.append(f"- {tool.name}: {tool.description} (requires: {', '.join(tool.required_params)})")
                else:
                    lines.append(f"- {tool.name}: {tool.description}")

        lines.append("")
        lines.append("Example REFERENCE phrases:")
        lines.append('- "list services" → REFERENCE with tool_name=list_services')
        lines.append('- "show config of user-api" → REFERENCE with tool_name=list_config_of_service, slot_parameters={service_name: "user-api"}')
        lines.append('- "what is the cpu of order-processor" → REFERENCE with tool_name=list_a_param_of_service, slot_parameters={service_name: "order-processor", parameter_name: "cpu"}')
        lines.append("")
        lines.append("REFERENCE is for QUERYING existing services, not creating new infrastructure.")

        return "\n".join(lines)

    def get_tool_names(self) -> List[str]:
        """Get list of available tool names."""
        mcp_tools = self._get_mcp_tools()
        if mcp_tools:
            return [t["name"] for t in mcp_tools]
        return [t.name for t in self.config.tools]


# Singleton instance - uses MCP by default
default_reference_prompt_builder = ReferencePromptBuilder()
