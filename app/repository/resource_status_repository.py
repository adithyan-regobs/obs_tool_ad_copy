"""
Resource Status Repository

Bulk-fetches the UI-ready status from service_configs and infrastructure_mst.
No joins needed — just two simple SELECTs.
"""
import logging
from typing import List, Tuple

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.utils.service_routing import display_service_path
from app.utils.service_urls import build_health_url, build_service_url

logger = logging.getLogger(__name__)


# Only EKS services have an ArgoCD Application. ECS/Vercel/Lambda services live
# in the same table and must not be looked up, or every one of them would come
# back as "not found in ArgoCD".
EKS_INFRA_TYPE_REF = "eks_infrastructuretype_ref"


class ResourceStatusRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_bulk_status(
        self,
        resources: List[Tuple[str, str]],
    ) -> list:
        """Fetch status for a list of (table_name, code) pairs.

        Returns a list of dicts with table_name, code, status, status_updated_at.
        """
        # Split by table_name
        svc_codes = [code for table, code in resources if table == "SERVICE_CONFIG"]
        infra_codes = [code for table, code in resources if table == "INFRASTRUCTURE"]

        results = []

        if svc_codes:
            stmt = (
                select(
                    ServiceConfigModel.code,
                    ServiceConfigModel.status,
                    ServiceConfigModel.status_updated_at,
                    ServiceConfigModel.deployment_status,
                    ServiceConfigModel.deployment_error_message,
                    ServiceConfigModel.iac_locked_at,
                    ServiceConfigModel.environment,
                    ServiceConfigModel.config,
                    ServiceConfigModel.infrastructuretype_ref_code,
                    ServicesMstModel.name.label("service_mst_name"),
                )
                .select_from(ServiceConfigModel)
                .outerjoin(
                    ServicesMstModel,
                    ServicesMstModel.code == ServiceConfigModel.services_mst_code,
                )
                .where(
                    and_(
                        ServiceConfigModel.code.in_(svc_codes),
                        ServiceConfigModel.is_deleted == False,
                    )
                )
            )
            rows = (await self.db.execute(stmt)).all()
            for row in rows:
                config = row.config if isinstance(row.config, dict) else {}
                alb_url = (config.get("alb_url") or "").strip()
                service_name = (config.get("service_name") or row.service_mst_name or "").strip()
                service_path = display_service_path(config)
                results.append({
                    "table_name": "SERVICE_CONFIG",
                    "code": row.code,
                    "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                    "status_updated_at": row.status_updated_at.isoformat() if row.status_updated_at else None,
                    "deployment_status": row.deployment_status.value if hasattr(row.deployment_status, "value") else (row.deployment_status or None),
                    "deployment_error_message": row.deployment_error_message,
                    "iac_locked_at": row.iac_locked_at.isoformat() if row.iac_locked_at else None,
                    "app_url": build_service_url(alb_url, service_path) or None,
                    "health_url": build_health_url(alb_url, config.get("health") or "", service_path) or None,
                    # Consumed by the endpoint to resolve the ArgoCD Application,
                    # then dropped — not part of the response schema.
                    "_environment": (
                        row.environment.value if hasattr(row.environment, "value")
                        else str(row.environment or "")
                    ),
                    "_service_name": (
                        service_name if row.infrastructuretype_ref_code == EKS_INFRA_TYPE_REF else ""
                    ),
                })

        if infra_codes:
            stmt = (
                select(
                    InfrastructureMstModel.code,
                    InfrastructureMstModel.status,
                    InfrastructureMstModel.status_updated_at,
                    InfrastructureMstModel.deployment_status,
                    InfrastructureMstModel.deployment_error_message,
                    InfrastructureMstModel.iac_locked_at,
                )
                .where(
                    and_(
                        InfrastructureMstModel.code.in_(infra_codes),
                        InfrastructureMstModel.is_deleted == False,
                    )
                )
            )
            rows = (await self.db.execute(stmt)).all()
            for row in rows:
                results.append({
                    "table_name": "INFRASTRUCTURE",
                    "code": row.code,
                    "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                    "status_updated_at": row.status_updated_at.isoformat() if row.status_updated_at else None,
                    "deployment_status": row.deployment_status.value if hasattr(row.deployment_status, "value") else (row.deployment_status or None),
                    "deployment_error_message": row.deployment_error_message,
                    "iac_locked_at": row.iac_locked_at.isoformat() if row.iac_locked_at else None,
                })

        return results
