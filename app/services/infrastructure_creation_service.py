"""
Infrastructure Creation Service

Creates infrastructure resources in infrastructure_mst.
Kong routes write a different table and live in KongRouteConfigService;
create_resource() still accepts them and delegates, so callers keep one door.
"""
import logging
import re
from typing import Any, Dict, Optional
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.core.enum import DeploymentStatusEnum, EnvironmentEnum, InfraVendorEnum, WorkflowSourceTableEnum
from app.services.workspace_service import WorkspaceService
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.services.kong_route_config_service import KONG_INFRA_TYPE_REFS
from app.domain.validators import infra_config_validator as infra_config
from app.domain.validators import infrastructure_update_guard as update_guard
from app.repository.services_mst_repository import ServicesMstRepository
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.repository.resource_group_mst_repository import ResourceGroupMstRepository
from app.repository.geo_loc_mst_repository import GeoLocMstRepository
from app.repository.infrastructuretype_ref_repository import InfrastructureTypeRefRepository
from app.repository.infra_vendor_accounts_mst_repository import InfraVendorAccountsMstRepository
from app.schemas.infrastructure_schemas import InfrastructureCreateRequest, InfrastructureCreateResponse
from app.domain.factories.infrastructure_mst_factory import (
    make_infrastructure_mst_s3,
    make_infrastructure_mst_sqs,
    make_infrastructure_mst_dynamodb,
    make_infrastructure_mst_k8s_postgres,
    make_infrastructure_mst_redis,
    make_infrastructure_mst_aurora_postgres,
    make_infrastructure_mst_aurora_mysql,
)

logger = logging.getLogger(__name__)


# Business/deployment region code to AWS region. Module level so the request
# validator can tell whether a supplied `region` contradicts the geo code, using
# the same table the derivation uses.
_AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-\d$")

_GEO_LOC_TO_AWS_REGION = {
    "mumbai": "ap-south-1",
    "london": "eu-west-2",
    "uk": "eu-west-2",
    "us": "us-east-1",
    "aspora-mumbai": "ap-south-1",
    "aspora-london": "eu-west-2",
    "aspora-uk": "eu-west-2",
    "aspora-us": "us-east-1",
    "region-aspora-mumbai": "ap-south-1",
    "region-aspora-london": "eu-west-2",
    "region-aspora-us": "us-east-1",
}


class InfrastructureCreationService:
    """
    Creates infrastructure resources in infrastructure_mst.

    create_resource() still accepts kong requests and delegates them to
    KongRouteConfigService, which owns kong_route_configs — so callers keep a
    single door. Which table actually got written is on the response:
    - kong type ref  → KongRouteConfigService  → table_name=KONG_ROUTE, KRC_ code
    - anything else  → here                    → table_name=INFRASTRUCTURE, INFRA_ code

    Callers recording that code on a transaction_queue row must key the row from
    response.table_name, never a hardcoded enum: a KRC_ code stamped
    INFRASTRUCTURE matches no gateway join and disappears from the Gateway tab.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.infrastructure_repo = InfrastructureMstRepository(db)
        self.services_repo = ServicesMstRepository(db)
        self.infra_type_repo = InfrastructureTypeRefRepository(db)
        self.infra_vendor_account_repo = InfraVendorAccountsMstRepository(db)
        self.applications_repo = ApplicationsMstRepository(db)
        self.resource_group_repo = ResourceGroupMstRepository(db)
        self.geo_loc_repo = GeoLocMstRepository(db)

    @staticmethod
    def _get_aws_region_from_geo_loc(geo_loc: str) -> str:
        """
        Map business/deployment region (geo_loc) to AWS region.

        Args:
            geo_loc: Geographic location code (e.g., 'mumbai', 'london', 'region-aspora-mumbai')

        Returns:
            AWS region code (e.g., 'ap-south-1', 'eu-west-2')
        """
        return _GEO_LOC_TO_AWS_REGION.get(geo_loc.lower(), "ap-south-1")

    async def create_resource(
        self,
        tenant_code: str,
        user_code: str,
        request: InfrastructureCreateRequest,
        user_email: str
    ) -> InfrastructureCreateResponse:
        """
        Unified entry point for upsert operations - routes based on infrastructuretype_ref_code.

        Supports UPSERT:
        - If request.code is provided: Updates existing record
        - If request.code is None: Creates new record

        Args:
            tenant_code: Tenant code from JWT
            request: Infrastructure creation/update request
            user_email: User email for tracking

        Returns:
            InfrastructureCreateResponse with table_name and code

        Raises:
            HTTPException: Various validation and creation errors
        """
        is_update = request.code is not None
        logger.info(f"{'Updating' if is_update else 'Creating'} resource: infrastructuretype_ref_code={request.infrastructuretype_ref_code}, tenant={tenant_code}, code={request.code}")

        # Workspace access guard
        if request.application_code:
            workspace_svc = WorkspaceService(self.db)
            if not await workspace_svc.verify_app_workspace_access(user_code, tenant_code, request.application_code):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: application workspace is not accessible"
                )
            from app.services.applications_mst_service import ApplicationsMstService
            app_svc = ApplicationsMstService(self.db)
            if not await app_svc.can_write_for_app(user_code, tenant_code, request.application_code):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: read-only role cannot create infrastructure"
                )

        # Route based on infrastructuretype_ref_code. Kong writes a different
        # table (kong_route_configs, a KRC code) and lives in its own service;
        # the response's table_name is what tells callers which one they got.
        if request.infrastructuretype_ref_code in KONG_INFRA_TYPE_REFS:
            from app.services.kong_route_config_service import KongRouteConfigService
            return await KongRouteConfigService(self.db).create_route_from_infrastructure_request(
                tenant_code, request, user_email
            )
        else:
            return await self._create_infrastructure_resource(tenant_code, request, user_email)

    async def _create_infrastructure_resource(
        self,
        tenant_code: str,
        request: InfrastructureCreateRequest,
        user_email: str
    ) -> InfrastructureCreateResponse:
        """
        Create or update infrastructure resource (S3, SQS, DynamoDB, etc.).

        Saves to infrastructure_mst table.
        - If request.code is provided: Updates existing record
        - If request.code is None: Creates new record
        """
        # Check if this is an update (code provided) or create (no code)
        is_update = request.code is not None

        if is_update:
            logger.info(f"Updating infrastructure: code={request.code}, type={request.infrastructuretype_ref_code}")
        else:
            logger.info(f"Creating infrastructure: type={request.infrastructuretype_ref_code}")

        # 1. Validate infrastructure type exists
        infra_type = await self.infra_type_repo.get_by_code(request.infrastructuretype_ref_code)
        if not infra_type:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Infrastructure type not found: {request.infrastructuretype_ref_code}"
            )

        # 2. Determine vendor from infrastructure type
        infra_type_code = request.infrastructuretype_ref_code
        logger.info(f"Infrastructure type code: {infra_type_code}")

        # 2a. Validate the supplied configuration values. On an update the row
        # has to be read first — a name the request is not changing is exempt,
        # so rows holding a name these rules would now refuse stay saveable —
        # so that call lives in the update branch below.
        #
        # require_all is False on create too: the canvas writes the row when a
        # node is dropped, before the user has named anything, so that first
        # request legitimately carries only placement fields. What matters here
        # is that any value actually SENT is well formed. Presence is enforced
        # by the form's required-field gate and by the terragrunt generation
        # step.
        if not is_update:
            self._validate_config_or_400(request, infra_type_code)

        # 2b. The placement codes are foreign keys the factory copies onto the
        # row without checking. An application code from another tenant would
        # otherwise attach the resource across the isolation boundary, and an
        # unknown geo code would fall through _get_aws_region_from_geo_loc's
        # default and silently build the resource in Mumbai.
        await self._validate_placement_references(tenant_code, request)

        # 3. Resolve infra_vendor_accounts_mst_code via hierarchical lookup
        #    Priority: resource_group > application > tenant (fallback)
        vendor_enum = infra_type.infra_vendor_enum if hasattr(infra_type, 'infra_vendor_enum') else InfraVendorEnum.aws
        vendor_account = await self.infra_vendor_account_repo.get_by_hierarchy(
            infra_vendor_enum=vendor_enum,
            environments_enum=request.environment,
            resource_group_code=request.resource_group_mst_code,
            application_code=request.application_code,
            tenant_code=tenant_code
        )
        if not vendor_account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No vendor account found for vendor={vendor_enum.value}, tenant={tenant_code}, environment={request.environment.value}"
            )
        infra_vendor_accounts_code = vendor_account.code
        logger.info(f"Using vendor account code: {infra_vendor_accounts_code} (resolved via hierarchy)")

        # 4. Validate service if provided (optional service association)
        if request.service_mst_code:
            service = await self.services_repo.get_by_code(request.service_mst_code)
            if not service:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Service not found: {request.service_mst_code}"
                )
            if service.tenants_mst_code != tenant_code:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Service belongs to different tenant"
                )

        # 5. For CREATE: refuse a name another resource in the same placement
        # already holds. The update path runs the same check further down, but
        # only when the name actually changed — an update that resends its own
        # name would otherwise find itself.
        submitted_identifier = infra_config.submitted_name(
            request.type_specific_config, infra_type_code,
        )
        if not is_update and submitted_identifier:
            await self._check_infrastructure_duplicate(
                infra_type_code=infra_type_code,
                identifier=submitted_identifier,
                tenant_code=tenant_code,
                environment=request.environment,
                application_code=request.application_code,
                geo_loc_mst_code=request.geo_loc_mst_code,
            )

        # 6. Route to appropriate factory based on type. On an update the
        # effective config may differ from the request (see the name seeding
        # below), so the update branch rebuilds this from that config.
        factory_data = self._build_infrastructure_factory_data(
            infra_type_code=infra_type_code,
            tenant_code=tenant_code,
            request=request,
            infra_vendor_accounts_code=infra_vendor_accounts_code,
            user_email=user_email
        )

        # 6a. The resolved AWS name is only known once the factory has built it,
        # so the length check lives here rather than with the other rules. A
        # typed name inside the AWS limit can still overflow it after the
        # tenant/environment/region prefix goes on.
        try:
            infra_config.validate_resolved_names(
                factory_data.get("locator"), infra_type_code,
            )
        except infra_config.InfraConfigError as exc:
            raise infra_config.as_http_error(exc)

        # 7. Create or Update in database
        if is_update:
            # UPDATE: Fetch existing record and update it
            infrastructure = await self.infrastructure_repo.get_by_code(request.code)
            if not infrastructure:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Infrastructure not found with code: {request.code}"
                )

            # Verify tenant ownership
            if infrastructure.tenants_mst_code != tenant_code:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Infrastructure belongs to different tenant"
                )

            # Product, environment, region, resource type and (once deployed)
            # the name are the resource's ADDRESS: the terragrunt path is built
            # from them, so changing one writes a new file and orphans the old,
            # leaving the original resource running and creating a second. The
            # web hides these behind a disabled input; that is not a guard, and
            # a direct call to this endpoint had nothing stopping it.
            try:
                update_guard.guard_update(request, infrastructure)
            except update_guard.InfrastructureUpdateError as exc:
                raise update_guard.as_http_error(exc)

            stored_identifier = infra_config.stored_name(
                infrastructure.locator, infra_type_code,
            )

            self._validate_config_or_400(
                request, infra_type_code, existing_name=stored_identifier,
            )

            # An update that says nothing about the name must not erase it. The
            # factories default a missing name to "" and rebuild every derived
            # value from that, so an ordinary settings save on a deployed bucket
            # would blank the identifier and leave the ARN a stub — the locator
            # merge cannot rescue it, because "" is a value and wins.
            if submitted_identifier is None and stored_identifier:
                effective_request = request.model_copy(update={
                    "type_specific_config": infra_config.with_name(
                        request.type_specific_config, infra_type_code, stored_identifier,
                    )
                })
                factory_data = self._build_infrastructure_factory_data(
                    infra_type_code=infra_type_code,
                    tenant_code=tenant_code,
                    request=effective_request,
                    infra_vendor_accounts_code=infra_vendor_accounts_code,
                    user_email=user_email,
                )

            # A rename that got past the guard is a draft being named. It still
            # may not take a name a sibling already holds, so the create-side
            # uniqueness contract applies here too — with this row excluded.
            if submitted_identifier and submitted_identifier != stored_identifier:
                await self._check_infrastructure_duplicate(
                    infra_type_code=infra_type_code,
                    identifier=submitted_identifier,
                    tenant_code=tenant_code,
                    environment=request.environment,
                    application_code=request.application_code,
                    geo_loc_mst_code=request.geo_loc_mst_code,
                    exclude_code=request.code,
                )

            # Exclude code — factory always generates a new one, but on update it must stay unchanged
            factory_data.pop("code", None)

            # The factory fills these for a CREATE — a null workflow id, a null
            # ARN and INITIATED. BaseRepository.update writes every key it is
            # given, nulls included, so passing them through clears the real ARN
            # the Jenkins webhook wrote back, breaks the workflow link, and puts
            # a live resource back to INITIATED. Dropping them leaves the
            # columns untouched, which is what an update that says nothing about
            # them should do.
            factory_data = update_guard.strip_preserved_columns(factory_data)

            # resource_group_mst_code is not in that set because it IS settable
            # on an update — but only when the caller actually sent one. The web
            # omits it entirely, so writing the factory's None would silently
            # unlink every resource from its group on an ordinary settings save.
            if request.resource_group_mst_code is None:
                factory_data.pop("resource_group_mst_code", None)

            # Merge locator: preserve existing keys (e.g. ARNs written back by provisioning)
            # while applying the new values from the request — same pattern as service config
            if "locator" in factory_data:
                factory_data["locator"] = {**(infrastructure.locator or {}), **factory_data["locator"]}

            updated_infrastructure = await self.infrastructure_repo.update(
                db_obj=infrastructure,
                updates=factory_data
            )
            await self._commit_or_400()
            await self.db.refresh(updated_infrastructure)

            logger.info(f"Updated infrastructure: code={updated_infrastructure.code}, id={updated_infrastructure.id}")
            infrastructure = updated_infrastructure
        else:
            # CREATE: Create new record
            infrastructure = await self.infrastructure_repo.create(**factory_data)
            await self._commit_or_400()
            await self.db.refresh(infrastructure)

            logger.info(f"Created infrastructure: code={infrastructure.code}, id={infrastructure.id}")

        # 8. Build response
        return InfrastructureCreateResponse(
            table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
            code=infrastructure.code
        )

    async def _check_infrastructure_duplicate(
        self,
        infra_type_code: str,
        identifier: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        application_code: str,
        geo_loc_mst_code: str,
        exclude_code: Optional[str] = None,
    ):
        """Check for duplicate infrastructure resources.

        Uniqueness contract enforced here:
            (tenant, application, environment, geo_loc, resource_name)

        This MUST stay in sync with the pre-create validators in
        `app.services.aws_ops.{s3,sqs,dynamodb}_ops` — both code paths must
        agree on what counts as a duplicate, otherwise the validator can
        return `valid=true` for a name the create flow will then reject.

        Every type the factory routes is covered, not just the three with a
        hand-written helper: redis, aurora and k8s postgres could previously be
        created twice in the same place under the same name, which produces two
        terragrunt files with the same path and a deploy that overwrites one
        with the other. Each type is matched on its OWN name key — aurora stores
        `db_server_name`, k8s postgres `server_name` — so a type whose name key
        is unknown is skipped rather than compared against the wrong field.
        """
        name_key = infra_config.name_key_for(infra_type_code)
        if not name_key or not identifier:
            return

        existing = await self.infrastructure_repo.check_identifier_exists(
            infrastructuretype_ref_code=infra_type_code,
            name_key=name_key,
            name_value=identifier,
            tenant_code=tenant_code,
            environment=environment,
            application_code=application_code,
            geo_loc_mst_code=geo_loc_mst_code,
            exclude_code=exclude_code,
        )

        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Resource '{identifier}' already exists for application "
                    f"'{application_code}' in {environment.value} ({geo_loc_mst_code}) "
                    f"for {infra_type_code}"
                ),
            )

    async def _commit_or_400(self) -> None:
        """Turn a foreign-key violation into a 400 naming the bad code.

        The placement codes are only warned about when this service cannot find
        them — the Slack path sends a product's display name when it has no
        application code, and refusing that outright breaks a working flow. The
        database still refuses them, and without this the caller gets a bare 500
        with the real cause buried in the logs.
        """
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("Infrastructure write rejected by the database: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "One of the placement codes on this request does not exist: "
                    "check application_code, geo_loc_mst_code and "
                    "resource_group_mst_code."
                ),
            )

    def _validate_config_or_400(
        self,
        request: InfrastructureCreateRequest,
        infra_type_code: str,
        existing_name: Optional[str] = None,
    ) -> None:
        try:
            infra_config.validate_config(
                request.type_specific_config,
                infra_type_code,
                require_all=False,
                existing_name=existing_name,
            )
        except infra_config.InfraConfigError as exc:
            raise infra_config.as_http_error(exc)

    async def _validate_placement_references(
        self,
        tenant_code: str,
        request: InfrastructureCreateRequest,
    ) -> None:
        """Refuse a placement code that belongs to ANOTHER tenant.

        Scoped deliberately to that one question. A code this service cannot
        find is logged and allowed through: the Slack builder falls back to the
        product's display name when it has no application code
        (infrastructure_request_builder.py:158-161), and refusing there would
        turn a working chat flow into a hard 404 for a value that has never been
        checked before. Cross-tenant attachment is the part that has to fail —
        the row would carry the caller's tenant while pointing at someone
        else's product.

        The geo location is checked for existence only. `geo_loc_mst.code` is
        globally unique with a single owning tenant, so tenants that share a
        region code would all fail an ownership check on every create.
        """
        application = await self.applications_repo.get_by_code(request.application_code)
        if application is None:
            logger.warning(
                "Application code not found, allowing: %s", request.application_code,
            )
        elif application.tenants_mst_code != tenant_code:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Application belongs to different tenant",
            )

        geo_loc = await self.geo_loc_repo.get_by_code(request.geo_loc_mst_code)
        if geo_loc is None:
            logger.warning(
                "Geo location code not found, allowing: %s", request.geo_loc_mst_code,
            )

        if request.resource_group_mst_code:
            resource_group = await self.resource_group_repo.get_by_code(
                request.resource_group_mst_code
            )
            if resource_group is None:
                logger.warning(
                    "Resource group code not found, allowing: %s",
                    request.resource_group_mst_code,
                )
            elif resource_group.tenants_mst_code != tenant_code:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Resource group belongs to different tenant",
                )

        self._check_region_matches_geo_loc(request)

    @staticmethod
    def _check_region_matches_geo_loc(request: InfrastructureCreateRequest) -> None:
        """A supplied region may not contradict the geo location.

        Both end up on the row — the geo code decides the terragrunt path, the
        region decides where AWS builds — so a request carrying two different
        answers has no correct outcome.

        The chat and Slack flows send a BUSINESS region here ("mumbai"), not an
        AWS one: the tool schema documents it that way and the request builder
        copies it straight into type_specific_config
        (infrastructure_request_builder.py:181-184). Both sides are resolved
        through the same map first, so "mumbai" and "region-aspora-mumbai"
        agree instead of colliding. A value the map does not know is left alone
        — there is nothing to compare it against.
        """
        supplied = (request.type_specific_config or {}).get("region")
        supplied = str(supplied).strip() if supplied is not None else ""
        if not supplied:
            return

        geo_loc = (request.geo_loc_mst_code or "").lower()
        if geo_loc not in _GEO_LOC_TO_AWS_REGION:
            return

        expected = _GEO_LOC_TO_AWS_REGION[geo_loc]
        resolved = _GEO_LOC_TO_AWS_REGION.get(supplied.lower(), supplied)
        if resolved == expected:
            return

        # Only an unambiguous AWS region can contradict; anything else is a
        # label this service has no table for.
        if not _AWS_REGION_PATTERN.match(resolved):
            return

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"region '{supplied}' does not match geo location "
                f"'{request.geo_loc_mst_code}', which is {expected}. Send one or "
                f"the other, not two different answers."
            ),
        )

    def _build_infrastructure_factory_data(
        self,
        infra_type_code: str,
        tenant_code: str,
        request: InfrastructureCreateRequest,
        infra_vendor_accounts_code: str,
        user_email: str
    ) -> Dict[str, Any]:
        """Route to appropriate factory function based on infrastructure type code."""

        config = request.type_specific_config

        # Derive region from geo_loc_mst_code if not provided in type_specific_config
        if "region" in config and config["region"]:
            region = config["region"]
        else:
            region = self._get_aws_region_from_geo_loc(request.geo_loc_mst_code)
            logger.info(f"Derived region '{region}' from geo_loc_mst_code '{request.geo_loc_mst_code}'")

        # Common parameters for all factories
        common_params = {
            "tenant_code": tenant_code,
            "application_code": request.application_code,
            "environment": request.environment,
            "infrastructuretype_ref_code": request.infrastructuretype_ref_code,
            "infra_vendor_accounts_mst_code": infra_vendor_accounts_code,
            "resource_group_mst_code": request.resource_group_mst_code,
            "geo_loc_mst_code": request.geo_loc_mst_code,
            "infra_status": DeploymentStatusEnum.INITIATED,
            "infra_status_updated_by": user_email
        }

        # Route to type-specific factory
        if infra_type_code == "s3_infrastructuretype_ref":
            return make_infrastructure_mst_s3(
                config=config,
                region=region,
                **common_params
            )
        elif infra_type_code == "sqs_infrastructuretype_ref":
            return make_infrastructure_mst_sqs(
                config=config,
                region=region,
                **common_params
            )
        elif infra_type_code == "dynamodb_infrastructuretype_ref":
            return make_infrastructure_mst_dynamodb(
                config=config,
                region=region,
                **common_params
            )
        elif infra_type_code == "devlift_k8s_postgres_infrastructuretype_ref":
            return make_infrastructure_mst_k8s_postgres(
                config=config,
                **common_params
            )
        elif infra_type_code == "elasticache_redis_infrastructuretype_ref":
            return make_infrastructure_mst_redis(
                config=config,
                region=region,
                **common_params
            )
        elif infra_type_code == "aurora_postgres_infrastructuretype_ref":
            return make_infrastructure_mst_aurora_postgres(
                config=config,
                region=region,
                **common_params
            )
        elif infra_type_code == "aurora_mysql_infrastructuretype_ref":
            return make_infrastructure_mst_aurora_mysql(
                config=config,
                region=region,
                **common_params
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Infrastructure type '{infra_type_code}' not yet supported."
            )
