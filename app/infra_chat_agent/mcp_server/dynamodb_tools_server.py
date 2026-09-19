"""
MCP Server for DynamoDB table creation tools.

DynamoDB Table Creation Tools:
- create_dynamodb_table: Create a new DynamoDB table (validate and format parameters)
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from typing import Any, Dict
import json
import logging

from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_environment, normalize_geo_loc_code

logger = logging.getLogger(__name__)


class DynamoDbToolsMCPServer:
    """MCP Server exposing DynamoDB table creation tools."""

    def __init__(self):
        self.server = Server("dynamodb-creation-tools")
        self._register_handlers()

    def _register_handlers(self):
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="create_dynamodb_table",
                    description=(
                        "Create a new DynamoDB table. "
                        "Required parameters = identifier(name), partition_key, partition_key_type. "
                        "DynamoDB table will be created appropriately."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "identifier": {
                                "type": "string",
                                "description": "DynamoDB table name/identifier (e.g., 'user-sessions', 'order_events')"
                            },
                            "partition_key": {
                                "type": "string",
                                "description": "Partition key attribute name (e.g., 'user_id', 'order_id')"
                            },
                            "partition_key_type": {
                                "type": "string",
                                "description": "Partition key data type"
                            },
                            "tenant_code": {
                                "type": "string",
                                "description": "Tenant identifier (e.g., 'aspora', 'vance')"
                            },
                            "product_name": {
                                "type": "string",
                                "description": "Product name (e.g., 'Core', 'Falcon')"
                            },
                            "environment": {
                                "type": "string",
                                "description": "Environment (e.g., 'qa', 'stage', 'staging', 'prod', 'production')"
                            },
                            "geo_loc_code": {
                                "type": "string",
                                "description": "Geographic location code (e.g., 'region-aspora-mumbai', 'region-aspora-london')"
                            }
                        },
                        "required": ["identifier", "partition_key", "partition_key_type", "tenant_code", "product_name", "environment", "geo_loc_code"]
                    }
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> list[TextContent]:
            logger.info(f"MCP call_tool: {name} with args: {arguments}")

            if name == "create_dynamodb_table":
                return await self._call_create_dynamodb_table(arguments)
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    async def _call_create_dynamodb_table(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Create DynamoDB table via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.dynamodb_creation_service import DynamoDbCreationService

            async with AsyncSessionLocal() as db:
                service = DynamoDbCreationService(db)
                result = await service.create_dynamodb_table(
                    identifier=arguments["identifier"],
                    partition_key=arguments["partition_key"],
                    partition_key_type=arguments["partition_key_type"],
                    tenant_code=arguments["tenant_code"],
                    product_name=arguments["product_name"],
                    environment=Environment(normalize_environment(arguments["environment"])),
                    geo_loc_code=normalize_geo_loc_code(arguments["geo_loc_code"]),
                )
                return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"create_dynamodb_table failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "error": str(e)
            }))]

    async def run(self):
        """Run the MCP server via stdio."""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )


def main():
    """Entry point for DynamoDB creation MCP server."""
    import asyncio
    server = DynamoDbToolsMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
