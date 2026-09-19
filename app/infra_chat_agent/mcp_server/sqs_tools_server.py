"""
MCP Server for SQS queue creation tools.

SQS Queue Creation Tools:
- create_sqs_queue: Create a new SQS queue (validate and format parameters)
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from typing import Any, Dict
import json
import logging

from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.config_models import TenantId, InfraTypeCode

logger = logging.getLogger(__name__)

_SQS_INFRA_TYPE = InfraTypeCode("sqs_infrastructuretype_ref")


def _resolve_and_validate(tid: TenantId, param_name: str, meta_key: str, raw_value: str) -> str:
    """Resolve user input and validate against resource metadata options.

    Returns the resolved canonical value.
    Raises ValueError with valid options if the value is invalid.
    """
    resolved = resource_meta_repo.resolve_placement_value(tid, _SQS_INFRA_TYPE, meta_key, raw_value)
    options = resource_meta_repo.get_placement_options(tid, _SQS_INFRA_TYPE, meta_key)
    valid_values = {opt["value"] for opt in options}

    if resolved not in valid_values:
        raise ValueError(json.dumps({
            "parameter": param_name,
            "invalid_value": raw_value,
            "valid_options": options,
        }))
    return resolved


def validate_environment(tid: TenantId, raw_value: str) -> str:
    """Validate environment against resource metadata options (e.g., qa, stage, prod)."""
    return _resolve_and_validate(tid, "environment", "environment_enum", raw_value)


def validate_geo_loc_code(tid: TenantId, raw_value: str) -> str:
    """Validate geographic location code (region) against resource metadata options."""
    return _resolve_and_validate(tid, "region", "geo_loc_mst_code", raw_value)


def validate_product_name(tid: TenantId, raw_value: str) -> str:
    """Validate product name and resolve label to code (e.g., 'Core' → UUID)."""
    return _resolve_and_validate(tid, "product_name", "applications_mst_code", raw_value)


# Placement params that require user input
# Keys: (tool_arg_key, user_facing_name, meta_key)
_USER_PLACEMENT_KEYS = [
    ("tenant_code", "tenant_code", "tenant_code"),
    ("product_name", "product_name", "applications_mst_code"),
    ("environment", "environment", "environment_enum"),
    ("geo_loc_code", "region", "geo_loc_mst_code"),
]


class SqsToolsMCPServer:
    """MCP Server exposing SQS queue creation tools."""

    def __init__(self):
        self.server = Server("sqs-creation-tools")
        self._register_handlers()

    def _register_handlers(self):
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="create_sqs_queue",
                    description=(
                        "Create a new SQS queue. "
                        "Required parameter = identifier(name). "
                        "SQS queue will be created appropriately."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "identifier": {
                                "type": "string",
                                "description": "SQS queue name/identifier (e.g., 'order-events', 'payment-processor')"
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
                                "description": "Environment (e.g., 'qa', 'stage', 'prod')"
                            },
                            "geo_loc_code": {
                                "type": "string",
                                "description": "Region (e.g., 'Mumbai', 'London')"
                            },
                            "fifo_queue": {
                                "type": "boolean",
                                "description": "Enable FIFO (First-In-First-Out) ordering (default: true). Only set if user explicitly requests it.",
                                "default": True
                            },
                            "create_dlq": {
                                "type": "boolean",
                                "description": "Create a Dead Letter Queue (default: true). Only set if user explicitly requests it.",
                                "default": True
                            },
                            "max_receive_count": {
                                "type": "integer",
                                "description": "Number of times a message can be received before moving to DLQ (1-1000). Optional."
                            },
                            "visibility_timeout_seconds": {
                                "type": "integer",
                                "description": "Time in seconds a message is invisible after being received (0-43200). Optional."
                            },
                            "message_retention_seconds": {
                                "type": "integer",
                                "description": "How long messages are retained in the main queue in seconds (60-1209600). Optional."
                            },
                            "dlq_message_retention_seconds": {
                                "type": "integer",
                                "description": "How long messages are retained in the DLQ in seconds (60-1209600). Optional."
                            },
                            "cross_account_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of 12-digit AWS account IDs for cross-account access. Optional."
                            }
                        },
                        "required": ["identifier"]
                    }
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> list[TextContent]:
            logger.info(f"MCP call_tool: {name} with args: {arguments}")

            if name == "create_sqs_queue":
                return await self._call_create_sqs_queue(arguments)
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    async def _call_create_sqs_queue(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Create SQS queue — validate and format parameters."""
        try:
            tenant_code = arguments.get("tenant_code", "")
            tid = TenantId(tenant_code or "aspora")

            # Check missing AND validate provided params in a single pass
            missing_params = []
            invalid_params = []
            resolved_values = {}

            validators = [
                ("tenant_code", "tenant_code", "tenant_code", None),
                ("product_name", "product_name", "applications_mst_code", validate_product_name),
                ("environment", "environment", "environment_enum", validate_environment),
                ("geo_loc_code", "region", "geo_loc_mst_code", validate_geo_loc_code),
            ]

            for tool_key, display_name, meta_key, validator_fn in validators:
                if not arguments.get(tool_key):
                    options = resource_meta_repo.get_placement_options(tid, _SQS_INFRA_TYPE, meta_key)
                    missing_params.append({
                        "parameter": display_name,
                        "options": options,
                    })
                elif validator_fn:
                    try:
                        resolved_values[tool_key] = validator_fn(tid, arguments[tool_key])
                    except ValueError as ve:
                        invalid_params.append(json.loads(str(ve)))

            # Return all errors at once — missing and invalid are separate keys
            if missing_params or invalid_params:
                error_response = {"status": "error"}
                if missing_params and invalid_params:
                    error_response["error"] = "Some placement parameters are missing and others have invalid values."
                elif missing_params:
                    error_response["error"] = "Missing required placement parameters."
                else:
                    error_response["error"] = "Invalid placement parameter values."
                if missing_params:
                    error_response["missing_parameters"] = missing_params
                if invalid_params:
                    error_response["invalid_parameters"] = invalid_params

                # Include validated params collected so far (attribute + placement)
                # so response_handler can surface them to the frontend
                placement_keys = {k for k, _, _ in _USER_PLACEMENT_KEYS}
                collected_attribute_params = {
                    k: v for k, v in arguments.items()
                    if k not in placement_keys and v is not None and v != ""
                }
                collected_placement_params = {}
                if tenant_code:
                    collected_placement_params["tenant_code"] = tenant_code
                collected_placement_params.update(resolved_values)

                if collected_attribute_params:
                    error_response["attribute_parameters"] = collected_attribute_params
                if collected_placement_params:
                    error_response["placement_parameters"] = collected_placement_params

                return [TextContent(type="text", text=json.dumps(error_response))]

            resolved_product = resolved_values.get("product_name")
            resolved_env = resolved_values.get("environment")
            resolved_geo = resolved_values.get("geo_loc_code")

            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.sqs_creation_service import SqsCreationService

            async with AsyncSessionLocal() as db:
                service = SqsCreationService(db)
                result = await service.create_sqs_queue(
                    identifier=arguments["identifier"],
                    tenant_code=tenant_code,
                    product_name=resolved_product,
                    environment=Environment(resolved_env),
                    geo_loc_code=resolved_geo,
                    fifo_queue=arguments.get("fifo_queue", True),
                    create_dlq=arguments.get("create_dlq", True),
                    max_receive_count=arguments.get("max_receive_count"),
                    visibility_timeout_seconds=arguments.get("visibility_timeout_seconds"),
                    message_retention_seconds=arguments.get("message_retention_seconds"),
                    dlq_message_retention_seconds=arguments.get("dlq_message_retention_seconds"),
                    cross_account_ids=arguments.get("cross_account_ids"),
                )
                return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"create_sqs_queue failed: {e}", exc_info=True)
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
    """Entry point for SQS creation MCP server."""
    import asyncio
    server = SqsToolsMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
