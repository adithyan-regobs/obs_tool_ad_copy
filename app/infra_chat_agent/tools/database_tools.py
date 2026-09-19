import json
import logging
from typing import Any, Dict

from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_environment, normalize_geo_loc_code

logger = logging.getLogger(__name__)


async def list_database_servers(arguments: Dict[str, Any]) -> str:
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
                product_name=arguments["product_name"],
            )
            return json.dumps(result, default=str)

    except Exception as e:
        logger.error(f"list_database_servers failed: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "error": str(e)
        })


async def list_databases_for_server(arguments: Dict[str, Any]) -> str:
    """List databases for a specific server via service layer."""
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
            return json.dumps(result, default=str)

    except Exception as e:
        logger.error(f"list_databases_for_server failed: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "error": str(e)
        })




