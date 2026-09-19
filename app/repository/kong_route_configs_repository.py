"""
Kong Route Config repository with specific route operations
"""

from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from sqlalchemy import select, update, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.kong_route_config_model import KongRouteConfigModel
from app.db.models.kong_route_group_model import KongRouteGroupModel
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.repository.base_repository import BaseRepository
from app.core.enum import DeploymentStatusEnum
from app.domain.factories.infrastructure_transaction_factory import make_kong_route_transaction


class KongRouteConfigsRepository(BaseRepository[KongRouteConfigModel]):
    """Repository for KongRouteConfig operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(KongRouteConfigModel, session)

    async def get_kong_route_record(
        self,
        filters: List[Any],
    ) -> Optional[KongRouteConfigModel]:
        """
        Fetch a single Kong route record matching the given filters.

        Pass any list of SQLAlchemy filter expressions and get back the
        first matching KongRouteConfigModel (or None).

        Args:
            filters: List of SQLAlchemy filter expressions to AND together.

        Returns:
            KongRouteConfigModel if found, None otherwise.

        Example:
            entry = await repo.get_kong_route_record([
                KongRouteConfigModel.api_name == "user_api",
                KongRouteConfigModel.http_method == "GET",
                KongRouteConfigModel.route_path == "~/api/v1/users$",
                KongRouteConfigModel.is_deleted == False,
            ])
        """
        stmt = select(self.model).where(and_(*filters))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_routes_for_service(
        self,
        services_code: str,
        environment: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> List[KongRouteConfigModel]:
        """
        List all active (not soft-deleted) Kong routes for a service, optionally
        scoped to an environment and region. Used by the v2 terragrunt generator
        to rebuild a service's kong_configs block from the database.

        Args:
            services_code: Service code (route_group_key derives from this).
            environment: Optional environment filter (dev/stage/qa/prod).
            geo_loc_mst_code: Optional region filter.

        Returns:
            List of KongRouteConfigModel, ordered by method then path.
        """
        filters: List[Any] = [
            self.model.services_mst_code == services_code,
            self.model.is_deleted == False,
        ]
        if environment:
            filters.append(self.model.environments_enum == environment)
        if geo_loc_mst_code:
            filters.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        stmt = (
            select(self.model)
            # Eager-load: generation reads plugins/group key off route_group, and a
            # lazy load on an async session raises MissingGreenlet.
            .options(selectinload(self.model.route_group))
            .where(and_(*filters))
            .order_by(self.model.http_method, self.model.route_path)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_deleted_routes_for_service(
        self,
        services_code: str,
        environment: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> List[KongRouteConfigModel]:
        """
        Soft-deleted routes for a service (+env/region). The incremental deploy
        uses these to surgically REMOVE the matching lines from the terragrunt.
        """
        filters: List[Any] = [
            self.model.services_mst_code == services_code,
            self.model.is_deleted == True,
        ]
        if environment:
            filters.append(self.model.environments_enum == environment)
        if geo_loc_mst_code:
            filters.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        stmt = (
            select(self.model)
            .options(selectinload(self.model.route_group))
            .where(and_(*filters))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_routes_for_gateway(
        self,
        environment: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> List[KongRouteConfigModel]:
        """
        List ALL active routes for a gateway (env + region), across every service.
        Used to build the common `kong_plugins` block, whose target_keys aggregate
        every devlift-managed route (the plugin config is common, not per-service).
        """
        filters: List[Any] = [self.model.is_deleted == False]
        if environment:
            filters.append(self.model.environments_enum == environment)
        if geo_loc_mst_code:
            filters.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        stmt = (
            select(self.model)
            .options(selectinload(self.model.route_group))
            # Outer-joined purely to ORDER BY the group's key — selectinload issues a
            # second query, so its columns are not available to the ORDER BY.
            .outerjoin(KongRouteGroupModel, self.model.kong_route_group_id == KongRouteGroupModel.id)
            .where(and_(*filters))
            .order_by(KongRouteGroupModel.route_group_key, self.model.http_method, self.model.route_path)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_group_id(
        self, group_id: int, include_deleted: bool = False,
    ) -> List[KongRouteConfigModel]:
        """
        A group's path rows, live only by default.

        Queried rather than read off `KongRouteGroupModel.routes`: that
        relationship is lazy, and touching it outside an eager load raises
        MissingGreenlet under async. It also has no is_deleted criteria, so it
        would hand back rows that have already left.

        include_deleted is for the save path, which has to tell "this path is
        new" from "this path was deleted and is coming back". Without it a
        returning path is INSERTed under a fresh code, leaving the old row
        soft-deleted beside it — two rows for one route.
        """
        filters = [self.model.kong_route_group_id == group_id]
        if not include_deleted:
            filters.append(self.model.is_deleted == False)
        stmt = select(self.model).where(and_(*filters)).order_by(self.model.route_path)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_code_with_group(self, code: str) -> Optional[KongRouteConfigModel]:
        """
        Fetch one route by code with its group eager-loaded.

        Use instead of get_by(code=...) whenever the caller reads group-level config
        (plugins / regex_priority / route_group_key): the plain getter does not load
        the relationship, and touching it on an async session raises MissingGreenlet.
        """
        stmt = (
            select(self.model)
            .options(selectinload(self.model.route_group))
            .where(self.model.code == code)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def bulk_update_creation_status(
        self,
        service_code: str,
        creation_status,
        environment: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> int:
        """
        Set creation_status on all active routes of a service (scoped to env/region
        when given). Used after a gateway deploy so the routes reflect ACTIVE/FAILED.
        Returns the number of rows updated.
        """
        filters: List[Any] = [
            self.model.services_mst_code == service_code,
            self.model.is_deleted == False,
        ]
        if environment:
            filters.append(self.model.environments_enum == environment)
        if geo_loc_mst_code:
            filters.append(self.model.geo_loc_mst_code == geo_loc_mst_code)

        stmt = (
            update(self.model)
            .where(and_(*filters))
            .values(
                creation_status=creation_status,
                creation_status_updated_at=datetime.now(timezone.utc),
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    async def check_route_exists(
        self,
        api_name: str,
        http_method: str,
        route_path: str,
        services_code: Optional[str] = None,
        environment: Optional[str] = None,
        route_group_id: Optional[int] = None,
    ) -> Optional[KongRouteConfigModel]:
        """
        Check if a route already exists for given API, method, and path.

        Args:
            api_name: API identifier in kong_configs
            http_method: HTTP method (GET, POST, etc.)
            route_path: Kong route pattern
            services_code: Optional service code (for service-specific routes)
            environment: Optional environment to scope the check (e.g. staging, production)
            route_group_id: Optional group to scope the check to. The SAME path may
                legitimately exist in two groups — that is an override, resolved by
                regex_priority, and the gateway relies on it (a public /health group
                at 200 shadowing the service's group at 0). Without this the lookup
                matches the other group's row and the caller re-points it, silently
                turning an override into a move.

        Returns:
            KongRouteConfigModel if exists, None otherwise

        Example:
            existing = await repo.check_route_exists(
                api_name="user_api",
                http_method="GET",
                route_path="~/api/v1/users$",
                environment="staging"
            )
        """
        # Match the anchored path OR the same path without its trailing `$`. Terragrunt
        # holds a handful of unanchored routes (e.g. `~/goblin-service/actuator/health`),
        # but the UI shows paths decompiled — `$` stripped — and compiling one back
        # always re-adds it. So re-saving an untouched imported path produced a string
        # that matched nothing, and the save became a delete + add: the Gateway tab
        # showed a deletion the user never asked for.
        variants = [route_path]
        if route_path.endswith("$"):
            variants.append(route_path[:-1])

        stmt = select(self.model).where(
            self.model.api_name == api_name,
            self.model.http_method == http_method,
            self.model.route_path.in_(variants),
            # Only ACTIVE rows: a soft-deleted route must NOT be resurrected in place
            # (that leaves is_deleted=True, so the re-added path never shows). Re-adding
            # a deleted path should create a fresh active row instead.
            self.model.is_deleted == False,
        )

        if services_code:
            stmt = stmt.where(self.model.services_mst_code == services_code)

        if environment:
            # Column is `environments_enum` (not `environment`) — matches the model.
            stmt = stmt.where(self.model.environments_enum == environment)

        if route_group_id is not None:
            stmt = stmt.where(self.model.kong_route_group_id == route_group_id)

        # Tolerate pre-existing duplicates (older buggy saves) instead of raising
        # MultipleResultsFound — take the oldest; load-time dedup cleans the rest.
        stmt = stmt.order_by(self.model.id.asc()).limit(1)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def update_creation_status(
        self,
        route_id: int,
        status: DeploymentStatusEnum,
        updated_by: Optional[str] = None
    ) -> Optional[KongRouteConfigModel]:
        """
        Update creation_status and related tracking fields for a route.

        Args:
            route_id: Route ID to update
            status: New deployment status
            updated_by: Email or username of updater (optional)

        Returns:
            Updated KongRouteConfigModel or None if not found

        Example:
            route = await repo.update_creation_status(
                route_id=123,
                status=DeploymentStatusEnum.PR_CREATED,
                updated_by="admin@example.com"
            )
        """
        route = await self.get_by_id(route_id)
        if not route:
            return None

        updates = {
            "creation_status": status,
            "creation_status_updated_at": datetime.now(timezone.utc)
        }
        if updated_by:
            updates["creation_status_updated_by"] = updated_by

        return await self.update(route, updates)

    async def link_to_gitops_workflow(
        self,
        route_ids: List[int],
        workflow_id: int
    ) -> None:
        """
        Link multiple routes to a GitOps workflow.

        Used when multiple routes share one PR/workflow.

        Args:
            route_ids: List of route IDs to link
            workflow_id: GitOps workflow detail ID

        Example:
            # Link multiple routes to same workflow
            await repo.link_to_gitops_workflow(
                route_ids=[1, 2, 3],
                workflow_id=789
            )
        """
        stmt = (
            update(self.model)
            .where(self.model.id.in_(route_ids))
            .values(gitops_workflow_id=workflow_id)
        )
        await self.session.execute(stmt)

    async def update_by_code(
        self,
        code: str,
        updates: Dict[str, Any]
    ) -> Optional[KongRouteConfigModel]:
        """
        Update a Kong route by code.

        Args:
            code: Kong route code
            updates: Dictionary of fields to update

        Returns:
            Updated KongRouteConfigModel or None if not found

        Example:
            route = await repo.update_by_code(
                code="kong-route-1",
                updates={"name": "Updated Name", "route_path": "~/new/path$"}
            )
        """
        route = await self.get_by(code=code)
        if not route:
            return None

        return await self.update(route, updates)

    async def update_resource_identifier(
        self,
        route_id: int,
        resource_identifier: str
    ) -> Optional[KongRouteConfigModel]:
        """
        Update resource_identifier after Kong route is created.

        Args:
            route_id: Route ID to update
            resource_identifier: Kong route ID from vendor

        Returns:
            Updated KongRouteConfigModel or None if not found

        Example:
            route = await repo.update_resource_identifier(
                route_id=123,
                resource_identifier="kong-route-abc123"
            )
        """
        route = await self.get_by_id(route_id)
        if not route:
            return None

        return await self.update(route, {"resource_identifier": resource_identifier})

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
        Get kong route transactions with LEFT JOIN to gitops_workflow_detail.

        Tenant isolation is achieved by joining through services_mst table.

        Args:
            tenant_code: Tenant code for isolation
            status: Optional filter by creation_status
            skip: Pagination offset
            limit: Page size
            sort_order: 'asc' or 'desc' by created_at
            paginate: If True, apply skip/limit in SQL. If False, return all for service-level pagination.

        Returns:
            Dict with 'total' count and 'data' list of transformed transactions
        """
        # Build query with LEFT JOIN to gitops_workflow_detail
        # Tenant isolation via JOIN to services_mst
        stmt = (
            select(
                # Kong route fields
                self.model.id,
                self.model.code,
                self.model.name,
                self.model.description,
                self.model.created_at,
                self.model.updated_at,
                self.model.is_active,
                self.model.is_deleted,
                self.model.services_mst_code,
                self.model.api_name,
                self.model.http_method,
                self.model.route_path,
                self.model.creation_status,
                self.model.creation_status_updated_by,
                self.model.creation_status_updated_at,
                self.model.resource_identifier,
                self.model.creation_error,
                self.model.gitops_workflow_id,
                # Related entity names
                ServicesMstModel.name.label('service_name'),
                ApplicationsMstModel.name.label('application_name'),
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
            .join(
                ServicesMstModel,
                self.model.services_mst_code == ServicesMstModel.code
            )
            .join(
                ApplicationsMstModel,
                ServicesMstModel.applications_mst_code == ApplicationsMstModel.code
            )
            .where(self.model.is_deleted == False)
            .where(ServicesMstModel.tenants_mst_code == tenant_code)  # Tenant isolation via service
            .where(self.model.gitops_workflow_id.isnot(None))
        )

        # Apply status filter
        if status:
            stmt = stmt.where(self.model.creation_status == status)

        # Sort by created_at
        if sort_order == "desc":
            stmt = stmt.order_by(self.model.created_at.desc())
        else:
            stmt = stmt.order_by(self.model.created_at.asc())

        # If paginating at SQL level, get total count first
        if paginate:
            count_stmt = (
                select(func.count())
                .select_from(self.model)
                .join(
                    ServicesMstModel,
                    self.model.services_mst_code == ServicesMstModel.code
                )
                .where(self.model.is_deleted == False)
                .where(ServicesMstModel.tenants_mst_code == tenant_code)
                .where(self.model.gitops_workflow_id.isnot(None))
            )
            if status:
                count_stmt = count_stmt.where(self.model.creation_status == status)
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
        data = [make_kong_route_transaction(row) for row in rows]

        return {
            "total": total if paginate else len(data),
            "data": data
        }
