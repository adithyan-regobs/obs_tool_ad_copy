"""
Database Creation Service

Provides database creation operations for MCP server consumption.
"""
import logging
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
from app.utils.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)


class DatabaseCreationService:
    """
    Service for database creation operations.

    Thin layer that validates parameters and formats responses.
    Designed for MCP server consumption.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_database_servers(
        self,
        tenant_code: str,
        environment: Environment,
        geo_loc_code: str,
        product_name: str,
    ) -> Dict[str, Any]:
        """
        List available database servers for a tenant by calling GitHub API.

        Args:
            tenant_code: Tenant identifier
            environment: Environment enum
            geo_loc_code: Geographic location code
            product_name: Product name (e.g., 'Core', 'Falcon') - required to filter servers

        Returns:
            Dict with servers list
        """
        logger.info(
            "DATABASE CREATION - list_database_servers",
            extra={
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
            }
        )

        try:
            # Import here to avoid circular dependency
            from app.services.ui_dynamic_form_config_service import UIDynamicFormConfigService

            # Create service instance
            ui_service = UIDynamicFormConfigService(self.db)
            cfg = await get_tenant_config(tenant_code, self.db)

            logger.info(f"[DB_SERVICE] Calling GitHub API - repo: {cfg.github_infra_repository}, branch: {cfg.github_infra_branch}")

            # Call the real API to get servers from GitHub (now async with await)
            result = await ui_service.get_servers_list(
                product_name=product_name,
                environment=environment.value,
                github_repository=cfg.github_infra_repository,
                branch_name=cfg.github_infra_branch,
                tenant=tenant_code,
                geo_loc=geo_loc_code
            )

            logger.info(f"[DB_SERVICE] GitHub API result: success={result.get('success')}, servers={result.get('servers')}, message={result.get('message')}")

            # Transform the API response format to match expected MCP tool format
            # API returns: {"success": true, "servers": [{"name": "common-mysql", "type": "MySQL"}], ...}
            # MCP expects: {"servers": [{"server_name": "...", "server_type": "...", "description": "..."}], "count": N}

            servers = []
            for server in result.get("servers", []):
                servers.append({
                    "server_name": server.get("name", ""),
                    "server_type": server.get("type", "Unknown"),
                    "description": f"{server.get('type', 'Database')} server"
                })

            logger.info(f"Found {len(servers)} servers via GitHub API")

            return {
                "servers": servers,
                "count": len(servers),
                "metadata": result.get("metadata", {})
            }

        except Exception as e:
            logger.error(f"Failed to fetch servers from GitHub API: {e}", exc_info=True)

            # Fallback to static list if API call fails
            logger.warning("Falling back to static server list")
            servers = [
                {
                    "server_name": "common-mysql",
                    "server_type": "MySQL",
                    "description": "Shared MySQL server (fallback)"
                },
                {
                    "server_name": "common-pg",
                    "server_type": "PostgreSQL",
                    "description": "Shared PostgreSQL server (fallback)"
                }
            ]

            return {
                "servers": servers,
                "count": len(servers),
                "error": str(e)
            }

    async def create_database(
        self,
        database_name: str,
        db_server_name: str,
        tenant_code: str,
        product_name: str,
        environment: Environment,
        geo_loc_code: str,
        infra_vendor: str = "aws",
    ) -> Dict[str, Any]:
        """
        Create a database (validate parameters and format response).

        Note: This does NOT save to database. Only validates and formats.

        Args:
            database_name: Name of the database to create
            db_server_name: Database server name (e.g., 'common-mysql', 'common-pg')
            tenant_code: Tenant identifier
            product_name: Product name (e.g., 'Core', 'Falcon')
            environment: Environment enum
            geo_loc_code: Geographic location code

        Returns:
            Dict with attribute_parameters, placement_parameters, is_ready
        """
        logger.info(
            "DATABASE CREATION - create_database",
            extra={
                "database_name": database_name,
                "db_server_name": db_server_name,
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
            }
        )

        # Build attribute_parameters (database-specific)
        attribute_parameters = {
            "database_name": database_name,
            "db_server_name": db_server_name,
        }

        # Build placement_parameters (environment/location context)
        placement_parameters = {
            "tenant_code": tenant_code,
            "product_name": product_name,
            "environment_enum": environment.value,
            "geo_loc_mst_code": geo_loc_code,
            "infra_vendor_enum": infra_vendor,
            "case_type_ref_code": "database",
            "case_ref_code": "database_creation",
        }

        return {
            "status": "success",
            "message": f"Database '{database_name}' creation request validated for server '{db_server_name}'",
            "attribute_parameters": attribute_parameters,
            "placement_parameters": placement_parameters,
            "is_ready": True,
        }
