"""
Placement Parameter API Endpoints

Merged dropdown payload combining applications (as products), environments and
geographic locations in a single nested response — saves a caller from making
multiple round-trips when populating placement selectors.

The tree is built from `infrastructure_mst`: only combinations that actually
have infrastructure are offered. It used to be a cartesian product of every
product, a hardcoded environment list and every geo location the tenant owned,
which let a user pick a placement with no cluster behind it and only discover
it when provisioning failed.
"""
from typing import Optional, Tuple
import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.config import settings
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.schemas.placement_parameter_schemas import (
    PlacementEnvironment,
    PlacementGeoLocation,
    PlacementOptionsRequest,
    PlacementParametersResponse,
    PlacementProduct,
)
from app.services.applications_mst_service import ApplicationsMstService
from app.services.geo_loc_mst_service import GeoLocMstService


# Display names for the environment enum. A LABEL LOOKUP ONLY — the set of
# environments offered comes from the infrastructure that exists, never from
# this dict.
_ENVIRONMENT_LABELS = {
    "dev": "Dev",
    "stage": "Stage",
    "qa": "QA",
    "prod": "Prod",
}

# The order environments should appear in, least to most production-like.
_ENVIRONMENT_ORDER = ["dev", "stage", "qa", "prod"]


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/get-placement-options",
    response_model=PlacementParametersResponse,
    summary="Get Merged Placement Parameters",
)
async def get_placement_options(
    payload: Optional[PlacementOptionsRequest] = Body(None),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> PlacementParametersResponse:
    """
    Merged payload of the tenant's applications (as products) with the
    environments and geographic locations that actually have infrastructure —
    suitable for populating placement-parameter selectors in a single request.

    Pass `infra_type` to narrow the tree to one infrastructure type, so a form
    that creates an EKS service is only offered placements with an EKS cluster.

    Security:
        - JWT authentication required
        - Tenant isolation enforced via JWT
        - Products are further limited to the user's workspaces
    """
    try:
        user, tenant = user_and_tenant
        logger.info(
            "GET PLACEMENT OPTIONS - Tenant isolation check",
            extra={
                "user_code": user.code,
                "user_email": user.email_id,
                "tenant_code": tenant.code,
                "tenant_name": tenant.name,
                "tenant_subdomain": tenant.subdomain,
                "endpoint": "/get-placement-options",
            },
        )

        infra_type = payload.infra_type if payload else None

        applications_service = ApplicationsMstService(db)
        geo_loc_service = GeoLocMstService(db)

        # Products the USER may see (workspace-scoped), and the geo names.
        application_options = await applications_service.get_applications_dropdown(
            tenant_code=tenant.code,
            user_code=user.code,
        )
        geo_loc_options = await geo_loc_service.get_geo_locs_dropdown(tenant.code)
        geo_names = {geo["value"]: geo["label"] for geo in geo_loc_options}
        product_names = {app["value"]: app["label"] for app in application_options}

        # Placements that actually EXIST, narrowed to what the user can see.
        infra_repo = InfrastructureMstRepository(db)
        placements = await infra_repo.get_placement_tree(
            tenant_code=tenant.code,
            infrastructuretype_ref_code=infra_type,
            application_codes=list(product_names.keys()) or None,
        )

        # What THIS deployment offers, on top of what exists. One database
        # holds every environment, so a stage deployment's placements
        # legitimately include prod — the clusters and services really are
        # there. Same per-deployment narrowing the dashboard applies with
        # NEXT_PUBLIC_ALLOWED_ENVS; unset means no restriction.
        allowed_envs = settings.allowed_env_codes
        withheld_by_policy = 0

        # app_code -> env -> [geo_code], preserving discovery order per level.
        tree: dict[str, dict[str, list[str]]] = {}
        for row in placements:
            app_code = row["applications_mst_code"]
            env = row["environment"]
            geo_code = row["geo_loc_mst_code"]
            if not settings.is_env_allowed(env):
                withheld_by_policy += 1
                continue
            # A geo location the tenant no longer lists cannot be labelled, and
            # would show as a bare code — leave it out.
            if geo_code not in geo_names:
                continue
            geos = tree.setdefault(app_code, {}).setdefault(env, [])
            if geo_code not in geos:
                geos.append(geo_code)

        products = []
        for app_code, envs_for_app in tree.items():
            environments = [
                PlacementEnvironment(
                    environment_enum=env,
                    environment_label=_ENVIRONMENT_LABELS.get(env, env.title()),
                    geo_locations=[
                        PlacementGeoLocation(
                            geo_loc_mst_code=code,
                            geo_loc_name=geo_names[code],
                        )
                        for code in sorted(envs_for_app[env], key=lambda c: geo_names[c])
                    ],
                )
                for env in sorted(
                    envs_for_app,
                    key=lambda e: (
                        _ENVIRONMENT_ORDER.index(e)
                        if e in _ENVIRONMENT_ORDER
                        else len(_ENVIRONMENT_ORDER)
                    ),
                )
            ]
            products.append(
                PlacementProduct(
                    applications_mst_code=app_code,
                    product_code=app_code,
                    product_name=product_names.get(app_code, app_code),
                    environments=environments,
                )
            )
        products.sort(key=lambda p: p.product_name.lower())

        if not products:
            logger.warning(
                "No placement options for tenant %s (infra_type=%s): %s product(s) "
                "visible to the user, %s placement row(s) with infrastructure, %s "
                "withheld by ALLOWED_ENVIRONMENTS=%s. An empty dropdown usually "
                "means no registered infrastructure of that type, applications not "
                "mapped to the user's workspace, or an allowlist that excludes "
                "every environment this deployment can actually reach.",
                tenant.code, infra_type, len(application_options), len(placements),
                withheld_by_policy, allowed_envs or "(unset)",
            )
        elif withheld_by_policy:
            logger.info(
                "Withheld %s placement(s) for tenant %s: this deployment serves "
                "ALLOWED_ENVIRONMENTS=%s",
                withheld_by_policy, tenant.code, allowed_envs,
            )
        else:
            logger.info(
                "Successfully built placement options for tenant %s",
                tenant.code,
                extra={
                    "tenant_code": tenant.code,
                    "infra_type": infra_type,
                    "product_count": len(products),
                    "placement_count": len(placements),
                    "visible_application_count": len(application_options),
                },
            )

        return PlacementParametersResponse(
            tenant_code=tenant.code,
            products=products,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in get_placement_options: {str(e)}",
            exc_info=True,
            extra={"tenant_code": tenant.code if "tenant" in locals() else None},
        )
        raise HTTPException(status_code=500, detail=str(e))
