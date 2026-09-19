"""
Model Registry API Endpoints

Jenkins-facing endpoints for model download status tracking.
Auth: X-Webhook-Secret header (same shared secret as Jenkins webhook).

Also exposes a user-facing HuggingFace model validation endpoint (no auth needed —
the HF token is supplied by the caller in the request body).
"""
import logging

from fastapi import APIRouter, Header, HTTPException, Query, status

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.repository.model_registry_repository import ModelRegistryRepository
from app.schemas.model_serving_schemas import (
    MarkReadyRequest,
    ModelStatusResponse,
    ValidateModelRequest,
    ValidateModelResponse,
)
from app.services.model_serving_service import validate_hf_model

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_webhook_secret(webhook_secret: str | None) -> None:
    """Validate X-Webhook-Secret header against pipeline_webhook_secret."""
    if not webhook_secret or webhook_secret != settings.pipeline_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret",
        )


@router.get("/status", response_model=ModelStatusResponse)
async def get_model_status(
    model_id: str = Query(..., description="HuggingFace model ID"),
    revision: str = Query(..., description="Model revision (commit SHA)"),
    x_webhook_secret: str | None = Header(None, alias="X-Webhook-Secret"),
):
    """Check download status for a model+revision. Called by Jenkins before downloading."""
    _verify_webhook_secret(x_webhook_secret)
    async with AsyncSessionLocal() as db:
        repo = ModelRegistryRepository(db)
        record = await repo.get_shared(model_id, revision)
    if not record:
        return ModelStatusResponse(status="unknown")
    return ModelStatusResponse(
        status=record.download_status or "unknown",
        error_message=record.error_message,
    )


@router.post("/mark-ready", response_model=ModelStatusResponse)
async def mark_model_ready(
    body: MarkReadyRequest,
    x_webhook_secret: str | None = Header(None, alias="X-Webhook-Secret"),
):
    """Mark a model as ready on EFS. Best-effort call from Jenkins after download completes."""
    _verify_webhook_secret(x_webhook_secret)
    try:
        async with AsyncSessionLocal() as db:
            repo = ModelRegistryRepository(db)
            await repo.mark_ready(body.model_id, body.revision)
            await db.commit()
        logger.info("[EFS] Marked model ready: %s@%s", body.model_id, body.revision)
    except Exception as exc:
        logger.warning("[EFS] Failed to mark model ready: %s", exc)
    return ModelStatusResponse(status="ready")


@router.post("/validate-model", response_model=ValidateModelResponse)
async def validate_model(body: ValidateModelRequest):
    """Check if a HuggingFace model is accessible, with or without a token.

    Returns whether the model exists, is gated, and the resolved commit SHA.
    No authentication required — the HF token is supplied by the caller.
    """
    return await validate_hf_model(body.model_name, body.hf_token)
