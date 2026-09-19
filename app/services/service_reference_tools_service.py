"""
Service Reference Tools Service

Provides reference data operations for MCP server consumption.
"""
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.service_config_chat_repository import ServiceConfigChatRepository
from app.core.enum import EnvironmentEnum

logger = logging.getLogger(__name__)


class ServiceReferenceToolsService:
    """
    Service for service reference tool operations.

    Thin layer that coordinates repository calls.
    Designed for REST API endpoints (MCP server consumption).
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.config_repo = ServiceConfigChatRepository(db)

    async def list_services(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        infra_vendor: Optional[str] = None,
        infrastructure_type: Optional[str] = None,
    ) -> dict:
        """
        List all services with configurations.

        Args:
            tenant_code: Tenant identifier (from JWT)
            environment: Environment enum
            geo_loc_code: Geographic location code
            infra_vendor: Infrastructure vendor for response context
            infrastructure_type: Infrastructure type for response context

        Returns:
            Dict with services list, count, and infra suffix
        """
        logger.info(
            "SERVICE REFERENCE TOOLS - list_services",
            extra={
                "tenant_code": tenant_code,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
            }
        )

        # Query services from repository
        services_data = await self.config_repo.get_services_with_config_status(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_code=geo_loc_code,
        )

        # Filter to only services with existing configs
        services = [
            {
                "service_code": svc["service_code"],
                "service_name": svc["service_name"],
                "service_type": svc["service_type"],
                "has_existing_config": svc["has_existing_config"],
            }
            for svc in services_data
            if svc["has_existing_config"]
        ]

        # Build infra suffix for response formatting
        infra_parts = []
        if infra_vendor:
            infra_parts.append(infra_vendor.upper())
        if infrastructure_type:
            infra_parts.append(infrastructure_type)
        infra_suffix = f" {' '.join(infra_parts)}" if infra_parts else ""

        return {
            "services": services,
            "count": len(services),
            "infra_suffix": infra_suffix,
        }
