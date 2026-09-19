import json
import logging
import sys
from typing import Any, Dict
from pydantic import BaseModel
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from app.infra_chat_agent.mcp_server.models import (
    TENANT_MODELS,
    _build_tools_summary,
    build_all_tenants_tools_summary,
)
from app.infra_chat_agent.config.master_data_config import MasterData, build_master_data_summary
from app.infra_chat_agent.tools.master_data_tools.env_master_tool import (
    read_environment,
    list_environments,
)
from app.infra_chat_agent.tools.master_data_tools.product_master_tool import (
    read_product,
    list_products,
)
from app.infra_chat_agent.tools.master_data_tools.region_master_tool import (
    read_region,
    list_regions,
)
from app.infra_chat_agent.tools.service_tools import (
    read_service,
    list_services,
)
from app.infra_chat_agent.tools.database_tools import list_database_servers
from app.infra_chat_agent.mcp_server.validation import (
    _normalize_conversation_state,
    validate_params_structured,
)
from app.infra_chat_agent.executors import EXECUTORS

logger = logging.getLogger(__name__)


def get_validate_params_tool_info(tenant_id: str | None = None) -> tuple[str, str]:
    """Return (tool_name, tool_description) for ValidateParams."""
    if tenant_id:
        if tenant_id not in TENANT_MODELS:
            raise ValueError(f"Unknown tenant '{tenant_id}'. Available: {list(TENANT_MODELS.keys())}")
        tools_summary = _build_tools_summary(TENANT_MODELS[tenant_id])
        tenant_hint = f"Tenant scope: '{tenant_id}'."
    else:
        tools_summary = build_all_tenants_tools_summary()
        tenant_hint = (
            "tenant_id is required on each tool call and is injected by orchestration. "
            f"Supported tenants: {list(TENANT_MODELS.keys())}."
        )

    description = (
        "ALWAYS call this tool when a user wants to create any infrastructure resource. "
        "Do NOT respond to the user without first calling this tool.\n\n"
        "Validates and accumulates parameters across conversation turns. "
        "Auto-executes creation when all required parameters are collected and valid.\n\n"
        f"{tenant_hint}\n\n"
        "ALWAYS call with:\n"
        "- tool_name: the target resource (e.g. 'CreateKongRoute', 'CreateS3', 'CreateSQS')\n"
        "- params: JSON string of ALL parameter values from the current message (use '{}' if none)\n"
        "- user_message: the user's exact message copied verbatim\n\n"
        f"Available tools and parameters:\n{tools_summary}"
    )
    return "ValidateParams", description

class ValidatorMCPServer:
    """MCP Server exposing the ValidateParams tool for infrastructure creation."""

    def __init__(self):
        self.server = Server("InfraTools")
        self._register_handlers()

    def _register_handlers(self):
        """Register MCP protocol handlers."""
        tools_summary = build_all_tenants_tools_summary()
        master_data_summary = build_master_data_summary("aspora")
        description = f"""ALWAYS call this tool when a user wants to create any infrastructure resource. Do NOT respond to the user without first calling this tool.

Validates and accumulates parameters across conversation turns. Auto-executes creation when all required parameters are collected and valid.

ALWAYS call with:
- tool_name: the target resource (e.g. 'CreateKongRoute', 'CreateS3', 'CreateSQS')
- params: JSON string of ALL parameter values from the current message (use '{{}}' if none)
- user_message: the user's exact message copied verbatim

Available tools and parameters:
{tools_summary}

{master_data_summary}

IMPORTANT: Use the master data above to fuzzy-match user input to correct values.
If the input is a close match (e.g. "londn" → "london", "falcn" → "falcon","goms" → "goms-service"), use the corrected value.
If no close match exists, show available options from the master data and ask the user to choose.

IMPORTANT: Before passing params, verify value combinations exist in the MasterData above.
Previously accepted values (from earlier turns) have priority — new values must be compatible.
If a combination is invalid, exclude ONLY the failed params but still pass all other valid params.
Show the user what failed and what options are available."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="ValidateParams",
                    description=description,
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "description": "Target tool name (e.g. 'CreateS3', 'CreateSQS', 'CreateDynamoDB','CreateKongRoute')",
                            },
                            "params": {
                                "type": "string",
                                "description": (
                                    "JSON of matched param key-value pairs from current message. "
                                    'Example: \'{"name": "mybucket", "region": "us"}\''
                                ),
                            },
                            "user_message": {
                                "type": "string",
                                "description": "The user's raw message exactly as typed",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier (for model selection and placement resolution). "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                            "conversation_state": {
                                "type": "object",
                                "description": (
                                    "Caller-managed state from the previous ValidateParams response. "
                                    "Shape: {'tool_name': str|null, 'valid': object}."
                                ),
                            },
                            "thread_id": {
                                "type": "string",
                                "description": "Optional conversation thread ID for observability/logging",
                            },
                        },
                        "required": ["tool_name", "params", "user_message", "tenant_id", "conversation_state"],
                    },
                ),
                Tool(
                    name="ReadEnvMaster",
                    description=(
                        "Get all product+region combinations where a specific environment is available "
                        "for the given tenant. Use this to check whether a particular environment "
                        "(e.g. 'dev', 'qa', 'prod') exists and which product+region pairs support it."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "environment": {
                                "type": "string",
                                "description": "Environment name to look up (e.g. 'dev', 'qa', 'prod')",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["environment", "tenant_id"],
                    },
                ),
                Tool(
                    name="ListEnvMaster",
                    description=(
                        "List all available environments for the given tenant, optionally filtered "
                        "by product and/or region. Returns the set of environment names."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "product": {
                                "type": "string",
                                "description": "Filter by product name (optional)",
                            },
                            "region": {
                                "type": "string",
                                "description": "Filter by region name (optional)",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["tenant_id"],
                    },
                ),
                Tool(
                    name="ReadProductMaster",
                    description=(
                        "Get full availability details for a specific product across all regions "
                        "for the given tenant. Returns which regions and environments support this product."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "product": {
                                "type": "string",
                                "description": "Product name to look up (e.g. 'core', 'falcon')",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["product", "tenant_id"],
                    },
                ),
                Tool(
                    name="ListProductMaster",
                    description=(
                        "List all available products for the given tenant, optionally filtered "
                        "by region and/or environment. Returns the set of product names."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "region": {
                                "type": "string",
                                "description": "Filter by region name (optional)",
                            },
                            "environment": {
                                "type": "string",
                                "description": "Filter by environment name (optional)",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["tenant_id"],
                    },
                ),
                Tool(
                    name="ReadRegionMaster",
                    description=(
                        "Get full availability details for a specific region across all products "
                        "for the given tenant. Returns which products and environments are available in this region."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "region": {
                                "type": "string",
                                "description": "Region name to look up (e.g. 'london', 'mumbai')",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["region", "tenant_id"],
                    },
                ),
                Tool(
                    name="ListRegionMaster",
                    description=(
                        "List all available regions for the given tenant, optionally filtered "
                        "by product and/or environment. Returns the set of region names."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "product": {
                                "type": "string",
                                "description": "Filter by product name (optional)",
                            },
                            "environment": {
                                "type": "string",
                                "description": "Filter by environment name (optional)",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["tenant_id"],
                    },
                ),
                Tool(
                    name="ReadServiceMaster",
                    description=(
                        "Get all region+product+environment combinations where a specific service "
                        "is deployed for the given tenant. Use this to check where a particular service "
                        "(e.g. 'payment-service', 'auth-service') is available."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "service": {
                                "type": "string",
                                "description": "Service name to look up (e.g. 'payment-service', 'auth-service')",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["service", "tenant_id"],
                    },
                ),
                Tool(
                    name="ListServiceMaster",
                    description=(
                        "List all available services for the given tenant, optionally filtered "
                        "by region, product, and/or environment. Returns the set of service names."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "region": {
                                "type": "string",
                                "description": "Filter by region name (optional)",
                            },
                            "product": {
                                "type": "string",
                                "description": "Filter by product name (optional)",
                            },
                            "environment": {
                                "type": "string",
                                "description": "Filter by environment name (optional)",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["tenant_id"],
                    },
                ),
                Tool(
                    name="ListDatabaseServers",
                    description=(
                        "List all available database servers for the given tenant, filtered "
                        "by product, environment, and geo location. Returns server names and types "
                        "from the service layer (GitHub API)."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "product_name": {
                                "type": "string",
                                "description": "Product name (e.g. 'Core', 'Falcon')",
                            },
                            "environment": {
                                "type": "string",
                                "description": "Environment (e.g. 'dev', 'qa', 'stage', 'prod')",
                            },
                            "geo_loc_code": {
                                "type": "string",
                                "description": "Geographic location code (e.g. 'region-aspora-mumbai', 'london', 'mumbai')",
                            },
                            "tenant_id": {
                                "type": "string",
                                "description": (
                                    "Tenant identifier. "
                                    "Injected automatically by orchestration from authenticated context."
                                ),
                            },
                        },
                        "required": ["tenant_id", "product_name", "environment", "geo_loc_code"],
                    },
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> list[TextContent]:
            logger.info(f"MCP call_tool: {name} with args: {arguments}")

            if name == "ValidateParams":
                return await self._call_validate_params(arguments)
            elif name == "ReadEnvMaster":
                return await self._call_read_env_master(arguments)
            elif name == "ListEnvMaster":
                return await self._call_list_env_master(arguments)
            elif name == "ReadProductMaster":
                return await self._call_read_product_master(arguments)
            elif name == "ListProductMaster":
                return await self._call_list_product_master(arguments)
            elif name == "ReadRegionMaster":
                return await self._call_read_region_master(arguments)
            elif name == "ListRegionMaster":
                return await self._call_list_region_master(arguments)
            elif name == "ReadServiceMaster":
                return await self._call_read_service_master(arguments)
            elif name == "ListServiceMaster":
                return await self._call_list_service_master(arguments)
            elif name == "ListDatabaseServers":
                return await self._call_list_database_server_master(arguments)
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    async def _call_validate_params(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ValidateParams tool call."""
        try:
            params_str = arguments.get("params", "{}")
            try:
                if isinstance(params_str, dict):
                    params_dict = params_str
                elif isinstance(params_str, str):
                    params_dict = json.loads(params_str)
                else:
                    return [TextContent(type="text", text=json.dumps({
                        "error": "Invalid params type. Expected JSON string or object."
                    }))]
            except json.JSONDecodeError as e:
                return [TextContent(type="text", text=json.dumps({"error": f"Invalid JSON in params: {e}"}))]

            print("Received ValidateParams call with params: {params_dict}")
            incoming_state = _normalize_conversation_state(arguments.get("conversation_state"))
            logger.info(
                "[VALIDATE_PARAMS] _call_validate_params received conversation_state: "
                f"tool_name={incoming_state.get('tool_name')!r}, "
                f"valid_count={len(incoming_state.get('valid', {}))}"
            )

            tenant_id_raw = arguments.get("tenant_id")
            if not isinstance(tenant_id_raw, str) or not tenant_id_raw.strip():
                return [TextContent(type="text", text=json.dumps({
                    "error": (
                        "Missing required 'tenant_id'. "
                        f"Supported tenants: {list(TENANT_MODELS.keys())}"
                    )
                }))]
            tenant_id = tenant_id_raw.strip()
            tenant_tools = TENANT_MODELS.get(tenant_id)
            if tenant_tools is None:
                return [TextContent(type="text", text=json.dumps({
                    "error": (
                        f"Unknown tenant_id '{tenant_id}'. "
                        f"Supported tenants: {list(TENANT_MODELS.keys())}"
                    )
                }))]
            logger.info(
                "[VALIDATE_PARAMS] Resolved tenant for request: "
                f"tenant_id={tenant_id!r}, tools={list(tenant_tools.keys())}"
            )
            tool_name = arguments.get("tool_name", "")
            if not isinstance(tool_name, str) or not tool_name.strip():
                return [TextContent(type="text", text=json.dumps({
                    "error": (
                        "Missing required 'tool_name'. "
                        f"Available tools for tenant '{tenant_id}': {list(tenant_tools.keys())}"
                    )
                }))]
            tool_name = tool_name.strip()
            tool_model = tenant_tools.get(tool_name)
            if tool_model is None:
                return [TextContent(type="text", text=json.dumps({
                    "error": (
                        f"Unknown tool_name '{tool_name}' for tenant '{tenant_id}'. "
                        f"Available: {list(tenant_tools.keys())}"
                    )
                }))]
            selected_tool_summary = build_all_tenants_tools_summary(
                tenant_id=tenant_id,
                resource_type=tool_name,
            )

            result, next_state = await validate_params_structured(
                tool_name,
                params_dict,
                arguments.get("user_message", ""),
                {tool_name: tool_model},
                incoming_state,
                tenant_id=tenant_id,
                thread_id=arguments.get("thread_id", ""),
            )

            # Execute if validation says all params are ready
            if result.get("ready"):
                validated = tool_model(
                    **{p["param"]: p["value"] for p in result["valid"] if not p.get("default")}
                )
                executor = EXECUTORS.get(tool_name)
                if executor:
                    exec_result = await executor(validated, tenant_id)
                    exec_result["conversation_state"] = next_state
                    exec_result["tool_summary"] = selected_tool_summary
                    logger.info(
                        "[VALIDATE_PARAMS] Auto-executed successfully: "
                        f"tool_name={tool_name!r}, tenant_id={tenant_id!r}, "
                        f"valid_count={len(next_state.get('valid', {}))}"
                    )
                    return [TextContent(type="text", text=json.dumps(exec_result, default=str))]

            # Always return next conversation state so caller can persist it.
            result["tool_summary"] = selected_tool_summary
            result["conversation_state"] = next_state
            logger.info(
                "[VALIDATE_PARAMS] Returning conversation_state: "
                f"tool_name={next_state.get('tool_name')!r}, "
                f"valid_count={len(next_state.get('valid', {}))}"
            )
            return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"ValidateParams failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"status": "error", "error": str(e)}))]

    async def _call_read_env_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ReadEnvMaster tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            environment = arguments.get("environment", "").strip()
            if not environment:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'environment'."
                }))]

            result = read_environment(tenant_id, environment)
            return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"ReadEnvMaster failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_list_env_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ListEnvMaster tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            product = arguments.get("product")
            region = arguments.get("region")
            if isinstance(product, str):
                product = product.strip() or None
            if isinstance(region, str):
                region = region.strip() or None

            result = list_environments(tenant_id, product, region)
            return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"ListEnvMaster failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_read_product_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ReadProductMaster tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            product = arguments.get("product", "").strip()
            if not product:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'product'."
                }))]

            result = read_product(tenant_id, product)
            return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"ReadProductMaster failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_list_product_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ListProductMaster tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            region = arguments.get("region")
            environment = arguments.get("environment")
            if isinstance(region, str):
                region = region.strip() or None
            if isinstance(environment, str):
                environment = environment.strip() or None

            result = list_products(tenant_id, region, environment)
            return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"ListProductMaster failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_read_region_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ReadRegionMaster tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            region = arguments.get("region", "").strip()
            if not region:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'region'."
                }))]

            result = read_region(tenant_id, region)
            return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"ReadRegionMaster failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_list_region_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ListRegionMaster tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            product = arguments.get("product")
            environment = arguments.get("environment")
            if isinstance(product, str):
                product = product.strip() or None
            if isinstance(environment, str):
                environment = environment.strip() or None

            result = list_regions(tenant_id, product, environment)
            return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"ListRegionMaster failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_read_service_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ReadServiceMaster tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            service = arguments.get("service", "").strip()
            if not service:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'service'."
                }))]

            result = read_service(tenant_id, service)
            return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"ReadServiceMaster failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_list_service_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ListServiceMaster tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            region = arguments.get("region")
            product = arguments.get("product")
            environment = arguments.get("environment")
            if isinstance(region, str):
                region = region.strip() or None
            if isinstance(product, str):
                product = product.strip() or None
            if isinstance(environment, str):
                environment = environment.strip() or None

            result = list_services(tenant_id, region, product, environment)
            return [TextContent(type="text", text=json.dumps(result))]

        except Exception as e:
            logger.error(f"ListServiceMaster failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def _call_list_database_server_master(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Handle ListDatabaseServers tool call."""
        try:
            tenant_id = arguments.get("tenant_id", "").strip()
            if not tenant_id:
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required 'tenant_id'."
                }))]

            product_name = arguments.get("product_name", "").strip()
            environment = arguments.get("environment", "").strip()
            geo_loc_code = arguments.get("geo_loc_code", "").strip()

            if not all([product_name, environment, geo_loc_code]):
                return [TextContent(type="text", text=json.dumps({
                    "error": "Missing required parameters: product_name, environment, geo_loc_code."
                }))]

            result_str = await list_database_servers({
                "tenant_code": tenant_id,
                "product_name": product_name,
                "environment": environment,
                "geo_loc_code": geo_loc_code,
            })
            return [TextContent(type="text", text=result_str)]

        except Exception as e:
            logger.error(f"ListDatabaseServers failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    async def run(self):
        """Run the MCP server via stdio."""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )


def create_server(_tenant_id: str | None = None) -> ValidatorMCPServer:
    """Create an MCP server for ValidateParams (tenant resolved per request)."""
    return ValidatorMCPServer()


def _setup_file_logging() -> None:
    """
    Configure file-based logging for the MCP subprocess.

    The MCP server runs as a stdio subprocess — its stdout/stderr are consumed
    by the MCP transport and never reach the main uvicorn process.  We write
    logs to a dedicated file so they are visible for debugging.
    """
    import logging.handlers
    import os
    from pathlib import Path

    log_dir = Path(os.environ.get("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "mcp_validator.log"

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


def main():
    """Entry point for validator MCP server."""
    import asyncio

    _setup_file_logging()

    if len(sys.argv) > 1:
        logger.info(
            "[VALIDATE_PARAMS] Startup tenant argument is ignored; "
            "tenant_id is resolved dynamically per tool call."
        )
    server = ValidatorMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
