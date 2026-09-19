"""
Service Configuration Repository
Handles data access operations for service_configs table
"""
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, or_, and_, cast, func, literal_column, Text
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import joinedload
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.models.geo_loc_mst_model import GeoLocMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.repository.base_repository import BaseRepository
from app.core.enum import ResourceStatusEnum, ResourceDeploymentStatusEnum, EnvironmentEnum


# Resource statuses that mean "the row exists but is no longer an active
# resource". List/discovery methods filter these out by default; single-row
# lookups by code intentionally still see them so admin/post-action paths
# can update soft-deleted rows.
_DELETED_STATUSES = (
    ResourceStatusEnum.SOFT_DELETED,
    ResourceStatusEnum.HARD_DELETED,
)

# Statuses representing any phase of the deletion lifecycle. Used to guard
# `bulk_update_status` so async webhook stage updates (e.g. Jenkins build
# progress: PROVISIONING → DEPLOYING → ONLINE) can't roll a row that's
# already in / past SOFT_DELETING back to an "active" state.
_DELETION_LIFECYCLE_STATUSES = (
    ResourceStatusEnum.SOFT_DELETING,
    ResourceStatusEnum.SOFT_DELETED,
    ResourceStatusEnum.HARD_DELETING,
    ResourceStatusEnum.HARD_DELETED,
)

# Clone geo allowlist for the aspora/vance tenants: per environment, the
# geo_loc_mst codes a clone SOURCE may live in. Mirrors the create/clone modal
# rules (stage/qa → Mumbai only; prod → Mumbai + London). Other tenants are
# unrestricted, and environments absent from this map (e.g. dev) are too.
# Public so the clone service layer (env-option filtering) shares this one
# source of truth with the SQL predicate below — no duplicated allowlist.
CLONE_GEO_ALLOWLIST_TENANTS = ("aspora", "vance")
CLONE_ENV_GEO_ALLOWLIST = {
    EnvironmentEnum.stage: ("region-aspora-mumbai",),
    EnvironmentEnum.qa: ("region-aspora-mumbai",),
    EnvironmentEnum.prod: ("region-aspora-mumbai", "region-aspora-london"),
}


def _clone_geo_allowlist_predicate(tenant_code: str):
    """SQLAlchemy predicate restricting a service_config's (environment, geo_loc)
    to the tenant's clone geo allowlist, or None when the tenant is unrestricted.

    A config passes if its environment is not in the allowlist (e.g. dev) OR its
    geo_loc is allowed for that environment. Used so a service whose only configs
    sit in disallowed regions is dropped from the clone picker entirely — never
    sent to the frontend — instead of listed with no selectable environment.
    """
    if tenant_code not in CLONE_GEO_ALLOWLIST_TENANTS:
        return None
    restricted_envs = list(CLONE_ENV_GEO_ALLOWLIST.keys())
    clauses = [ServiceConfigModel.environment.notin_(restricted_envs)]
    for env, geos in CLONE_ENV_GEO_ALLOWLIST.items():
        clauses.append(
            and_(
                ServiceConfigModel.environment == env,
                ServiceConfigModel.geo_loc_mst_code.in_(list(geos)),
            )
        )
    return or_(*clauses)


class ServiceConfigRepository(BaseRepository[ServiceConfigModel]):
    """Repository for ServiceConfig operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ServiceConfigModel, session)

    async def find_public_facing_services(
        self,
        filters: List[Any],
        skip: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find public-facing API services by joining service_config with
        services_mst and applying any SQLAlchemy filters provided.

        The caller is responsible for adding the `service_type == API` and
        `is_public_facing == True` filters in the filter list — this method
        is generic so it can be reused for similar lookups while keeping the
        join logic in one place.

        Args:
            filters: List of SQLAlchemy filter expressions to AND together.

        Returns:
            List of dicts containing service_config + services_mst columns.

        Example:
            rows = await repo.find_public_facing_services([
                ServicesMstModel.applications_mst_code == "myapp",
                ServiceConfigModel.environment == "dev",
                ServiceConfigModel.geo_loc_mst_code == "mumbai",
                ServicesMstModel.service_type == ServiceTypeEnum.API,
                ServicesMstModel.is_public_facing == True,
                ServiceConfigModel.is_deleted == False,
                ServicesMstModel.is_deleted == False,
            ])
        """
        # Default filter: hide soft/hard-deleted service configs from
        # public-facing discovery. Callers can still pass any other filters.
        all_filters = list(filters) + [
            ServiceConfigModel.status.notin_(_DELETED_STATUSES),
        ]
        stmt = (
            select(
                ServicesMstModel.code.label("service_code"),
                ServicesMstModel.name.label("service_name"),
                ServicesMstModel.service_type,
                ServicesMstModel.is_public_facing,
                ServicesMstModel.applications_mst_code,
                ServiceConfigModel.code.label("service_config_code"),
                ServiceConfigModel.environment,
                ServiceConfigModel.geo_loc_mst_code,
                ServiceConfigModel.infrastructuretype_ref_code,
                ServiceConfigModel.infrastructure_mst_code,
            )
            .join(
                ServicesMstModel,
                ServiceConfigModel.services_mst_code == ServicesMstModel.code,
            )
            .where(and_(*all_filters))
        )
        if skip is not None:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return [dict(row._mapping) for row in result.all()]

    async def get_by_tenant_service_env_geo_loc(
        self,
        tenant_code: str,
        service_code: str,
        environment: str,
        geo_loc_code: str,
        alb_selection: str = "existing_alb",
        infra_vendor: Optional[str] = None,
        infrastructure_type: Optional[str] = None,
        infrastructure_mst_code: Optional[str] = None
    ) -> Optional[ServiceConfigModel]:
        """
        Get service configuration by tenant, service code, environment, geo location, ALB type,
        and optionally infra vendor, infrastructure type, and infrastructure instance.

        Args:
            tenant_code: Tenant code (from JWT)
            service_code: Service code
            environment: Environment (dev/staging/prod)
            geo_loc_code: Geographic location code
            alb_selection: ALB type (no_alb/existing_alb/create_new_alb)
            infra_vendor: Infrastructure vendor (aws/azure/gcp/on_prem) - optional filter
            infrastructure_type: Infrastructure type code - optional filter
            infrastructure_mst_code: Infrastructure instance (cluster) code - optional filter

        Returns:
            ServiceConfigModel if found, None otherwise
        """
        filters = [
            self.model.tenant_mst_code == tenant_code,
            self.model.services_mst_code == service_code,
            self.model.environment == environment,
            self.model.geo_loc_mst_code == geo_loc_code,
            self.model.is_deleted == False,
            self.model.status.notin_(_DELETED_STATUSES),
            self.model.alb_selection == alb_selection
        ]

        # Add optional filters if provided
        if infra_vendor:
            filters.append(self.model.infra_vendor_enum == infra_vendor)
        if infrastructure_type:
            filters.append(self.model.infrastructuretype_ref_code == infrastructure_type)
        if infrastructure_mst_code:
            filters.append(self.model.infrastructure_mst_code == infrastructure_mst_code)

        result = await self.session.execute(
            select(self.model).where(*filters)
        )
        return result.scalar_one_or_none()

    async def exists_by_tenant_service_env_geo_loc(
        self,
        tenant_code: str,
        service_code: str,
        environment: str,
        geo_loc_code: str,
        alb_selection: str = "existing_alb"
    ) -> bool:
        """
        Check if service configuration exists for tenant + service + environment + geo location + ALB type.

        Args:
            tenant_code: Tenant code (from JWT)
            service_code: Service code
            environment: Environment (dev/staging/prod)
            geo_loc_code: Geographic location code
            alb_selection: ALB type (no_alb/existing_alb/create_new_alb)

        Returns:
            True if exists, False otherwise
        """
        config = await self.get_by_tenant_service_env_geo_loc(tenant_code, service_code, environment, geo_loc_code, alb_selection)
        return config is not None

    async def get_by_code_and_tenant(
        self,
        code: str,
        tenant_code: str
    ) -> Optional[ServiceConfigModel]:
        """
        Get service configuration by code with tenant filtering.

        Args:
            code: Service config code
            tenant_code: Tenant code (from JWT)

        Returns:
            ServiceConfigModel if found, None otherwise
        """
        filters = [
            self.model.code == code,
            self.model.tenant_mst_code == tenant_code,
            self.model.is_deleted == False
        ]
        result = await self.session.execute(
            select(self.model).where(*filters)
        )
        return result.scalar_one_or_none()

    async def get_by_service_env_and_geo_loc(
        self,
        service_code: str,
        environment: str,
        geo_loc_mst_code: str
    ) -> Optional[ServiceConfigModel]:
        """
        Get service configuration by service code, environment, and geographic location.

        This method does NOT filter by tenant - used by pipeline service where
        the service is already validated to belong to the user's tenant.

        Args:
            service_code: Service code
            environment: Environment (dev/staging/prod)
            geo_loc_mst_code: Geographic location code

        Returns:
            ServiceConfigModel if found, None otherwise
        """
        filters = [
            self.model.services_mst_code == service_code,
            self.model.environment == environment,
            self.model.geo_loc_mst_code == geo_loc_mst_code,
            self.model.is_deleted == False,
            self.model.status.notin_(_DELETED_STATUSES),
        ]
        result = await self.session.execute(
            select(self.model).where(*filters)
        )
        return result.scalar_one_or_none()

    async def link_to_gitops_workflow(
        self,
        service_config_ids: List[int],
        workflow_id: int
    ) -> None:
        """
        Link multiple service configs to a GitOps workflow.

        Used when service configs share one PR/workflow.

        Args:
            service_config_ids: List of service config IDs to link
            workflow_id: GitOps workflow detail ID

        Example:
            # Link service config to workflow
            await repo.link_to_gitops_workflow(
                service_config_ids=[1],
                workflow_id=789
            )
        """
        stmt = (
            update(self.model)
            .where(self.model.id.in_(service_config_ids))
            .values(gitops_workflow_id=workflow_id)
        )
        await self.session.execute(stmt)

    async def link_to_dockerfile_gitops_workflow(
        self,
        service_config_ids: List[int],
        workflow_id: int
    ) -> None:
        """
        Link multiple service configs to a Dockerfile GitOps workflow.

        Used when Dockerfile PRs are created for service configurations.

        Args:
            service_config_ids: List of service config IDs to link
            workflow_id: GitOps workflow detail ID for Dockerfile PR
        """
        stmt = (
            update(self.model)
            .where(self.model.id.in_(service_config_ids))
            .values(dockerfile_gitops_workflow_id=workflow_id)
        )
        await self.session.execute(stmt)

    async def get_configs_by_tenant(
        self,
        tenant_code: str,
        exclude_service_code: Optional[str] = None
    ) -> List[ServiceConfigModel]:
        """
        Get all service configurations for a tenant.
        Used for listener priority validation - priority must be unique across entire tenant.

        Args:
            tenant_code: Tenant code (from JWT)
            exclude_service_code: Optional service code to exclude (for update validation)

        Returns:
            List of ServiceConfigModel matching the criteria
        """
        filters = [
            self.model.tenant_mst_code == tenant_code,
            self.model.is_deleted == False,
            self.model.status.notin_(_DELETED_STATUSES),
        ]

        if exclude_service_code:
            filters.append(self.model.services_mst_code != exclude_service_code)

        result = await self.session.execute(
            select(self.model).where(*filters)
        )
        return list(result.scalars().all())

    async def get_by_service_env_and_cluster_name(
        self,
        tenant_code: str,
        service_code: str,
        environment: str,
        cluster_name: str,
        cluster_type: Optional[str] = None,
    ) -> Optional[ServiceConfigModel]:
        """
        Find the service config for a given service + environment whose linked
        infrastructure_mst record has locator['cluster_name'] == cluster_name.

        Used for canvas enrichment: matches a scanned EKS/ECS node's cluster name
        to the exact service_config row in the database.

        Args:
            tenant_code: Tenant code for multi-tenant isolation
            service_code: services_mst.code value
            environment: Environment string (dev/stage/qa/prod)
            cluster_name: EKS/ECS cluster name from infrastructure_mst.locator['cluster_name']
            cluster_type: Short cluster type "eks" or "ecs" (NOT the node
                resourceSubtype "eks_service"/"ecs_service"). Substring-matched
                against infrastructuretype_ref_code, so "eks" hits
                "eks_infrastructuretype_ref" and "ecs" hits
                "ecs_ec2_infrastructuretype_ref". EKS and ECS clusters can share
                the same cluster_name (only the ARN differs), so without this
                filter an EKS node can pick up a stale ECS config row.

        Returns:
            ServiceConfigModel if found, None otherwise
        """
        filters = [
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServiceConfigModel.services_mst_code == service_code,
            ServiceConfigModel.environment == environment,
            ServiceConfigModel.is_deleted == False,
            ServiceConfigModel.status.notin_(_DELETED_STATUSES),
            InfrastructureMstModel.locator["cluster_name"].astext == cluster_name,
            InfrastructureMstModel.is_deleted == False,
            InfrastructureMstModel.status.notin_(_DELETED_STATUSES),
        ]
        if cluster_type:
            filters.append(
                ServiceConfigModel.infrastructuretype_ref_code.ilike(f"%{cluster_type}%")
            )
        stmt = (
            select(ServiceConfigModel)
            .join(
                InfrastructureMstModel,
                ServiceConfigModel.infrastructure_mst_code == InfrastructureMstModel.code,
            )
            .where(*filters)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_configs_for_service(
        self,
        tenant_code: str,
        service_code: str
    ) -> List[ServiceConfigModel]:
        """
        Get all service configurations for a service across all environments.
        Used to check if Datadog or other sidecars are enabled.

        Args:
            tenant_code: Tenant code (from JWT)
            service_code: Service code

        Returns:
            List of ServiceConfigModel for the service
        """
        filters = [
            self.model.tenant_mst_code == tenant_code,
            self.model.services_mst_code == service_code,
            self.model.is_deleted == False,
            self.model.status.notin_(_DELETED_STATUSES),
        ]

        result = await self.session.execute(
            select(self.model).where(*filters)
        )
        return list(result.scalars().all())

    async def save_service_config(
        self,
        code: str,
        name: str,
        tenant_mst_code: str,
        services_mst_code: str,
        infrastructuretype_ref_code: str,
        infra_vendor_enum: str,
        infrastructure_mst_code: str,
        environment: str,
        geo_loc_mst_code: str,
        alb_selection: str,
        language_ref_code: str,
        config: dict,
        sidecar_config: list,
        deployment_strategy: dict,
        dockerfile: str,
        sync_status: str = "SAVED"
    ) -> ServiceConfigModel:
        """
        Save (create) a new service configuration to database.

        Args:
            code: Service config code
            name: Service config name
            tenant_mst_code: Tenant code
            services_mst_code: Service code
            infrastructuretype_ref_code: Infrastructure type code
            infra_vendor_enum: Infrastructure vendor enum
            infrastructure_mst_code: Infrastructure instance code
            environment: Environment enum value
            geo_loc_mst_code: Geographic location code
            alb_selection: ALB selection type
            language_ref_code: Language reference code
            config: Configuration dict (JSONB)
            sidecar_config: Sidecar configuration list (JSONB)
            deployment_strategy: Deployment strategy dict (JSONB)
            dockerfile: Dockerfile content
            sync_status: Sync status (default: "SAVED")

        Returns:
            Created ServiceConfigModel
        """
        return await self.create(
            code=code,
            name=name,
            tenant_mst_code=tenant_mst_code,
            services_mst_code=services_mst_code,
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            infra_vendor_enum=infra_vendor_enum,
            infrastructure_mst_code=infrastructure_mst_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            alb_selection=alb_selection,
            language_ref_code=language_ref_code,
            config=config,
            sidecar_config=sidecar_config,
            deployment_strategy=deployment_strategy,
            dockerfile=dockerfile,
            sync_status=sync_status,
            is_active=True,
            is_deleted=False
        )

    async def update_service_config_fields(
        self,
        config: ServiceConfigModel,
        infrastructuretype_ref_code: str = None,
        infra_vendor_enum: str = None,
        infrastructure_mst_code: str = None,
        language_ref_code: str = None,
        config_dict: dict = None,
        sidecar_config: list = None,
        deployment_strategy: dict = None,
        dockerfile: str = None,
        sync_status: str = "SAVED"
    ) -> ServiceConfigModel:
        """
        Update existing service configuration fields.

        Args:
            config: Existing ServiceConfigModel to update
            infrastructuretype_ref_code: Infrastructure type code
            infra_vendor_enum: Infrastructure vendor enum
            infrastructure_mst_code: Infrastructure instance code
            language_ref_code: Language reference code
            config_dict: Configuration dict (JSONB)
            sidecar_config: Sidecar configuration list (JSONB)
            deployment_strategy: Deployment strategy dict (JSONB)
            dockerfile: Dockerfile content
            sync_status: Sync status (default: "SAVED")

        Returns:
            Updated ServiceConfigModel
        """
        updates = {}

        if infrastructuretype_ref_code is not None:
            updates['infrastructuretype_ref_code'] = infrastructuretype_ref_code
        if infra_vendor_enum is not None:
            updates['infra_vendor_enum'] = infra_vendor_enum
        if infrastructure_mst_code is not None:
            updates['infrastructure_mst_code'] = infrastructure_mst_code
        if language_ref_code is not None:
            updates['language_ref_code'] = language_ref_code
        if config_dict is not None:
            # Merge: {**existing, **new} — preserves cluster context fields
            # from canvas creation while applying settings page updates
            updates['config'] = {**(config.config or {}), **config_dict}
        if sidecar_config is not None:
            updates['sidecar_config'] = sidecar_config
        if deployment_strategy is not None:
            updates['deployment_strategy'] = deployment_strategy
        if dockerfile is not None:
            updates['dockerfile'] = dockerfile

        # Always update sync_status
        updates['sync_status'] = sync_status

        return await self.update(config, updates)

    async def update_alb_url_by_codes(self, service_config_codes: List[str], alb_url: str) -> int:
        """
        Set config['alb_url'] on service_configs matching the given codes.

        Uses jsonb_set to merge alb_url into the existing config JSONB
        without overwriting other keys.

        Args:
            service_config_codes: List of service_configs.code values
            alb_url: ALB URL to store

        Returns:
            Number of rows updated
        """
        if not service_config_codes or not alb_url:
            return 0

        stmt = (
            update(self.model)
            .where(
                self.model.code.in_(service_config_codes),
                self.model.is_deleted == False,
            )
            .values(
                config=func.jsonb_set(
                    func.coalesce(self.model.config, cast("{}", JSONB)),
                    cast(literal_column("'{alb_url}'"), ARRAY(Text)),
                    func.to_jsonb(alb_url),
                )
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def update_resolved_service_path(self, code: str, service_path: str) -> int:
        """Record the ingress path a manifest generator actually used.

        service_path is optional on a service config, and each generator has its
        own default for the empty case — one uses "/", another "/<name>-service".
        The ALB URL is assembled much later, in a different process, and used to
        re-derive the same value; that second guess is what drifted. Writing the
        real value down removes the guess.
        """
        if not code or not service_path:
            return 0

        stmt = (
            update(self.model)
            .where(
                self.model.code == code,
                self.model.is_deleted == False,
            )
            .values(
                config=func.jsonb_set(
                    func.coalesce(self.model.config, cast("{}", JSONB)),
                    cast(literal_column("'{resolved_service_path}'"), ARRAY(Text)),
                    func.to_jsonb(service_path),
                )
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def update_status(self, code: str, status: "ResourceStatusEnum") -> int:
        """Update the UI-ready status on a service config by code.

        Returns the number of rows updated (0 or 1).
        """
        from datetime import datetime, timezone
        from app.core.enum import ResourceStatusEnum  # noqa
        stmt = (
            update(self.model)
            .where(
                and_(
                    self.model.code == code,
                    self.model.is_deleted == False,
                )
            )
            .values(
                status=status,
                status_updated_at=datetime.now(timezone.utc),
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def bulk_update_status(self, codes: List[str], status: "ResourceStatusEnum") -> int:
        """Bulk-update the UI-ready status for multiple service configs.

        Skips rows already in any deletion-lifecycle state (SOFT_DELETING /
        SOFT_DELETED / HARD_DELETING / HARD_DELETED). This guards against
        async webhook stage updates (e.g. Jenkins build progress reporting
        PROVISIONING → DEPLOYING → ONLINE) rolling a soft-deleted row back
        to "active". Explicit single-row transitions (`update_status`) stay
        unguarded so the pre/post-action code paths can still write the
        deletion statuses themselves.
        """
        if not codes:
            return 0
        from datetime import datetime, timezone
        from app.core.enum import ResourceStatusEnum  # noqa
        stmt = (
            update(self.model)
            .where(
                and_(
                    self.model.code.in_(codes),
                    self.model.is_deleted == False,
                    self.model.status.notin_(_DELETION_LIFECYCLE_STATUSES),
                )
            )
            .values(
                status=status,
                status_updated_at=datetime.now(timezone.utc),
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def bulk_update_deployment_status(
        self,
        codes: List[str],
        status: ResourceDeploymentStatusEnum,
        error_message: Optional[str] = None,
    ) -> int:
        """Bulk-update Temporal deployment_status for multiple service configs by code."""
        from datetime import datetime, timezone
        if not codes:
            return 0
        values: dict = {
            "deployment_status": status.value,
            "deployment_status_updated_at": datetime.now(timezone.utc),
        }
        if error_message is not None:
            values["deployment_error_message"] = error_message
        result = await self.session.execute(
            update(self.model)
            .where(self.model.code.in_(codes))
            .values(**values)
        )
        return result.rowcount

    async def get_env_geo_options_for_service(
        self,
        tenant_code: str,
        service_code: str,
    ) -> List[Dict[str, Any]]:
        """
        Distinct (environment, geo_loc, cluster) combos a service is configured for.
        Drives the clone flow's target env/region/cluster dropdowns.

        Returns dicts with keys:
          environment, geo_loc_code, geo_loc_name, infra_vendor_enum, config,
          config_code (service_config code — the clone target transaction_code),
          infrastructure_mst_code + cluster_name (the cluster this config lives on),
          infrastructuretype_ref_code (infra type, e.g. EKS vs ECS)
        A service can hold several configs in the SAME env+geo on DIFFERENT
        clusters (e.g. one EKS + one ECS) — those are kept as separate combos so
        each is selectable. True duplicates (same env+geo+cluster, differing only
        in e.g. alb_selection) still collapse to the first row.
        """
        stmt = (
            select(
                ServiceConfigModel.environment,
                ServiceConfigModel.geo_loc_mst_code.label("geo_loc_code"),
                GeoLocMstModel.name.label("geo_loc_name"),
                ServiceConfigModel.infra_vendor_enum,
                ServiceConfigModel.config,
                ServiceConfigModel.code.label("config_code"),
                ServiceConfigModel.infrastructure_mst_code,
                ServiceConfigModel.infrastructuretype_ref_code,
                InfrastructureMstModel.name.label("cluster_name"),
                InfrastructureMstModel.locator.label("infra_locator"),
                # Whether the cluster row still exists. The canvas only draws a
                # service under a LIVE cluster; callers that must agree with the
                # canvas (the CLI assistant) filter on this.
                InfrastructureMstModel.is_deleted.label("cluster_deleted"),
            )
            .join(
                GeoLocMstModel,
                ServiceConfigModel.geo_loc_mst_code == GeoLocMstModel.code,
            )
            # Left join: a config with a missing/unset cluster must still appear.
            .outerjoin(
                InfrastructureMstModel,
                ServiceConfigModel.infrastructure_mst_code == InfrastructureMstModel.code,
            )
            .where(
                ServiceConfigModel.tenant_mst_code == tenant_code,
                ServiceConfigModel.services_mst_code == service_code,
                ServiceConfigModel.is_deleted == False,
                ServiceConfigModel.status.notin_(_DELETED_STATUSES),
                GeoLocMstModel.is_deleted == False,
            )
            .order_by(ServiceConfigModel.id)
        )
        result = await self.session.execute(stmt)
        combos: Dict[tuple, Dict[str, Any]] = {}
        for row in result.all():
            key = (row.environment, row.geo_loc_code, row.infrastructure_mst_code)
            if key not in combos:
                combos[key] = dict(row._mapping)
        return list(combos.values())

    async def resolve_service_names(
        self,
        tenant_code: str,
        config_codes: List[str],
    ) -> List[Dict[str, Any]]:
        """Join service_configs -> services_mst to name a batch of config codes.

        Replaces the caller-side trick of pulling the services_mst uuid out of
        the config code string: that only worked while the code kept its
        sc-<uuid>-<env>-<suffix> shape, silently failed for the older
        service-config-<uuid>-… rows, and needed the whole service catalog
        client-side to look the uuid up. The FK is authoritative; the string is
        not.

        Soft-deleted rows are INCLUDED, flagged rather than dropped. An authz
        tuple pointing at a deleted config is precisely what an operator needs
        to see in order to revoke it — filtering it out leaves an object that
        cannot be named and therefore cannot be cleaned up.

        Tenant-scoped: a config belonging to another tenant is simply not
        returned, so its name never leaks across the boundary.

        Returns dicts with keys: config_code, service_mst_code, service_name,
        resource_group_code, environment, geo_loc_code, is_deleted.
        """
        if not config_codes:
            return []
        stmt = (
            select(
                ServiceConfigModel.code.label("config_code"),
                ServiceConfigModel.services_mst_code.label("service_mst_code"),
                ServicesMstModel.name.label("service_name"),
                ServicesMstModel.resource_group_mst_code.label("resource_group_code"),
                ServiceConfigModel.environment,
                ServiceConfigModel.geo_loc_mst_code.label("geo_loc_code"),
                # either side being soft-deleted makes the pair stale
                or_(
                    ServiceConfigModel.is_deleted == True,
                    ServicesMstModel.is_deleted == True,
                ).label("is_deleted"),
            )
            .join(
                ServicesMstModel,
                ServiceConfigModel.services_mst_code == ServicesMstModel.code,
            )
            .where(
                ServiceConfigModel.tenant_mst_code == tenant_code,
                ServiceConfigModel.code.in_(set(config_codes)),
            )
        )
        result = await self.session.execute(stmt)
        return [dict(row._mapping) for row in result.all()]

    async def get_service_codes_having_configs(
        self,
        tenant_code: str,
        service_codes: List[str],
    ) -> set:
        """Subset of `service_codes` that have at least one active service_config."""
        if not service_codes:
            return set()
        stmt = (
            select(ServiceConfigModel.services_mst_code)
            .where(
                ServiceConfigModel.tenant_mst_code == tenant_code,
                ServiceConfigModel.services_mst_code.in_(service_codes),
                ServiceConfigModel.is_deleted == False,
                ServiceConfigModel.status.notin_(_DELETED_STATUSES),
            )
            .distinct()
        )
        result = await self.session.execute(stmt)
        return {row[0] for row in result.all()}

    async def get_clonable_source_services(
        self,
        tenant_code: str,
        infra_type_ref_code: str,
        search_query: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        allowed_service_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Paginated source-service picker for the SETTINGS clone, restricted to
        services holding at least one active service_config of `infra_type_ref_code`.

        A settings clone requires source and target to share the same infra type
        (EKS↔EKS, etc.), so a service with no config of the target's type is not a
        valid source and is excluded entirely — the picker never lists it. The
        EXISTS filter and the count run over the same predicate, so pagination and
        total stay correct. `has_source_configs` is implicitly True for every row.

        Returns {"total": int, "services": [{service_code, service_name,
        application_code, application_name}]}.
        """
        config_filters = [
            ServiceConfigModel.services_mst_code == ServicesMstModel.code,
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServiceConfigModel.infrastructuretype_ref_code == infra_type_ref_code,
            ServiceConfigModel.is_deleted == False,
            ServiceConfigModel.status.notin_(_DELETED_STATUSES),
        ]
        # Drop services whose only configs sit in disallowed regions (aspora/vance
        # geo allowlist) — they'd otherwise list with no selectable environment.
        geo_predicate = _clone_geo_allowlist_predicate(tenant_code)
        if geo_predicate is not None:
            config_filters.append(geo_predicate)

        has_config_exists = (
            select(ServiceConfigModel.id)
            .where(*config_filters)
            .correlate(ServicesMstModel)
            .exists()
        )

        filters = [
            ServicesMstModel.is_deleted == False,
            ServicesMstModel.tenants_mst_code == tenant_code,
            has_config_exists,
        ]
        if search_query and search_query.strip():
            filters.append(ServicesMstModel.name.ilike(f"%{search_query.strip()}%"))
        if allowed_service_codes is not None:
            if not allowed_service_codes:
                return {"total": 0, "services": []}
            filters.append(ServicesMstModel.code.in_(allowed_service_codes))

        base = (
            select(
                ServicesMstModel.code.label("service_code"),
                ServicesMstModel.name.label("service_name"),
                ApplicationsMstModel.code.label("application_code"),
                ApplicationsMstModel.name.label("application_name"),
                ServicesMstModel.created_at.label("created_at"),
            )
            .select_from(ServicesMstModel)
            .join(
                ApplicationsMstModel,
                ServicesMstModel.applications_mst_code == ApplicationsMstModel.code,
            )
            .where(*filters)
        )

        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = base.order_by(ServicesMstModel.created_at.desc()).offset(skip).limit(limit)
        rows = (await self.session.execute(stmt)).all()
        services = [
            {
                "service_code": r.service_code,
                "service_name": r.service_name,
                "application_code": r.application_code,
                "application_name": r.application_name,
            }
            for r in rows
        ]
        return {"total": total, "services": services}

    async def get_eks_ecs_service_configs(
        self,
        tenant_code: str,
        application_code: str,
        environment: str,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all EKS/ECS service configs for the given app/env and service name.

        Used to build "draft/pending" canvas nodes for services that exist in DB
        but have no matching node in the AWS scan (i.e., configured but not yet deployed).

        The service_config.config JSONB is expected to contain:
          cluster_name, cluster_arn, region, cloud_region_id, subnet_ids, launch_type

        Returns a list of dicts with keys:
          services_mst_code, geo_loc_mst_code, infrastructuretype_ref_code,
          infrastructure_mst_code, service_name, config
        """
        stmt = (
            select(
                ServiceConfigModel.services_mst_code,
                ServiceConfigModel.code.label("service_config_code"),
                ServiceConfigModel.geo_loc_mst_code,
                ServiceConfigModel.infrastructuretype_ref_code,
                ServiceConfigModel.infrastructure_mst_code,
                ServicesMstModel.name.label("service_name"),
                ServicesMstModel.service_type,
                ServiceConfigModel.config,
                ServiceConfigModel.status.label("resource_status"),
                ServiceConfigModel.status_updated_at.label("resource_status_updated_at"),
                ServiceConfigModel.deployment_status,
            )
            .join(
                ServicesMstModel,
                ServiceConfigModel.services_mst_code == ServicesMstModel.code,
            )
            .where(
                ServiceConfigModel.tenant_mst_code == tenant_code,
                ServicesMstModel.applications_mst_code == application_code,
                ServiceConfigModel.environment == environment,
                ServiceConfigModel.is_deleted == False,
                ServiceConfigModel.status.notin_(_DELETED_STATUSES),
                ServicesMstModel.is_deleted == False,
                or_(
                    ServiceConfigModel.infrastructuretype_ref_code.ilike("%eks%"),
                    ServiceConfigModel.infrastructuretype_ref_code.ilike("%ecs%"),
                ),
            )
        )
        result = await self.session.execute(stmt)
        rows = result.all()
        return [dict(row._mapping) for row in rows]
