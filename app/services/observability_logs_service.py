"""Service layer for querying application logs via configurable providers."""

import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.enum import LogProviderEnum, ServiceTypeEnum
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.service_config_model import ServiceConfigModel
from app.repository.log_provider_config_repository import LogProviderConfigRepository
from app.integrations.cloudwatch_logs_integration import (
    CloudWatchLogsIntegration,
    CloudWatchLogsError,
)
from app.integrations.datadog_logs_integration import (
    DatadogLogsIntegration,
    DatadogLogsError,
)

logger = logging.getLogger(__name__)

# Either class is acceptable — both duck-type to the same interface.
LogsIntegrationError = (CloudWatchLogsError, DatadogLogsError)


def _derive_name_prefix(tenant_code: str, geo_loc_mst_code: str = "") -> str:
    """Build k8s name prefix: {tenant}-{env}-{region_name}-{index}.

    Uses tenant code, onboarding defaults for env/index, and extracts
    the region name from the geo_loc_mst_code (e.g. "region-aslam-mumbai" → "mumbai").
    """
    if not tenant_code:
        return ""
    env = settings.onboarding_default_env
    index = settings.onboarding_default_index
    region_name = ""
    if geo_loc_mst_code:
        # geo_loc_mst_code format: "region-{tenant}-{region_name}"
        parts = geo_loc_mst_code.rsplit("-", 1)
        if len(parts) > 1:
            region_name = parts[-1]
    if not region_name:
        region_name = settings.onboarding_default_region_code
    return f"{tenant_code}-{env}-{region_name}-{index}"


def _check_sidecar_for_datadog(sidecar_config: list | None) -> LogProviderEnum:
    """Check if any enabled Datadog sidecar has logs enabled."""
    if not sidecar_config:
        return LogProviderEnum.CLOUDWATCH
    for sidecar in sidecar_config:
        if not isinstance(sidecar, dict):
            continue
        is_enabled = sidecar.get("enabled", False)
        is_datadog = "datadog" in (sidecar.get("name", "") or "").lower() or \
                     "datadog" in (sidecar.get("sidecar_config_code", "") or "").lower()
        logs_enabled = sidecar.get("datadog_logs_enabled", False)
        if is_enabled and is_datadog and logs_enabled:
            return LogProviderEnum.DATADOG
    return LogProviderEnum.CLOUDWATCH


class ObservabilityLogsService:
    """Business logic for application log querying with tenant isolation."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._log_provider_repo = LogProviderConfigRepository(session)
        self._resolved_provider: tuple[LogProviderEnum, dict] | None = None

    async def _resolve_log_provider(
        self, service_name: str, tenant_code: str
    ) -> tuple[LogProviderEnum, dict]:
        """Resolve which log provider to use and its auth_config.

        Priority:
        1. Service-level override (service_configs.log_provider column)
        2. Auto-detect from sidecar config (Datadog sidecar + logs enabled → DATADOG)
        3. Tenant default log provider
        4. Fallback to CLOUDWATCH
        """
        if self._resolved_provider is not None:
            return self._resolved_provider

        provider = LogProviderEnum.CLOUDWATCH

        if service_name and tenant_code:
            try:
                stmt = (
                    select(ServiceConfigModel)
                    .join(ServicesMstModel, ServiceConfigModel.services_mst_code == ServicesMstModel.code)
                    .where(
                        ServicesMstModel.name == service_name,
                        ServicesMstModel.tenants_mst_code == tenant_code,
                        ServicesMstModel.is_deleted == False,
                    )
                    .order_by(ServiceConfigModel.created_at.desc())
                    .limit(1)
                )
                result = await self.session.execute(stmt)
                config = result.scalar_one_or_none()

                if config:
                    if config.log_provider:
                        provider = config.log_provider
                    else:
                        provider = _check_sidecar_for_datadog(config.sidecar_config)
            except Exception as exc:
                logger.warning("Failed to resolve log provider for %s: %s", service_name, exc)

        # Get auth_config from log_provider_config table
        auth_config = {}
        if tenant_code:
            try:
                provider_config = await self._log_provider_repo.get_by_tenant_and_provider(
                    tenant_code, provider
                )
                if provider_config:
                    auth_config = provider_config.auth_config or {}
                elif provider == LogProviderEnum.DATADOG:
                    # Datadog provider not configured — fall back to CloudWatch
                    logger.info("Datadog log provider not configured for tenant %s, falling back to CloudWatch", tenant_code)
                    provider = LogProviderEnum.CLOUDWATCH
                    provider_config = await self._log_provider_repo.get_by_tenant_and_provider(
                        tenant_code, LogProviderEnum.CLOUDWATCH
                    )
                    auth_config = provider_config.auth_config if provider_config else {}
            except Exception as exc:
                logger.warning("Failed to get log provider config for tenant %s: %s", tenant_code, exc)

        self._resolved_provider = (provider, auth_config)
        return provider, auth_config

    async def _get_integration(self, service_name: str = "", tenant_code: str = ""):
        """Return the log integration client for the resolved provider.

        Both CloudWatchLogsIntegration and DatadogLogsIntegration duck-type to
        the same interface (get_log_events, get_log_streams, get_log_volume,
        health), so callers don't need to branch on the return type.
        """
        provider, auth_config = await self._resolve_log_provider(service_name, tenant_code)

        if provider == LogProviderEnum.DATADOG:
            logger.info("Log provider resolved: DATADOG (service=%s, tenant=%s)", service_name, tenant_code)
            return DatadogLogsIntegration(auth_config=auth_config or {})

        logger.info("Log provider resolved: CLOUDWATCH (service=%s, tenant=%s)", service_name, tenant_code)
        return CloudWatchLogsIntegration(auth_config=auth_config if auth_config else None)

    async def _resolve_k8s_name(self, service_name: str, tenant_code: str) -> str:
        """Resolve the full k8s deployment name from service_config fields.

        Regular EKS pods: {name_prefix}-{service_name}-{pod-hash}
          where name_prefix = {tenant}-{env}-{region_name}-{index}.

        Model serving (KServe) pods: {tenant}-{service}-predictor-{revision}-{hash}
          KServe uses a shorter name without the env/region/index prefix.
        """
        try:
            stmt = (
                select(ServiceConfigModel, ServicesMstModel.service_type)
                .join(ServicesMstModel, ServiceConfigModel.services_mst_code == ServicesMstModel.code)
                .where(
                    ServicesMstModel.name == service_name,
                    ServicesMstModel.tenants_mst_code == tenant_code,
                    ServicesMstModel.is_deleted == False,
                )
                .order_by(ServiceConfigModel.created_at.desc())
                .limit(1)
            )
            result = await self.session.execute(stmt)
            row = result.first()
            config = row[0] if row else None
            service_type = row[1] if row else None

            safe_name = re.sub(r"[^a-z0-9-]", "-", service_name.lower()).strip("-")
            # Strip trailing "-service" suffix — K8s deployments don't include it
            if safe_name.endswith("-service"):
                safe_name = safe_name[:-8]

            # Model serving uses KServe with short names: {tenant}-{service}
            if service_type == ServiceTypeEnum.MODEL_SERVING:
                k8s_name = f"{tenant_code}-{safe_name}"
                logger.info("Resolved k8s name (model-serving): %s -> %s", service_name, k8s_name)
                return k8s_name

            geo_loc_mst_code = config.geo_loc_mst_code if config else ""
            logger.info(
                "k8s name resolution: service=%s, tenant=%s, config_found=%s, geo_loc=%s",
                service_name, tenant_code, config is not None, geo_loc_mst_code,
            )
            prefix = _derive_name_prefix(tenant_code, geo_loc_mst_code)
            if prefix:
                k8s_name = f"{prefix}-{safe_name}"
                logger.info("Resolved k8s name: %s -> %s (prefix=%s)", service_name, k8s_name, prefix)
                return k8s_name
        except Exception as exc:
            logger.warning("Failed to resolve k8s name for %s: %s — using bare name", service_name, exc)
        return service_name

    async def get_log_events(
        self,
        namespace: str,
        service_name: str,
        start_time: datetime,
        end_time: datetime,
        tenant_code: str = "",
        search: str | None = None,
        level: str | None = None,
        limit: int = 200,
        next_token: str | None = None,
    ) -> dict:
        """Fetch log events for a specific service.

        Uses FilterLogEvents which returns chronological order (oldest first).
        Pagination via next_token for loading more.
        """
        k8s_name = await self._resolve_k8s_name(service_name, tenant_code) if tenant_code else service_name
        integration = await self._get_integration(service_name, tenant_code)

        try:
            result = await integration.get_log_events(
                namespace=namespace,
                start=start_time,
                end=end_time,
                service_name=k8s_name,
                search=search,
                level=level,
                limit=limit,
                next_token=next_token,
            )
            return {
                "events": result.get("events", []),
                "next_token": result.get("nextToken"),
                "has_older": result.get("nextToken") is not None,
            }
        except LogsIntegrationError as exc:
            error_msg = str(exc).lower()
            if "not found" in error_msg or "does not exist" in error_msg:
                logger.info("Log source not found for namespace %s — service likely not deployed", namespace)
                return {"events": [], "next_token": None, "has_older": False}
            raise

    async def get_newest_log_events(
        self,
        namespace: str,
        service_name: str,
        start_time: datetime,
        end_time: datetime,
        tenant_code: str = "",
        search: str | None = None,
        level: str | None = None,
        limit: int = 500,
    ) -> dict:
        """Fetch newest events. Delegates to integration.get_log_events_newest().

        Each provider implements newest-first its own way internally. This service
        method is a thin wrapper: resolve k8s name, pick the integration, delegate,
        handle errors. Zero provider-specific logic.
        """
        k8s_name = await self._resolve_k8s_name(service_name, tenant_code) if tenant_code else service_name
        integration = await self._get_integration(service_name, tenant_code)

        try:
            result = await integration.get_log_events_newest(
                namespace=namespace,
                start=start_time,
                end=end_time,
                service_name=k8s_name,
                search=search,
                level=level,
                limit=limit,
            )
            return {
                "events": result.get("events", []),
                "next_token": result.get("nextToken"),
                "has_older": result.get("has_older", False),
                "oldest_timestamp": result.get("oldest_timestamp"),
            }
        except LogsIntegrationError as exc:
            error_msg = str(exc).lower()
            if "not found" in error_msg or "does not exist" in error_msg:
                logger.info("Log source not found for namespace %s", namespace)
                return {"events": [], "next_token": None, "has_older": False, "oldest_timestamp": None}
            raise

    async def get_log_streams(
        self,
        namespace: str,
        service_name: str | None = None,
        tenant_code: str = "",
    ) -> list[dict]:
        """List log streams (pods) for a namespace, optionally filtered by service prefix."""
        k8s_name = await self._resolve_k8s_name(service_name, tenant_code) if (service_name and tenant_code) else service_name
        integration = await self._get_integration(service_name or "", tenant_code)
        try:
            streams = await integration.get_log_streams(namespace=namespace)
            if k8s_name:
                streams = [
                    s for s in streams
                    if s["logStreamName"].startswith(k8s_name)
                ]
            return streams
        except LogsIntegrationError as exc:
            if "not found" in str(exc).lower():
                return []
            raise

    async def get_log_volume(
        self,
        namespace: str,
        service_name: str,
        start_time: datetime,
        end_time: datetime,
        tenant_code: str = "",
        interval_minutes: int = 5,
    ) -> list[dict]:
        """Get log volume over time for histogram visualization."""
        k8s_name = await self._resolve_k8s_name(service_name, tenant_code) if tenant_code else service_name
        integration = await self._get_integration(service_name, tenant_code)
        try:
            return await integration.get_log_volume(
                namespace=namespace,
                start=start_time,
                end=end_time,
                service_name=k8s_name,
                interval_minutes=interval_minutes,
            )
        except LogsIntegrationError as exc:
            if "not found" in str(exc).lower():
                return []
            raise

    async def check_feature_status(self, tenant_code: str = "") -> dict:
        """Check if log provider is configured and reachable for the tenant."""
        if not tenant_code:
            return {"enabled": settings.cloudwatch_logs_enabled, "healthy": False}

        # Check if tenant has any log provider configured
        providers = await self._log_provider_repo.get_all_for_tenant(tenant_code)
        if not providers:
            # Fall back to global setting
            enabled = settings.cloudwatch_logs_enabled
            healthy = False
            if enabled:
                try:
                    integration = CloudWatchLogsIntegration()
                    healthy = await integration.health()
                except Exception:
                    pass
            return {"enabled": enabled, "healthy": healthy}

        # Tenant has at least one provider configured
        healthy = False
        default_provider = next((p for p in providers if p.is_default), providers[0])
        if default_provider.provider == LogProviderEnum.CLOUDWATCH:
            try:
                integration = CloudWatchLogsIntegration(auth_config=default_provider.auth_config)
                healthy = await integration.health()
            except Exception:
                pass
        elif default_provider.provider == LogProviderEnum.DATADOG:
            try:
                integration = DatadogLogsIntegration(auth_config=default_provider.auth_config)
                healthy = await integration.health()
            except Exception:
                pass

        return {"enabled": True, "healthy": healthy}
