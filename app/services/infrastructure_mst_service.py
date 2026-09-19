"""
Infrastructure Master Service

Service layer for Infrastructure Master operations.
Handles business logic for managing infrastructure instances (clusters, compute resources).
"""
import logging
from typing import Dict, Any, Optional, List

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.infra_vendor_accounts_mst_repository import InfraVendorAccountsMstRepository
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.services.locator_variable_mapper import map_locator_to_values
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.domain.factories.infrastructure_mst_factory import (
    make_infrastructure_mst_eks,
    resolve_infra_name_env,
    resolve_sqs_name,
)
from app.mcp_servers.devlift_mcp.meta_data.registry import is_paas_tenant
from app.core.enum import EnvironmentEnum, DeploymentStatusEnum, InfraVendorEnum, ResourceStatusEnum
from app.core.config import settings
from app.services.workspace_service import WorkspaceService
from app.domain.validators import infra_config_validator as infra_config

logger = logging.getLogger(__name__)


class InfrastructureMstService:
    """
    Service layer for Infrastructure Master operations.
    Handles business logic for infrastructure instances.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.infrastructure_mst_repository = InfrastructureMstRepository(session)
        self.applications_repository = ApplicationsMstRepository(session)

    async def list_infrastructures(
        self,
        tenant_code: str,
        user_code: str,
        infrastructuretype_ref_code: Optional[str] = None,
        environment: Optional[EnvironmentEnum] = None,
        geo_loc_mst_code: Optional[str] = None,
        applications_mst_code: Optional[str] = None,
        cluster_name: Optional[str] = None,
        status: Optional[ResourceStatusEnum] = None,
    ) -> Dict[str, Any]:
        """
        List infrastructure instances filtered by type, environment, and optionally geo location.

        Business Logic:
        - Returns active, non-deleted infrastructure instances
        - Filters by tenant for multi-tenant isolation
        - Filters by infrastructure type and environment (required)
        - Optionally filters by geographic location

        Args:
            tenant_code: Tenant code for multi-tenant isolation
            infrastructuretype_ref_code: Infrastructure type code (required)
            environment: Environment enum (required)
            geo_loc_mst_code: Geographic location code (optional)

        Returns:
            Dict with:
                - total: Count of matching infrastructure instances
                - infrastructures: List of infrastructure dictionaries

        Example:
            >>> result = await service.list_infrastructures(
            ...     tenant_code="vance",
            ...     infrastructuretype_ref_code="ecs_fargate_infrastructuretype_ref",
            ...     environment=EnvironmentEnum.PROD,
            ...     geo_loc_mst_code="mumbai"
            ... )
        """
        workspace_svc = WorkspaceService(self.session)
        accessible_app_codes = None

        if applications_mst_code:
            if not await workspace_svc.verify_app_workspace_access(user_code, tenant_code, applications_mst_code):
                return {"total": 0, "infrastructures": []}
        else:
            workspace_codes = await workspace_svc.get_user_workspace_codes(user_code, tenant_code)
            accessible_app_codes = await self.applications_repository.get_codes_by_workspaces(
                workspace_codes, tenant_code
            )
            if not accessible_app_codes:
                return {"total": 0, "infrastructures": []}

        # Get infrastructures from repository
        infrastructures = await self.infrastructure_mst_repository.list_by_filters(
            tenant_code=tenant_code,
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            applications_mst_code=applications_mst_code,
            applications_mst_codes=accessible_app_codes,
            cluster_name=cluster_name,
            status=status,
        )

        # For EKS/ECS cluster types, only return records marked as registered in locator
        _CLUSTER_TYPES = {"eks_infrastructuretype_ref", "ecs_ec2_infrastructuretype_ref"}
        if infrastructuretype_ref_code in _CLUSTER_TYPES:
            infrastructures = [
                i for i in infrastructures
                if (i.locator or {}).get("isRegistered", False)
            ]

        # Convert model instances to response format
        infrastructure_list = []
        for infra in infrastructures:
            locator = infra.locator or {}
            cluster_name = locator.get("cluster") or locator.get("cluster_name")
            infrastructure_list.append({
                "code": infra.code,
                "name": infra.name,
                "infrastructuretype_ref_code": infra.infrastructuretype_ref_code,
                "environment": infra.environments_enum.value if hasattr(infra.environments_enum, 'value') else str(infra.environments_enum),
                "geo_loc_mst_code": infra.geo_loc_mst_code,
                "resource_identifier": infra.resource_identifier,
                "cluster_name": cluster_name,
                "locator": locator,
                # Predefined reference-variable values mapped from the locator
                # (S3_BUCKET_NAME, QUEUE_URL, AWS_REGION, …) for the Add picker.
                "derived_variables": map_locator_to_values(infra.infrastructuretype_ref_code, locator),
                "status": infra.status.value if getattr(infra, "status", None) is not None else None,
            })

        return {
            "total": len(infrastructure_list),
            "infrastructures": infrastructure_list
        }

    async def get_detail(self, code: str, tenant_code: str) -> Optional[Dict[str, Any]]:
        """
        Get full detail of an infrastructure_mst record by code, including locator.
        Returns None if not found or belongs to a different tenant.
        """
        record = await self.infrastructure_mst_repository.get_by_code(code)
        if record is None or record.tenants_mst_code != tenant_code:
            return None
        return {
            "code": record.code,
            "name": record.name,
            "infrastructuretype_ref_code": record.infrastructuretype_ref_code,
            "environment": record.environments_enum.value if record.environments_enum else None,
            "geo_loc_mst_code": record.geo_loc_mst_code,
            "infra_status": record.infra_status.value if record.infra_status else None,
            "locator": record.locator,
        }

    # Locator keys that identify or place the TARGET resource — never overwritten
    # by a clone. Everything else in the source locator is copied over.
    CLONE_PRESERVED_LOCATOR_KEYS = (
        "identifier",
        "server_name",
        "db_server_name",
        "cloudRegion",
        "cloudRegionId",
        "accountId",
        "account_id",
        "cluster_name",
        "cluster_arn",
        "region",
        "subnetIds",
        "vpcId",
    )

    async def list_clone_sources(
        self,
        tenant_code: str,
        user_code: str,
        infrastructuretype_ref_code: str,
        exclude_code: Optional[str] = None,
        environment: Optional[EnvironmentEnum] = None,
        geo_loc_mst_code: Optional[str] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        One page of eligible clone sources for the clone-settings modal.

        Filtering, search and paging all happen in SQL so the modal can lazy
        load instead of pulling every resource of the type.

        Returns:
            Dict with total, skip, limit, has_more and the page's infrastructures
        """
        workspace_svc = WorkspaceService(self.session)
        workspace_codes = await workspace_svc.get_user_workspace_codes(user_code, tenant_code)
        accessible_app_codes = await self.applications_repository.get_codes_by_workspaces(
            workspace_codes, tenant_code
        )
        if not accessible_app_codes:
            return {
                "total": 0,
                "skip": skip,
                "limit": limit,
                "has_more": False,
                "infrastructures": [],
            }

        rows, total = await self.infrastructure_mst_repository.list_clone_sources_paginated(
            tenant_code=tenant_code,
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            applications_mst_codes=accessible_app_codes,
            exclude_code=exclude_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            search=search,
            skip=skip,
            limit=limit,
        )

        infrastructure_list = []
        for infra in rows:
            locator = infra.locator or {}
            infrastructure_list.append({
                "code": infra.code,
                "name": infra.name,
                "infrastructuretype_ref_code": infra.infrastructuretype_ref_code,
                "environment": infra.environments_enum.value if hasattr(infra.environments_enum, 'value') else str(infra.environments_enum),
                "geo_loc_mst_code": infra.geo_loc_mst_code,
                "cluster_name": locator.get("cluster") or locator.get("cluster_name"),
                "status": infra.status.value if getattr(infra, "status", None) is not None else None,
            })

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "has_more": skip + len(infrastructure_list) < total,
            "infrastructures": infrastructure_list,
        }

    async def clone_settings(
        self,
        tenant_code: str,
        user_code: str,
        source_code: str,
        target_code: str,
    ) -> Dict[str, Any]:
        """
        Copy the settings portion of one infra resource's locator into another
        resource of the same infrastructure type.

        Identity and placement keys of the target (name/identifier, region,
        account, cluster, network) are preserved — only configuration values
        (e.g. SQS fifo/dlq/retention, S3 versioning, DynamoDB keys) move over.

        Field-level validation of the cloned values is intentionally deferred
        to the metadata-driven validation layer.

        Raises HTTPException 404/400/403 on structural problems.
        """
        if source_code == target_code:
            raise HTTPException(status_code=400, detail="Source and target must be different resources")

        source = await self.infrastructure_mst_repository.get_by_code(source_code)
        target = await self.infrastructure_mst_repository.get_by_code(target_code)

        for label, record in (("Source", source), ("Target", target)):
            if record is None or record.tenants_mst_code != tenant_code or record.is_deleted:
                raise HTTPException(status_code=404, detail=f"{label} resource not found")

        if source.infrastructuretype_ref_code != target.infrastructuretype_ref_code:
            raise HTTPException(
                status_code=400,
                detail="Source and target must be the same infrastructure type",
            )

        # Write-permission check on the target's application — mirrors the
        # infrastructure create/update flow.
        if target.applications_mst_code:
            workspace_svc = WorkspaceService(self.session)
            if not await workspace_svc.verify_app_workspace_access(user_code, tenant_code, target.applications_mst_code):
                raise HTTPException(status_code=403, detail="Access denied: application workspace is not accessible")
            from app.services.applications_mst_service import ApplicationsMstService
            app_svc = ApplicationsMstService(self.session)
            if not await app_svc.can_write_for_app(user_code, tenant_code, target.applications_mst_code):
                raise HTTPException(status_code=403, detail="Access denied: read-only role cannot clone settings")

        source_locator = dict(source.locator or {})
        target_locator = dict(target.locator or {})

        # The resolved AWS names are derived from the TARGET's own identifier —
        # `bucket_name`, `queue_name`, the ARNs, `table_name`. They are not
        # settings, and copying them leaves the target's row pointing at the
        # source's real bucket or queue. That heals on the next save, which
        # rebuilds them, but a deploy straight after a clone reads the locator
        # and writes terraform for the wrong resource.
        preserved = set(self.CLONE_PRESERVED_LOCATOR_KEYS) | set(
            infra_config.identity_keys(target.infrastructuretype_ref_code)
        )

        merged = {
            **target_locator,
            **{k: v for k, v in source_locator.items() if k not in preserved},
        }

        updated = await self.infrastructure_mst_repository.update(target, {"locator": merged})
        await self.session.commit()

        logger.info(
            f"Cloned infra settings: {source_code} -> {target_code} "
            f"(type={target.infrastructuretype_ref_code}, tenant={tenant_code}, by={user_code})"
        )

        return {
            "code": updated.code,
            "name": updated.name,
            "infrastructuretype_ref_code": updated.infrastructuretype_ref_code,
            "environment": updated.environments_enum.value if updated.environments_enum else None,
            "geo_loc_mst_code": updated.geo_loc_mst_code,
            "infra_status": updated.infra_status.value if updated.infra_status else None,
            "locator": updated.locator,
        }

    async def get_status(self, code: str) -> Optional[Dict[str, Any]]:
        """
        Get the infra_status of an infrastructure_mst record by code.

        Returns a dict with status fields, or None if the record is not found.
        """
        record = await self.infrastructure_mst_repository.get_by_code(code)
        if record is None:
            return None
        return {
            "code": record.code,
            "infra_status": record.infra_status.value if record.infra_status else None,
            "infra_status_updated_by": record.infra_status_updated_by,
            "infra_status_updated_at": record.infra_status_updated_at,
        }

    async def create_workload_infrastructure(
        self,
        tenant_code: str,
        user_email: str,
        infrastructuretype_ref_code: Optional[str] = None,
        application_mst_code: Optional[str] = None,
        locator: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Add an infrastructure_mst entry for an already-provisioned cluster.

        Used when a shared/existing cluster needs a record for a particular tenant.
        All fields have sensible defaults matching the standard EKS provisioning.
        """
        default_region = settings.onboarding_default_region
        default_region_code = settings.onboarding_default_region_code
        default_index = settings.onboarding_default_index
        default_env = settings.onboarding_default_env
        default_cluster_name = settings.onboarding_default_vm_name

        default_cluster_name_full = (
            f"devlift-{default_env}-{default_region_code}-{default_index}-{default_cluster_name}-cluster"
        )
        default_locator = {
            "vpcId": "vpc-placeholder-main",
            "region": default_region,
            "subnetIds": [
                "subnet-placeholder-private-1a",
                "subnet-placeholder-private-1b",
            ],
            "cloudRegion": default_region,
            "cluster_arn": f"arn:aws:eks:{default_region}:{settings.onboarding_default_account_id}:cluster/{default_cluster_name_full}",
            "cluster_name": default_cluster_name_full,
            "cloudRegionId": f"cr-000000000000-{default_region}",
            "acm_cert_arn": settings.onboarding_default_acm_cert_arn,
            "efs_volume_handle": settings.onboarding_default_efs_volume_handle,
        }

        # Resolve values with fallbacks
        infra_type = infrastructuretype_ref_code or "eks_infrastructuretype_ref"
        resolved_locator = locator or default_locator

        # Resolve application_mst_code — fallback to trail app
        if not application_mst_code:
            stmt = (
                select(ApplicationsMstModel)
                .where(
                    ApplicationsMstModel.tenants_mst_code == tenant_code,
                    ApplicationsMstModel.name == f"{tenant_code}_trail",
                    ApplicationsMstModel.is_deleted == False,
                )
            )
            result = await self.session.execute(stmt)
            trail_app = result.scalar_one_or_none()
            if not trail_app:
                raise HTTPException(
                    status_code=400,
                    detail=f"Trail application not found for tenant '{tenant_code}'. "
                           f"Please create an application first or provide application_mst_code."
                )
            application_mst_code = trail_app.code
            logger.info(f"[WORKLOAD INFRA] Using trail app: {application_mst_code}")

        # Ensure vendor account exists — create if not (same as _ensure_vendor_account)
        vendor_repo = InfraVendorAccountsMstRepository(self.session)
        existing_vendor = await vendor_repo.get_by_tenant_and_vendor(
            tenant_code=tenant_code,
            infra_vendor_enum=InfraVendorEnum.aws,
            environments_enum=EnvironmentEnum.stage,
        )
        if existing_vendor:
            vendor_account_code = existing_vendor.code
            logger.info(f"[WORKLOAD INFRA] Using existing vendor account: {vendor_account_code}")
        else:
            vendor_account_code = f"aws_{tenant_code}_stage"
            deploy_role_name = f"{tenant_code}-deploy-role"
            vendor_account = await vendor_repo.create(
                code=vendor_account_code,
                name=f"AWS {tenant_code.capitalize()} Stage Account",
                description=f"AWS account config for {tenant_code} - Stage environment",
                tenants_mst_code=tenant_code,
                applications_mst_code=None,
                resource_group_mst_code=None,
                auth_config={
                    "authentication_type": "iam_role",
                    "account_id": settings.onboarding_default_account_id,
                    "region": default_region,
                    "assume_role_arn": f"arn:aws:iam::{settings.onboarding_default_account_id}:role/Devlift-PlatformAccess",
                    "github_role_arn": f"arn:aws:iam::{settings.onboarding_default_account_id}:role/Devlift-github-role",
                    "deploy_role_arn": f"arn:aws:iam::{settings.onboarding_default_account_id}:role/{deploy_role_name}",
                    "external_id": settings.secrets_external_id,
                    "session_name": "DevliftSession",
                    "session_duration": 3600,
                },
                environments_enum=EnvironmentEnum.stage,
                infra_vendor_enum=InfraVendorEnum.aws,
            )
            vendor_account_code = vendor_account.code
            logger.info(f"[WORKLOAD INFRA] Created vendor account: {vendor_account_code}")

        # Ensure DevLift K8s vendor account exists (needed for K8s-hosted resources like Postgres helm)
        existing_k8s_vendor = await vendor_repo.get_by_tenant_and_vendor(
            tenant_code=tenant_code,
            infra_vendor_enum=InfraVendorEnum.devlift_k8s,
            environments_enum=EnvironmentEnum.stage,
        )
        if not existing_k8s_vendor:
            k8s_vendor_code = f"devlift_k8s_{tenant_code}_stage"
            deploy_role_name = f"{tenant_code}-deploy-role"
            await vendor_repo.create(
                code=k8s_vendor_code,
                name=f"DevLift K8s {tenant_code.capitalize()} Stage Account",
                description=f"DevLift K8s account config for {tenant_code} - Stage environment",
                tenants_mst_code=tenant_code,
                applications_mst_code=None,
                resource_group_mst_code=None,
                auth_config={
                    "authentication_type": "iam_role",
                    "account_id": settings.onboarding_default_account_id,
                    "region": default_region,
                    "assume_role_arn": f"arn:aws:iam::{settings.onboarding_default_account_id}:role/Devlift-PlatformAccess",
                    "github_role_arn": f"arn:aws:iam::{settings.onboarding_default_account_id}:role/Devlift-github-role",
                    "deploy_role_arn": f"arn:aws:iam::{settings.onboarding_default_account_id}:role/{deploy_role_name}",
                    "external_id": settings.secrets_external_id,
                    "session_name": "DevliftSession",
                    "session_duration": 3600,
                },
                environments_enum=EnvironmentEnum.stage,
                infra_vendor_enum=InfraVendorEnum.devlift_k8s,
            )
            logger.info(f"[WORKLOAD INFRA] Created DevLift K8s vendor account: {k8s_vendor_code}")
        else:
            logger.info(f"[WORKLOAD INFRA] DevLift K8s vendor account already exists: {existing_k8s_vendor.code}")

        # Derive cluster name and geo_loc_mst_code
        cluster_name = resolved_locator.get(
            "cluster_name",
            f"{tenant_code}-{default_env}-{default_region_code}-{default_index}-{default_cluster_name}-cluster"
        )
        geo_loc_mst_code = f"region-{tenant_code}-us"

        # Check if an EKS infrastructure record already exists for this tenant + cluster
        existing = await self.infrastructure_mst_repository.list_by_filters(
            tenant_code=tenant_code,
            cluster_name=cluster_name,
        )
        if existing:
            record = existing[0]
            logger.info(f"[WORKLOAD INFRA] EKS record already exists: {record.code}")
            return {
                "success": True,
                "code": record.code,
                "name": record.name,
                "message": "Workload infrastructure already exists",
            }

        # Build infrastructure record via factory
        eks_data = make_infrastructure_mst_eks(
            identifier=cluster_name,
            tenant_code=tenant_code,
            application_code=application_mst_code,
            environment=EnvironmentEnum.stage,
            region=default_region,
            infrastructuretype_ref_code=infra_type,
            infra_vendor_accounts_mst_code=vendor_account_code,
            locator=resolved_locator,
            infra_status=DeploymentStatusEnum.ACTIVE,
            infra_status_updated_by=user_email,
            geo_loc_mst_code=geo_loc_mst_code,
        )

        created = await self.infrastructure_mst_repository.create(**eks_data)
        await self.session.commit()

        logger.info(f"[WORKLOAD INFRA] Created infrastructure record: {created.code}")

        return {
            "success": True,
            "code": created.code,
            "name": created.name,
            "message": "Workload infrastructure created successfully",
        }

    @staticmethod
    def get_infra_name_prefix(tenant_code: str, environment: str) -> str:
        """
        Build the infrastructure resource naming prefix.
        Follows the Terraform convention: {organization}-{env}-{region_code}-{index}

        The ``env`` segment always comes from ``onboarding_default_env`` (i.e.
        "trial"); the deployment environment (stage/dev/prod) is a separate
        axis that does NOT affect AWS resource naming. The ``environment``
        parameter is ignored — kept for API back-compat only.

        Example: prefix="aslam-trial-virginia-01", identifier="orders"
                 → full name = "aslam-trial-virginia-01-orders"
        """
        return (
            f"{tenant_code}-{settings.onboarding_default_env}"
            f"-{settings.onboarding_default_region_code}"
            f"-{settings.onboarding_default_index}"
        )

    @staticmethod
    def get_infra_naming(
        tenant_code: str,
        environment: str,
        infra_type: Optional[str] = None,
        identifier: Optional[str] = None,
        fifo_queue: bool = False,
    ) -> Dict[str, Any]:
        """Return `{"prefix": ..., "name": ...}` for UI preview.

        `prefix` is always populated (generic `{tenant}-{env}-{region}-{index}`).
        `name` is the fully resolved AWS resource name — only computed when
        `infra_type` + `identifier` are both provided. SQS and DynamoDB get the
        Terraform-layer-specific formula; everything else falls back to
        `{prefix}-{identifier}`.

        The preview has to agree with what the factory will actually store, so
        both consult the same helpers. For an enterprise tenant the layers
        prefix nothing, and the SQS formula branches on the resource's own env
        rather than the onboarding one.
        """
        prefix = InfrastructureMstService.get_infra_name_prefix(tenant_code, environment)
        name: Optional[str] = None
        if infra_type and identifier:
            if infra_type == "sqs_infrastructuretype_ref":
                name = resolve_sqs_name(
                    identifier=identifier,
                    tenant_code=tenant_code,
                    env=resolve_infra_name_env(tenant_code, environment),
                    region_code=settings.onboarding_default_region_code,
                    index=settings.onboarding_default_index,
                    fifo_queue=fifo_queue,
                )
            elif (
                infra_type == "dynamodb_infrastructuretype_ref"
                and not is_paas_tenant(tenant_code)
            ):
                name = f"{identifier}-{settings.onboarding_default_index}"
            else:
                name = f"{prefix}-{identifier}"
        return {"prefix": prefix, "name": name}

    # ── Cluster inventory (EKS / ECS-EC2) ────────────────────────────────────
    # Short display labels for the cluster types the clusters screen covers.
    CLUSTER_TYPE_LABELS = {
        "eks_infrastructuretype_ref": "EKS",
        "ecs_ec2_infrastructuretype_ref": "ECS EC2",
    }

    # locator keys holding the cluster ARN, in the order they're trusted.
    _CLUSTER_ARN_KEYS = ("cluster_arn", "clusterArn", "arn")

    async def list_clusters(
        self,
        tenant_code: str,
        user_code: str,
        infrastructuretype_ref_code: Optional[str] = None,
        environment: Optional[EnvironmentEnum] = None,
        geo_loc_mst_code: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Every EKS / ECS-EC2 cluster the caller can see, with the two locator
        flags the clusters screen toggles.

        ``is_registered`` and ``is_listed`` both default to the value the rest
        of the codebase already assumes when the key is absent: registration is
        opt-in (``False``), listing is opt-out (``True``, matching the
        ``isListed`` check in the canvas discovery service).

        Args:
            tenant_code: Tenant code for multi-tenant isolation
            user_code: Caller, used to resolve accessible workspaces
            infrastructuretype_ref_code: Narrow to one cluster type (optional)
            environment: Environment enum (optional)
            geo_loc_mst_code: Geographic location code (optional)
            search: Case-insensitive match on the resource name

        Returns:
            Dict with ``total`` and ``clusters``
        """
        if (
            infrastructuretype_ref_code
            and infrastructuretype_ref_code not in self.CLUSTER_TYPE_LABELS
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "infrastructuretype_ref_code must be one of "
                    f"{sorted(self.CLUSTER_TYPE_LABELS)}"
                ),
            )

        workspace_svc = WorkspaceService(self.session)
        workspace_codes = await workspace_svc.get_user_workspace_codes(user_code, tenant_code)
        accessible_app_codes = await self.applications_repository.get_codes_by_workspaces(
            workspace_codes, tenant_code
        )
        if not accessible_app_codes:
            return {"total": 0, "clusters": []}

        records = await self.infrastructure_mst_repository.list_clusters(
            tenant_code=tenant_code,
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            applications_mst_codes=accessible_app_codes,
            search=search,
        )

        app_names = await self._application_names(
            {r.applications_mst_code for r in records if r.applications_mst_code}
        )
        clusters = [self._to_cluster_item(record, app_names) for record in records]
        return {"total": len(clusters), "clusters": clusters}

    async def _application_names(self, codes: set) -> Dict[str, str]:
        """Map application code → name in one query (empty set → empty map)."""
        if not codes:
            return {}
        stmt = select(
            ApplicationsMstModel.code, ApplicationsMstModel.name
        ).where(ApplicationsMstModel.code.in_(codes))
        result = await self.session.execute(stmt)
        return {code: name for code, name in result.all()}

    def _to_cluster_item(
        self, record, app_names: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Shape one infrastructure_mst row for the clusters listing."""
        locator = record.locator or {}
        cluster_arn = next(
            (locator[key] for key in self._CLUSTER_ARN_KEYS if locator.get(key)),
            None,
        )
        geo_loc = getattr(record, "geo_loc", None)
        return {
            "code": record.code,
            "name": record.name,
            "infrastructuretype_ref_code": record.infrastructuretype_ref_code,
            "cluster_type": self.CLUSTER_TYPE_LABELS.get(
                record.infrastructuretype_ref_code, record.infrastructuretype_ref_code
            ),
            "environment": (
                record.environments_enum.value
                if hasattr(record.environments_enum, "value")
                else str(record.environments_enum)
            ),
            "geo_loc_mst_code": record.geo_loc_mst_code,
            "geo_loc_name": geo_loc.name if geo_loc else None,
            "applications_mst_code": record.applications_mst_code,
            "application_name": (app_names or {}).get(record.applications_mst_code),
            # The AWS-side name lives in the locator; fall back to the row name
            # so the column is never blank for DB-only clusters.
            "cluster_name": locator.get("cluster_name") or locator.get("cluster") or record.name,
            "cluster_arn": cluster_arn or record.resource_identifier,
            "region": locator.get("cloudRegion") or locator.get("region"),
            "status": record.status.value if getattr(record, "status", None) is not None else None,
            # Absent key → not registered (matches list_infrastructures).
            "is_registered": bool(locator.get("isRegistered", False)),
            # Absent key → listed (matches the canvas discovery service).
            "is_listed": locator.get("isListed", True) is not False,
        }

    async def set_cluster_flags(
        self,
        tenant_code: str,
        user_code: str,
        code: str,
        is_registered: Optional[bool] = None,
        is_listed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Toggle ``isRegistered`` / ``isListed`` on one cluster's locator.

        Either flag may be omitted; an omitted flag is left untouched rather
        than reset. Returns the cluster in its post-update shape.
        """
        if is_registered is None and is_listed is None:
            raise HTTPException(
                status_code=400,
                detail="At least one of is_registered or is_listed must be provided",
            )

        record = await self.infrastructure_mst_repository.get_by_code(code)
        if (
            record is None
            or record.tenants_mst_code != tenant_code
            or record.is_deleted
        ):
            raise HTTPException(status_code=404, detail=f"Cluster '{code}' not found")

        if record.infrastructuretype_ref_code not in self.CLUSTER_TYPE_LABELS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Infrastructure '{code}' is not an EKS or ECS EC2 cluster "
                    f"(type: {record.infrastructuretype_ref_code})"
                ),
            )

        # Tenant-level clusters carry a NULL applications_mst_code — there is
        # no owning workspace to check, and tenant isolation is already
        # enforced above. Only app-owned clusters get the workspace gate.
        if record.applications_mst_code is not None:
            workspace_svc = WorkspaceService(self.session)
            if not await workspace_svc.verify_app_workspace_access(
                user_code, tenant_code, record.applications_mst_code
            ):
                raise HTTPException(status_code=404, detail=f"Cluster '{code}' not found")

        flags: Dict[str, bool] = {}
        if is_registered is not None:
            flags["isRegistered"] = is_registered
        if is_listed is not None:
            flags["isListed"] = is_listed

        updated = await self.infrastructure_mst_repository.set_locator_flags(
            infrastructure_code=code,
            tenant_code=tenant_code,
            flags=flags,
        )
        if updated == 0:
            raise HTTPException(status_code=404, detail=f"Cluster '{code}' not found")
        await self.session.commit()

        # Re-read so the response carries the merged locator — the UPDATE went
        # straight to the DB and never touched the in-session copy. This row is
        # used rather than `record` because it also carries the eagerly loaded
        # geo_loc; `record` would lazy-load it and blow up on the async session.
        refreshed = await self.infrastructure_mst_repository.get_cluster_by_code(
            code=code, tenant_code=tenant_code
        )
        if refreshed is None:
            raise HTTPException(status_code=404, detail=f"Cluster '{code}' not found")
        app_names = await self._application_names(
            {record.applications_mst_code} if record.applications_mst_code else set()
        )
        return self._to_cluster_item(refreshed, app_names)
