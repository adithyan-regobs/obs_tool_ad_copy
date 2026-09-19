import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.core.enum import EnvironmentEnum, ServiceTypeEnum
from app.db.models.kong_route_config_model import KongRouteConfigModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.repository.kong_route_configs_repository import KongRouteConfigsRepository
from app.repository.service_config_repository import ServiceConfigRepository
from app.schemas.validator_response_schemas import ValidationResult

logger = logging.getLogger(__name__)


class KongRouteOps:
    def __init__(
        self,
        kong_route_repo: KongRouteConfigsRepository,
        service_config_repo: Optional[ServiceConfigRepository] = None,
    ):
        self.kong_route_repo = kong_route_repo
        self.service_config_repo = service_config_repo

    async def get_services(
        self,
        tenant_code: str,
        product_code: str,
        environment: EnvironmentEnum,
        geo_loc: str,
        page: Optional[int] = None,
        page_size: int = 20,
    ) -> List[Dict[str, str]]:
        """Return all public-facing API services for the given
        (tenant_code, product_code, environment, geo_loc) combination as a
        list of `{label, value}` dropdown options where `value` is the service
        code and `label` is the service name.

        Joins service_config with services_mst and filters by:
            - tenant_mst_code == tenant_code
            - applications_mst_code == product_code
            - environment == environment
            - geo_loc_mst_code == geo_loc
            - service_type == API
            - is_public_facing == True

        If `page` is provided (1-indexed), pagination is applied with the given
        `page_size`. If not, all matching services are returned.
        """
        logger.info(
            "Fetching public-facing API services",
            extra={
                "tenant_code": tenant_code,
                "product_code": product_code,
                "environment": getattr(environment, "value", environment),
                "geo_loc": geo_loc,
                "page": page,
                "page_size": page_size,
            },
        )

        if self.service_config_repo is None:
            logger.error(
                "get_services() called without service_config_repo",
                extra={"tenant_code": tenant_code},
            )
            raise ValueError(
                "service_config_repo is required to call get_services()"
            )

        filters = [
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServicesMstModel.tenants_mst_code == tenant_code,
            ServicesMstModel.applications_mst_code == product_code,
            ServiceConfigModel.environment == environment,
            ServiceConfigModel.geo_loc_mst_code == geo_loc,
            ServicesMstModel.service_type == ServiceTypeEnum.API,
            ServicesMstModel.is_public_facing == True,
            ServiceConfigModel.is_deleted == False,
            ServicesMstModel.is_deleted == False,
        ]
        skip = None
        limit = None
        if page is not None:
            skip = max(0, (page - 1) * page_size)
            limit = page_size
        logger.debug(
            f"Calling find_public_facing_services with skip={skip}, limit={limit}"
        )
        rows = await self.service_config_repo.find_public_facing_services(
            filters, skip=skip, limit=limit
        )
        logger.debug(f"Repository returned {len(rows)} raw rows before dedup")

        # Deduplicate by service code — a single service can have multiple
        # service_config rows (per env/geo/infra), but as a dropdown option it
        # should appear only once. Preserve insertion order so pagination /
        # ordering from the repo is respected.
        seen_codes: set = set()
        options: List[Dict[str, str]] = []
        for row in rows:
            code = row.get("service_code")
            if code is None or code in seen_codes:
                continue
            seen_codes.add(code)
            options.append({
                "label": row.get("service_name"),
                "value": code,
            })
        logger.info(
            f"Returning {len(options)} unique service options "
            f"(deduplicated from {len(rows)} rows) for tenant {tenant_code}"
        )
        return options

    async def duplicate_route_validator(
        self,
        tenant_code: str,
        api_name: str,
        http_method: str,
        route_path: str,
        services_code: Optional[str] = None,
    ) -> ValidationResult:
        """Check whether a Kong route with the given (api_name, http_method,
        route_path) — and optionally services_code — already exists for the
        given tenant.

        Returns a ValidationResult — see app.schemas.validator_response_schemas
        for the canonical format used by all validators.
        """
        logger.info(
            "Validating Kong route for duplicates",
            extra={
                "tenant_code": tenant_code,
                "api_name": api_name,
                "http_method": http_method,
                "route_path": route_path,
                "services_code": services_code,
            },
        )

        # Tenant isolation via subquery on services_mst (KongRouteConfigModel
        # has no direct tenant column).
        tenant_services_subq = select(ServicesMstModel.code).where(
            ServicesMstModel.tenants_mst_code == tenant_code
        )

        filters = [
            KongRouteConfigModel.api_name == api_name,
            KongRouteConfigModel.http_method == http_method,
            KongRouteConfigModel.route_path == route_path,
            KongRouteConfigModel.services_mst_code.in_(tenant_services_subq),
            KongRouteConfigModel.is_deleted == False,
        ]
        if services_code:
            filters.append(KongRouteConfigModel.services_mst_code == services_code)

        logger.debug(
            f"Querying kong_route_configs with {len(filters)} filter(s)"
        )
        existing = await self.kong_route_repo.get_kong_route_record(filters)

        if existing:
            logger.warning(
                "Duplicate Kong route detected",
                extra={
                    "tenant_code": tenant_code,
                    "api_name": api_name,
                    "http_method": http_method,
                    "route_path": route_path,
                    "services_code": services_code,
                    "existing_route_id": getattr(existing, "id", None),
                    "existing_route_code": getattr(existing, "code", None),
                },
            )
            scope = f" for service '{services_code}'" if services_code else ""
            return ValidationResult(
                type="duplicateValidation",
                description=(
                    f"Route '{http_method} {route_path}' on api '{api_name}'"
                    f"{scope} already exists."
                ),
                valid=False,
            )

        logger.info(
            "Kong route is available (no duplicate found)",
            extra={
                "tenant_code": tenant_code,
                "api_name": api_name,
                "http_method": http_method,
                "route_path": route_path,
                "services_code": services_code,
            },
        )
        return ValidationResult(
            type="duplicateValidation",
            description=(
                f"Route '{http_method} {route_path}' on api '{api_name}' is available."
            ),
            valid=True,
        )