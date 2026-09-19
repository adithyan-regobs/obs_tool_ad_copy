"""
MCP Server for S3 bucket creation tools.

S3 Bucket Creation Tools:
- ValidateParams: Validate and submit S3 parameters (replaces create_s3_bucket)
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

_S3_INFRA_TYPE = InfraTypeCode("s3_infrastructuretype_ref")


def _resolve_and_validate(tid: TenantId, param_name: str, meta_key: str, raw_value: str) -> str:
    """Resolve user input and validate against resource metadata options.

    Returns the resolved canonical value.
    Raises ValueError with valid options if the value is invalid.
    """
    resolved = resource_meta_repo.resolve_placement_value(tid, _S3_INFRA_TYPE, meta_key, raw_value)
    options = resource_meta_repo.get_placement_options(tid, _S3_INFRA_TYPE, meta_key)
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


def validate_infra_vendor(tid: TenantId, raw_value: str) -> str:
    """Validate infrastructure vendor against resource metadata options (e.g., aws)."""
    return _resolve_and_validate(tid, "infra_vendor", "infra_vendor_enum", raw_value)


# Placement params that require user input
# Keys: (tool_arg_key, user_facing_name, meta_key)
_USER_PLACEMENT_KEYS = [
    ("tenant_code", "tenant_code", "tenant_code"),
    ("product_name", "product_name", "applications_mst_code"),
    ("environment", "environment", "environment_enum"),
    ("geo_loc_code", "region", "geo_loc_mst_code"),
]

# Placement params auto-filled from resource metadata (single-option, not in tool schema)
_AUTO_PLACEMENT_KEYS = {
    "infra_vendor": "infra_vendor_enum",
}


class S3ToolsMCPServer:
    """MCP Server exposing S3 bucket creation tools."""

    def __init__(self):
        self.server = Server("s3-creation-tools")
        self._register_handlers()

    def _register_handlers(self):
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="create_s3_bucket",
                    description=(
                        "Create a new S3 bucket. "
                        "Required parameters = identifier(name). "
                        "S3 will be created appropriately."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "identifier": {
                                "type": "string",
                                "description": "S3 bucket name/identifier (e.g., 'app-logs', 'data-backup')"
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
                            "versioning": {
                                "type": "boolean",
                                "description": "Enable versioning (default: false). Only set if user explicitly requests it.",
                                "default": False
                            },
                            "enable_s3_replication": {
                                "type": "boolean",
                                "description": "Enable cross-account replication (default: false). Only set if user explicitly requests it.",
                                "default": False
                            },
                            "cross_account_account_id": {
                                "type": "string",
                                "description": "12-digit AWS account ID for cross-account access. Optional — can be set independently. REQUIRED when enable_s3_replication is true."
                            }
                        },
                        "required": ["identifier"]
                    }
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> list[TextContent]:
            logger.info(f"MCP call_tool: {name} with args: {arguments}")

            if name == "create_s3_bucket":
                return await self._call_create_s3_bucket(arguments)
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    async def _call_create_s3_bucket(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Create S3 bucket via service layer."""
        try:
            tenant_code = arguments.get("tenant_code", "")
            tid = TenantId(tenant_code or "aspora")

            # Auto-fill single-option placement params (e.g., infra_vendor = "aws")
            for tool_key, meta_key in _AUTO_PLACEMENT_KEYS.items():
                if not arguments.get(tool_key):
                    options = resource_meta_repo.get_placement_options(tid, _S3_INFRA_TYPE, meta_key)
                    if len(options) == 1:
                        arguments[tool_key] = options[0]["value"]

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
                    options = resource_meta_repo.get_placement_options(tid, _S3_INFRA_TYPE, meta_key)
                    missing_params.append({
                        "parameter": display_name,
                        "options": options,
                    })
                elif validator_fn:
                    try:
                        resolved_values[tool_key] = validator_fn(tid, arguments[tool_key])
                    except ValueError as ve:
                        invalid_params.append(json.loads(str(ve)))

            # Validate auto-filled params
            if arguments.get("infra_vendor"):
                try:
                    resolved_values["infra_vendor"] = validate_infra_vendor(tid, arguments["infra_vendor"])
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
                placement_keys = {k for k, _, _ in _USER_PLACEMENT_KEYS} | set(_AUTO_PLACEMENT_KEYS)
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

                logger.info(
                    f"[S3_TOOL_ERROR_PATH] arguments received from LLM: {arguments}"
                )
                logger.info(
                    f"[S3_TOOL_ERROR_PATH] resolved_values: {resolved_values}"
                )
                logger.info(
                    f"[S3_TOOL_ERROR_PATH] collected_placement_params: {collected_placement_params}"
                )
                logger.info(
                    f"[S3_TOOL_ERROR_PATH] error_response: {json.dumps(error_response)}"
                )

                return [TextContent(type="text", text=json.dumps(error_response))]

            resolved_product = resolved_values.get("product_name")
            resolved_env = resolved_values.get("environment")
            resolved_geo = resolved_values.get("geo_loc_code")
            resolved_vendor = resolved_values.get("infra_vendor")

            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.s3_creation_service import S3CreationService

            async with AsyncSessionLocal() as db:
                service = S3CreationService(db)
                result = await service.create_s3_bucket(
                    identifier=arguments["identifier"],
                    tenant_code=tenant_code,
                    product_name=resolved_product,
                    environment=Environment(resolved_env),
                    geo_loc_code=resolved_geo,
                    infra_vendor=resolved_vendor,
                    versioning=arguments.get("versioning", False),
                    enable_s3_replication=arguments.get("enable_s3_replication", False),
                    cross_account_account_id=arguments.get("cross_account_account_id"),
                )
                return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"create_s3_bucket failed: {e}", exc_info=True)
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
    """Entry point for S3 creation MCP server."""
    import asyncio
    server = S3ToolsMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
