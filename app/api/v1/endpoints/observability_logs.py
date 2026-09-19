"""API endpoints for querying application logs from CloudWatch."""

import logging
from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.config import settings
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.observability_logs_schemas import (
    LogsQueryParams,
    LogStreamsQueryParams,
    LogVolumeQueryParams,
    LogEventsResponse,
    LogStreamsResponse,
    LogVolumeResponse,
    LogsFeatureStatusResponse,
    LogEvent,
    LogStream,
    LogVolumeEntry,
)
from app.services.observability_logs_service import ObservabilityLogsService

logger = logging.getLogger(__name__)

router = APIRouter()


async def _check_enabled(tenant_code: str, db: AsyncSession):
    """Raise 503 if no log provider is configured for the tenant."""
    from app.repository.log_provider_config_repository import LogProviderConfigRepository
    repo = LogProviderConfigRepository(db)
    providers = await repo.get_all_for_tenant(tenant_code)
    if providers:
        return  # Tenant has explicit log provider config
    if not settings.cloudwatch_logs_enabled:
        raise HTTPException(
            status_code=503,
            detail="Application logs are not enabled. Configure a log provider or set CLOUDWATCH_LOGS_ENABLED=true.",
        )


def _namespace_from_tenant(tenant: TenantsMstModel) -> str:
    """Derive the K8s namespace from the authenticated tenant. Never from user input."""
    return f"{tenant.code}-ns"


@router.post("/events", response_model=LogEventsResponse, summary="Get Application Log Events")
async def get_log_events(
    data: LogsQueryParams,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Fetch paginated log events for a service. Namespace derived from JWT tenant."""
    user, tenant = user_and_tenant
    await _check_enabled(tenant.code, db)
    namespace = _namespace_from_tenant(tenant)
    logger.info(
        "Log events request: service=%s, namespace=%s, tenant=%s, start=%s, end=%s",
        data.service_name, namespace, tenant.code, data.start_time, data.end_time,
    )

    service = ObservabilityLogsService(db)
    try:
        if data.newest_first:
            result = await service.get_newest_log_events(
                namespace=namespace,
                service_name=data.service_name,
                start_time=data.start_time,
                end_time=data.end_time,
                tenant_code=tenant.code,
                search=data.search,
                level=data.level,
                limit=data.limit,
            )
        else:
            result = await service.get_log_events(
                namespace=namespace,
                service_name=data.service_name,
                start_time=data.start_time,
                end_time=data.end_time,
                tenant_code=tenant.code,
                search=data.search,
                level=data.level,
                limit=data.limit,
                next_token=data.next_token,
            )
        return LogEventsResponse(
            events=[
                LogEvent(
                    timestamp=e["timestamp"],
                    message=e["message"],
                    log_stream_name=e.get("logStreamName", ""),
                    event_id=e.get("eventId", ""),
                )
                for e in result["events"]
            ],
            next_token=result.get("next_token"),
            has_older=result["has_older"],
            oldest_timestamp=result.get("oldest_timestamp"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch log events: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch logs: {exc}")


@router.post("/streams", response_model=LogStreamsResponse, summary="List Log Streams (Pods)")
async def get_log_streams(
    data: LogStreamsQueryParams,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """List available log streams (pods) for a service."""
    user, tenant = user_and_tenant
    await _check_enabled(tenant.code, db)
    namespace = _namespace_from_tenant(tenant)

    service = ObservabilityLogsService(db)
    try:
        streams = await service.get_log_streams(
            namespace=namespace,
            service_name=data.service_name,
            tenant_code=tenant.code,
        )
        return LogStreamsResponse(
            streams=[
                LogStream(
                    log_stream_name=s["logStreamName"],
                    last_event_timestamp=s.get("lastEventTimestamp", 0),
                    first_event_timestamp=s.get("firstEventTimestamp", 0),
                )
                for s in streams
            ]
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to list log streams: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list log streams: {exc}")


@router.post("/volume", response_model=LogVolumeResponse, summary="Get Log Volume Over Time")
async def get_log_volume(
    data: LogVolumeQueryParams,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Get log volume histogram data for a service."""
    user, tenant = user_and_tenant
    await _check_enabled(tenant.code, db)
    namespace = _namespace_from_tenant(tenant)

    service = ObservabilityLogsService(db)
    try:
        volume = await service.get_log_volume(
            namespace=namespace,
            service_name=data.service_name,
            start_time=data.start_time,
            end_time=data.end_time,
            tenant_code=tenant.code,
            interval_minutes=data.interval_minutes,
        )
        return LogVolumeResponse(
            volume=[LogVolumeEntry(**v) for v in volume]
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to get log volume: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get log volume: {exc}")


@router.get("/status", response_model=LogsFeatureStatusResponse, summary="Check Logs Feature Status")
async def get_feature_status(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Check if application logs feature is enabled and CloudWatch is reachable."""
    user, tenant = user_and_tenant
    service = ObservabilityLogsService(db)
    result = await service.check_feature_status(tenant_code=tenant.code)
    return LogsFeatureStatusResponse(**result)
