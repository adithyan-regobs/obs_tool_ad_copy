"""
Resource Status Endpoint

Bulk-fetches the UI-ready deployment status for canvas resources.
Polled by the frontend every 5s (in-progress) / 30s (idle) to drive
canvas node badges without the complex client-side queue derivation.
"""
import logging
from typing import Tuple

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.repository.resource_status_repository import ResourceStatusRepository
from app.services.argocd_status_service import status_for_services
from app.schemas.resource_status_schemas import (
    ResourceStatusRequest,
    ResourceStatusResponse,
    ResourceStatusItem,
    QueueStatusRequest,
    QueueStatusResponse,
    QueueStatusItem,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/bulk-status",
    response_model=ResourceStatusResponse,
    summary="Bulk Resource Deployment Status",
)
async def get_bulk_resource_status(
    request: ResourceStatusRequest,
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Fetch the current deployment status for a batch of resources.

    Two sources, merged. The persisted status comes from the resource tables,
    written by the Jenkins/pipeline webhooks — that is "did DevLift finish
    deploying". Services also carry live ArgoCD sync/health, read through a
    short Redis cache — that is "is the application actually running". The
    ArgoCD half is always optional: any failure leaves those fields null and
    the response is exactly what it was before.

    Used by the frontend canvas to poll resource node badges.
    """
    _, tenant = user_and_tenant
    repo = ResourceStatusRepository(db)

    resources_tuples = [
        (ref.table_name, ref.code) for ref in request.resources
    ]

    results = await repo.get_bulk_status(resources_tuples)

    services = [
        (r["code"], r["_service_name"], r["_environment"])
        for r in results
        if r["table_name"] == "SERVICE_CONFIG" and r.get("_service_name") and r.get("_environment")
    ]

    try:
        argocd_state = await status_for_services(tenant.code, services, db)
    except Exception:
        logger.exception("bulk-status: ArgoCD enrichment failed for tenant %s", tenant.code)
        argocd_state = {}

    items = []
    for row in results:
        row.pop("_service_name", None)
        row.pop("_environment", None)
        items.append(ResourceStatusItem(**row, **argocd_state.get(row["code"], {})))

    return ResourceStatusResponse(resources=items)


@router.post(
    "/bulk-queue-status",
    response_model=QueueStatusResponse,
    summary="Bulk Queue Deployment Status",
)
async def get_bulk_queue_status(
    request: QueueStatusRequest,
    db: AsyncSession = Depends(get_db),
    _: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Fetch the current status for a batch of transaction queue records.

    Accepts a list of queue codes (already known by the frontend from canvas
    node settings) and returns status + last-updated timestamp for each.
    Used to poll deployment pipeline state without needing table_name splits.
    """
    from sqlalchemy import select
    from app.db.models.transaction_queue_model import TransactionQueueModel

    result = await db.execute(
        select(
            TransactionQueueModel.code,
            TransactionQueueModel.status,
            TransactionQueueModel.status_last_updated_at,
        ).where(TransactionQueueModel.code.in_(request.queue_codes))
    )
    rows = {row.code: row for row in result.all()}

    items = []
    for queue_code in request.queue_codes:
        row = rows.get(queue_code)
        items.append(QueueStatusItem(
            queue_code=queue_code,
            queue_status=row.status.value if (row and row.status) else None,
            status_last_updated_at=(
                row.status_last_updated_at.isoformat()
                if (row and row.status_last_updated_at)
                else None
            ),
        ))

    return QueueStatusResponse(resources=items)
