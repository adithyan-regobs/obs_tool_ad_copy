"""
MCP Server for infra_chat_agent_with_tools.

Two tools:
- list_services: List services with configurations
- show_service_config: Show config for a specific service
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from typing import Any, Dict
import json
import logging

from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo

logger = logging.getLogger(__name__)


class InfraChatMCPServer:
    """MCP Server exposing service reference tools."""

    def __init__(self):
        self.server = Server("infra-chat-tools")
        self._register_handlers()

    def _register_handlers(self):
        """Register MCP protocol handlers."""
        env_values = resource_meta_repo.get_all_environment_values()

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="list_services",
                    description="List all services that have configurations",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "environment": {
                                "type": "string",
                                "description": "Environment",
                                "enum": env_values
                            },
                            "geo_loc_code": {
                                "type": "string",
                                "description": "Geographic location code"
                            }
                        },
                        "required": ["environment", "geo_loc_code"]
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
                            "environment": {
                                "type": "string",
                                "description": "Environment",
                                "enum": env_values
                            },
                            "geo_loc_code": {
                                "type": "string",
                                "description": "Geographic location code"
                            }
                        },
                        "required": ["service_name", "environment", "geo_loc_code"]
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
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async def _call_list_services(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """List services via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.services.service_reference_tools_service import ServiceReferenceToolsService
            from app.core.enum import EnvironmentEnum

            async with AsyncSessionLocal() as db:
                service = ServiceReferenceToolsService(db)
                result = await service.list_services(
                    tenant_code="vance",  # TODO: Pass from context
                    environment=EnvironmentEnum(arguments["environment"]),
                    geo_loc_code=arguments["geo_loc_code"],
                )
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

        except Exception as e:
            logger.error(f"list_services failed: {e}")
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
                    tenant_code="vance",  # TODO: Pass from context
                    environment=EnvironmentEnum(arguments["environment"]),
                    geo_loc_code=arguments["geo_loc_code"],
                )

                return [TextContent(type="text", text=json.dumps(dict(result), indent=2, default=str))]

        except Exception as e:
            logger.error(f"show_service_config failed: {e}")
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
    server = InfraChatMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
