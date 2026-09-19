"""
MCP Tool Provider - Singleton pattern.

Provides MCP tools for LLM binding via MultiServerMCPClient.
Initialized lazily on first use, kept alive for application lifetime.
"""
import asyncio
import os
import sys
import logging
from typing import List, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.infra_chat_agent.config.tools_enum import (
    ENVIRONMENT_VALUES,
    GEO_LOC_CODE_VALUES,
)

logger = logging.getLogger(__name__)

def _get_mcp_servers_config() -> dict:
    """
    Build MCP server configuration with current environment variables.

    Called lazily at initialization time to ensure env vars are captured
    after they're fully loaded (important for ECS/container environments).
    """
    return {
        "infra-chat-reference-tools": {
            "command": sys.executable,
            "args": ["-m", "app.infra_chat_agent.mcp_server.reference_tools_server"],
            "transport": "stdio",
            "env": os.environ.copy(),
        },
        "database-creation-tools": {
            "command": sys.executable,
            "args": ["-m", "app.infra_chat_agent.mcp_server.database_tools_server"],
            "transport": "stdio",
            "env": os.environ.copy(),
        },
        "s3-creation-tools": {
            "command": sys.executable,
            "args": ["-m", "app.infra_chat_agent.mcp_server.validator_server"],
            "transport": "stdio",
            "env": os.environ.copy(),
        },
        "dynamodb-creation-tools": {
            "command": sys.executable,
            "args": ["-m", "app.infra_chat_agent.mcp_server.dynamodb_tools_server"],
            "transport": "stdio",
            "env": os.environ.copy(),
        }
    }

# Module-level singleton state
_mcp_client: Optional[MultiServerMCPClient] = None
_tools: Optional[List] = None
_lock = asyncio.Lock()
_initialized = False


async def get_mcp_tools() -> List:
    """
    Get MCP tools (lazy singleton).

    First call initializes the MCP client and fetches tools.
    Subsequent calls return cached tools.

    Returns:
        List of LangChain-compatible tools from MCP server
    """
    global _mcp_client, _tools, _initialized

    if _initialized:
        return _tools

    async with _lock:
        # Double-check after acquiring lock
        if _initialized:
            return _tools

        logger.info("Initializing MCP client (singleton)...")
        mcp_servers = _get_mcp_servers_config()
        logger.info(f"MCP Servers: {list(mcp_servers.keys())}")
        _mcp_client = MultiServerMCPClient(mcp_servers)
        _tools = await _mcp_client.get_tools()
        _initialized = True

        # Log detailed tool information
        for tool in _tools:
            logger.info(f"Loaded tool: {tool.name}")
            if hasattr(tool, 'args_schema') and tool.args_schema:
                # args_schema can be a dict or a Pydantic model
                if hasattr(tool.args_schema, 'schema'):
                    # Pydantic model
                    schema = tool.args_schema.schema()
                    required = schema.get('required', [])
                elif isinstance(tool.args_schema, dict):
                    # Dict schema
                    required = tool.args_schema.get('required', [])
                else:
                    required = []
                logger.info(f"  Required params: {required}")

        logger.info(f"MCP tools loaded: {[t.name for t in _tools]}")

    return _tools


async def cleanup_mcp_client():
    """
    Cleanup MCP client on application shutdown.

    Call this during FastAPI shutdown event to properly close subprocess.
    """
    global _mcp_client, _tools, _initialized

    if _mcp_client is not None:
        logger.info("Cleaning up MCP client...")
        try:
            # MultiServerMCPClient should handle cleanup
            # If it has a close method, call it
            if hasattr(_mcp_client, "close"):
                await _mcp_client.close()
            elif hasattr(_mcp_client, "__aexit__"):
                await _mcp_client.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"Error during MCP client cleanup: {e}")

        _mcp_client = None
        _tools = None
        _initialized = False
        logger.info("MCP client cleaned up")


def clear_cache():
    """
    Clear the cached tools (for testing).

    Note: This doesn't close the subprocess. Use cleanup_mcp_client for proper cleanup.
    """
    global _tools, _initialized
    _tools = None
    _initialized = False
    logger.info("MCP tools cache cleared")


# =============================================================================
# Synchronous helpers (for intent detection prompt building)
# =============================================================================

# Tool name constants for filtering
REFERENCE_TOOL_NAMES = ["list_services", "show_service_config"]
DATABASE_TOOL_NAMES = [
    "list_database_servers",
    "create_database",
    "list_databases_for_server",
    "get_database_users_with_grants",
    "structure_mysql_database_user_grants",
    "structure_psql_database_user_grants",
    "finalize_database_user_creation",
]
S3_TOOL_NAMES = [
    "ValidateParams",
]
SQS_TOOL_NAMES = [
    "ValidateParams",
]
DYNAMODB_TOOL_NAMES = [
    "ValidateParams",
]
KONG_TOOL_NAMES = [
    "ValidateParams",
]
DATABASE_TOOL_NAMES = [
    "ValidateParams",
]
MASTER_DATA_TOOL_NAMES = [
    "ReadEnvMaster",
    "ListEnvMaster",
    "ReadProductMaster",
    "ListProductMaster",
    "ReadRegionMaster",
    "ListRegionMaster",
    "ReadServiceMaster",
    "ListServiceMaster",
    "ListDatabaseServers",
]

# Database creation subset (excludes user management tools)
DB_CREATE_TOOL_NAMES = ["list_database_servers", "create_database"]

# Resource type → tool names for create_flow_v2 node
CREATE_FLOW_V2_RESOURCE_TOOL_MAP = {
    "database_infrastructuretype_ref": set(DATABASE_TOOL_NAMES) | set(MASTER_DATA_TOOL_NAMES),
    "s3_infrastructuretype_ref": set(S3_TOOL_NAMES) | set(MASTER_DATA_TOOL_NAMES),
    "sqs_infrastructuretype_ref": set(SQS_TOOL_NAMES) | set(MASTER_DATA_TOOL_NAMES),
    "dynamodb_infrastructuretype_ref": set(DYNAMODB_TOOL_NAMES) | set(MASTER_DATA_TOOL_NAMES),
    "kong_gateway_infrastructuretype_ref": set(KONG_TOOL_NAMES) | set(MASTER_DATA_TOOL_NAMES),
}


def build_resource_tools_map(tools: List) -> dict:
    """
    Build a resource_type → filtered tools mapping for create_flow_v2_node.

    Args:
        tools: All MCP tools from get_mcp_tools()

    Returns:
        Dict mapping resource type string to list of filtered tool objects
    """
    return {
        resource_key: [t for t in tools if t.name in tool_names]
        for resource_key, tool_names in CREATE_FLOW_V2_RESOURCE_TOOL_MAP.items()
    }


def get_reference_mcp_tools_sync() -> List[dict]:
    """
    Get REFERENCE workflow tool definitions for prompt building.

    Returns tools: list_services, show_service_config

    Returns:
        List of REFERENCE tool definitions
    """
    return [
        {
            "name": "list_services",
            "description": "List all services with configurations for a tenant",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tenant_code": {"type": "string"},
                    "environment": {
                        "type": "string",
                        "enum": ENVIRONMENT_VALUES,
                        "description": "Deployment environment"
                    },
                    "geo_loc_code": {
                        "type": "string",
                        "enum": GEO_LOC_CODE_VALUES,
                        "description": "Geographic location"
                    },
                },
                "required": ["tenant_code", "environment", "geo_loc_code"]
            }
        },
        {
            "name": "show_service_config",
            "description": "Show configuration for a specific service",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "Name of the service"
                    },
                    "tenant_code": {"type": "string"},
                    "environment": {
                        "type": "string",
                        "enum": ENVIRONMENT_VALUES,
                        "description": "Deployment environment"
                    },
                    "geo_loc_code": {
                        "type": "string",
                        "enum": GEO_LOC_CODE_VALUES,
                        "description": "Geographic location"
                    },
                },
                "required": ["service_name", "tenant_code", "environment", "geo_loc_code"]
            }
        },
    ]


def get_database_mcp_tools_sync() -> List[dict]:
    """
    Get DATABASE/CREATE workflow tool definitions for prompt building.

    Returns tools for database creation and user management:
    - list_database_servers
    - create_database
    - list_databases_for_server
    - get_database_users_with_grants
    - structure_mysql_database_user_grants
    - structure_psql_database_user_grants
    - finalize_database_user_creation

    Returns:
        List of DATABASE tool definitions
    """
    return [
        {
            "name": "list_database_servers",
            "description": "List all available database servers for a tenant. Use this to show users what database servers are available before creating a database or managing database users.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tenant_code": {"type": "string"},
                    "environment": {"type": "string"},
                    "geo_loc_code": {"type": "string"},
                },
                "required": ["tenant_code", "environment", "geo_loc_code"]
            }
        },
        {
            "name": "create_database",
            "description": "Create a new database on an existing database server. Required parameters: database_name, db_server_name, tenant_code, product_name, environment, geo_loc_code. The database will be added to the appropriate server (MySQL or PostgreSQL).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "database_name": {"type": "string"},
                    "db_server_name": {"type": "string"},
                    "tenant_code": {"type": "string"},
                    "product_name": {"type": "string"},
                    "environment": {"type": "string", "enum": ["prod", "stage", "qa"]},
                    "geo_loc_code": {"type": "string"},
                },
                "required": ["database_name", "db_server_name", "tenant_code", "product_name", "environment", "geo_loc_code"]
            }
        },
        {
            "name": "list_databases_for_server",
            "description": "List all databases available on a specific database server. Use this after the user has selected a server to show what databases are available on that server. This also returns the server type (MySQL or PostgreSQL) which determines the grant structure. Used for database user management flow.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "server_name": {"type": "string"},
                    "tenant_code": {"type": "string"},
                    "product_name": {"type": "string"},
                    "environment": {"type": "string", "enum": ["prod", "stage", "qa"]},
                    "geo_loc_code": {"type": "string"},
                },
                "required": ["server_name", "tenant_code", "product_name", "environment", "geo_loc_code"]
            }
        },
        {
            "name": "get_database_users_with_grants",
            "description": "Get all database users with their grants across ALL servers. Use this AFTER the user has selected a server AND databases, AND provided a username. This tool checks if the user already exists in ANY server and returns their existing grants. Returns: users list with username, password, database_type, server, grants details.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "server_name": {"type": "string", "description": "The selected database server name"},
                    "tenant_code": {"type": "string"},
                    "product_name": {"type": "string"},
                    "environment": {"type": "string", "enum": ["prod", "stage", "qa"]},
                    "geo_loc_code": {"type": "string"},
                },
                "required": ["server_name", "tenant_code", "product_name", "environment", "geo_loc_code"]
            }
        },
        {
            "name": "structure_mysql_database_user_grants",
            "description": "Structure MySQL database user grants. This tool validates that the databases provided by the user exist on the selected MySQL server. If a database is not found on this server, an error will be returned. This tool returns a 'structured_grants' object that you MUST pass to finalize_database_user_creation. Required: server_name, username, password, available_databases, mysql_databases",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "server_name": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "available_databases": {"type": "array", "items": {"type": "string"}},
                    "mysql_databases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "database": {"type": "string"},
                                "tables": {"type": "string"},
                                "privileges": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["database", "privileges"]
                        }
                    },
                },
                "required": ["server_name", "username", "password", "available_databases", "mysql_databases"]
            }
        },
        {
            "name": "structure_psql_database_user_grants",
            "description": "Structure PostgreSQL database user grants. Validates database exists on server. Three optional grant levels - user can grant at any level independently: Database: CONNECT, CREATE. Schema: USAGE, CREATE (object_type='schema'). Table: SELECT, INSERT, UPDATE, DELETE (object_type='table'). Only 'public' schema supported. Ask user which grant levels and privileges they want. Backend does not auto-add grants. This tool returns a 'structured_grants' object to pass to finalize_database_user_creation. Required: server_name, username, password, available_databases, pg_database. Optional: pg_schema_grants",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "server_name": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "available_databases": {"type": "array", "items": {"type": "string"}},
                    "pg_database": {"type": "string"},
                    "pg_schema_grants": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "schema": {"type": "string"},
                                "object_type": {"type": "string"},
                                "privileges": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["schema", "object_type", "privileges"]
                        }
                    },
                },
                "required": ["server_name", "username", "password", "available_databases", "pg_database"]
            }
        },
        {
            "name": "finalize_database_user_creation",
            "description": "Finalize database user creation. IMPORTANT: You MUST call structure_mysql_database_user_grants or structure_psql_database_user_grants FIRST. Then use the 'structured_grants' field from that tool's result as the 'structured_grants' parameter for this tool. Do NOT skip the structure step - the structured_grants parameter is required and must come from the previous tool result. This tool combines all the structured data and sets is_ready=true to trigger backend processing.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "server_name": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "db_type": {"type": "string", "enum": ["mysql", "postgresql"]},
                    "structured_grants": {"type": "object"},
                },
                "required": ["server_name", "username", "password", "db_type", "structured_grants"]
            }
        },
    ]


def get_s3_mcp_tools_sync() -> List[dict]:
    """
    Get S3/CREATE workflow tool definitions for prompt building.

    Returns tools for S3 bucket creation:
    - list_s3_buckets
    - create_s3_bucket

    Returns:
        List of S3 tool definitions
    """
    return [
        # {
        #     "name": "list_s3_buckets",
        #     "description": "List existing S3 buckets for a tenant and environment. Use this to show users what S3 buckets already exist before creating a new one.",
        #     "inputSchema": {
        #         "type": "object",
        #         "properties": {
        #             "tenant_code": {"type": "string"},
        #             "product_name": {"type": "string"},
        #             "environment": {"type": "string", "enum": ["prod", "stage", "qa"]},
        #             "geo_loc_code": {"type": "string"},
        #         },
        #         "required": ["tenant_code", "product_name", "environment", "geo_loc_code"]
        #     }
        # },
        {
            "name": "create_s3_bucket",
            "description": "Collect and validate parameters for S3 bucket creation. This does NOT create the bucket — it only validates and formats the parameters. All parameters can be changed by the user until they confirm. The only user-provided attribute is the bucket name (identifier). All other attributes have sensible defaults — do NOT ask the user for versioning or replication unless they mention it. Defaults: versioning=false, enable_s3_replication=false. cross_account_account_id (12-digit AWS account ID) is optional and can be provided independently. It becomes REQUIRED only when enable_s3_replication=true. Disabling replication does NOT auto-remove cross_account_account_id. User can remove it only when replication is false. Placement parameters (tenant_code, product_name, environment, geo_loc_code) come from context.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "S3 bucket name/identifier"},
                    "tenant_code": {"type": "string"},
                    "product_name": {"type": "string"},
                    "environment": {"type": "string", "enum": ["prod", "stage", "qa"]},
                    "geo_loc_code": {"type": "string"},
                    "versioning": {"type": "boolean", "description": "Enable versioning (default: false). Only set if user explicitly requests it."},
                    "enable_s3_replication": {"type": "boolean", "description": "Enable cross-account replication (default: false). Only set if user explicitly requests it."},
                    "cross_account_account_id": {"type": "string", "description": "12-digit AWS account ID for cross-account access. Optional — can be set independently. REQUIRED when enable_s3_replication is true."},
                },
                "required": ["identifier", "tenant_code", "product_name", "environment", "geo_loc_code"]
            }
        },
    ]


def get_sqs_mcp_tools_sync() -> List[dict]:
    """
    Get SQS/CREATE workflow tool definitions for prompt building.

    Returns tools for SQS queue creation:
    - ValidateParams (tool_name=CreateSQS)

    Returns:
        List of SQS tool definitions
    """
    return [
        {
            "name": "ValidateParams",
            "description": "Validate and submit parameters for SQS queue creation (tool_name=CreateSQS). Strict SQS keys only: name, product, region, environment, fifo, dlq, max_receive_count, visibility_timeout_seconds, main_queue_retention_seconds, dlq_retention_seconds, cross_account_ids. Defaults: fifo=true, dlq=true. Legacy aliases are not supported.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "enum": ["CreateSQS"],
                        "description": "Target validator model for SQS create flow",
                    },
                    "params": {
                        "type": "string",
                        "description": (
                            "JSON string of strict SQS params, e.g. "
                            '\'{"name":"orders","product":"obs","region":"mumbai","environment":"qa","fifo":true,'
                            '"dlq":true,"max_receive_count":5,"visibility_timeout_seconds":30,'
                            '"main_queue_retention_seconds":345600,"dlq_retention_seconds":345600,'
                            '"cross_account_ids":["123456789012"]}\''
                        ),
                    },
                    "user_message": {"type": "string"},
                    "tenant_id": {"type": "string"},
                    "conversation_state": {"type": "object"},
                    "thread_id": {"type": "string"},
                },
                "required": ["tool_name", "params", "user_message", "tenant_id", "conversation_state"]
            }
        },
    ]


def get_dynamodb_mcp_tools_sync() -> List[dict]:
    """
    Get DynamoDB/CREATE workflow tool definitions for prompt building.

    Returns tools for DynamoDB table creation:
    - ValidateParams (tool_name=CreateDynamoDB)

    Returns:
        List of DynamoDB tool definitions
    """
    return [
        {
            "name": "ValidateParams",
            "description": "Validate and submit parameters for DynamoDB table creation (tool_name=CreateDynamoDB). Strict DynamoDB keys only: identifier, partition_key, partition_key_type, product, region, environment. partition_key_type accepts S|N|B. Placement values are resolved by validator flow.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "enum": ["CreateDynamoDB"],
                        "description": "Target validator model for DynamoDB create flow",
                    },
                    "params": {
                        "type": "string",
                        "description": (
                            "JSON string of strict DynamoDB params, e.g. "
                            '\'{"identifier":"users","partition_key":"user_id","partition_key_type":"S",'
                            '"product":"core","region":"mumbai","environment":"qa"}\''
                        ),
                    },
                    "user_message": {"type": "string"},
                    "tenant_id": {"type": "string"},
                    "conversation_state": {"type": "object"},
                    "thread_id": {"type": "string"},
                },
                "required": ["tool_name", "params", "user_message", "tenant_id", "conversation_state"]
            }
        },
    ]


def get_parameter_options(tool_name: str, include_text_params: bool = False) -> dict:
    """
    Get parameter options for frontend rendering.

    Args:
        tool_name: Name of the tool (e.g., "list_services", "show_service_config")
        include_text_params: If True, include non-enum params as text inputs

    Returns:
        Dict of parameter key -> {name, required, type, order, value_source}
    """
    # Select the appropriate tool list based on tool name
    if tool_name in REFERENCE_TOOL_NAMES:
        tools = get_reference_mcp_tools_sync()
    elif tool_name in DATABASE_TOOL_NAMES:
        tools = get_database_mcp_tools_sync()
    elif tool_name in S3_TOOL_NAMES:
        tools = get_s3_mcp_tools_sync()
    elif tool_name in SQS_TOOL_NAMES:
        tools = get_sqs_mcp_tools_sync()
    elif tool_name in DYNAMODB_TOOL_NAMES:
        tools = get_dynamodb_mcp_tools_sync()
    else:
        # Fallback: search all lists
        tools = get_reference_mcp_tools_sync() + get_database_mcp_tools_sync() + get_s3_mcp_tools_sync() + get_sqs_mcp_tools_sync() + get_dynamodb_mcp_tools_sync()
    for tool in tools:
        if tool["name"] == tool_name:
            schema = tool.get("inputSchema", {})
            properties = schema.get("properties", {})
            required_params = schema.get("required", [])

            result = {}
            order = 0
            for param_key, props in properties.items():
                # Skip tenant_code - it's auto-filled
                if param_key == "tenant_code":
                    continue

                enum_values = props.get("enum")
                if enum_values:
                    # Dropdown parameter
                    result[param_key] = {
                        "name": props.get("description", param_key.replace("_", " ").title()),
                        "required": param_key in required_params,
                        "type": props.get("type", "string"),
                        "order": order,
                        "value_source": {
                            "type": "static",
                            "options": [
                                {"label": val.replace("_", " ").title(), "value": val}
                                for val in enum_values
                            ]
                        }
                    }
                    order += 1
                elif include_text_params:
                    # Text input parameter (no predefined options)
                    result[param_key] = {
                        "name": props.get("description", param_key.replace("_", " ").title()),
                        "required": param_key in required_params,
                        "type": props.get("type", "string"),
                        "order": order,
                        "value_source": {
                            "type": "text"
                        }
                    }
                    order += 1
            return result
    return {}


def build_parameter_options_json_example() -> str:
    """
    Build a JSON example string for the LLM prompt.

    Dynamically generates the example from the tool schema so it stays
    in sync with the actual enum values. Uses show_service_config to include
    both dropdown (environment, geo_loc_code) and text (service_name) params.

    Returns:
        JSON string formatted for inclusion in LLM prompt
    """
    import json
    # Use show_service_config to get ALL param types (text + dropdown)
    options = get_parameter_options("show_service_config", include_text_params=True)
    return json.dumps(options, indent=2)


def format_tools_for_prompt(tools: List[dict]) -> str:
    """
    Format tool definitions for inclusion in LLM system prompt.

    Args:
        tools: List of tool definitions

    Returns:
        Formatted string for prompt inclusion
    """
    lines = []
    for tool in tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "")
        schema = tool.get("inputSchema", {})
        required = schema.get("required", [])

        if required:
            lines.append(f"- {name}: {desc} (requires: {', '.join(required)})")
        else:
            lines.append(f"- {name}: {desc}")

    return "\n".join(lines)
