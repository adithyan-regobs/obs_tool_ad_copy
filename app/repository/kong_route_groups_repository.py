"""
Kong Route Group repository — get-or-create by group identity.
"""

import logging
from typing import Any, List, Optional

from sqlalchemy import and_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.kong_route_group_model import KongRouteGroupModel
from app.domain.factories.kong_route_group_factory import make_kong_route_group
from app.repository.base_repository import BaseRepository

logger = logging.getLogger(__name__)


class KongRouteGroupsRepository(BaseRepository[KongRouteGroupModel]):
    """Repository for KongRouteGroup operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(KongRouteGroupModel, session)

    def _identity_filters(
        self,
        services_mst_code: Optional[str],
        environments_enum: Optional[Any],
        geo_loc_mst_code: Optional[str],
        route_group_key: str,
        http_method: str,
    ) -> List[Any]:
        """
        Filters matching uq_kong_route_groups_identity exactly. The nullable
        columns use IS NULL rather than `== None` so the lookup finds the same
        row the unique index would collide on (chat-created routes carry NULL
        env AND region today).
        """
        m = self.model
        return [
            m.services_mst_code.is_(None) if services_mst_code is None
            else m.services_mst_code == services_mst_code,
            m.environments_enum.is_(None) if environments_enum is None
            else m.environments_enum == environments_enum,
            m.geo_loc_mst_code.is_(None) if geo_loc_mst_code is None
            else m.geo_loc_mst_code == geo_loc_mst_code,
            m.route_group_key == route_group_key,
            m.http_method == (http_method or "").upper(),
            m.is_deleted == False,
        ]

    async def get_by_identity(
        self,
        route_group_key: str,
        http_method: str,
        services_mst_code: Optional[str] = None,
        environments_enum: Optional[Any] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> Optional[KongRouteGroupModel]:
        """Fetch the active group for this identity, or None."""
        filters = self._identity_filters(
            services_mst_code, environments_enum, geo_loc_mst_code,
            route_group_key, http_method,
        )
        result = await self.session.execute(select(self.model).where(and_(*filters)))
        return result.scalars().first()

    async def resolve_routing(self, transaction_code: str) -> Optional[dict]:
        """
        Where a gateway item's terragrunt lives: product / environment / region,
        read LIVE from whatever its transaction_code points at.

        The gateway file path is
        `environment/{product}-{env}-{version}/{region}/gateway/terragrunt.hcl`,
        and every part of it is reachable from either shape of gateway row — env
        and region are columns on both, product comes from the service's
        application.

        Resolved at deploy time rather than stamped onto the queue row when it is
        saved. A stored copy is a photocopy: rename the application after saving and
        the row would keep targeting a folder that no longer exists. (The other
        resource types do stamp product_name — see the enrichment in
        TransactionQueueService — so they carry exactly that staleness.)

        Handles BOTH gateway shapes here rather than in the callers, and that is
        the point: _apply_gateway_routing exists twice — once in
        ScriptPRWorkflowService and once inlined in multiple_deploy_activities —
        and teaching both about a second shape is how the two silently drift. They
        both call this, so neither needs to know.

        Tries the route GROUP first (the older per-group rows), then
        service_configs (the scope-keyed rows). Codes are disjoint — KRG_ vs the
        `sc-...` service_configs form — so at most one can hit.

        Returns None when nothing resolves, so the caller can fail loudly rather
        than build a path with empty segments.
        """
        row = (await self.session.execute(text("""
            SELECT g.environments_enum::text AS environment,
                   g.geo_loc_mst_code        AS region,
                   a.name                    AS product_name,
                   s.name                    AS service_name,
                   s.code                    AS service_mst_code
            FROM kong_route_groups g
            JOIN services_mst     s ON s.code = g.services_mst_code
            JOIN applications_mst a ON a.code = s.applications_mst_code
            WHERE g.code = :code AND g.is_deleted = false
        """), {"code": transaction_code})).mappings().first()
        if row:
            return dict(row)

        # Scope-keyed gateway row: transaction_code is a service_configs code, and
        # that row's own columns carry the scope.
        #
        # No is_deleted filter on the config, matching the queue lookups: it is
        # only a NAME for the scope, and the routes never depended on it. A config
        # replaced after the change was queued must still resolve, or the deploy
        # loses its file path and every gateway item falls back to a per-item PR.
        row = (await self.session.execute(text("""
            SELECT c.environment::text AS environment,
                   c.geo_loc_mst_code  AS region,
                   a.name              AS product_name,
                   s.name              AS service_name,
                   s.code              AS service_mst_code
            FROM service_configs  c
            JOIN services_mst     s ON s.code = c.services_mst_code
            JOIN applications_mst a ON a.code = s.applications_mst_code
            WHERE c.code = :code
        """), {"code": transaction_code})).mappings().first()
        return dict(row) if row else None

    async def list_scopes(self, services_mst_code: str) -> List[dict]:
        """
        Which (environment, region) pairs this service has a gateway in.

        Exists because /gateway REQUIRES both — a service's stage and prod
        gateways are separate group rows and an unscoped read would merge them.
        The Gateway tab normally takes the region off the canvas node, which
        reads it from service_configs; a service imported from terragrunt has
        routes but no config row, so it has no region to send and could not open
        its own tab at all.

        Answered from kong_route_groups alone — deliberately NOT from
        service_configs, which is the table these services are missing.

        count(DISTINCT g.id) rather than count(*): the LEFT JOIN produces one row
        per path, so count(*) would report the path total as the group total.
        """
        rows = (await self.session.execute(text("""
            SELECT g.environments_enum::text AS environment,
                   g.geo_loc_mst_code        AS geo_loc_mst_code,
                   count(DISTINCT g.id)      AS group_count,
                   count(r.id)               AS path_count
            FROM kong_route_groups g
            LEFT JOIN kong_route_configs r
                   ON r.kong_route_group_id = g.id AND r.is_deleted = false
            WHERE g.services_mst_code = :svc AND g.is_deleted = false
            GROUP BY 1, 2
            ORDER BY 1, 2
        """), {"svc": services_mst_code})).mappings().all()
        return [dict(r) for r in rows]

    async def get_by_code(self, code: str) -> Optional[KongRouteGroupModel]:
        """The group with this code, deleted or not — callers decide what that means."""
        result = await self.session.execute(
            select(self.model).where(self.model.code == code)
        )
        return result.scalars().first()

    async def list_for_scope(
        self,
        services_mst_code: str,
        environments_enum: Optional[Any] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> List[KongRouteGroupModel]:
        """
        Every active group for a service, scoped to an environment and region.

        Scope comes off the GROUP, not the service: services_mst has no
        environment or region columns, so this is the only place stage and prod
        are distinguishable. Omitting either filter returns both, which is
        almost never what a caller wants — pass them.

        Paths arrive eager-loaded. `routes` does NOT filter soft-deleted rows
        (the relationship has no criteria), so callers must drop is_deleted
        themselves or deleted paths reappear in the Gateway tab.
        """
        m = self.model
        filters = [m.services_mst_code == services_mst_code, m.is_deleted == False]
        if environments_enum is not None:
            filters.append(m.environments_enum == environments_enum)
        if geo_loc_mst_code is not None:
            filters.append(m.geo_loc_mst_code == geo_loc_mst_code)

        stmt = (
            select(m)
            .options(selectinload(m.routes))
            .where(and_(*filters))
            .order_by(m.route_group_key, m.http_method)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def _resolve_ownership(
        self,
        services_mst_code: Optional[str],
        environments_enum: Optional[Any],
        geo_loc_mst_code: Optional[str],
        route_group_key: str,
    ) -> bool:
        """
        Should a NEW group in this scope carry the terragrunt `service { }` block?

        Two rules, in order:
          1. Its label already owns the service -> yes. A kong_configs entry is
             keyed by label and spans every method, so all rows sharing the
             owner's label carry the flag; adding a method must not drop it.
          2. The scope has no owner at all -> yes, this is the first group for the
             service and becomes the owner.
        Otherwise no: another label already owns it.

        Mirrors backfill_kong_route_groups.pick_owner_labels, so a service created
        through the UI/chat ends up flagged the same way a backfilled one does.
        """
        m = self.model
        scope = [
            m.services_mst_code.is_(None) if services_mst_code is None
            else m.services_mst_code == services_mst_code,
            m.environments_enum.is_(None) if environments_enum is None
            else m.environments_enum == environments_enum,
            m.geo_loc_mst_code.is_(None) if geo_loc_mst_code is None
            else m.geo_loc_mst_code == geo_loc_mst_code,
            m.is_deleted == False,
        ]

        same_label_owner = (
            await self.session.execute(
                select(m.id).where(and_(*scope, m.route_group_key == route_group_key,
                                        m.is_service_owner == True)).limit(1)
            )
        ).scalars().first()
        if same_label_owner is not None:
            return True

        any_owner = (
            await self.session.execute(
                select(m.id).where(and_(*scope, m.is_service_owner == True)).limit(1)
            )
        ).scalars().first()
        return any_owner is None

    async def get_or_create(
        self,
        route_group_key: str,
        http_method: str,
        api_name: Optional[str] = None,
        services_mst_code: Optional[str] = None,
        environments_enum: Optional[Any] = None,
        geo_loc_mst_code: Optional[str] = None,
        plugins: Optional[List[str]] = None,
        regex_priority: int = 0,
    ) -> KongRouteGroupModel:
        """
        Fetch the group for this identity, creating it if absent.

        Concurrency: SELECT -> INSERT -> on IntegrityError re-SELECT. The unique
        index is the real guard; the retry just turns a lost race into a normal
        read. The INSERT runs inside a SAVEPOINT (begin_nested) so a failed
        insert does not poison the surrounding transaction and break the
        caller's commit.

        On an existing group, plugins/regex_priority are refreshed so the new
        table tracks the old per-route columns during the dual-write phase. Last
        write wins, which matches how the per-route columns behave today.
        """
        filters = self._identity_filters(
            services_mst_code, environments_enum, geo_loc_mst_code,
            route_group_key, http_method,
        )

        existing = (
            await self.session.execute(select(self.model).where(and_(*filters)))
        ).scalars().first()

        if existing:
            desired_plugins = sorted(plugins or [])
            if (
                sorted(existing.plugins or []) != desired_plugins
                or (existing.regex_priority or 0) != (regex_priority or 0)
            ):
                existing.plugins = list(plugins or [])
                existing.regex_priority = regex_priority or 0
                self.session.add(existing)
                await self.session.flush()
            return existing

        group_data = make_kong_route_group(
            route_group_key=route_group_key,
            http_method=http_method,
            api_name=api_name,
            services_mst_code=services_mst_code,
            environments_enum=environments_enum,
            geo_loc_mst_code=geo_loc_mst_code,
            plugins=plugins,
            regex_priority=regex_priority,
        )
        group_data["is_service_owner"] = await self._resolve_ownership(
            services_mst_code, environments_enum, geo_loc_mst_code, route_group_key,
        )

        try:
            async with self.session.begin_nested():
                group = self.model(**group_data)
                self.session.add(group)
                await self.session.flush()
            return group
        except IntegrityError:
            # Only a LOST RACE is recoverable: the row we failed to insert must now
            # exist. Anything else (a bad services_mst_code / geo_loc_mst_code FK,
            # say) must surface — returning None here would hand the caller a value
            # it dereferences straight into an AttributeError, far from the cause.
            result = await self.session.execute(select(self.model).where(and_(*filters)))
            winner = result.scalars().first()
            if winner is None:
                logger.warning(
                    "Kong route group insert failed and no existing row matches (%s %s) — re-raising",
                    route_group_key, http_method,
                )
                raise
            logger.info(
                "Kong route group raced on create (%s %s) — re-reading",
                route_group_key, http_method,
            )
            return winner
