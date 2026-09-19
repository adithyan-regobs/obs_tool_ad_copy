"""
MCP Server for infra_chat_agent reference tools.

Two tools:
- list_services: List services with configurations
- show_service_config: Show config for a specific service

Adapted from infra_chat_agent_with_tools POC.
Key change: tenant_code is now a required parameter (not hardcoded).
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from typing import Any, Dict
import json
import logging

from app.infra_chat_agent.config.tools_enum import (
    ENVIRONMENT_VALUES,
    GEO_LOC_CODE_VALUES,
    normalize_environment,
    normalize_geo_loc_code,
)

logger = logging.getLogger(__name__)


class ReferenceToolsMCPServer:
    """MCP Server exposing service reference tools."""

    def __init__(self):
        self.server = Server("infra-chat-reference-tools")
        self._register_handlers()

    def _register_handlers(self):
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="list_services",
                    description="List all services that have configurations for a tenant",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tenant_code": {
                                "type": "string",
                                "description": "Tenant identifier (e.g., 'vance', 'aspora')"
                            },
                            "environment": {
                                "type": "string",
                                "description": "Deployment environment (e.g., 'qa', 'stage', 'staging', 'prod', 'production')"
                            },
                            "geo_loc_code": {
                                "type": "string",
                                "description": "Geographic location",
                                "enum": GEO_LOC_CODE_VALUES
                            }
                        },
                        "required": ["tenant_code", "environment", "geo_loc_code"]
                    }
                ),
                Tool(
                    name="show_service_config",
                    description="Show configuration for a specific service",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "service_name": {
                                "type": "string",
                                "description": "Name of the service"
                            },
                            "tenant_code": {
                                "type": "string",
                                "description": "Tenant identifier (e.g., 'vance', 'aspora')"
                            },
                            "environment": {
                                "type": "string",
                                "description": "Deployment environment (e.g., 'qa', 'stage', 'staging', 'prod', 'production')"
                            },
                            "geo_loc_code": {
                                "type": "string",
                                "description": "Geographic location",
                                "enum": GEO_LOC_CODE_VALUES
                            }
                        },
                        "required": ["service_name", "tenant_code", "environment", "geo_loc_code"]
                    }
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> list[TextContent]:
            logger.info(f"MCP call_tool: {name} with args: {arguments}")

            if name == "list_services":
                return await self._call_list_services(arguments)
            elif name == "show_service_config":
                return await self._call_show_service_config(arguments)
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    async def _call_list_services(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """List services via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.services.service_reference_tools_service import ServiceReferenceToolsService
            from app.core.enum import EnvironmentEnum

            async with AsyncSessionLocal() as db:
                service = ServiceReferenceToolsService(db)
                result = await service.list_services(
                    tenant_code=arguments["tenant_code"],
                    environment=EnvironmentEnum(normalize_environment(arguments["environment"])),
                    geo_loc_code=normalize_geo_loc_code(arguments["geo_loc_code"]),
                )
                return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"list_services failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_show_service_config(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Get config for a specific service."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.core.enum import EnvironmentEnum
            from app.repository.service_config_chat_repository import ServiceConfigChatRepository
            from app.services.service_config_agent.tools.config_tools import GetServiceConfig
            from app.services.service_config_agent.tools.service_resolver import ServiceResolver
            from app.utils.service_config_chat.service_matcher import ServiceMatcher

            async with AsyncSessionLocal() as db:
                repository = ServiceConfigChatRepository(db)
                matcher = ServiceMatcher()
                resolver = ServiceResolver(matcher)

                tool = GetServiceConfig(
                    service_resolver=resolver,
                    repository=repository,
                )

                result = await tool.execute(
                    service_name=arguments["service_name"],
                    tenant_code=arguments["tenant_code"],
                    environment=EnvironmentEnum(normalize_environment(arguments["environment"])),
                    geo_loc_code=normalize_geo_loc_code(arguments["geo_loc_code"]),
                )

                return [TextContent(type="text", text=json.dumps(dict(result), default=str))]

        except Exception as e:
            logger.error(f"show_service_config failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def run(self):
        """Run the MCP server via stdio."""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )


def main():
    """Entry point for MCP server."""
    import asyncio
    server = ReferenceToolsMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
