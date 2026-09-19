"""
Pipeline Webhook Endpoint

Receives status callbacks from GitHub Actions pipelines (terragrunt-apply)
when infrastructure provisioning completes or fails.

No JWT auth — authenticated via shared secret in X-Webhook-Secret header.
"""

import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.core.config import settings
from app.core.enum import DeploymentStatusEnum, ResourceStatusEnum
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from sqlalchemy import select, and_

router = APIRouter()
logger = logging.getLogger(__name__)


class PipelineStatusPayload(BaseModel):
    """Payload sent by the pipeline webhook callback."""
    tenant_code: str = Field(..., description="Tenant subdomain / code")
    status: str = Field(..., description="Pipeline result: 'success' or 'failure'")
    workflow_run_id: str = Field(default="", description="GitHub Actions workflow run ID")
    workflow_run_url: str = Field(default="", description="GitHub Actions workflow run URL")


@router.post("/pipeline-status", summary="Pipeline Status Webhook")
async def pipeline_status_webhook(
    payload: PipelineStatusPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Webhook endpoint for infrastructure pipeline status callbacks.

    Called by GitHub Actions at the end of terragrunt-apply workflow.
    Authenticated via shared secret (X-Webhook-Secret header).

    Updates infrastructure_mst.infra_status to ACTIVE on success or FAILED on failure.
    """
    # Validate shared secret
    webhook_secret = request.headers.get("X-Webhook-Secret", "")
    if not webhook_secret or webhook_secret != settings.pipeline_webhook_secret:
        logger.warning(f"[PIPELINE-WEBHOOK] Invalid or missing webhook secret for tenant: {payload.tenant_code}")
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    tenant_code = payload.tenant_code
    new_status = (
        DeploymentStatusEnum.ACTIVE if payload.status == "success"
        else DeploymentStatusEnum.FAILED
    )

    logger.info(
        f"[PIPELINE-WEBHOOK] Received status '{payload.status}' for tenant '{tenant_code}' "
        f"(workflow_run_id: {payload.workflow_run_id})"
    )

    # Find the EKS infrastructure record for this tenant (most recent with GIT_COMMITTED status)
    stmt = (
        select(InfrastructureMstModel)
        .where(
            and_(
                InfrastructureMstModel.tenants_mst_code == tenant_code,
                InfrastructureMstModel.infrastructuretype_ref_code == "eks_infrastructuretype_ref",
                InfrastructureMstModel.is_deleted == False,
            )
        )
        .order_by(InfrastructureMstModel.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    infra_record = result.scalar_one_or_none()

    if not infra_record:
        logger.warning(f"[PIPELINE-WEBHOOK] No EKS infrastructure record found for tenant '{tenant_code}'")
        raise HTTPException(status_code=404, detail=f"No infrastructure record found for tenant '{tenant_code}'")

    # Update the status
    infra_repo = InfrastructureMstRepository(db)
    updated = await infra_repo.update_infra_status(
        infrastructure_id=infra_record.id,
        status=new_status,
        updated_by="pipeline-webhook",
    )

    # Update the UI-ready resource status
    resource_status = (
        ResourceStatusEnum.ONLINE if payload.status == "success"
        else ResourceStatusEnum.FAILED
    )
    await infra_repo.update_status(infra_record.code, resource_status)

    logger.info(
        f"[PIPELINE-WEBHOOK] Updated infra record {infra_record.code} "
        f"status: {infra_record.infra_status} -> {new_status.value}, "
        f"resource_status: {resource_status.value}"
    )

    return {
        "status": "ok",
        "infrastructure_code": infra_record.code,
        "new_status": new_status.value,
        "tenant_code": tenant_code,
    }
