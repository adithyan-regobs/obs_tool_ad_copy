"""
Variable Clone Service (listing only)

Source discovery for the env-variable clone modal (pull model — the caller is
on the TARGET resource and picks where to clone FROM). Three operations:
  - get_source_services: paginated + searchable source picker, with a
    preloaded default (service, environment, region) selection.
  - get_source_options: env/region options for a selected source service.
  - get_source_variables: deployed variables of a chosen source combo.

Recommendation rules live in app/domain/policies/variable_clone_policy.py.
The clone execute API is POST /resource-variable/clone (VariableService).
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import EnvironmentEnum, WorkflowSourceTableEnum
from app.domain.policies.variable_clone_policy import (
    pick_environment,
    pick_geo_loc,
    resolve_cloud_region,
    sort_environments,
)
from app.repository.service_config_repository import (
    ServiceConfigRepository,
    CLONE_GEO_ALLOWLIST_TENANTS,
    CLONE_ENV_GEO_ALLOWLIST,
)
from app.repository.services_mst_repository import ServicesMstRepository
from app.repository.variable_mst_repository import VariableMstRepository
from app.schemas.variable_clone_schemas import (
    CloneEnvironmentOption,
    CloneRecommendation,
    CloneRegionOption,
    CloneSourceOptionsResponse,
    CloneSourceServiceItem,
    CloneSourceServicesRequest,
    CloneSourceServicesResponse,
    CloneSourceVariableItem,
    CloneSourceVariablesResponse,
)

logger = logging.getLogger(__name__)


class VariableCloneService:
    """Service layer for the clone source-discovery endpoints."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.services_repo = ServicesMstRepository(db)
        self.config_repo = ServiceConfigRepository(db)
        self.variable_repo = VariableMstRepository(db)

    # ==================== SOURCE SERVICES ====================

    async def get_source_services(
        self,
        request: CloneSourceServicesRequest,
        tenant_code: str,
        allowed_service_codes: Optional[List[str]],
    ) -> CloneSourceServicesResponse:
        if request.restrict_infra_type_ref_code:
            # Settings clone: only services holding a config of the target's infra
            # type are valid sources. Filtered + counted in SQL so paging is correct,
            # and every returned row is clonable (has_source_configs implicitly True).
            result = await self.config_repo.get_clonable_source_services(
                tenant_code=tenant_code,
                infra_type_ref_code=request.restrict_infra_type_ref_code,
                search_query=request.search_query,
                skip=request.skip,
                limit=request.limit,
                allowed_service_codes=allowed_service_codes,
            )
            services = [
                CloneSourceServiceItem(
                    service_code=s["service_code"],
                    service_name=s["service_name"],
                    application_code=s["application_code"],
                    application_name=s["application_name"],
                    has_source_configs=True,
                )
                for s in result["services"]
            ]
        else:
            result = await self.services_repo.get_all_services(
                tenant_code=tenant_code,
                search_query=request.search_query,
                skip=request.skip,
                limit=request.limit,
                allowed_service_codes=allowed_service_codes,
            )

            page_codes = [s["service_code"] for s in result["services"]]
            clonable_codes = await self.config_repo.get_service_codes_having_configs(
                tenant_code, page_codes
            )
            services = [
                CloneSourceServiceItem(
                    service_code=s["service_code"],
                    service_name=s["service_name"],
                    application_code=s["application_code"],
                    application_name=s["application_name"],
                    has_source_configs=s["service_code"] in clonable_codes,
                )
                for s in result["services"]
            ]

        recommendation: Optional[CloneRecommendation] = None
        if request.include_recommendation:
            recommendation = await self._build_recommendation(request, tenant_code)

        return CloneSourceServicesResponse(
            total=result["total"],
            skip=request.skip,
            limit=request.limit,
            services=services,
            recommendation=recommendation,
        )

    async def _build_recommendation(
        self,
        request: CloneSourceServicesRequest,
        tenant_code: str,
    ) -> Optional[CloneRecommendation]:
        """Suggest cloning from the target service itself (another env/region).
        No fallback to other services — if the target has no other source
        combo, there is no recommendation."""
        target = await self.services_repo.get_by_code(request.target_service_code)
        if not target or target.is_deleted or target.tenants_mst_code != tenant_code:
            raise HTTPException(status_code=404, detail="Target service not found")

        environments = await self._build_env_options(
            tenant_code=tenant_code,
            service_code=target.code,
            target_environment=request.target_environment,
            target_geo_loc_code=request.target_geo_loc_code,
            exclude_target_combo=True,
            restrict_infra_type_ref_code=request.restrict_infra_type_ref_code,
        )
        if not environments:
            return None

        app_name = target.application.name if target.application else target.applications_mst_code
        best_env = next((e for e in environments if e.suggested), environments[0])
        best_region = next((r for r in best_env.regions if r.suggested), None)
        return CloneRecommendation(
            service=CloneSourceServiceItem(
                service_code=target.code,
                service_name=target.name,
                application_code=target.applications_mst_code,
                application_name=app_name,
                has_source_configs=True,
            ),
            environment=best_env.environment,
            geo_loc_code=best_region.geo_loc_code if best_region else None,
            environments=environments,
        )

    # ==================== SOURCE OPTIONS ====================

    async def get_source_options(
        self,
        service_code: str,
        target_environment: EnvironmentEnum,
        target_geo_loc_code: Optional[str],
        target_service_code: Optional[str],
        tenant_code: str,
        restrict_infra_type_ref_code: Optional[str] = None,
    ) -> CloneSourceOptionsResponse:
        service = await self.services_repo.get_by_code(service_code)
        if not service or service.is_deleted or service.tenants_mst_code != tenant_code:
            raise HTTPException(status_code=404, detail="Service not found")

        environments = await self._build_env_options(
            tenant_code=tenant_code,
            service_code=service_code,
            target_environment=target_environment,
            target_geo_loc_code=target_geo_loc_code,
            exclude_target_combo=target_service_code == service_code,
            restrict_infra_type_ref_code=restrict_infra_type_ref_code,
        )
        return CloneSourceOptionsResponse(
            service_code=service_code,
            environments=environments,
        )

    async def _build_env_options(
        self,
        tenant_code: str,
        service_code: str,
        target_environment: EnvironmentEnum,
        target_geo_loc_code: Optional[str],
        exclude_target_combo: bool,
        restrict_infra_type_ref_code: Optional[str] = None,
    ) -> List[CloneEnvironmentOption]:
        combos = await self.config_repo.get_env_geo_options_for_service(
            tenant_code, service_code
        )
        # Dev configs are never offered as clone sources.
        combos = [c for c in combos if c["environment"] != EnvironmentEnum.dev]
        # Geo allowlist (aspora/vance): a combo is only offered if its region is
        # allowed for its environment. Same source of truth as the source-list
        # SQL predicate, so the picker list and the env dropdown stay in sync.
        if tenant_code in CLONE_GEO_ALLOWLIST_TENANTS:
            combos = [
                c for c in combos
                if c["environment"] not in CLONE_ENV_GEO_ALLOWLIST
                or c["geo_loc_code"] in CLONE_ENV_GEO_ALLOWLIST[c["environment"]]
            ]
        if restrict_infra_type_ref_code:
            # Settings clone: only combos of the target's infra type are valid
            # sources. Filter before grouping so suggested/auto_select are computed
            # on the surviving combos.
            combos = [
                c for c in combos
                if c.get("infrastructuretype_ref_code") == restrict_infra_type_ref_code
            ]
        if exclude_target_combo:
            # Without a target region we can't tell which combo the variables
            # would land on, so the whole target environment is off the table —
            # otherwise the recommendation could point back at the target.
            if target_geo_loc_code:
                combos = [
                    c for c in combos
                    if not (
                        c["environment"] == target_environment
                        and c["geo_loc_code"] == target_geo_loc_code
                    )
                ]
            else:
                combos = [
                    c for c in combos if c["environment"] != target_environment
                ]

        by_env: Dict[EnvironmentEnum, List[Dict[str, Any]]] = {}
        for combo in combos:
            by_env.setdefault(combo["environment"], []).append(combo)
        if not by_env:
            return []

        envs = sort_environments(list(by_env.keys()))
        suggested_env = pick_environment(target_environment, envs)

        options: List[CloneEnvironmentOption] = []
        for env in envs:
            env_combos = sorted(
                by_env[env],
                key=lambda c: (c["geo_loc_name"].lower(), (c.get("cluster_name") or "").lower()),
            )
            suggested_geo = pick_geo_loc(
                target_geo_loc_code, [c["geo_loc_code"] for c in env_combos]
            )
            # A geo can now carry several configs (one per cluster), so mark only
            # the FIRST config of the suggested geo as the default — otherwise the
            # UI would see multiple "suggested" regions.
            regions: List[CloneRegionOption] = []
            suggested_taken = False
            for c in env_combos:
                is_suggested = not suggested_taken and c["geo_loc_code"] == suggested_geo
                if is_suggested:
                    suggested_taken = True
                # Actual AWS cluster name from the infra locator (same derivation
                # as the infrastructure list API), falling back to the infra's
                # display name for clusters with no locator.
                infra_locator = c.get("infra_locator") or {}
                cluster_name = (
                    infra_locator.get("cluster")
                    or infra_locator.get("cluster_name")
                    or c.get("cluster_name")
                )
                regions.append(
                    CloneRegionOption(
                        geo_loc_code=c["geo_loc_code"],
                        geo_loc_name=c["geo_loc_name"],
                        cloud_region=resolve_cloud_region(c["infra_vendor_enum"], c["config"]),
                        config_code=c.get("config_code"),
                        cluster_code=c.get("infrastructure_mst_code"),
                        cluster_name=cluster_name,
                        suggested=is_suggested,
                        auto_select=len(env_combos) == 1,
                    )
                )
            options.append(
                CloneEnvironmentOption(
                    environment=env,
                    suggested=env == suggested_env,
                    auto_select=len(envs) == 1,
                    regions=regions,
                )
            )
        return options

    # ==================== SOURCE VARIABLES ====================

    async def get_source_variables(
        self,
        config_code: str,
        tenant_code: str,
    ) -> CloneSourceVariablesResponse:
        """Deployed variables of a source service_config, for the clone picker.

        Only deployed rows are listed: the clone execute API reads the latest
        deployed value from the audit bucket, and un-deployed drafts are
        private to their author."""
        config = await self.config_repo.get_by_code_and_tenant(config_code, tenant_code)
        if not config:
            raise HTTPException(status_code=404, detail="Source resource not found")

        rows = await self.variable_repo.get_all_by_transaction(
            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
            transaction_code=config_code,
            environment=config.environment,
        )
        # Write-only keys are excluded outright: their value is never readable,
        # so offering them would only produce an empty clone. The clone execute
        # API rejects them too, so this is UX, not the guard.
        active = [
            r for r in rows
            if not r.is_deleted and r.variable_cloud_identifier and not r.is_write_only
        ]

        # Live AWS value per row (same read the Env listing uses). Plain vars
        # show their value; secrets are masked (value stays None) so no
        # plaintext secret is sent to the picker.
        from app.services.project_variables_service import ProjectVariablesService

        pv = ProjectVariablesService(self.db)
        auth_config = None
        app_code = next(
            ((r.metadata_json or {}).get("application_code") for r in active
             if (r.metadata_json or {}).get("application_code")),
            None,
        )
        try:
            auth_config = await pv._get_auth_config(
                application_code=app_code,
                environment=config.environment,
                tenant_code=tenant_code,
            )
        except Exception as exc:
            logger.warning("clone source-variables: auth resolve failed: %s", exc)

        sm_cache: dict = {}
        variables = []
        for r in active:
            is_secret = (
                (r.variable_type.value or "").upper() == "SECRET"
                if r.variable_type else False
            )
            value = None
            if auth_config is not None:
                try:
                    resolved = await pv._resolve_single_value(r, auth_config, sm_cache)
                    value = resolved or None
                except Exception as exc:
                    logger.info("clone source-variables: value read failed for '%s': %s", r.key, exc)
            variables.append(
                CloneSourceVariableItem(key=r.key, secret=is_secret, value=value)
            )
        variables.sort(key=lambda v: v.key.lower())
        return CloneSourceVariablesResponse(
            config_code=config_code,
            variables=variables,
        )
