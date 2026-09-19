import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from sqlalchemy import select, and_, or_, update, func, cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models.infrastructure_mst_model import InfrastructureMstModel

logger = logging.getLogger(__name__)
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.resource_group_mst_model import ResourceGroupMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.infrastructuretype_ref_model import InfrastructureTypeRefModel
from app.repository.base_repository import BaseRepository
from app.core.enum import EnvironmentEnum, DeploymentStatusEnum, ResourceStatusEnum, ResourceDeploymentStatusEnum
from app.domain.factories.infrastructure_transaction_factory import make_infrastructure_transaction


# Resource statuses that mean "the row exists but is no longer an active
# resource". List/discovery methods filter these out by default; single-row
# lookups by code (and AWS-name conflict checks) intentionally still see them.
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


class InfrastructureMstRepository(BaseRepository[InfrastructureMstModel]):
    """Repository for Infrastructure Master operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(InfrastructureMstModel, session)

    async def get_by_code(self, code: str) -> Optional[InfrastructureMstModel]:
        """Get infrastructure by code with infra_vendor_account relationship loaded"""
        stmt = (
            select(self.model)
            .options(joinedload(self.model.infra_vendor_account))
            .where(self.model.code == code)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_filters(
        self,
        tenant_code: str,
        infrastructuretype_ref_code: Optional[str] = None,
        environment: Optional[EnvironmentEnum] = None,
        geo_loc_mst_code: Optional[str] = None,
        applications_mst_code: Optional[str] = None,
        applications_mst_codes: Optional[List[str]] = None,
        cluster_name: Optional[str] = None,
        status: Optional[ResourceStatusEnum] = None,
    ) -> List[InfrastructureMstModel]:
        """
        List infrastructure instances filtered by type, environment, and optionally geo location.

        Args:
            tenant_code: Tenant code for multi-tenant isolation
            infrastructuretype_ref_code: Infrastructure type code (optional)
            environment: Environment enum (optional)
            geo_loc_mst_code: Geographic location code (optional)
            applications_mst_code: Application code (optional)
            cluster_name: Cluster name to match against locator JSONB (optional)

        Returns:
            List of InfrastructureMstModel instances matching the filters
        """
        from sqlalchemy import or_
        filters = [
            self.model.tenants_mst_code == tenant_code,
            self.model.is_deleted == False,
            self.model.is_active == True,
            self.model.status.notin_(_DELETED_STATUSES),
        ]

        if infrastructuretype_ref_code:
            filters.append(self.model.infrastructuretype_ref_code == infrastructuretype_ref_code)

        if environment:
            filters.append(self.model.environments_enum == environment)

        if status:
            filters.append(self.model.status == status)

        if geo_loc_mst_code:
            filters.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        if applications_mst_code:
            filters.append(self.model.applications_mst_code == applications_mst_code)
        elif applications_mst_codes is not None:
            filters.append(self.model.applications_mst_code.in_(applications_mst_codes))

        # Filter by cluster name inside locator JSONB (matches either "cluster" or "cluster_name" key)
        if cluster_name:
            filters.append(
                or_(
                    self.model.locator["cluster"].as_string() == cluster_name,
                    self.model.locator["cluster_name"].as_string() == cluster_name
                )
            )

        stmt = (
            select(self.model)
            .options(joinedload(self.model.infra_vendor_account))
            .where(and_(*filters))
        )
        result = await self.session.execute(stmt)
        return result.unique().scalars().all()

    async def list_clone_sources_paginated(
        self,
        tenant_code: str,
        infrastructuretype_ref_code: str,
        applications_mst_codes: Optional[List[str]] = None,
        exclude_code: Optional[str] = None,
        environment: Optional[EnvironmentEnum] = None,
        geo_loc_mst_code: Optional[str] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[List[InfrastructureMstModel], int]:
        """
        One page of eligible clone sources plus the total match count.

        Eligibility mirrors ``list_by_filters``, plus a blank-name exclusion the
        picker needs. Ordering is ``created_at DESC, id DESC`` — the id tiebreak
        keeps paging stable when several rows share a timestamp.

        Returns:
            Tuple of (rows for this page, total matching rows)
        """
        filters = [
            self.model.tenants_mst_code == tenant_code,
            self.model.infrastructuretype_ref_code == infrastructuretype_ref_code,
            self.model.is_deleted == False,
            self.model.is_active == True,
            self.model.status.notin_(_DELETED_STATUSES),
            # Rows created with a blank identifier end up with a blank name
            # (see infrastructure_mst_factory). A source the user can't identify
            # is not selectable, so it never reaches the picker.
            self.model.name.isnot(None),
            func.length(func.trim(self.model.name)) > 0,
        ]

        if applications_mst_codes is not None:
            filters.append(self.model.applications_mst_code.in_(applications_mst_codes))

        if exclude_code:
            filters.append(self.model.code != exclude_code)

        if environment:
            filters.append(self.model.environments_enum == environment)

        if geo_loc_mst_code:
            filters.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        if search:
            # Escape LIKE wildcards so a literal % or _ in the query doesn't
            # silently widen the match.
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(self.model.name.ilike(f"%{escaped}%", escape="\\"))

        where_clause = and_(*filters)

        count_stmt = select(func.count()).select_from(self.model).where(where_clause)
        total = (await self.session.execute(count_stmt)).scalar_one()

        stmt = (
            select(self.model)
            .options(joinedload(self.model.infra_vendor_account))
            .where(where_clause)
            .order_by(self.model.created_at.desc(), self.model.id.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all()), total

    async def list_tenant_level_clusters(
        self,
        tenant_code: str,
        infrastructuretype_ref_code: str,
        environment: EnvironmentEnum,
    ) -> List[InfrastructureMstModel]:
        """
        Fetch active infra records for tenant + type + environment where
        applications_mst_code IS NULL.

        Used as a fallback when an application-scoped cluster query returns no
        results — the cluster may be registered at tenant level (shared across
        all applications).
        """
        stmt = (
            select(self.model)
            .options(joinedload(self.model.infra_vendor_account))
            .where(
                and_(
                    self.model.tenants_mst_code == tenant_code,
                    self.model.infrastructuretype_ref_code == infrastructuretype_ref_code,
                    self.model.environments_enum == environment,
                    self.model.applications_mst_code.is_(None),
                    self.model.is_deleted == False,
                    self.model.is_active == True,
                    self.model.status.notin_(_DELETED_STATUSES),
                )
            )
        )
        result = await self.session.execute(stmt)
        return result.unique().scalars().all()

    async def get_by_service_hierarchy(
        self,
        service_code: str,
        resource_group_code: Optional[str],
        application_code: Optional[str],
        tenant_code: str,
        environment: EnvironmentEnum,
        infrastructuretype_ref_code: str
    ) -> Optional[InfrastructureMstModel]:
        """
        Find infrastructure instance using hierarchical lookup with tenant isolation.

        Lookup priority with proper isolation:
        1. Resource Group level (within application and tenant)
        2. Application level (within tenant)
        3. Tenant level

        Multi-tenant isolation ensures:
        - Resource groups are only searched within their parent application and tenant
        - Applications are only searched within their parent tenant
        - Each level maintains the hierarchy: Tenant → Application → Resource Group

        Args:
            service_code: Service code (for future direct lookup)
            resource_group_code: Resource group code (optional)
            application_code: Application code (optional)
            tenant_code: Tenant code (required)
            environment: Environment enum (dev, staging, prod)
            infrastructuretype_ref_code: Infrastructure type code (EC2, ECS, Lambda, etc.)

        Returns:
            InfrastructureMstModel with infra_vendor_account relationship loaded, or None
        """

        # Base query with vendor account relationship and tenant isolation
        base_stmt = (
            select(self.model)
            .options(joinedload(self.model.infra_vendor_account))
            .where(
                and_(
                    self.model.is_deleted == False,
                    self.model.status.notin_(_DELETED_STATUSES),
                    self.model.tenants_mst_code == tenant_code,  # Always filter by tenant
                    self.model.environments_enum == environment,
                    self.model.infrastructuretype_ref_code == infrastructuretype_ref_code
                )
            )
        )

        # 1. Try resource group level (most specific) - within application and tenant
        if resource_group_code and application_code:
            stmt = base_stmt.where(
                and_(
                    self.model.resource_group_mst_code == resource_group_code,
                    self.model.applications_mst_code == application_code
                )
            ).order_by(self.model.created_at.asc())
            result = await self.session.execute(stmt)
            infrastructure = result.scalars().first()
            if infrastructure:
                return infrastructure

        # 2. Try application level - within tenant (exclude resource group level records)
        if application_code:
            stmt = base_stmt.where(
                and_(
                    self.model.applications_mst_code == application_code,
                    self.model.resource_group_mst_code.is_(None)
                )
            ).order_by(self.model.created_at.asc())
            result = await self.session.execute(stmt)
            infrastructure = result.scalars().first()
            if infrastructure:
                return infrastructure

        # 3. Try tenant level (most generic - exclude application and resource group level records)
        stmt = base_stmt.where(
            and_(
                self.model.applications_mst_code.is_(None),
                self.model.resource_group_mst_code.is_(None)
            )
        ).order_by(self.model.created_at.asc())
        result = await self.session.execute(stmt)
        infrastructure = result.scalars().first()

        return infrastructure

    async def get_by_region_hierarchy(
        self,
        geo_loc_mst_code: str,
        resource_group_code: Optional[str],
        application_code: Optional[str],
        tenant_code: str,
        environment: EnvironmentEnum,
        infrastructuretype_ref_code: str
    ) -> Optional[InfrastructureMstModel]:
        """
        Find infrastructure instance using hierarchical lookup with geo location and tenant isolation.

        Lookup priority with proper isolation:
        1. Resource Group level (within application and tenant)
        2. Application level (within tenant)
        3. Tenant level

        Args:
            geo_loc_mst_code: Geographic location code (e.g., 'mumbai', 'london')
            resource_group_code: Resource group code (optional)
            application_code: Application code (optional)
            tenant_code: Tenant code (required)
            environment: Environment enum (dev, staging, prod)
            infrastructuretype_ref_code: Infrastructure type code (EC2, ECS, Lambda, etc.)

        Returns:
            InfrastructureMstModel with infra_vendor_account relationship loaded, or None
        """

        # Base query with vendor account relationship, tenant and geo_loc isolation
        base_stmt = (
            select(self.model)
            .options(joinedload(self.model.infra_vendor_account))
            .where(
                and_(
                    self.model.is_deleted == False,
                    self.model.status.notin_(_DELETED_STATUSES),
                    self.model.tenants_mst_code == tenant_code,
                    self.model.geo_loc_mst_code == geo_loc_mst_code,
                    self.model.environments_enum == environment,
                    self.model.infrastructuretype_ref_code == infrastructuretype_ref_code
                )
            )
        )

        # 1. Try resource group level (most specific) - within application and tenant
        if resource_group_code and application_code:
            stmt = base_stmt.where(
                and_(
                    self.model.resource_group_mst_code == resource_group_code,
                    self.model.applications_mst_code == application_code
                )
            ).order_by(self.model.created_at.asc())
            result = await self.session.execute(stmt)
            infrastructure = result.scalars().first()
            if infrastructure:
                return infrastructure

        # 2. Try application level - within tenant (exclude resource group level records)
        if application_code:
            stmt = base_stmt.where(
                and_(
                    self.model.applications_mst_code == application_code,
                    self.model.resource_group_mst_code.is_(None)
                )
            ).order_by(self.model.created_at.asc())
            result = await self.session.execute(stmt)
            infrastructure = result.scalars().first()
            if infrastructure:
                return infrastructure

        # 3. Try tenant level (most generic - exclude application and resource group level records)
        stmt = base_stmt.where(
            and_(
                self.model.applications_mst_code.is_(None),
                self.model.resource_group_mst_code.is_(None)
            )
        ).order_by(self.model.created_at.asc())
        result = await self.session.execute(stmt)
        infrastructure = result.scalars().first()

        return infrastructure

    async def check_identifier_exists(
        self,
        *,
        infrastructuretype_ref_code: str,
        name_key: str,
        name_value: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        application_code: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
        exclude_code: Optional[str] = None,
    ) -> Optional[InfrastructureMstModel]:
        """Duplicate check for any infrastructure type, by its own name key.

        The per-type helpers below predate this and match their type with a LIKE
        prefix; this one matches the type code exactly, which is what the
        create/update flow already has in hand. `exclude_code` leaves the row
        being updated out, so renaming a draft does not find itself.

        Uniqueness contract, same as the per-type helpers:
            (tenant, application, environment, geo_loc, name)
        """
        conditions = [
            self.model.tenants_mst_code == tenant_code,
            self.model.environments_enum == environment,
            self.model.infrastructuretype_ref_code == infrastructuretype_ref_code,
            self.model.locator[name_key].astext == name_value,
            self.model.is_deleted == False,
        ]
        if application_code is not None:
            conditions.append(self.model.applications_mst_code == application_code)
        if geo_loc_mst_code is not None:
            conditions.append(self.model.geo_loc_mst_code == geo_loc_mst_code)
        if exclude_code is not None:
            conditions.append(self.model.code != exclude_code)

        stmt = select(self.model).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def check_s3_bucket_exists(
        self,
        bucket_identifier: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        application_code: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> Optional[InfrastructureMstModel]:
        """
        Check if an S3 bucket already exists in the infrastructure_mst table.

        Base scope: `(tenant, environment, bucket_name)`.
        If `application_code` and/or `geo_loc_mst_code` are provided, the
        scope is narrowed to also require those fields to match. The
        infrastructure-create flow passes both so its uniqueness contract
        matches the `aws_ops.s3_ops.duplicate_bucket_validator` pre-check
        and the two paths can never disagree.

        Args:
            bucket_identifier: S3 bucket name/identifier
            tenant_code: Tenant code
            environment: Environment enum
            application_code: Optional applications_mst_code to scope by
            geo_loc_mst_code: Optional geo_loc_mst_code to scope by

        Returns:
            InfrastructureMstModel if exists, None otherwise
        """
        conditions = [
            self.model.tenants_mst_code == tenant_code,
            self.model.environments_enum == environment,
            self.model.infrastructuretype_ref_code.like('s3%'),  # Match s3_infrastructuretype_ref
            self.model.locator['identifier'].astext == bucket_identifier,
            self.model.is_deleted == False,
        ]
        if application_code is not None:
            conditions.append(self.model.applications_mst_code == application_code)
        if geo_loc_mst_code is not None:
            conditions.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        stmt = select(self.model).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def check_sqs_queue_exists(
        self,
        queue_identifier: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        application_code: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> Optional[InfrastructureMstModel]:
        """
        Check if an SQS queue already exists in the infrastructure_mst table.

        Base scope: `(tenant, environment, queue_name)`.
        If `application_code` and/or `geo_loc_mst_code` are provided, the
        scope is narrowed to also require those fields to match. The
        infrastructure-create flow passes both so its uniqueness contract
        matches the `aws_ops.sqs_ops.duplicate_queue_validator` pre-check
        and the two paths can never disagree.

        Args:
            queue_identifier: SQS queue name/identifier
            tenant_code: Tenant code
            environment: Environment enum
            application_code: Optional applications_mst_code to scope by
            geo_loc_mst_code: Optional geo_loc_mst_code to scope by

        Returns:
            InfrastructureMstModel if exists, None otherwise
        """
        conditions = [
            self.model.tenants_mst_code == tenant_code,
            self.model.environments_enum == environment,
            self.model.infrastructuretype_ref_code.like('sqs%'),  # Match sqs_infrastructuretype_ref
            self.model.locator['identifier'].astext == queue_identifier,
            self.model.is_deleted == False,
        ]
        if application_code is not None:
            conditions.append(self.model.applications_mst_code == application_code)
        if geo_loc_mst_code is not None:
            conditions.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        stmt = select(self.model).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_infrastructure_record(
        self,
        filters: List[Any],
    ) -> Optional[InfrastructureMstModel]:
        """
        Fetch a single infrastructure record matching the given filters.

        Pass any list of SQLAlchemy filter expressions and get back the
        first matching InfrastructureMstModel (or None).

        Args:
            filters: List of SQLAlchemy filter expressions to AND together.

        Returns:
            InfrastructureMstModel if found, None otherwise.

        Example:
            entry = await repo.get_infrastructure_record([
                InfrastructureMstModel.applications_mst_code == "myapp",
                InfrastructureMstModel.environments_enum == EnvironmentEnum.DEV,
                InfrastructureMstModel.geo_loc_mst_code == "mumbai",
                InfrastructureMstModel.infrastructuretype_ref_code.like("dynamodb%"),
                InfrastructureMstModel.locator["table_name"].astext == "users",
                InfrastructureMstModel.is_deleted == False,
            ])
        """
        stmt = select(self.model).where(and_(*filters))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def check_dynamodb_table_exists(
        self,
        table_identifier: str,
        tenant_code: str,
        environment: EnvironmentEnum,
        application_code: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> Optional[InfrastructureMstModel]:
        """
        Check if a DynamoDB table already exists in the infrastructure_mst table.

        Base scope: `(tenant, environment, table_name)`.
        If `application_code` and/or `geo_loc_mst_code` are provided, the
        scope is narrowed to also require those fields to match. The
        infrastructure-create flow passes both so its uniqueness contract
        matches the `aws_ops.dynamodb_ops.duplicate_table_validator`
        pre-check and the two paths can never disagree.

        Args:
            table_identifier: DynamoDB table name/identifier
            tenant_code: Tenant code
            environment: Environment enum
            application_code: Optional applications_mst_code to scope by
            geo_loc_mst_code: Optional geo_loc_mst_code to scope by

        Returns:
            InfrastructureMstModel if exists, None otherwise
        """
        conditions = [
            self.model.tenants_mst_code == tenant_code,
            self.model.environments_enum == environment,
            self.model.infrastructuretype_ref_code.like('dynamodb%'),  # Match dynamodb_infrastructuretype_ref
            self.model.locator['identifier'].astext == table_identifier,
            self.model.is_deleted == False,
        ]
        if application_code is not None:
            conditions.append(self.model.applications_mst_code == application_code)
        if geo_loc_mst_code is not None:
            conditions.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        stmt = select(self.model).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def update_infra_status(
        self,
        infrastructure_id: int,
        status: DeploymentStatusEnum,
        updated_by: Optional[str] = None
    ) -> Optional[InfrastructureMstModel]:
        """
        Update infra_status and related tracking fields for an infrastructure record.

        Args:
            infrastructure_id: Infrastructure ID to update
            status: New deployment status
            updated_by: Email or username of updater (optional)

        Returns:
            Updated InfrastructureMstModel or None if not found

        Example:
            infrastructure = await repo.update_infra_status(
                infrastructure_id=123,
                status=DeploymentStatusEnum.PR_CREATED,
                updated_by="admin@example.com"
            )
        """
        infrastructure = await self.get_by_id(infrastructure_id)
        if not infrastructure:
            return None

        updates = {
            "infra_status": status,
            "infra_status_updated_at": datetime.now(timezone.utc)
        }
        if updated_by:
            updates["infra_status_updated_by"] = updated_by

        return await self.update(infrastructure, updates)

    async def update_status(
        self,
        code: str,
        status: "ResourceStatusEnum",
    ) -> int:
        """Update the UI-ready status on an infrastructure record by code.

        Returns the number of rows updated (0 or 1).
        """
        from app.core.enum import ResourceStatusEnum  # noqa: avoid circular
        stmt = (
            update(InfrastructureMstModel)
            .where(
                and_(
                    InfrastructureMstModel.code == code,
                    InfrastructureMstModel.is_deleted == False,
                )
            )
            .values(
                status=status,
                status_updated_at=datetime.now(timezone.utc),
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def bulk_update_status(
        self,
        codes: List[str],
        status: "ResourceStatusEnum",
    ) -> int:
        """Bulk-update the UI-ready status for multiple infrastructure records.

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
        from app.core.enum import ResourceStatusEnum  # noqa
        stmt = (
            update(InfrastructureMstModel)
            .where(
                and_(
                    InfrastructureMstModel.code.in_(codes),
                    InfrastructureMstModel.is_deleted == False,
                    InfrastructureMstModel.status.notin_(_DELETION_LIFECYCLE_STATUSES),
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
        """Bulk-update Temporal deployment_status for multiple infra records by code."""
        if not codes:
            return 0
        values: dict = {
            "deployment_status": status.value,
            "deployment_status_updated_at": datetime.now(timezone.utc),
        }
        if error_message is not None:
            values["deployment_error_message"] = error_message
        result = await self.session.execute(
            update(InfrastructureMstModel)
            .where(InfrastructureMstModel.code.in_(codes))
            .values(**values)
        )
        return result.rowcount

    async def link_to_gitops_workflow(
        self,
        infrastructure_ids: List[int],
        workflow_id: int
    ) -> None:
        """
        Link multiple infrastructure records to a GitOps workflow.

        Used in bulk operations where multiple infrastructure resources share one PR/workflow.

        Args:
            infrastructure_ids: List of infrastructure IDs to link
            workflow_id: GitOps workflow detail ID

        Example:
            # Link 5 S3 buckets to same workflow (bulk operation)
            await repo.link_to_gitops_workflow(
                infrastructure_ids=[1, 2, 3, 4, 5],
                workflow_id=789
            )
        """
        stmt = (
            update(self.model)
            .where(self.model.id.in_(infrastructure_ids))
            .values(gitops_workflow_id=workflow_id)
        )
        await self.session.execute(stmt)

    async def update_resource_identifier(
        self,
        infrastructure_id: int,
        resource_identifier: str
    ) -> Optional[InfrastructureMstModel]:
        """
        Update resource_identifier field (e.g., AWS S3 bucket ARN).

        This is typically called after the infrastructure resource is created
        by the vendor to store the actual resource ARN/ID.

        Args:
            infrastructure_id: Infrastructure ID to update
            resource_identifier: AWS ARN or resource identifier

        Returns:
            Updated InfrastructureMstModel or None if not found

        Example:
            infrastructure = await repo.update_resource_identifier(
                infrastructure_id=123,
                resource_identifier="arn:aws:s3:::my-bucket"
            )
        """
        infrastructure = await self.get_by_id(infrastructure_id)
        if not infrastructure:
            return None

        return await self.update(infrastructure, {"resource_identifier": resource_identifier})

    async def update_locator_field(
        self,
        infrastructure_code: str,
        key: str,
        value: str,
    ) -> None:
        """
        Merge a single key into the JSONB ``locator`` column for the record
        identified by *infrastructure_code*.

        Uses a direct UPDATE with the PostgreSQL ``||`` JSONB concat operator
        so the rest of the locator dict is preserved.
        """
        stmt = (
            update(InfrastructureMstModel)
            .where(
                InfrastructureMstModel.code == infrastructure_code,
                InfrastructureMstModel.is_deleted == False,
            )
            .values(
                locator=InfrastructureMstModel.locator.op("||")(
                    cast({key: value}, JSONB)
                )
            )
        )
        await self.session.execute(stmt)

    async def merge_locator_fields(
        self,
        infrastructure_code: str,
        fields: Dict[str, Any],
    ) -> None:
        """
        Merge multiple keys into the JSONB ``locator`` column for the record
        identified by *infrastructure_code* — dict variant of
        ``update_locator_field``. Shallow merge via the PostgreSQL ``||``
        operator: existing keys are preserved, passed keys are added/overwritten.
        """
        if not fields:
            return
        stmt = (
            update(InfrastructureMstModel)
            .where(
                InfrastructureMstModel.code == infrastructure_code,
                InfrastructureMstModel.is_deleted == False,
            )
            .values(
                locator=InfrastructureMstModel.locator.op("||")(
                    cast(fields, JSONB)
                )
            )
        )
        await self.session.execute(stmt)

    async def update_locator_field_by_tenant(
        self,
        tenant_code: str,
        key: str,
        value: str,
    ) -> None:
        """
        Merge a single key into the JSONB ``locator`` column for all non-deleted
        infrastructure records belonging to *tenant_code*.

        Used by the Jenkins webhook handler after bootstrap to persist
        ``shared_alb_hostname`` without needing to look up the infra code first.
        """
        stmt = (
            update(InfrastructureMstModel)
            .where(
                InfrastructureMstModel.tenants_mst_code == tenant_code,
                InfrastructureMstModel.is_deleted == False,
            )
            .values(
                locator=InfrastructureMstModel.locator.op("||")(
                    cast({key: value}, JSONB)
                )
            )
        )
        await self.session.execute(stmt)

    async def get_transactions_with_gitops(
        self,
        tenant_code: str,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        sort_order: str = "desc",
        paginate: bool = True
    ) -> Dict[str, Any]:
        """
        Get infrastructure transactions with LEFT JOIN to gitops_workflow_detail.

        Args:
            tenant_code: Tenant code for isolation
            status: Optional filter by infra_status
            skip: Pagination offset
            limit: Page size
            sort_order: 'asc' or 'desc' by created_at
            paginate: If True, apply skip/limit in SQL. If False, return all for service-level pagination.

        Returns:
            Dict with 'total' count and 'data' list of transformed transactions
        """
        # Build query with LEFT JOIN to gitops_workflow_detail, resource_group_mst, applications_mst
        stmt = (
            select(
                # Infrastructure fields
                self.model.id,
                self.model.code,
                self.model.name,
                self.model.description,
                self.model.created_at,
                self.model.updated_at,
                self.model.is_active,
                self.model.is_deleted,
                self.model.infrastructuretype_ref_code,
                self.model.infra_vendor_accounts_mst_code,
                self.model.resource_group_mst_code,
                self.model.tenants_mst_code,
                self.model.applications_mst_code,
                self.model.environments_enum,
                self.model.locator,
                self.model.infra_status,
                self.model.infra_status_updated_by,
                self.model.infra_status_updated_at,
                self.model.resource_identifier,
                self.model.gitops_workflow_id,
                # Related entity names
                ResourceGroupMstModel.name.label('resource_group_name'),
                ApplicationsMstModel.name.label('application_name'),
                InfrastructureTypeRefModel.name.label('infrastructure_type_name'),
                # GitOps workflow fields
                GitopsWorkflowDetailModel.id.label('workflow_id'),
                GitopsWorkflowDetailModel.code.label('workflow_code'),
                GitopsWorkflowDetailModel.name.label('workflow_name'),
                GitopsWorkflowDetailModel.git_repository,
                GitopsWorkflowDetailModel.git_branch,
                GitopsWorkflowDetailModel.git_commit_sha,
                GitopsWorkflowDetailModel.pr_number,
                GitopsWorkflowDetailModel.pr_url,
                GitopsWorkflowDetailModel.workflow_run_id,
                GitopsWorkflowDetailModel.workflow_run_url,
                GitopsWorkflowDetailModel.workflow_run_outputs,
                GitopsWorkflowDetailModel.run_initiated_at,
                GitopsWorkflowDetailModel.run_completed_at,
            )
            .select_from(self.model)
            .outerjoin(
                GitopsWorkflowDetailModel,
                self.model.gitops_workflow_id == GitopsWorkflowDetailModel.id
            )
            .outerjoin(
                ResourceGroupMstModel,
                self.model.resource_group_mst_code == ResourceGroupMstModel.code
            )
            .join(
                ApplicationsMstModel,
                self.model.applications_mst_code == ApplicationsMstModel.code
            )
            .join(
                InfrastructureTypeRefModel,
                self.model.infrastructuretype_ref_code == InfrastructureTypeRefModel.code
            )
            .where(self.model.is_deleted == False)
            .where(self.model.tenants_mst_code == tenant_code)
            .where(self.model.gitops_workflow_id.isnot(None))
        )

        # Apply status filter
        if status:
            stmt = stmt.where(self.model.infra_status == status)

        # Sort by created_at
        if sort_order == "desc":
            stmt = stmt.order_by(self.model.created_at.desc())
        else:
            stmt = stmt.order_by(self.model.created_at.asc())

        # If paginating at SQL level, get total count first
        if paginate:
            count_stmt = select(func.count()).select_from(
                select(self.model.id)
                .where(self.model.is_deleted == False)
                .where(self.model.tenants_mst_code == tenant_code)
                .where(self.model.gitops_workflow_id.isnot(None))
                .subquery()
            )
            if status:
                count_stmt = select(func.count()).select_from(
                    select(self.model.id)
                    .where(self.model.is_deleted == False)
                    .where(self.model.tenants_mst_code == tenant_code)
                    .where(self.model.infra_status == status)
                    .where(self.model.gitops_workflow_id.isnot(None))
                    .subquery()
                )
            total_result = await self.session.execute(count_stmt)
            total = total_result.scalar() or 0

            # Apply pagination
            stmt = stmt.offset(skip).limit(limit)
        else:
            total = None  # Will be calculated from data length

        # Execute query
        result = await self.session.execute(stmt)
        rows = result.all()

        # Transform rows to response format
        data = [make_infrastructure_transaction(row) for row in rows]

        return {
            "total": total if paginate else len(data),
            "data": data
        }

    async def find_eks_cluster_for_mcp(
        self,
        tenant_code: str,
        application_code: str,
        environment: EnvironmentEnum,
        geo_loc_mst_code: str,
    ) -> tuple[Optional[str], list[dict]]:
        """Resolve the infrastructure_mst_code for an EKS cluster.

        Used by the DevLift MCP `provision_service` tool to determine which EKS
        cluster to target without the LLM needing to know cluster codes.

        Filters by tenant + application + environment + geo_loc +
        infrastructuretype_ref_code='eks_infrastructuretype_ref'.

        Returns:
            (cluster_code, candidates)
            - Exactly one match  → cluster_code is set, candidates has 1 entry.
            - Zero matches       → cluster_code is None, candidates is [].
            - Multiple matches   → cluster_code is None, candidates lists all so
                                   the caller can ask the user to pick.
        """
        EKS_INFRA_TYPE = "eks_infrastructuretype_ref"

        def _stmt(app_filter):
            return (
                select(InfrastructureMstModel)
                .where(
                    and_(
                        InfrastructureMstModel.tenants_mst_code == tenant_code,
                        InfrastructureMstModel.is_deleted == False,   # noqa: E712
                        InfrastructureMstModel.is_active == True,     # noqa: E712
                        InfrastructureMstModel.status.notin_(_DELETED_STATUSES),
                        InfrastructureMstModel.infrastructuretype_ref_code == EKS_INFRA_TYPE,
                        app_filter,
                        InfrastructureMstModel.environments_enum == environment,
                        InfrastructureMstModel.geo_loc_mst_code == geo_loc_mst_code,
                    )
                )
            )

        def _registered(rows):
            """Only clusters marked registered may take a new service.

            `isRegistered` IS the "service creation allowed" flag
            (CanvasEksCluster.isRegistered), and
            `InfrastructureMstService.list_infrastructures` already hides
            unregistered clusters from the web's picker for cluster types. This
            asked without it, so the MCP offered a cluster the dashboard does
            not — three for stage/Mumbai where the web shows two — and a
            service could be placed on one nobody had cleared for use. Filtered
            in Python, like list_infrastructures, so a locator holding `true`
            or "true" reads the same.
            """
            return [r for r in rows if (r.locator or {}).get("isRegistered", False)]

        result = await self.session.execute(
            _stmt(InfrastructureMstModel.applications_mst_code == application_code)
        )
        clusters = _registered(result.scalars().all())

        if not clusters:
            # Fall back to tenant-level clusters, which carry a NULL
            # applications_mst_code and are shared across every application —
            # the same fallback `list_tenant_level_clusters` exists for, and
            # what `list_clusters` keeps so the web UI can offer them. Without
            # this, a placement the dropdown legitimately offers (aspora's QA
            # clusters are all tenant-level) resolves to "no cluster found".
            # Application-scoped clusters still win when both exist.
            result = await self.session.execute(
                _stmt(InfrastructureMstModel.applications_mst_code.is_(None))
            )
            clusters = _registered(result.scalars().all())

        candidates = [
            {
                "code": c.code,
                "name": c.name or c.code,
                "locator": c.locator or {},
            }
            for c in clusters
        ]

        if len(clusters) == 1:
            return clusters[0].code, candidates
        return None, candidates

    # Infrastructure types that represent a compute cluster in the UI. Kept
    # alongside the identical set in InfrastructureMstService.list_infrastructures.
    CLUSTER_INFRA_TYPES = (
        "eks_infrastructuretype_ref",
        "ecs_ec2_infrastructuretype_ref",
    )

    async def get_placement_tree(
        self,
        tenant_code: str,
        infrastructuretype_ref_code: Optional[str] = None,
        application_codes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """The (application, environment, geo) combinations that actually have
        infrastructure — the honest source for placement dropdowns.

        The placement selectors used to be a cartesian product of every product,
        a hardcoded environment list and every geo location the tenant owned, so
        a user could pick a combination with no cluster behind it and only find
        out when provisioning failed. This asks the same question the resolver
        asks, with the SAME predicates as `find_eks_cluster_for_mcp`, so the two
        can never disagree: whatever this offers, that one can resolve.

        `isRegistered` gates only CLUSTER types, matching
        `InfrastructureMstService.list_infrastructures`. The flag is not set on
        S3/SQS/DynamoDB rows, so applying it everywhere would empty those.

        A NULL `applications_mst_code` is a TENANT-LEVEL cluster, shared across
        every application — not an unattributed row. It is offered under each of
        `application_codes`, the same way `list_clusters` keeps those rows
        beside the scoped ones and `list_tenant_level_clusters` falls back to
        them. Dropping them here would hide a whole environment: aspora's QA
        clusters are all tenant-level, and the web UI offers QA because of them.

        Returns dicts of applications_mst_code / environment / geo_loc_mst_code.
        """
        stmt = (
            select(
                InfrastructureMstModel.applications_mst_code,
                InfrastructureMstModel.environments_enum,
                InfrastructureMstModel.geo_loc_mst_code,
                InfrastructureMstModel.locator,
            )
            .where(
                and_(
                    InfrastructureMstModel.tenants_mst_code == tenant_code,
                    InfrastructureMstModel.is_deleted == False,   # noqa: E712
                    InfrastructureMstModel.is_active == True,     # noqa: E712
                    InfrastructureMstModel.status.notin_(_DELETED_STATUSES),
                )
            )
        )
        if infrastructuretype_ref_code:
            stmt = stmt.where(
                InfrastructureMstModel.infrastructuretype_ref_code
                == infrastructuretype_ref_code
            )
        if application_codes is not None:
            # A plain IN () drops the NULL rows, which are the tenant-level
            # clusters shared across every application — keep them, the same
            # way `list_clusters` does.
            stmt = stmt.where(
                or_(
                    InfrastructureMstModel.applications_mst_code.in_(application_codes),
                    InfrastructureMstModel.applications_mst_code.is_(None),
                )
            )

        rows = (await self.session.execute(stmt)).all()

        cluster_only = bool(
            infrastructuretype_ref_code
            and infrastructuretype_ref_code in self.CLUSTER_INFRA_TYPES
        )

        seen: set[tuple] = set()
        placements: List[Dict[str, Any]] = []
        unregistered = 0
        shared: List[tuple] = []

        def _add(app_code: str, env_value: str, geo_code: str) -> None:
            key = (app_code, env_value, geo_code)
            if key in seen:
                return
            seen.add(key)
            placements.append(
                {
                    "applications_mst_code": app_code,
                    "environment": env_value,
                    "geo_loc_mst_code": geo_code,
                }
            )

        for app_code, env, geo_code, locator in rows:
            if env is None or not geo_code:
                continue
            if cluster_only and not (locator or {}).get("isRegistered", False):
                unregistered += 1
                continue
            env_value = env.value if hasattr(env, "value") else str(env)
            if app_code:
                _add(app_code, env_value, geo_code)
            else:
                shared.append((env_value, geo_code))

        # Tenant-level infrastructure belongs to every product the caller can
        # see. Held back until the scoped rows are in so the dedup covers both.
        for env_value, geo_code in shared:
            for app_code in application_codes or ():
                _add(app_code, env_value, geo_code)

        if unregistered:
            logger.info(
                "get_placement_tree: %s placement row(s) skipped for tenant=%s "
                "type=%s because locator.isRegistered is not set",
                unregistered, tenant_code, infrastructuretype_ref_code,
            )
        if shared and not application_codes:
            logger.warning(
                "get_placement_tree: %s tenant-level row(s) for tenant=%s "
                "type=%s could not be offered because no application_codes "
                "were given to attach them to — an environment may be missing "
                "from the dropdown because of this",
                len(shared), tenant_code, infrastructuretype_ref_code,
            )
        return placements

    async def list_clusters(
        self,
        tenant_code: str,
        infrastructuretype_ref_code: Optional[str] = None,
        environment: Optional[EnvironmentEnum] = None,
        geo_loc_mst_code: Optional[str] = None,
        applications_mst_codes: Optional[List[str]] = None,
        search: Optional[str] = None,
    ) -> List[InfrastructureMstModel]:
        """
        Every EKS / ECS-EC2 cluster row for a tenant, with its geo location
        eagerly loaded so the caller can render a location name.

        Unlike ``list_by_filters`` this does NOT drop rows whose locator lacks
        ``isRegistered`` — the clusters screen exists precisely to show and
        toggle that flag, so unregistered and unlisted rows must come back too.

        Args:
            tenant_code: Tenant code for multi-tenant isolation
            infrastructuretype_ref_code: Narrow to one cluster type (optional)
            environment: Environment enum (optional)
            geo_loc_mst_code: Geographic location code (optional)
            applications_mst_codes: Restrict to workspace-accessible applications
            search: Case-insensitive match on the resource name

        Returns:
            List of InfrastructureMstModel rows ordered by name
        """
        filters = [
            self.model.tenants_mst_code == tenant_code,
            self.model.is_deleted == False,  # noqa: E712
            self.model.is_active == True,  # noqa: E712
            self.model.status.notin_(_DELETED_STATUSES),
        ]

        if infrastructuretype_ref_code:
            filters.append(self.model.infrastructuretype_ref_code == infrastructuretype_ref_code)
        else:
            filters.append(self.model.infrastructuretype_ref_code.in_(self.CLUSTER_INFRA_TYPES))

        if environment:
            filters.append(self.model.environments_enum == environment)

        if geo_loc_mst_code:
            filters.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        if applications_mst_codes is not None:
            # Tenant-level clusters (shared across every application, e.g. the
            # ECS EC2 rows) carry a NULL applications_mst_code, which a plain
            # IN () silently drops — keep them alongside the workspace-scoped
            # rows, mirroring the list_tenant_level_clusters fallback.
            filters.append(
                or_(
                    self.model.applications_mst_code.in_(applications_mst_codes),
                    self.model.applications_mst_code.is_(None),
                )
            )

        if search:
            # Escape LIKE wildcards so a literal % or _ in the query doesn't
            # silently widen the match.
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(self.model.name.ilike(f"%{escaped}%", escape="\\"))

        stmt = (
            select(self.model)
            .options(joinedload(self.model.geo_loc))
            .where(and_(*filters))
            .order_by(self.model.name.asc(), self.model.id.asc())
        )
        result = await self.session.execute(stmt)
        return result.unique().scalars().all()

    async def get_cluster_by_code(
        self,
        code: str,
        tenant_code: str,
    ) -> Optional[InfrastructureMstModel]:
        """
        One tenant-owned cluster row with its geo location eagerly loaded.

        ``get_by_code`` is not reused because it loads the vendor account
        instead of the geo location and does not scope to a tenant.
        """
        stmt = (
            select(self.model)
            .options(joinedload(self.model.geo_loc))
            .where(
                self.model.code == code,
                self.model.tenants_mst_code == tenant_code,
                self.model.is_deleted == False,  # noqa: E712
            )
        )
        result = await self.session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def set_locator_flags(
        self,
        infrastructure_code: str,
        tenant_code: str,
        flags: Dict[str, bool],
    ) -> int:
        """
        Merge boolean flags (``isRegistered`` / ``isListed``) into the JSONB
        ``locator`` of one tenant-owned record.

        ``merge_locator_fields`` is not reused here because it neither scopes to
        a tenant nor reports whether a row matched, and this path is driven
        straight from a user toggle. A NULL locator is coalesced to ``{}`` so
        the concat produces an object instead of NULL.

        Returns:
            Number of rows updated (0 when the code is unknown to the tenant)
        """
        if not flags:
            return 0
        stmt = (
            update(self.model)
            .where(
                self.model.code == infrastructure_code,
                self.model.tenants_mst_code == tenant_code,
                self.model.is_deleted == False,  # noqa: E712
            )
            .values(
                locator=func.coalesce(
                    self.model.locator, cast({}, JSONB)
                ).op("||")(cast(flags, JSONB))
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount or 0
