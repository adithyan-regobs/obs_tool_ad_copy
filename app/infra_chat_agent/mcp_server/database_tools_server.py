"""
MCP Server for database creation and user management tools.

Database Creation Tools:
- list_database_servers: List available database servers for a tenant
- create_database: Create a new database on an existing database server

Database User Management Tools:
- list_database_servers: List available database servers for a tenant
- list_databases_for_server: List databases available on a specific server
- get_database_users_with_grants: Get all database users with their grants across all servers
- structure_mysql_database_user_grants: Capture and structure MySQL user grants (is_ready=false)
- structure_psql_database_user_grants: Capture and structure PostgreSQL user grants (is_ready=false)
- finalize_database_user_creation: Finalize and set is_ready=true
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from typing import Any, Dict
import json
import logging

from app.core.enum import (
    MysqlPrivilegeEnum,
    PgDatabasePrivilegeEnum,
    PgSchemaPrivilegeEnum,
    PgTablePrivilegeEnum,
)
from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_environment, normalize_geo_loc_code

logger = logging.getLogger(__name__)

MYSQL_PRIVILEGE_ENUM = [p.value for p in MysqlPrivilegeEnum]
PG_DATABASE_PRIVILEGE_ENUM = [p.value for p in PgDatabasePrivilegeEnum]
PG_SCHEMA_PRIVILEGE_ENUM = [p.value for p in PgSchemaPrivilegeEnum]
PG_TABLE_PRIVILEGE_ENUM = [p.value for p in PgTablePrivilegeEnum]
PG_SCHEMA_GRANT_PRIVILEGE_ENUM = list(dict.fromkeys(PG_SCHEMA_PRIVILEGE_ENUM + PG_TABLE_PRIVILEGE_ENUM))


class DatabaseToolsMCPServer:
    """MCP Server exposing database creation tools."""

    def __init__(self):
        self.server = Server("database-creation-tools")
        self._register_handlers()

    def _register_handlers(self):
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="list_database_servers",
                    description="List all available database servers for a tenant. Use this to show users what database servers are available before creating a database or managing database users.",
                    inputSchema={
                        "type": "object",
                        "properties": {
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
                        "required": ["tenant_code", "product_name", "environment", "geo_loc_code"]
                    }
                ),
                Tool(
                    name="create_database",
                    description="Create a new database on an existing database server. "
                    "Required parameters: database_name, db_server_name, tenant_code, product_name, environment, geo_loc_code. "
                    "The database will be added to the appropriate server (MySQL or PostgreSQL).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "database_name": {
                                "type": "string",
                                "description": "Name of the database to create"
                            },
                            "db_server_name": {
                                "type": "string",
                                "description": "Name of the database server (e.g., 'common-mysql', 'common-pg')"
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
                        "required": ["database_name", "db_server_name", "tenant_code", "product_name", "environment", "geo_loc_code"]
                    }
                ),
                Tool(
                    name="list_databases_for_server",
                    description="List all databases available on a specific database server. "
                    "Use this after the user has selected a server to show what databases are available on that server. "
                    "This also returns the server type (MySQL or PostgreSQL) which determines the grant structure. "
                    "Used for database user management flow.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "server_name": {
                                "type": "string",
                                "description": "Name of the database server (e.g., 'common-mysql', 'common-pg')"
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
                        "required": ["server_name", "tenant_code", "product_name", "environment", "geo_loc_code"]
                    }
                ),
                Tool(
                    name="structure_mysql_database_user_grants",
                    description="Structure MySQL database user grants. "
                    "This tool validates that the databases provided by the user exist on the selected MySQL server. "
                    "If a database is not found on this server, an error will be returned. "
                    ""
                    "This tool returns a 'structured_grants' object that you MUST pass to finalize_database_user_creation."
                    "Example mysql_databases: [{'database': 'app_db', 'tables': '*', 'privileges': ['SELECT', 'INSERT', 'UPDATE']}]"
                    ""
                    "Required: server_name, username, password, available_databases, mysql_databases",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "server_name": {
                                "type": "string",
                                "description": "Name of the MySQL database server (e.g., 'common-mysql')"
                            },
                            "username": {
                                "type": "string",
                                "description": "Username for the new database user"
                            },
                            "password": {
                                "type": "string",
                                "description": "Password for the new database user"
                            },
                            "available_databases": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of databases available on this server (from list_databases_for_server result)"
                            },
                            "mysql_databases": {
                                "type": "array",
                                "description": "MySQL grants - list of database grants",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "database": {
                                            "type": "string",
                                            "description": "Database name"
                                        },
                                        "tables": {
                                            "type": "string",
                                            "description": "Table pattern (e.g., '*', 'users', 'orders')"
                                        },
                                        "privileges": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "Privileges (e.g., ['SELECT', 'INSERT', 'UPDATE', 'DELETE'])"
                                        }
                                    },
                                    "required": ["database", "privileges"]
                                }
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
                        "required": ["server_name", "username", "password", "available_databases", "mysql_databases"]
                    }
                ),
                Tool(
                    name="structure_psql_database_user_grants",
                    description="Structure PostgreSQL database user grants. "
                    "Validates databases exist on server. "
                    ""
                    "Each database can have three optional grant levels: "
                    "- Database-level: CONNECT, CREATE (set database_permissions in pg_databases) "
                    "- Schema-level: USAGE, CREATE (use object_type='schema' in schema_grants) "
                    "- Table-level: SELECT, INSERT, UPDATE, DELETE (use object_type='table' in schema_grants) "
                    "Only 'public' schema supported. "
                    ""
                    "Example pg_databases for single database with all levels: "
                    "[{\"database\":\"recon_db\",\"database_permissions\":[\"CONNECT\"], "
                    "\"schema_grants\":[{\"schema\":\"public\",\"object_type\":\"schema\",\"privileges\":[\"USAGE\"]}, "
                    "{\"schema\":\"public\",\"object_type\":\"table\",\"privileges\":[\"SELECT\"]}]}] "
                    ""
                    "Example pg_databases for multiple databases: "
                    "[{\"database\":\"recon_db\",\"database_permissions\":[\"CONNECT\"],\"schema_grants\":[]}, "
                    "{\"database\":\"goms_db\",\"database_permissions\":[],\"schema_grants\":[{\"schema\":\"public\",\"object_type\":\"table\",\"privileges\":[\"SELECT\"]}]}] "
                    ""
                    "Ask user which grant levels and privileges they want for each database. Backend does not auto-add grants. "
                    ""
                    "This tool returns a 'structured_grants' object to pass to finalize_database_user_creation."
                    ""
                    "Required: server_name, username, password, available_databases, pg_databases",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "server_name": {
                                "type": "string",
                                "description": "Name of the PostgreSQL database server (e.g., 'common-pg')"
                            },
                            "username": {
                                "type": "string",
                                "description": "Username for the new database user"
                            },
                            "password": {
                                "type": "string",
                                "description": "Password for the new database user"
                            },
                            "available_databases": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of databases available on this server (from list_databases_for_server result)"
                            },
                            "pg_databases": {
                                "type": "array",
                                "description": "PostgreSQL databases - list of database grants",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "database": {
                                            "type": "string",
                                            "description": "Database name"
                                        },
                                        "database_permissions": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "Database-level permissions (e.g., ['CONNECT', 'CREATE'])"
                                        },
                                        "schema_grants": {
                                            "type": "array",
                                            "description": "Schema grants for this database (optional)",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "schema": {
                                                        "type": "string",
                                                        "description": "Schema name (e.g., 'public')"
                                                    },
                                                    "object_type": {
                                                        "type": "string",
                                                        "description": "Object type (e.g., 'table', 'schema')"
                                                    },
                                                    "privileges": {
                                                        "type": "array",
                                                        "items": {"type": "string"},
                                                        "description": "Privileges (e.g., ['SELECT', 'INSERT', 'CREATE', 'USAGE'])"
                                                    }
                                                },
                                                "required": ["schema", "object_type", "privileges"]
                                            }
                                        }
                                    },
                                    "required": ["database"]
                                }
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
                        "required": ["server_name", "username", "password", "available_databases", "pg_databases"]
                    }
                ),
                Tool(
                    name="get_database_users_with_grants",
                    description="Get all database users with their grants across ALL servers. "
                    "Use this AFTER the user has selected a server AND databases, AND provided a username. "
                    "This tool checks if the user already exists in ANY server and returns their existing grants. "
                    "Returns: users list with username, password, database_type, server, grants details.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "server_name": {
                                "type": "string",
                                "description": "The selected database server name (e.g., 'common-pg', 'common-mysql-1')"
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
                        "required": ["server_name", "tenant_code", "product_name", "environment", "geo_loc_code"]
                    }
                ),
                Tool(
                    name="finalize_database_user_creation",
                    description="Finalize database user creation. IMPORTANT: You MUST call structure_mysql_database_user_grants or structure_psql_database_user_grants FIRST. "
                    "Then use the 'structured_grants' field from that tool's result as the 'structured_grants' parameter for this tool. "
                    "Do NOT skip the structure step - the structured_grants parameter is required and must come from the previous tool result. "
                    "This tool combines all the structured data and sets is_ready=true to trigger backend processing.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "server_name": {
                                "type": "string",
                                "description": "Name of the database server"
                            },
                            "username": {
                                "type": "string",
                                "description": "Username for the new database user"
                            },
                            "password": {
                                "type": "string",
                                "description": "Password for the new database user"
                            },
                            "db_type": {
                                "type": "string",
                                "description": "Database type ('mysql' or 'postgresql')",
                                "enum": ["mysql", "postgresql"]
                            },
                            "structured_grants": {
                                "type": "object",
                                "description": "REQUIRED: The entire 'structured_grants' object returned by structure_mysql_database_user_grants or structure_psql_database_user_grants. You must call one of those tools first and pass its result here exactly."
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
                        "required": ["server_name", "username", "password", "db_type", "structured_grants"]
                    }
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> list[TextContent]:
            logger.info(f"MCP call_tool: {name} with args: {arguments}")

            if name == "list_database_servers":
                return await self._call_list_database_servers(arguments)
            elif name == "create_database":
                return await self._call_create_database(arguments)
            elif name == "list_databases_for_server":
                return await self._call_list_databases_for_server(arguments)
            elif name == "get_database_users_with_grants":
                return await self._call_get_database_users_with_grants(arguments)
            elif name == "structure_mysql_database_user_grants":
                return await self._call_structure_mysql_database_user_grants(arguments)
            elif name == "structure_psql_database_user_grants":
                return await self._call_structure_psql_database_user_grants(arguments)
            elif name == "finalize_database_user_creation":
                return await self._call_finalize_database_user_creation(arguments)
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    async def _call_list_database_servers(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """List database servers via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.database_creation_service import DatabaseCreationService

            async with AsyncSessionLocal() as db:
                service = DatabaseCreationService(db)
                result = await service.list_database_servers(
                    tenant_code=arguments["tenant_code"],
                    environment=Environment(normalize_environment(arguments["environment"])),
                    geo_loc_code=normalize_geo_loc_code(arguments["geo_loc_code"]),
                    product_name=arguments["product_name"],  # Required parameter
                )
                return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"list_database_servers failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "error": str(e)
            }))]

    async def _call_create_database(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Create database via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.database_creation_service import DatabaseCreationService

            async with AsyncSessionLocal() as db:
                service = DatabaseCreationService(db)

                result = await service.create_database(
                    database_name=arguments["database_name"],
                    db_server_name=arguments.get("db_server_name"),  # Optional
                    tenant_code=arguments["tenant_code"],
                    product_name=arguments["product_name"],
                    environment=Environment(normalize_environment(arguments["environment"])),
                    geo_loc_code=normalize_geo_loc_code(arguments["geo_loc_code"]),
                )

                return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"create_database failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "error": str(e)
            }))]

    async def _call_list_databases_for_server(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """List databases for server via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.database_user_management_service import DatabaseUserManagementService

            async with AsyncSessionLocal() as db:
                service = DatabaseUserManagementService(db)
                result = await service.list_databases_for_server(
                    server_name=arguments["server_name"],
                    tenant_code=arguments["tenant_code"],
                    environment=Environment(normalize_environment(arguments["environment"])),
                    geo_loc_code=normalize_geo_loc_code(arguments["geo_loc_code"]),
                    product_name=arguments["product_name"],
                )
                return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"list_databases_for_server failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "error": str(e)
            }))]

    async def _call_get_database_users_with_grants(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Get all database users with grants via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.services.database_user_management_service import DatabaseUserManagementService
            from app.utils.tenant_config import get_tenant_config

            tenant_code = arguments.get("tenant_code")
            async with AsyncSessionLocal() as db:
                cfg = await get_tenant_config(tenant_code, db)
                service = DatabaseUserManagementService(db)
                result = await service.get_database_users(
                    product_name=arguments["product_name"],
                    environment=normalize_environment(arguments["environment"]),
                    github_repository=cfg.github_infra_repository,
                    branch_name=cfg.github_infra_branch,
                    tenant=tenant_code,
                    geo_loc=normalize_geo_loc_code(arguments["geo_loc_code"]),
                    server_name=arguments.get("server_name"),
                )
                return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"get_database_users_with_grants failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "error": str(e)
            }))]

    async def _call_structure_mysql_database_user_grants(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Structure MySQL database user grants via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.database_user_management_service import DatabaseUserManagementService

            async with AsyncSessionLocal() as db:
                service = DatabaseUserManagementService(db)

                result = await service.structure_mysql_grants(
                    server_name=arguments["server_name"],
                    username=arguments["username"],
                    password=arguments["password"],
                    available_databases=arguments["available_databases"],
                    mysql_databases=arguments["mysql_databases"],
                )

                # Return structured data with structured_grants wrapper
                # The wrapper makes it clear what data to pass to finalize
                return [TextContent(type="text", text=json.dumps({
                    "status": "success",
                    "message": "MySQL grants structured successfully",
                    "structured_grants": result,  # Wrap in structured_grants for clarity
                    "is_ready": False,  # Not ready yet, need to call finalize
                }, default=str))]

        except Exception as e:
            logger.error(f"structure_mysql_database_user_grants failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "error": str(e)
            }))]

    async def _call_structure_psql_database_user_grants(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Structure PostgreSQL database user grants via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.database_user_management_service import DatabaseUserManagementService

            async with AsyncSessionLocal() as db:
                service = DatabaseUserManagementService(db)

                result = await service.structure_psql_grants(
                    server_name=arguments["server_name"],
                    username=arguments["username"],
                    password=arguments["password"],
                    available_databases=arguments["available_databases"],
                    pg_databases=arguments["pg_databases"],
                )

                # Return structured data with structured_grants wrapper
                # The wrapper makes it clear what data to pass to finalize
                return [TextContent(type="text", text=json.dumps({
                    "status": "success",
                    "message": "PostgreSQL grants structured successfully",
                    "structured_grants": result,  # Wrap in structured_grants for clarity
                    "is_ready": False,  # Not ready yet, need to call finalize
                }, default=str))]

        except Exception as e:
            logger.error(f"structure_psql_database_user_grants failed: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "error": str(e)
            }))]

    async def _call_finalize_database_user_creation(self, arguments: Dict[str, Any]) -> list[TextContent]:
        """Finalize database user creation via service layer."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
            from app.services.database_user_management_service import DatabaseUserManagementService

            async with AsyncSessionLocal() as db:
                service = DatabaseUserManagementService(db)

                result = await service.finalize_database_user_creation(
                    server_name=arguments["server_name"],
                    username=arguments["username"],
                    password=arguments["password"],
                    db_type=arguments["db_type"],
                    structured_grants=arguments["structured_grants"],
                    tenant_code=arguments.get("tenant_code"),
                    product_name=arguments.get("product_name"),
                    environment=Environment(normalize_environment(arguments["environment"])) if arguments.get("environment") else None,
                    geo_loc_code=normalize_geo_loc_code(arguments.get("geo_loc_code") or ""),
                )

                return [TextContent(type="text", text=json.dumps(result, default=str))]

        except Exception as e:
            logger.error(f"finalize_database_user_creation failed: {e}", exc_info=True)
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
    """Entry point for database creation MCP server."""
    import asyncio
    server = DatabaseToolsMCPServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
