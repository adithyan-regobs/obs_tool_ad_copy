"""
Kong Route Config Service

Dedicated service for creating/updating Kong Gateway routes.
Saves to kong_route_configs table — extracted from InfrastructureCreationService.
"""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.core.enum import DeploymentStatusEnum, EnvironmentEnum, WorkflowSourceTableEnum
from app.repository.kong_route_configs_repository import KongRouteConfigsRepository
from app.repository.kong_route_groups_repository import KongRouteGroupsRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.schemas.kong_route_schemas import (
    KongRouteConfigCreateRequest,
    KongRouteConfigCreateResponse,
    GatewayDelta,
    GatewayGroupItem,
    GatewayGroupSave,
    GatewayPathAction,
    GatewayPathInput,
    GatewayPathItem,
    GatewayPendingChange,
    GatewayScopeItem,
    GatewayStateResponse,
    GatewaySaveRequest,
    GatewaySavedGroup,
    GatewaySaveResponse,
)
from app.domain.factories.kong_route_config_factory import make_kong_route_config, make_kong_route_config_v2
from app.domain.policies.kong_route_group_naming import default_group_key
from app.domain.validators.kong_path_overlap_validator import (
    KongPathOverlapError,
    KongPathOverlapValidator,
)
from app.schemas.infrastructure_schemas import (
    InfrastructureCreateRequest,
    InfrastructureCreateResponse,
)
from app.services.workspace_service import WorkspaceService
from app.domain.validators.kong_route_validator import KongRouteValidator
# Still used by create_route (v1): that tab sends plain paths and knows nothing
# about regex. v2 stores what the user typed, so it does not compile.
from app.utils.kong_path_compiler import compile_route_path

logger = logging.getLogger(__name__)

#: infrastructuretype_ref_code values that mean "this is a kong route", so
#: InfrastructureCreationService.create_resource() hands the request here
#: instead of writing infrastructure_mst. Owned by this module because the
#: table it implies — kong_route_configs, a KRC code — is this module's.
KONG_INFRA_TYPE_REFS = ("kong_gateway", "kong_gateway_infrastructuretype_ref")

# Statuses that mean the route actually reached terragrunt. Mirrors the Gateway
# tab's statusFromCreation(), which maps exactly these to "deployed" — one rule,
# so the tab and the pending-changes diff cannot disagree about what is live.
_DEPLOYED_STATUSES = frozenset({
    DeploymentStatusEnum.ACTIVE,
    DeploymentStatusEnum.TERRAFORM_APPLIED,
    DeploymentStatusEnum.VENDOR_CREATED,
})


def _identity_key(route_group_key, http_method) -> str:
    """A group's identity when it has no code yet: its key and its method.

    Those two ARE the group in terragrunt — add_route finds kong_configs by
    name and the plugin target is "<group>-<method>" — so they identify a group
    that exists only as a proposal just as well as a KRG_ code identifies a
    saved one.
    """
    if not route_group_key:
        return ""
    return f"{route_group_key}|{(http_method or '').upper()}"


def _merged_paths(group, pending_rows) -> list:
    """This group's paths as the tab must see them: deployed plus proposed.

    With no draft, the table rows are the answer. With one, the draft's desired
    list is — it already describes the end state, so nothing has to be replayed
    action by action to reconstruct it.

    A path the draft TOUCHED comes back as INITIATED, which the tab renders as
    "Saved": stored, not yet live. That is exactly what a saved path looked like
    under the old flow, when the save wrote kong_route_configs itself. Paths the
    draft leaves alone keep the status they really have.

    Soft-deleted rows are dropped: a removal is described by the delta, not by a
    row, and it is still live in the gateway until the next deploy.
    """
    live = [r for r in (group.routes or []) if not r.is_deleted]
    status_by_path = {r.route_path: r.creation_status for r in live}
    code_by_path = {r.route_path: r.code for r in live}

    if not pending_rows:
        return [
            GatewayPathItem(
                code=r.code, route_path=r.route_path, creation_status=r.creation_status,
            )
            for r in sorted(live, key=lambda r: r.route_path or "")
        ]

    # One desired list per group. Two users cannot hold drafts on the same group
    # — the lane lock allows one live change per service — so the last entry
    # wins rather than being merged with a rival's.
    desired = None
    touched: set = set()
    for _row, entry in pending_rows:
        if entry.get("desired_paths") is not None:
            desired = entry.get("desired_paths") or []
        for action in (entry.get("paths") or []):
            if action.get("route_path"):
                touched.add(action["route_path"])

    if desired is None:
        # A row written before the entry carried its desired list holds the
        # actions and nothing else, and the end state cannot be rebuilt from
        # those alone — they name what moved, not the paths left untouched. The
        # live rows stand. Returning an empty list instead wiped every deployed
        # path off the tab, which is a far worse answer than "the deployed ones,
        # and this change is not reflected yet".
        return [
            GatewayPathItem(
                code=r.code, route_path=r.route_path, creation_status=r.creation_status,
            )
            for r in sorted(live, key=lambda r: r.route_path or "")
        ]

    out = []
    for item in desired:
        if not isinstance(item, dict):
            continue
        path = item.get("route_path")
        if not path:
            continue
        out.append(GatewayPathItem(
            # The real code when the path is already a row; None when this
            # change creates it. The tab sends the code back on save, and a
            # made-up one would send the server looking for a row that is not
            # there.
            code=item.get("code") or code_by_path.get(path),
            route_path=path,
            creation_status=(
                DeploymentStatusEnum.INITIATED if path in touched
                else status_by_path.get(path)
            ),
        ))
    return sorted(out, key=lambda p: p.route_path or "")


def _group_from_pending(pending_rows, user_code: str) -> GatewayGroupItem:
    """A route group that exists only as a proposal.

    Nothing in kong_route_groups backs it — this change creates it — so every
    field comes off the queue entry. code=None is the same null the tab sends on
    save to mean "create this group", so what it renders round-trips unchanged.
    """
    from app.repository.transaction_queue_repository import TransactionQueueRepository

    _row, entry = pending_rows[-1]
    return GatewayGroupItem(
        code=None,
        route_group_key=entry.get("route_group_key") or "",
        http_method=(entry.get("http_method") or "").upper(),
        api_name=None,
        plugins=list(entry.get("plugins") or entry.get("plugins_after") or []),
        regex_priority=int(entry.get("regex_priority")
                           or entry.get("regex_priority_after") or 0),
        # Ownership is a service-level fact settled at deploy, and claiming it
        # for a group that does not exist yet would let the tab show two owners.
        is_service_owner=False,
        updated_at=None,
        paths=[
            GatewayPathItem(
                code=item.get("code"),
                route_path=item.get("route_path"),
                creation_status=DeploymentStatusEnum.INITIATED,
            )
            for item in (entry.get("desired_paths") or [])
            if isinstance(item, dict) and item.get("route_path")
        ],
        pending=[
            GatewayPendingChange(
                queue_id=row.id,
                queue_code=row.code,
                in_flight=row.status in TransactionQueueRepository._IN_FLIGHT_STATUSES,
                status=(row.status.value if hasattr(row.status, "value") else str(row.status)),
                user_code=row.user_code,
                is_mine=row.user_code == user_code,
                delta=e,
            )
            for row, e in pending_rows
        ],
    )


class KongRouteConfigService:
    """
    Service for creating/updating Kong Gateway route configs.

    Flow:
    1. Validate service exists + tenant ownership
    2. Resolve api_name (from request or service name)
    3. Check for duplicate route (on create)
    4. Call factory → save to kong_route_configs table
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.kong_route_repo = KongRouteConfigsRepository(db)
        self.kong_route_group_repo = KongRouteGroupsRepository(db)
        self.services_repo = ServicesMstRepository(db)

    async def create_route(
        self,
        tenant_code: str,
        user_code: str,
        request: KongRouteConfigCreateRequest,
        user_email: str,
    ) -> KongRouteConfigCreateResponse:
        """
        Create or update a Kong Gateway route.

        Args:
            tenant_code: Tenant code from JWT
            request: Kong route creation/update request
            user_email: User email for tracking

        Returns:
            KongRouteConfigCreateResponse with table_name and code

        Raises:
            HTTPException 400: Duplicate route (on create)
            HTTPException 403: Tenant isolation violation
            HTTPException 404: Service or route not found
        """
        is_update = request.code is not None

        logger.info(
            f"{'Updating' if is_update else 'Creating'} Kong route: "
            f"service={request.service_mst_code}, method={request.http_method}, "
            f"path={request.route_path}, code={request.code}"
        )

        # Workspace access guard
        if request.application_code:
            workspace_svc = WorkspaceService(self.db)
            if not await workspace_svc.verify_app_workspace_access(user_code, tenant_code, request.application_code):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: application workspace is not accessible"
                )

        # 1. Validate service exists and belongs to tenant
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

        # 2. Resolve api_name — use request value or fall back to service name
        api_name = request.api_name or service.name

        # 2b. Compile the plain UI path to a Kong regex path (idempotent — a value
        #     already in ~/...$ form passes through unchanged).
        compiled_path = compile_route_path(request.route_path)

        # 3. For CREATE only: does this path already exist for this api_name+method?
        #    Not scoped by group — v1 (chat/KongGatewayTab) has no notion of route
        #    groups at all, so there is nothing to disambiguate an override by.
        existing_route = None
        if not is_update:
            existing_route = await self.kong_route_repo.check_route_exists(
                api_name=api_name,
                http_method=request.http_method,
                route_path=compiled_path,
                services_code=request.service_mst_code,
                environment=request.environment,
            )
            if existing_route:
                # Keep an unanchored legacy path exactly as deployed. It matched here
                # only because it is the same route with the trailing `$` missing, and
                # rewriting it would narrow what Kong matches (prefix -> anchored) on a
                # save where the user changed nothing about the path.
                same_route_unanchored = (
                    existing_route.route_path != compiled_path
                    and compiled_path.endswith("$")
                    and existing_route.route_path == compiled_path[:-1]
                )
                updates = {
                    "route_path": existing_route.route_path if same_route_unanchored else compiled_path,
                }
                kong_route = await self.kong_route_repo.update(existing_route, updates)
                await self.db.commit()
                logger.info(f"Upserted existing Kong route: code={kong_route.code}, id={kong_route.id}")
                return KongRouteConfigCreateResponse(
                    table_name=WorkflowSourceTableEnum.KONG_ROUTE,
                    code=kong_route.code,
                )

        # 4. On UPDATE, read the route (with its group, if any — older rows created
        #    while the dual-write was still in place may have one) so the identity
        #    comparison below can still tell a moved route from an edited one.
        existing = None
        old_group_key = old_plugins = None
        old_priority = 0
        if is_update:
            existing = await self.kong_route_repo.get_by_code_with_group(request.code)
            if not existing:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Kong route not found with code: {request.code}"
                )
            if existing.route_group:
                old_group_key = existing.route_group.route_group_key
                old_plugins = list(existing.route_group.plugins or [])
                old_priority = existing.route_group.regex_priority or 0

        # 5. Build route data via factory. No kong_route_group_id — v1 routes are
        #    not linked to a route group (that table is the Gateway tab's own).
        route_data = make_kong_route_config(
            api_name=api_name,
            http_method=request.http_method,
            route_path=compiled_path,
            services_mst_code=request.service_mst_code,
            environments_enum=request.environment,
            geo_loc_mst_code=request.geo_loc_mst_code,
            creation_status=DeploymentStatusEnum.INITIATED,
            creation_status_updated_by=user_email,
        )

        # 6. Create or Update in database
        if is_update:

            # The terragrunt LINE for a route is identified by (group, method, path).
            # If ANY of those change it's a MOVE — the old line must be removed and a
            # new one added. Model that as soft-delete-old + create-new so the
            # incremental deploy removes the old line and adds the new. (Rebuild mode
            # is unaffected: it regenerates from active rows either way.)
            identity_changed = (
                existing.route_path != compiled_path
                or (existing.http_method or "").upper() != request.http_method.upper()
                or (old_group_key or api_name) != (request.route_group_key or api_name)
            )
            if identity_changed:
                existing.soft_delete()
                self.db.add(existing)
                kong_route = await self.kong_route_repo.create(**route_data)
                await self.db.commit()
                logger.info(
                    f"Moved Kong route: soft-deleted {existing.code} → created {kong_route.code} "
                    f"({request.http_method} {compiled_path})"
                )
                return KongRouteConfigCreateResponse(
                    table_name=WorkflowSourceTableEnum.KONG_ROUTE,
                    code=kong_route.code,
                )

            # Same line — only plugins/priority changed. Update in place (preserve
            # code + vendor linkage); the plugins block rebuild picks up the change.
            updates = {
                "api_name": api_name,
            }
            # Compared against the group snapshot taken in step 4, if the route has
            # one (older rows created while the dual-write into kong_route_groups
            # was in place) — v1 no longer writes or updates that table itself.
            content_changed = (
                sorted(old_plugins or []) != sorted(request.plugins or [])
                or old_priority != (request.regex_priority or 0)
            )
            if content_changed:
                updates["creation_status"] = DeploymentStatusEnum.INITIATED
                updates["creation_status_updated_by"] = user_email
                updates["creation_status_updated_at"] = datetime.utcnow()
            kong_route = await self.kong_route_repo.update_by_code(
                code=request.code,
                updates=updates,
            )
            if not kong_route:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Kong route not found with code: {request.code}"
                )
            logger.info(
                f"Updated Kong route in place: code={kong_route.code}, content_changed={content_changed}"
            )
        else:
            kong_route = await self.kong_route_repo.create(**route_data)
            await self.db.commit()
            await self.db.refresh(kong_route)
            logger.info(f"Created Kong route: code={kong_route.code}, id={kong_route.id}")

        # 6. Response
        return KongRouteConfigCreateResponse(
            table_name=WorkflowSourceTableEnum.KONG_ROUTE,
            code=kong_route.code,
        )

    async def _guard_service_access(
        self,
        tenant_code: str,
        user_code: str,
        services_mst_code: str,
        application_code: Optional[str] = None,
    ):
        """
        Ensure the service exists, belongs to the tenant, and (if resolvable) the
        user has workspace access to its application. Returns the service.
        """
        service = await self.services_repo.get_by_code(services_mst_code)
        if not service:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Service not found: {services_mst_code}",
            )
        if service.tenants_mst_code != tenant_code:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service belongs to different tenant",
            )
        app_code = application_code or getattr(service, "applications_mst_code", None)
        if app_code:
            workspace_svc = WorkspaceService(self.db)
            if not await workspace_svc.verify_app_workspace_access(user_code, tenant_code, app_code):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: application workspace is not accessible",
                )
        return service

    async def get_gateway_state(
        self,
        tenant_code: str,
        user_code: str,
        service_mst_code: str,
        environment,
        geo_loc_mst_code: Optional[str] = None,
        application_code: Optional[str] = None,
    ) -> GatewayStateResponse:
        """
        The Gateway tab's read: every group for this (service, environment,
        region), its live paths, and its undeployed change if it has one.

        geo_loc_mst_code is optional, and resolved here when omitted. The tab
        normally takes the region off the canvas node, which reads it from
        service_configs — a service imported from terragrunt has routes but no
        config row, so it had no region to send and its tab came up empty. The
        route groups themselves are the one place that information definitely
        exists, so they answer it.

        Resolution NEVER merges regions. One candidate is used; several means
        none is chosen and `scopes` comes back instead, because a save has to
        name a single region (it stamps new groups with it) and a deploy writes
        one region's terragrunt file. Merging would silently put a new group in
        an arbitrary region.

        Three reads, deliberately not one join. Present state comes from the
        TABLES (what the generator will read, so the tab cannot show something
        other than what deploys); pending changes come from the QUEUE. The queue
        is never filtered by service/env/region — a group code already means
        exactly one of those — so scope is applied only when selecting groups.

        Clean groups are returned too, with pending=None. Filtering them out
        would hide every group that has nothing queued, which is most of them
        and all of the ones you might want to edit next.
        """
        from app.repository.transaction_queue_repository import TransactionQueueRepository
        from app.db.models.transaction_queue_model import TransactionQueueStatusEnum

        service = await self._guard_service_access(
            tenant_code, user_code, service_mst_code, application_code
        )

        env_value = getattr(environment, "value", environment)
        unresolved_scopes: list[GatewayScopeItem] = []
        if not geo_loc_mst_code:
            # Only this environment's scopes are candidates. A service with stage
            # in Mumbai and prod in Canada has one region per environment, and
            # offering the prod one while the tab is showing stage would be wrong.
            candidates = [
                s for s in await self.kong_route_group_repo.list_scopes(service_mst_code)
                if s.get("environment") == str(env_value) and s.get("geo_loc_mst_code")
            ]
            if len(candidates) == 1:
                geo_loc_mst_code = candidates[0]["geo_loc_mst_code"]
                logger.info(
                    "gateway region resolved for %s/%s: %s",
                    service_mst_code, env_value, geo_loc_mst_code,
                )
            elif len(candidates) > 1:
                # Ambiguous — return the choice rather than a merged, unsaveable view.
                logger.info(
                    "gateway region ambiguous for %s/%s: %s candidates",
                    service_mst_code, env_value, len(candidates),
                )
                unresolved_scopes = [GatewayScopeItem(**c) for c in candidates]

        if not geo_loc_mst_code:
            # Nothing to scope by: either the service has no gateway in this
            # environment, or the caller has to pick. Both are an empty state,
            # distinguished by whether `scopes` is populated.
            return GatewayStateResponse(
                service_mst_code=service_mst_code,
                service_name=service.name,
                environment=env_value,
                geo_loc_mst_code=None,
                scopes=unresolved_scopes,
            )

        groups = await self.kong_route_group_repo.list_for_scope(
            services_mst_code=service_mst_code,
            environments_enum=environment,
            geo_loc_mst_code=geo_loc_mst_code,
        )

        # ONE row per (scope, user) now, each carrying every group that user
        # changed — so the read is per SCOPE and the fan-out below puts each
        # group's slice back where the tab expects it. The response shape is
        # unchanged: still one pending entry per user per group.
        pending_rows_all = await TransactionQueueRepository(self.db).get_pending_for_scope(
            tenant_code=tenant_code,
            services_mst_code=service_mst_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
        )
        # Self-heal rows stuck at PR_RAISED. A gateway PR closed on GitHub
        # without merging tells us nothing — the row stayed pr_raised, so
        # in_flight held the tab locked (and saves 409ing) forever; the only
        # way out was the PR-history listing, which happens to run the same
        # sync. Do it here, where the lock is actually produced. Rows whose
        # status just changed are skipped so a live deploy's polling doesn't
        # hit GitHub every read.
        #
        # Runs on the flat SCOPE rows, before the per-group fan-out, and the
        # re-read after a heal is the same scope lookup as the initial one.
        if await self._sync_stale_gateway_prs(pending_rows_all):
            pending_rows_all = await TransactionQueueRepository(self.db).get_pending_for_scope(
                tenant_code=tenant_code,
                services_mst_code=service_mst_code,
                environment=environment,
                geo_loc_mst_code=geo_loc_mst_code,
            )

        # Keyed by group code when the group exists, and by identity when it does
        # not. A change that CREATES a group has no code to point at — under the
        # old flow the save wrote kong_route_groups first so there always was
        # one, but a draft writes no tables, so a brand-new group lives only
        # here. Keyed by code alone it was unreachable, and the group vanished
        # from the tab between saving it and deploying it.
        pending_by_group: dict[str, list] = {}
        for row in pending_rows_all:
            # Drafts are the author's private work — the POC rule, applied to
            # this read like everywhere else. Without it, one user's saved-but-
            # unsubmitted routes rendered on every colleague's Gateway tab as if
            # they were the service's state (Settings and Env & Permissions
            # already hid theirs; this was the one surface that leaked).
            # SUBMITTED and APPROVED rows stay visible to everyone: they hold
            # the service's review lane, and the tab must show what is pending
            # against it — read-only, marked with whose it is.
            if (
                row.status == TransactionQueueStatusEnum.DRAFT
                and row.user_code != user_code
            ):
                continue
            for entry in ((row.config_snapshot or {}).get("groups") or []):
                if not isinstance(entry, dict):
                    continue
                key = entry.get("group_code") or _identity_key(
                    entry.get("route_group_key"), entry.get("http_method")
                )
                if not key:
                    continue
                pending_by_group.setdefault(key, []).append((row, entry))

        items = []
        seen_pending_keys: set = set()
        for g in groups:
            pending_rows = (
                pending_by_group.get(g.code)
                or pending_by_group.get(_identity_key(g.route_group_key, g.http_method))
                or []
            )
            seen_pending_keys.add(g.code)
            seen_pending_keys.add(_identity_key(g.route_group_key, g.http_method))
            items.append(GatewayGroupItem(
                code=g.code,
                route_group_key=g.route_group_key,
                http_method=g.http_method,
                api_name=g.api_name,
                plugins=list(g.plugins or []),
                regex_priority=g.regex_priority or 0,
                is_service_owner=bool(g.is_service_owner),
                updated_at=g.updated_at,
                # selectinload does not filter soft-deleted rows — the relationship
                # carries no criteria — so drop them here. A deleted path is still
                # live in the gateway until the next deploy, but it is a REMOVAL,
                # and it is described by the delta, not by a row.
                # Deployed paths, plus the ones that exist only in a draft.
                #
                # `paths` has always meant "what this group will hold", not "what
                # a table row says": the old save wrote its edits into
                # kong_route_configs immediately, so the two coincided. A draft
                # writes no tables — that is the whole point of the gate — so the
                # queue row's desired list is merged in here instead, and the tab
                # reads exactly what it always did.
                paths=_merged_paths(g, pending_rows),
                # One entry per user — rows are keyed (scope, user), so every
                # user's undeployed slice is visible and none shadows another.
                #
                # `delta` is this group's ENTRY out of the row's snapshot, not the
                # whole snapshot: the row now spans every group the user changed,
                # and handing the tab all of them would make each group render its
                # neighbours' work as its own. queue_id/queue_code are the row's,
                # so they repeat across a user's groups — one row, one deploy.
                pending=[
                    GatewayPendingChange(
                        queue_id=row.id,
                        queue_code=row.code,
                        in_flight=row.status in TransactionQueueRepository._IN_FLIGHT_STATUSES,
                        status=(
                            row.status.value
                            if hasattr(row.status, "value") else str(row.status)
                        ),
                        user_code=row.user_code,
                        is_mine=row.user_code == user_code,
                        delta=entry,
                    )
                    for row, entry in pending_rows
                ],
            ))

        # A group that no table row backs yet: this change creates it. Built from
        # the queue entry alone, with code=None — the same null the tab sends on
        # save to mean "create this group", so it round-trips unchanged.
        for key, entries in pending_by_group.items():
            if key in seen_pending_keys:
                continue
            items.append(_group_from_pending(entries, user_code))

        return GatewayStateResponse(
            service_mst_code=service_mst_code,
            service_name=service.name,
            environment=getattr(environment, "value", environment),
            geo_loc_mst_code=geo_loc_mst_code,
            groups=items,
            total_groups=len(items),
            pending_groups=sum(1 for i in items if i.pending),
        )

    # A pr_raised row younger than this is likely a live deploy being polled;
    # skip it so gateway reads don't call GitHub on every poll. Anything older
    # that is still pr_raised is either genuinely awaiting merge (the sync is
    # then a no-op) or stale (the sync settles it).
    _PR_SYNC_MIN_AGE_SECONDS = 60

    async def _sync_stale_gateway_prs(self, pending_rows: list) -> bool:
        """
        Check GitHub for pending gateway rows sitting at PR_RAISED and settle
        any whose PR was closed externally (pr_rejected on close-without-merge,
        pr_approved on merge). Returns True when anything changed, so the
        caller re-reads pending state.

        Takes the flat scope-row list from get_pending_for_scope — NOT the
        per-group fan-out, whose values are (row, entry) tuples; iterating
        those as rows is exactly the 'tuple has no attribute status' 500.

        Newest PR per row only — syncing an old redeployed PR would overwrite
        the row's current one (see get_latest_pr_refs_for_queue_codes). Sync
        failures are swallowed: a GitHub hiccup must not take down the tab.
        """
        from app.db.models.transaction_queue_model import TransactionQueueStatusEnum

        now = datetime.now(timezone.utc)
        stale_codes = []
        for row in pending_rows:
            if row.status != TransactionQueueStatusEnum.PR_RAISED:
                continue
            ts = row.status_last_updated_at or row.updated_at
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts is None or (now - ts).total_seconds() >= self._PR_SYNC_MIN_AGE_SECONDS:
                stale_codes.append(row.code)
        if not stale_codes:
            return False

        from app.repository.transaction_queue_repository import TransactionQueueRepository
        refs = await TransactionQueueRepository(self.db).get_latest_pr_refs_for_queue_codes(stale_codes)
        if not refs:
            return False

        from app.services.transaction_queue_service import TransactionQueueService
        svc = TransactionQueueService(self.db)
        healed = False
        synced: set[int] = set()
        for ref in refs:
            pr_number = ref.get("pr_number")
            if not pr_number or pr_number in synced:
                continue
            synced.add(pr_number)
            try:
                result = await svc.sync_pr_status_from_github(
                    pr_number,
                    git_repository=ref.get("git_repository"),
                    pr_url=ref.get("pr_url"),
                )
            except Exception as e:
                logger.warning("gateway PR sync failed for #%s: %s", pr_number, e)
                continue
            if result and result.get("status") not in (None, "pr_raised"):
                healed = True
        return healed

    async def _scope_config_code(
        self, tenant_code: str, service_mst_code: str, environment, geo_loc_mst_code: str
    ) -> str:
        """A service_configs code for this gateway scope — what the queue row stores.

        ANY row in the scope will do, and that is not laziness. Every lookup
        resolves the stored code back THROUGH service_configs onto its own scope
        columns (see TransactionQueueRepository._scope_config_codes), so a scope
        holding both an ECS and an EKS config is found from either — whichever
        code was written. Ordered by id purely so repeat saves keep stamping the
        same one, which makes the rows easier to read by hand.

        Live rows are preferred, but a soft-deleted one is accepted rather than
        failing: the config is only a NAME for the scope, and the gateway's routes
        never depended on it.

        Raises when the scope has no config row at all. That is a real gap rather
        than something to paper over — a queue row with no resolvable
        transaction_code would be invisible to every lookup the moment it is
        written, so failing here is the only way the user finds out.
        """
        from app.db.models.service_config_model import ServiceConfigModel
        from sqlalchemy import select

        rows = (await self.db.execute(
            select(ServiceConfigModel)
            .where(
                ServiceConfigModel.tenant_mst_code == tenant_code,
                ServiceConfigModel.services_mst_code == service_mst_code,
                ServiceConfigModel.environment == environment,
                ServiceConfigModel.geo_loc_mst_code == geo_loc_mst_code,
            )
            .order_by(ServiceConfigModel.id)
        )).scalars().all()

        chosen = next((r for r in rows if not r.is_deleted), rows[0] if rows else None)
        if chosen is None:
            env_value = getattr(environment, "value", environment)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"No service configuration exists for '{service_mst_code}' in "
                    f"{env_value}/{geo_loc_mst_code}, so this gateway change cannot be "
                    f"queued. Create the service's configuration for this environment "
                    f"and region first, then save again."
                ),
            )
        return chosen.code

    async def save_gateway_changes(
        self,
        tenant_code: str,
        user_code: str,
        request: GatewaySaveRequest,
        user_email: Optional[str] = None,
    ) -> GatewaySaveResponse:
        """
        Persist pending gateway changes for one service/environment/region.

        Per group, in this order — the order is the point:
          1. claim the group on its updated_at (0 rows => somebody else saved)
          2. reconcile its paths to the desired list
        then ONCE, after the loop:
          3. upsert THIS USER's pending queue row, carrying EVERY changed group

        All of it in ONE transaction. If step 3 fails, 1 and 2 roll back, so the
        tables can never hold a change with no queue row to deploy it.

        One row per (scope, user), not per group, because one deploy writes ONE
        terragrunt gateway file covering every group in the scope. Three changed
        groups used to mean three rows, three file-locator passes and three
        generator calls that all edited the same staged buffer — a split created
        at save and undone moments later.

        The in-flight check moves with it. It is now scope-level and sits BEFORE
        the loop, deliberately: _claim_group bumps each group's updated_at, so a
        save rejected halfway would still have invalidated the client's tokens for
        the groups it had already claimed.

        The group LOCK stays per group. Claiming every group in the scope would
        hand every other user a 409 on groups they never touched, which is exactly
        what the client avoids by sending only changed groups.

        A lock failure on ANY group aborts the WHOLE request. Committing the
        groups that happened to be checked first would leave the client holding
        stale tokens for the rest, and a partial gateway save is not something a
        user can reason about.

        Every path in the delta is compiled here first — see _compile_delta_paths.
        """
        from app.repository.transaction_queue_repository import TransactionQueueRepository

        service = await self._guard_service_access(
            tenant_code, user_code, request.service_mst_code, request.application_code
        )
        api_name = service.name
        env_value = getattr(request.environment, "value", request.environment)
        queue_repo = TransactionQueueRepository(self.db)
        results: list = []
        entries: list[dict] = []

        try:
            # Refuse while ANY group in this scope is mid-deploy — whoever owns it.
            # Not just to avoid a confusing screen: the delta is computed against a
            # baseline built by undoing the PENDING change, so an in-flight one is
            # treated as already live. If it then fails or its PR is closed, its
            # routes are stranded — in the tables, absent from the file, and
            # unreachable by any later change set.
            #
            # Scope-level because the deploy is: one PR against one gateway file.
            # A per-group check let a second user edit a different group of the
            # same service mid-deploy and race a second PR against that file.
            #
            # Enforced here rather than only in the UI: a stale tab, a second
            # window, or a direct call would otherwise walk straight past it.
            in_flight = await queue_repo.get_in_flight_for_scope(
                tenant_code=tenant_code,
                services_mst_code=request.service_mst_code,
                environment=request.environment,
                geo_loc_mst_code=request.geo_loc_mst_code,
            )
            if in_flight is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"'{service.name}' gateway is deploying right now "
                        f"({in_flight.code}). Wait for it to finish, then reload the "
                        f"Gateway tab and make your change."
                    ),
                )

            for g in request.groups:
                self._compile_delta_paths(g.delta)
                if g.code:
                    # Two users may hold pending changes on the same group — but
                    # never on the same PATH. The other row's old_path is matched
                    # against the deployed file at generation time; a second change
                    # to the same line makes it stale, and replace_route_path then
                    # finds nothing and falls back to APPEND — both routes end up
                    # live.
                    await self._guard_path_overlap(
                        queue_repo, tenant_code, request, g, user_code
                    )
                group = await self._claim_group(g, request, api_name)
                counts = await self._reconcile_paths(group, g, request, api_name, user_email)

                if not g.delta.is_empty():
                    # Group identity travels WITH the delta. The generator reads
                    # route_group_key and http_method straight off the entry, so it
                    # needs no kong_route_groups lookup to apply it.
                    entries.append({
                        "group_code": group.code,
                        "route_group_key": g.route_group_key,
                        "http_method": (g.http_method or "").upper(),
                        **g.delta.model_dump(),
                    })

                results.append(GatewaySavedGroup(
                    group_code=group.code,
                    queue_code=None,  # filled in below — one code for all groups
                    updated_at=group.updated_at,
                    **counts,
                ))

            if entries:
                # The lane lock, gateway half — the same rule settings enforces
                # in ApprovalService.add_to_queue. A submitted row cannot be
                # rewritten under its reviewer, and the upsert below would
                # answer that by inserting a second live row for the scope.
                # Refuse instead, and say what to do about it.
                submitted = await queue_repo.find_submitted_for_scope(
                    tenant_code=tenant_code,
                    user_code=user_code,
                    services_mst_code=request.service_mst_code,
                    environment=request.environment,
                    geo_loc_mst_code=request.geo_loc_mst_code,
                )
                if submitted is not None:
                    raise HTTPException(
                        409,
                        f"This service already has a gateway change submit "
                        f"({submitted.code}) — withdraw it or ask a reviewer to "
                        f"send it back before starting another.",
                    )
                queue_row = await queue_repo.upsert_pending_for_scope(
                    transaction_code=await self._scope_config_code(
                        tenant_code, request.service_mst_code,
                        request.environment, request.geo_loc_mst_code,
                    ),
                    tenant_code=tenant_code,
                    user_code=user_code,
                    services_mst_code=request.service_mst_code,
                    environment=request.environment,
                    geo_loc_mst_code=request.geo_loc_mst_code,
                    snapshot={
                        # Scope, carried for the generator — which must not have to
                        # join anything to know which file it is editing. product_name
                        # is deliberately NOT here: it comes from the application NAME,
                        # which a rename would make stale, so it is resolved live at
                        # deploy time.
                        "service_mst_code": request.service_mst_code,
                        "api_name": api_name,
                        "environment": env_value,
                        "geo_loc_mst_code": request.geo_loc_mst_code,
                        "groups": entries,
                    },
                    display_name=self._gateway_display_name(service.name, entries),
                )
                queue_code = queue_row.code
            else:
                # Every edit undone across every group. Drop the row rather than
                # storing an empty change set — a deploy of "no changes" produces no
                # diff, and the tab must read the scope as clean.
                queue_code = None
                await queue_repo.clear_pending_for_scope(
                    tenant_code=tenant_code,
                    user_code=user_code,
                    services_mst_code=request.service_mst_code,
                    environment=request.environment,
                    geo_loc_mst_code=request.geo_loc_mst_code,
                )

            # One row, so every group reports the same queue code. Groups whose own
            # delta was empty share it too: they were still saved (their paths were
            # reconciled), and the row is what will deploy the scope.
            for saved in results:
                saved.queue_code = queue_code

            await self.db.commit()
        except HTTPException:
            await self.db.rollback()
            raise
        except Exception:
            await self.db.rollback()
            raise

        # updated_at is set by onupdate at flush time; re-read so the client gets
        # the token the next save will actually be checked against.
        for saved in results:
            row = await self.kong_route_group_repo.get_by_code(saved.group_code)
            if row is not None:
                saved.updated_at = row.updated_at

        logger.info(
            "Gateway save: service=%s env=%s groups=%d",
            request.service_mst_code,
            getattr(request.environment, "value", request.environment),
            len(results),
        )
        return GatewaySaveResponse(success=True, groups=results)

    @staticmethod

    def _compile_delta_paths(delta) -> None:
        """
        Put every path in the change set into Kong form, in place.

        The generator writes these VERBATIM into terragrunt — add_route and
        replace_route_path do not compile — so a path that reaches it uncompiled
        lands in the gateway as a prefix match where an anchored regex was meant.
        `~/x$` and `/x` are different routes to Kong; the second also matches
        `/x/anything`.

        Needed because the tab sends two forms: an untouched path arrives already
        compiled (it came from the database that way), but one the user edited in
        this session arrives as the plain text they typed. Only the second is
        wrong, which is why it survived until an edit was deployed.

        Done server-side because the client has no compiler, and because nothing
        should be able to hand the generator a form that has not been through
        this. compile_route_path is idempotent, so compiled input passes through.
        """
        for a in delta.paths or []:
            if a.route_path:
                a.route_path = compile_route_path(a.route_path)
            # old_path is matched against the line already IN the file, so it must
            # be compiled too — an uncompiled one simply finds nothing, and
            # replace_route_path then falls back to appending, leaving the old
            # route live alongside the new one.
            if a.old_path:
                a.old_path = compile_route_path(a.old_path)

    async def _guard_path_overlap(
        self, queue_repo, tenant_code: str, request, g, user_code: str
    ) -> None:
        """
        Refuse a save whose delta touches a path another user already has queued
        for the same group.

        Compared by code AND by compiled path string: an 'add' entry carries no
        code yet, but a second user touching that path WILL reference it by path
        (or by the code the reconcile gave it) — either key catches the clash.
        Plugin/priority changes are not guarded: both deltas record the full
        desired set, so the later deploy simply wins, nothing is corrupted.

        The fetch is scope-wide because rows are, but the COMPARISON stays per
        group: a teammate's row spans every group they changed, and only the
        entry for THIS group can clash. Comparing whole snapshots would reject a
        save because the same path exists under a different group and method,
        which is a different Kong route entirely and perfectly legal.

        Runs after _compile_delta_paths, so both sides hold compiled paths.
        """
        mine = self._delta_touch_set(g.delta.model_dump())
        if not mine:
            return

        rows = await queue_repo.get_pending_for_scope(
            tenant_code=tenant_code,
            services_mst_code=request.service_mst_code,
            environment=request.environment,
            geo_loc_mst_code=request.geo_loc_mst_code,
        )
        for other in rows:
            if other.user_code == user_code:
                continue
            for entry in ((other.config_snapshot or {}).get("groups") or []):
                if not isinstance(entry, dict) or entry.get("group_code") != g.code:
                    continue
                clash = mine & self._delta_touch_set(entry)
                if clash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"'{g.route_group_key} · {(g.http_method or '').upper()}': "
                            f"{sorted(clash)[0]} already has an undeployed change by "
                            f"{other.user_code} ({other.code}). Deploy or undo that "
                            f"change first, then reload the Gateway tab."
                        ),
                    )

    @staticmethod

    def _delta_touch_set(delta: dict) -> set:
        """Every code and compiled path a delta lays claim to — for overlap checks."""
        touched = set()
        for e in (delta.get("paths") or []):
            if not isinstance(e, dict):
                continue
            for k in ("code", "route_path", "old_path"):
                if e.get(k):
                    touched.add(e[k])
        return touched

    @staticmethod

    def _gateway_display_name(service_name: str, entries: list) -> str:
        """"goms — 2 groups, 5 path changes, plugins", for the queue list and modal.

        Service-level now, because the row is: one queue card per gateway deploy
        rather than one per group. Naming a single group would be a lie about what
        the card ships — deploying it writes every group in the snapshot.
        """
        groups = len(entries)
        paths = sum(len(e.get("paths") or []) for e in entries)
        plugins_changed = any(
            sorted(e.get("plugins_before") or []) != sorted(e.get("plugins_after") or [])
            for e in entries
        )

        bits = [f"{groups} group{'s' if groups != 1 else ''}"]
        if paths:
            bits.append(f"{paths} path change{'s' if paths != 1 else ''}")
        if plugins_changed:
            bits.append("plugins")
        return f"{service_name} — {', '.join(bits)}"

    async def _claim_group(self, g, request, api_name: str):
        """
        Take the group's lock and write its group-level config.

        The UPDATE carries `AND updated_at = :token`, so a client working from a
        stale form affects 0 rows and gets a 409 instead of silently overwriting
        whoever saved in between. A new group (code=None) has nothing to race
        against and skips the check.
        """
        from sqlalchemy import update as sa_update

        method = (g.http_method or "").upper()

        # Group-level validation. The v2 save chain (GatewaySaveRequest ->
        # GatewayGroupSave -> GatewayPathInput) carries no schema validators, so
        # these are the only server-side checks on the group's own fields — the
        # v1 request schema validates its equivalents, v2 never did.
        #
        # route_group_key matters most: it becomes the terragrunt kong_configs
        # map key. Checked on every save rather than only when changed, unlike
        # the path check in _reconcile_paths — all 195 stored groups already
        # satisfy the rule, so there is no legacy row to strand.
        KongRouteValidator.validate_route_group_key(g.route_group_key)
        KongRouteValidator.validate_http_method(method)
        KongRouteValidator.validate_plugins(g.plugins)
        KongRouteValidator.validate_regex_priority(g.regex_priority)

        if not g.code:
            # code=None claims this is a NEW group. If one already exists for the
            # identity, the client is working from a stale tab — and letting
            # get_or_create adopt it would refresh its plugins with no lock check,
            # silently overwriting whoever created it.
            clash = await self.kong_route_group_repo.get_by_identity(
                route_group_key=g.route_group_key,
                http_method=method,
                services_mst_code=request.service_mst_code,
                environments_enum=request.environment,
                geo_loc_mst_code=request.geo_loc_mst_code,
            )
            if clash is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"'{g.route_group_key} · {method}' already exists. Reload the "
                        f"Gateway tab and apply your change to the existing group."
                    ),
                )
            # get_or_create also refreshes plugins/priority, so this doubles as the write.
            return await self.kong_route_group_repo.get_or_create(
                route_group_key=g.route_group_key,
                http_method=method,
                api_name=api_name,
                services_mst_code=request.service_mst_code,
                environments_enum=request.environment,
                geo_loc_mst_code=request.geo_loc_mst_code,
                plugins=list(g.plugins or []),
                regex_priority=g.regex_priority or 0,
            )

        group = await self.kong_route_group_repo.get_by_code(g.code)
        if group is None or group.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Route group not found: {g.code}",
            )
        if group.services_mst_code != request.service_mst_code:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Route group {g.code} belongs to a different service",
            )
        if g.updated_at is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"updated_at is required for existing group {g.code} — without it a "
                    f"stale form would overwrite a newer save"
                ),
            )

        # Nothing about the group itself changed — only its paths, or nothing at all.
        # Skip the UPDATE so updated_at does not move: bumping it would invalidate the
        # token every other open tab is holding for this group, turning a group-level
        # lock back into a service-level one. Still verify the token first, so a stale
        # client cannot slip its path edits past the check.
        unchanged = (
            sorted(group.plugins or []) == sorted(g.plugins or [])
            and (group.regex_priority or 0) == (g.regex_priority or 0)
            and (group.route_group_key or "") == (g.route_group_key or "")
        )
        if unchanged:
            if group.updated_at != g.updated_at:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"'{g.route_group_key} · {method}' was changed by someone else since "
                        f"you loaded it. Reload the Gateway tab and redo the change."
                    ),
                )
            return group

        m = self.kong_route_group_repo.model
        result = await self.db.execute(
            sa_update(m)
            .where(m.code == g.code, m.updated_at == g.updated_at)
            .values(
                plugins=list(g.plugins or []),
                regex_priority=g.regex_priority or 0,
                route_group_key=g.route_group_key,
                api_name=api_name,
                updated_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"'{g.route_group_key} · {method}' was changed by someone else since "
                    f"you loaded it. Reload the Gateway tab and redo the change."
                ),
            )
        await self.db.refresh(group)
        return group

    async def _reconcile_paths(
        self, group, g, request, api_name: str, user_email: Optional[str] = None,
        creation_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    ) -> dict:
        """
        Make the group's path rows match the desired list.

        Reconciled rather than applied as a diff, deliberately: the delta is
        cumulative since the last DEPLOY, while the tables already hold every
        earlier save. Replaying it would insert the same path twice. Matching
        against the desired list makes a repeat save a no-op and an undo an
        actual removal.

        Paths carry a code once written, so an edit is "same code, new path" and
        keeps its identity — no delete-and-recreate, and the queue row's
        old_path stays valid.

        `creation_status` is what a written or changed row lands on. It defaults
        to INITIATED for the legacy save path, which stages rows ahead of a
        deploy. The post-merge write passes ACTIVE: it runs only once the routes
        are in the merged file, so the row is live the moment it exists and there
        is no staged state for it to pass through.
        """
        all_rows = await self.kong_route_repo.list_by_group_id(group.id, include_deleted=True)
        live = {r.code: r for r in all_rows if not r.is_deleted}
        # Soft-deleted rows are kept to hand so a path arriving with a code we have
        # already removed is RESTORED rather than inserted afresh. Inserting would
        # leave the deleted row beside a new one — two rows for one route, and the
        # deletion silently undone.
        gone = {r.code: r for r in all_rows if r.is_deleted}
        method = (g.http_method or "").upper()
        kept: set = set()
        created = updated = removed = promoted = 0

        for p in g.paths:
            # Stored exactly as typed. The v2 UI now takes a Kong regex directly, so
            # there is nothing to compile — what the user sees is what Kong matches
            # on, and the lossy decompile/recompile round-trip is gone. v1
            # (create_route) still compiles: its tab sends plain paths and has no
            # notion of regex, so compile_route_path stays for that flow.
            desired = (p.route_path or "").strip()
            row = live.get(p.code) if p.code else None

            # Validate only what is new or actually changed. A group save resends
            # every path in the group, including ones written before this rule
            # existed (e.g. the legacy "/dummy-route$" shape, anchored with no "~"),
            # and validating those would fail the whole save on a path the user
            # never touched.
            if row is None or row.route_path != desired:
                KongRouteValidator.validate_route_path(desired)

            if row is not None:
                if row.route_path != desired:
                    row.route_path = desired
                    # name embeds the path, so an edit has to refresh it or the row
                    # keeps advertising the path it used to have.
                    row.name = f"Kong Route - {api_name} - {method} {desired[:50]}"
                    row.creation_status = creation_status
                    # Who staged it. Recorded at SAVE, not only at deploy: a route
                    # saved and never shipped would otherwise carry no attribution
                    # at all — exactly the state a closed PR leaves behind.
                    row.creation_status_updated_by = user_email or row.creation_status_updated_by
                    row.creation_status_updated_at = datetime.now(timezone.utc)
                    updated += 1
                elif row.creation_status != creation_status:
                    # Same path, new stage. The write happens TWICE per change —
                    # PR_CREATED when the pull request exists, ACTIVE once it
                    # merges — and a path whose text did not move between them
                    # takes neither branch above. Without this it would keep the
                    # status it was first written with and never reach ACTIVE,
                    # leaving every unedited route reading as "deploying" for good.
                    row.creation_status = creation_status
                    row.creation_status_updated_by = user_email or row.creation_status_updated_by
                    row.creation_status_updated_at = datetime.now(timezone.utc)
                    row.updated_at = datetime.now(timezone.utc)
                    self.db.add(row)
                    promoted += 1
                kept.add(row.code)
                continue

            # Known code, but the row is soft-deleted: this path is coming back.
            # Revive it so it keeps its identity — the queue delta and the terragrunt
            # file both refer to routes by code.
            revived = gone.get(p.code) if p.code else None
            if revived is not None:
                revived.is_deleted = False
                revived.is_active = True
                revived.route_path = desired
                revived.name = f"Kong Route - {api_name} - {method} {desired[:50]}"
                revived.creation_status = creation_status
                revived.creation_status_updated_by = user_email or revived.creation_status_updated_by
                revived.creation_status_updated_at = datetime.now(timezone.utc)
                revived.updated_at = datetime.now(timezone.utc)
                self.db.add(revived)
                kept.add(revived.code)
                updated += 1
                continue

            data = make_kong_route_config_v2(
                api_name=api_name,
                http_method=method,
                route_path=desired,
                services_mst_code=request.service_mst_code,
                environments_enum=request.environment,
                geo_loc_mst_code=request.geo_loc_mst_code,
                creation_status=creation_status,
                creation_status_updated_by=user_email,
                kong_route_group_id=group.id,
            )
            new_row = self.kong_route_repo.model(**data)
            # kong_route_configs.updated_at has no DB default (only created_at
            # does), so an INSERT leaves it NULL and the row looks untouched.
            new_row.updated_at = datetime.now(timezone.utc)
            self.db.add(new_row)
            await self.db.flush()
            kept.add(new_row.code)
            created += 1

        for code, row in live.items():
            if code not in kept:
                row.soft_delete()
                row.updated_at = datetime.now(timezone.utc)
                self.db.add(row)
                removed += 1

        return {"created": created, "updated": updated, "removed": removed,
                "promoted": promoted}

    async def apply_deployed_gateway_snapshot(
        self, snapshot: dict, user_email: Optional[str] = None,
        creation_status: DeploymentStatusEnum = DeploymentStatusEnum.ACTIVE,
    ) -> dict:
        """Write a shipped gateway change into kong_route_groups / kong_route_configs.

        The only writer of those tables: save, submit and approve leave them
        alone, so what they hold is what has actually been shipped. Called twice
        per change — PR_CREATED once the PR exists, ACTIVE once it merges.

        Group identity comes from the ENTRY, not from group_code: a change that
        creates a group has no code to point at. Idempotent, so a Temporal retry
        converges. Does NOT commit — the caller owns the transaction.
        """
        empty = {"groups": 0, "created": 0, "updated": 0, "removed": 0, "promoted": 0}
        groups = [g for g in (snapshot.get("groups") or []) if isinstance(g, dict)]
        if not groups:
            return dict(empty)

        service_mst_code = (
            snapshot.get("service_mst_code") or snapshot.get("services_mst_code")
        )
        if not service_mst_code:
            raise ValueError(
                "gateway snapshot carries no service_mst_code — cannot write its routes"
            )

        api_name = snapshot.get("api_name") or snapshot.get("service_name") or ""
        geo_loc_mst_code = snapshot.get("geo_loc_mst_code")

        # JSONB stores the plain value ("dev"); the identity filters compare
        # against an Enum column, where a raw string matches nothing.
        environment = snapshot.get("environment")
        if isinstance(environment, str):
            environment = EnvironmentEnum(environment)

        # _reconcile_paths reads attributes (it is shared with the v2 save, which
        # passes pydantic models), so the snapshot's dicts are adapted here.
        scope = SimpleNamespace(
            service_mst_code=service_mst_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
        )

        totals = dict(empty)

        for entry in groups:
            if "desired_paths" not in entry:
                # No end state to reconcile to. Skipped rather than treated as
                # an empty one, which would soft-delete every path in the group.
                logger.warning(
                    "gateway snapshot entry %s carries no desired_paths — skipped",
                    entry.get("route_group_key") or entry.get("group_code"),
                )
                continue

            route_group_key = entry.get("route_group_key")
            method = (entry.get("http_method") or "").upper()
            if not route_group_key or not method:
                raise ValueError(
                    f"gateway snapshot entry for '{api_name or service_mst_code}' has no "
                    f"group identity (route_group_key / http_method) — cannot write it"
                )

            # The delta's target value if it recorded a move, else the entry's
            # desired state — so the tables match what the file was written to.
            plugins = entry.get("plugins_after")
            if plugins is None:
                plugins = entry.get("plugins") or []
            regex_priority = entry.get("regex_priority_after")
            if regex_priority is None:
                regex_priority = entry.get("regex_priority") or 0

            group = await self.kong_route_group_repo.get_or_create(
                route_group_key=route_group_key,
                http_method=method,
                api_name=api_name or None,
                services_mst_code=service_mst_code,
                environments_enum=environment,
                geo_loc_mst_code=geo_loc_mst_code,
                plugins=list(plugins),
                regex_priority=int(regex_priority or 0),
            )

            counts = await self._reconcile_paths(
                group,
                SimpleNamespace(
                    http_method=method,
                    paths=[
                        SimpleNamespace(
                            code=p.get("code"),
                            route_path=(p.get("route_path") or "").strip(),
                        )
                        for p in (entry.get("desired_paths") or [])
                        if isinstance(p, dict) and p.get("route_path")
                    ],
                ),
                scope,
                api_name,
                user_email=user_email,
                creation_status=creation_status,
            )

            totals["groups"] += 1
            for k in ("created", "updated", "removed", "promoted"):
                totals[k] += counts[k]

            logger.info(
                "gateway → tables [%s]: group=%s (%s · %s) "
                "created=%d updated=%d removed=%d promoted=%d",
                getattr(creation_status, "value", creation_status),
                group.code, route_group_key, method,
                counts["created"], counts["updated"], counts["removed"],
                counts["promoted"],
            )

        return totals

    async def _last_gateway_deploy_at(self, service_mst_code: str, environment=None):
        """
        When this service's gateway last deployed successfully, or None if never.

        Used to tell a deletion that is still waiting to ship from one that shipped
        long ago. Reads the newest DEPLOYED gateway (add_route) queue item for the
        service.

        Resolved through service_configs rather than matching transaction_code
        against the service directly: a gateway row points at a service_configs
        code from its scope, so the service is one join away, not on the row. The
        environment filter moves onto that join too — it is a real indexed column
        there, where the snapshot only had it as JSON text.
        """
        from sqlalchemy import text as _text
        env = getattr(environment, "value", environment)
        row = (await self.db.execute(_text("""
            select max(q.updated_at)
            from transaction_queue q
            join service_configs c on c.code = q.transaction_code
            where q.case_ref_code = 'add_route'
              and q.status = 'deployed'
              and c.services_mst_code = :svc
              and (cast(:env as text) is null or c.environment = cast(:env as environment_enum))
        """), {"svc": service_mst_code, "env": env})).scalar()
        return row

    async def list_routes(
        self,
        tenant_code: str,
        user_code: str,
        service_mst_code: str,
        application_code: Optional[str] = None,
        environment: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
        include_deleted: bool = False,
    ) -> list:
        """
        List a service's Kong routes, scoped to env/region if given. Validates tenant
        ownership + workspace access first.

        include_deleted also returns soft-deleted rows. Those are routes still live in
        the gateway that leave on the next deploy — the caller needs them to show a
        deletion as a pending change, otherwise a removed route simply vanishes and
        nothing reflects the outstanding work.
        """
        await self._guard_service_access(tenant_code, user_code, service_mst_code, application_code)
        if include_deleted:
            active = await self.kong_route_repo.list_routes_for_service(
                services_code=service_mst_code, environment=environment,
                geo_loc_mst_code=geo_loc_mst_code,
            )
            removed = await self.kong_route_repo.list_deleted_routes_for_service(
                services_code=service_mst_code, environment=environment,
                geo_loc_mst_code=geo_loc_mst_code,
            )
            # A deletion is only pending work if the route is actually IN the gateway,
            # which needs both of these:
            #
            # 1. It was deployed at all. Deleting a saved-but-never-deployed route
            #    (INITIATED) removes something terragrunt never had, so there is
            #    nothing to ship — it should just disappear, not sit in the diff
            #    asking to be deployed.
            # 2. It was deleted after the last successful gateway deploy. Soft-deleted
            #    rows are never retired (the post-deploy hook only touches live ones),
            #    so without this a single delete shows up in the diff for months.
            removed = [r for r in removed if r.creation_status in _DEPLOYED_STATUSES]
            cutoff = await self._last_gateway_deploy_at(service_mst_code, environment)
            if cutoff is not None:
                removed = [r for r in removed if (r.updated_at or r.created_at) > cutoff]
            return list(active) + list(removed)
        return await self.kong_route_repo.list_routes_for_service(
            services_code=service_mst_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
        )

    async def delete_route(
        self,
        tenant_code: str,
        user_code: str,
        code: str,
        reconcile: bool = True,
    ):
        """
        Soft-delete a Kong route by code. Validates tenant ownership + workspace
        access via the route's service. Soft delete keeps history and drops the
        route from generation (list_routes_for_service filters is_deleted).

        `reconcile` says WHY the route is going, which decides whether the
        caller's pending change set is rewritten with it — see
        _drop_route_from_pending. True for a delete a person asked for, False
        for housekeeping (the Gateway tab's duplicate self-heal), which must not
        edit a change set as a side effect of a page load.
        """
        route = await self.kong_route_repo.get_by(code=code)
        if not route or route.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Kong route not found: {code}",
            )
        service = None
        if route.services_mst_code:
            service = await self._guard_service_access(
                tenant_code, user_code, route.services_mst_code
            )

        route.soft_delete()
        self.db.add(route)
        # The route is only half the change. Its queue row still lists it, so
        # without this the Preview goes on showing a route that no longer
        # exists — see _drop_route_from_pending.
        if reconcile:
            await self._drop_route_from_pending(
                tenant_code=tenant_code,
                user_code=user_code,
                route=route,
                service_name=getattr(service, "name", "") or "",
            )
        await self.db.commit()
        logger.info(f"Soft-deleted Kong route: code={code}")
        return route

    async def _drop_route_from_pending(
        self,
        tenant_code: str,
        user_code: str,
        route,
        service_name: str,
    ) -> None:
        """Take a deleted route out of its author's pending gateway change.

        Deleting a route is TWO facts, not one: the route row goes, and the
        change set that proposed it no longer proposes it. Only the first used
        to happen, so a delete left the queue row still listing the route —
        Preview showed 4 when 3 were left, and a change whose every route had
        been deleted stayed on the board as an empty request that would deploy
        nothing.

        This mirrors what a save does at the end of save_gateway_changes: the
        snapshot is rewritten without the route, and a snapshot with nothing
        left in it drops the row entirely (clear_pending_for_scope) rather than
        being stored empty. The variables lane has behaved this way all along —
        this is the gateway lane catching up.

        Scoped to the caller's OWN row. Change sets are per author, and a
        teammate's pending slice for the same service is not this delete's to
        rewrite.

        A row under review is left alone and the delete is refused: rewriting a
        snapshot an approver is currently deciding on would move the ground
        under them, which is the same rule the save path enforces with the lane
        lock.
        """
        from app.repository.transaction_queue_repository import TransactionQueueRepository

        if not route.services_mst_code:
            return

        queue_repo = TransactionQueueRepository(self.db)
        scope = {
            "tenant_code": tenant_code,
            "services_mst_code": route.services_mst_code,
            "environment": route.environments_enum,
            "geo_loc_mst_code": route.geo_loc_mst_code,
        }

        rows = await queue_repo.get_pending_for_scope(**scope)
        mine = [r for r in rows if r.user_code == user_code]
        if not mine:
            return

        for row in mine:
            if (row.status or "") in ("submit", "approved"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "This gateway change is under review — withdraw it "
                        "before deleting its routes."
                    ),
                )

        row = mine[0]
        snapshot = dict(row.config_snapshot or {})
        kept: list[dict] = []
        for entry in (snapshot.get("groups") or []):
            paths = [
                p for p in (entry.get("paths") or [])
                if (p or {}).get("code") != route.code
            ]
            # A group survives on its own merits: the paths it still moves, or a
            # plugin / priority change that stands whether or not any path does.
            # Dropping a group that only ever carried this route is the point —
            # an entry with an empty delta would deploy nothing and read as a
            # change on every screen that counts it.
            plugins_moved = (
                sorted(entry.get("plugins_before") or [])
                != sorted(entry.get("plugins_after") or [])
            )
            priority_moved = (
                (entry.get("regex_priority_before") or 0)
                != (entry.get("regex_priority_after") or 0)
            )
            if paths or plugins_moved or priority_moved:
                kept.append({**entry, "paths": paths})

        if kept:
            await queue_repo.upsert_pending_for_scope(
                transaction_code=row.transaction_code,
                tenant_code=tenant_code,
                user_code=user_code,
                services_mst_code=route.services_mst_code,
                environment=route.environments_enum,
                geo_loc_mst_code=route.geo_loc_mst_code,
                snapshot={**snapshot, "groups": kept},
                display_name=self._gateway_display_name(service_name, kept),
            )
        else:
            await queue_repo.clear_pending_for_scope(
                tenant_code=tenant_code,
                user_code=user_code,
                services_mst_code=route.services_mst_code,
                environment=route.environments_enum,
                geo_loc_mst_code=route.geo_loc_mst_code,
            )


    async def create_route_from_infrastructure_request(
        self,
        tenant_code: str,
        request: InfrastructureCreateRequest,
        user_email: str
    ) -> InfrastructureCreateResponse:
        """
        Create or update Kong Gateway route from an InfrastructureCreateRequest.

        The chat/MCP door. `create_route` above is the Gateway tab's, taking
        KongRouteConfigCreateRequest; this one takes the infrastructure shape
        and answers InfrastructureCreateResponse, whose table_name is KONG_ROUTE
        so callers recording the code on a queue row key it correctly.

        Saves to kong_route_configs table.
        - If request.code is provided: Updates existing record
        - If request.code is None: Creates new record
        """
        # Check if this is an update (code provided) or create (no code)
        is_update = request.code is not None

        if is_update:
            logger.info(f"Updating Kong route: code={request.code}")
        else:
            logger.info(f"Creating Kong route: service_mst_code={request.service_mst_code}")

        # 1. Validate service exists and belongs to tenant (REQUIRED for Kong routes)
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

        # The chatbot's Kong form describes a whole Gateway card — one method,
        # one tag, a priority, plugins and MANY paths — translated upstream into
        # the scope-keyed `groups` shape. It carries no top-level `route`, so it
        # cannot go through the single-route path below.
        groups = request.type_specific_config.get("groups")
        if isinstance(groups, list) and groups and not is_update:
            return await self._create_kong_routes_from_groups(
                request=request, service=service, user_email=user_email, groups=groups,
            )

        # 2. Extract Kong route details from type_specific_config and service
        # Use api_name from type_specific_config if provided, otherwise use service.name
        api_name = request.type_specific_config.get("api_name") or service.name
        http_method = request.type_specific_config.get("method", "")
        route_path = request.type_specific_config.get("route", "")

        # The route group this route joins, and its priority. Both optional in
        # chat: no tag means the service's default group, which is what every
        # chat-created route used to get without anyone choosing it.
        route_group_key = (
            request.type_specific_config.get("route_group_key") or ""
        ).strip() or default_group_key(api_name)
        raw_priority = request.type_specific_config.get("regex_priority")
        # None and 0 are different answers: None is "the user said nothing", which
        # must not reset an existing group's priority to 0.
        requested_priority = None if raw_priority is None else int(raw_priority)

        # 3. For CREATE only: duplicate + cross-group overlap checks.
        if not is_update:
            existing_route = await self.kong_route_repo.check_route_exists(
                api_name=api_name,
                http_method=http_method,
                route_path=route_path,
                services_code=request.service_mst_code,
                environment=request.environment,
            )
            if existing_route:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Kong route already exists: {http_method} {route_path} for {api_name}"
                )

            # The check above only sees this service's own rows in this one group.
            # A path claimed by ANOTHER group of the same method is the case that
            # actually breaks the gateway: Kong serves the higher regex_priority
            # and shadows the other, and two groups at the same priority have no
            # tie-break at all. Until now nothing on this path looked for it, so
            # chat planted unrankable duplicates silently.
            gateway_rows = await self.kong_route_repo.list_routes_for_gateway(
                environment=request.environment,
                geo_loc_mst_code=request.geo_loc_mst_code,
            )
            try:
                KongPathOverlapValidator.validate(
                    paths=[route_path],
                    http_method=http_method,
                    route_group_key=route_group_key,
                    regex_priority=requested_priority or 0,
                    existing=KongPathOverlapValidator.from_models(gateway_rows),
                )
            except KongPathOverlapError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=" ".join(exc.errors),
                ) from exc

        # 4. Resolve the route group, creating it when it does not exist yet.
        #    Linking the row is what makes route groups and regex_priority reach
        #    terragrunt at all: priority lives on kong_route_groups, and the
        #    generator reads it from there. A group-less row — which is all this
        #    writer used to produce — deploys into the service default at 0.
        route_group = await self._resolve_kong_route_group(
            route_group_key=route_group_key,
            http_method=http_method,
            api_name=api_name,
            request=request,
            requested_priority=requested_priority,
        )

        route_data = make_kong_route_config(
            api_name=api_name,
            http_method=http_method,
            route_path=route_path,
            services_mst_code=request.service_mst_code,
            environments_enum=request.environment,
            geo_loc_mst_code=request.geo_loc_mst_code,
            creation_status=DeploymentStatusEnum.INITIATED,
            creation_status_updated_by=user_email,
            kong_route_group_id=route_group.id if route_group else None,
        )

        # 5. Create or Update in database
        if is_update:
            # UPDATE: Use direct update query
            kong_route = await self.kong_route_repo.update_by_code(
                code=request.code,
                updates=route_data
            )
            if not kong_route:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Kong route not found with code: {request.code}"
                )

            logger.info(f"Updated Kong route: code={kong_route.code}, id={kong_route.id}")
        else:
            # CREATE: Create new record
            kong_route = await self.kong_route_repo.create(**route_data)
            await self.db.commit()
            await self.db.refresh(kong_route)

            logger.info(f"Created Kong route: code={kong_route.code}, id={kong_route.id}")

        # 6. Build response
        return InfrastructureCreateResponse(
            table_name=WorkflowSourceTableEnum.KONG_ROUTE,
            code=kong_route.code
        )

    async def _create_kong_routes_from_groups(
        self,
        request: InfrastructureCreateRequest,
        service,
        user_email: str,
        groups: list,
    ) -> InfrastructureCreateResponse:
        """
        Create every route of one or more Gateway cards (the `groups` shape).

        Same rules as the single-route path, applied per group: refuse a path
        another group already claims and cannot be out-ranked, then link each
        route to its kong_route_groups row so the tag, plugins and priority
        reach terragrunt.

        Paths this group ALREADY holds are skipped rather than refused — the
        card describes a desired state, so re-sending it must converge instead
        of erroring on what is already true. If a card turns out to add nothing
        at all, that IS an error: it would otherwise become a queue row that
        deploys an empty change and reports success.
        """
        api_name = request.type_specific_config.get("api_name") or service.name
        gateway_rows = await self.kong_route_repo.list_routes_for_gateway(
            environment=request.environment,
            geo_loc_mst_code=request.geo_loc_mst_code,
        )
        existing_routes = KongPathOverlapValidator.from_models(gateway_rows)

        first_code: Optional[str] = None
        created = 0

        for entry in groups:
            if not isinstance(entry, dict):
                continue
            method = (entry.get("http_method") or "").strip().upper()
            route_group_key = (entry.get("route_group_key") or "").strip() or default_group_key(api_name)
            # `*_after` is the approved target state; the bare key is the fallback.
            raw_priority = entry.get("regex_priority_after")
            if raw_priority is None:
                raw_priority = entry.get("regex_priority")
            requested_priority = None if raw_priority is None else int(raw_priority)
            plugins = entry.get("plugins_after")
            if plugins is None:
                plugins = entry.get("plugins")

            paths = [
                (p.get("route_path") or "").strip()
                for p in (entry.get("paths") or [])
                if isinstance(p, dict)
                and (p.get("action") or "add").strip().lower() == "add"
                and (p.get("route_path") or "").strip()
            ]
            if not method or not paths:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Gateway group '{route_group_key}' has no http_method or no paths "
                        f"to add — nothing to create."
                    ),
                )

            # Cross-group overlap, for the whole card at once: one refused path
            # rejects the card rather than leaving half of it created.
            try:
                KongPathOverlapValidator.validate(
                    paths=paths,
                    http_method=method,
                    route_group_key=route_group_key,
                    regex_priority=requested_priority or 0,
                    existing=existing_routes,
                )
            except KongPathOverlapError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=" ".join(exc.errors),
                ) from exc

            route_group = await self._resolve_kong_route_group(
                route_group_key=route_group_key,
                http_method=method,
                api_name=api_name,
                request=request,
                requested_priority=requested_priority,
                plugins=plugins,
            )

            for route_path in paths:
                already = await self.kong_route_repo.check_route_exists(
                    api_name=api_name,
                    http_method=method,
                    route_path=route_path,
                    services_code=request.service_mst_code,
                    environment=request.environment,
                    route_group_id=route_group.id if route_group else None,
                )
                if already:
                    logger.info(
                        "Kong route already in '%s · %s', skipping: %s",
                        route_group_key, method, route_path,
                    )
                    first_code = first_code or already.code
                    continue

                kong_route = await self.kong_route_repo.create(
                    **make_kong_route_config(
                        api_name=api_name,
                        http_method=method,
                        route_path=route_path,
                        services_mst_code=request.service_mst_code,
                        environments_enum=request.environment,
                        geo_loc_mst_code=request.geo_loc_mst_code,
                        creation_status=DeploymentStatusEnum.INITIATED,
                        creation_status_updated_by=user_email,
                        kong_route_group_id=route_group.id if route_group else None,
                    )
                )
                created += 1
                first_code = first_code or kong_route.code

        if created == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Every path in this Kong request already exists on its route group — "
                    "there is nothing to deploy."
                ),
            )

        await self.db.commit()
        logger.info("Created %d Kong route(s) for service=%s", created, request.service_mst_code)

        return InfrastructureCreateResponse(
            table_name=WorkflowSourceTableEnum.KONG_ROUTE,
            code=first_code,
        )

    async def _resolve_kong_route_group(
        self,
        route_group_key: str,
        http_method: str,
        api_name: str,
        request: InfrastructureCreateRequest,
        requested_priority: Optional[int],
        plugins: Optional[list] = None,
    ):
        """
        Find the route group for this identity, creating it if it does not exist.

        Deliberately NOT ``get_or_create``. That call refreshes plugins and
        regex_priority on an existing group from whatever it is handed, and the
        single-route writer has no plugin data — so passing an empty list would
        strip JWT off a group the Gateway tab configured, and passing a defaulted
        0 would silently drop an override someone set on purpose.

        So both are opt-in: ``None`` means "the caller said nothing, leave it
        alone". The single-route path passes None for both and can only RAISE a
        priority it was explicitly given. The gateway-card path passes real
        values — that card describes the group's desired state, exactly like the
        Gateway tab's, so it is allowed to set them.
        """
        KongRouteValidator.validate_route_group_key(route_group_key)
        if requested_priority is not None:
            KongRouteValidator.validate_regex_priority(requested_priority)
        if plugins is not None:
            KongRouteValidator.validate_plugins(plugins)

        group_repo = self.kong_route_group_repo
        existing = await group_repo.get_by_identity(
            route_group_key=route_group_key,
            http_method=http_method,
            services_mst_code=request.service_mst_code,
            environments_enum=request.environment,
            geo_loc_mst_code=request.geo_loc_mst_code,
        )

        if existing is not None:
            changed = False
            if (
                requested_priority is not None
                and (existing.regex_priority or 0) != requested_priority
            ):
                logger.info(
                    "Kong route group %s: regex_priority %s -> %s (requested via chat)",
                    existing.code, existing.regex_priority or 0, requested_priority,
                )
                existing.regex_priority = requested_priority
                changed = True
            if plugins is not None and sorted(existing.plugins or []) != sorted(plugins):
                logger.info(
                    "Kong route group %s: plugins %s -> %s (requested via chat)",
                    existing.code, existing.plugins or [], plugins,
                )
                existing.plugins = list(plugins)
                changed = True
            if changed:
                self.db.add(existing)
                await self.db.flush()
            return existing

        logger.info(
            "Creating Kong route group '%s · %s' for service=%s",
            route_group_key, http_method, request.service_mst_code,
        )
        return await group_repo.get_or_create(
            route_group_key=route_group_key,
            http_method=http_method,
            api_name=api_name,
            services_mst_code=request.service_mst_code,
            environments_enum=request.environment,
            geo_loc_mst_code=request.geo_loc_mst_code,
            plugins=list(plugins or []),
            regex_priority=requested_priority or 0,
        )
