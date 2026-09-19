"""
Transaction Queue Repository

Data access layer for the transaction_queue table.
"""

import logging
from typing import List, Optional, Tuple, Dict, Any
from sqlalchemy import select, func, and_, or_, update, cast, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload

from app.repository.base_repository import BaseRepository
from app.db.models.transaction_queue_model import (
    TransactionQueueModel,
    TransactionQueueStatusEnum,
    gateway_rows_clause,
)
from app.db.models.transaction_queue_workflow_mapping_model import TransactionQueueWorkflowMappingModel
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.models.kong_route_config_model import KongRouteConfigModel
from app.db.models.kong_route_group_model import KongRouteGroupModel
from app.core.enum import WorkflowSourceTableEnum, EnvironmentEnum
from app.db.models.user_mst_model import UserMstModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.service_config_dockerfile_workflow_model import ServiceConfigDockerfileWorkflowModel
from app.db.models.alert_config_model import AlertConfigModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.models.kong_route_config_model import KongRouteConfigModel
from app.db.models.pipeline_mst_model import PipelineMstModel
from app.db.models.ticket_model import TicketModel
from app.core.enum import WorkflowSourceTableEnum

logger = logging.getLogger(__name__)

# service_configs, joined a SECOND time — for gateway (KONG_ROUTE) rows.
#
# A gateway row's transaction_code is a service_configs code from the change's
# SCOPE: (service, environment, region), which is what one terragrunt gateway
# file covers. The plain ServiceConfigModel join is already spoken for by
# SERVICE_CONFIG items, and the same table twice in one statement needs an alias
# or SQLAlchemy cannot tell the two ON clauses apart.
#
# Never used to look a row up BY its code — only to resolve the code a row
# already stores back to a scope. See _scope_config_codes for why that direction
# is the only one that works.
KongScopeConfigModel = aliased(ServiceConfigModel, name="kong_scope_config")


class TransactionQueueRepository(BaseRepository[TransactionQueueModel]):
    """Repository for Transaction Queue operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(TransactionQueueModel, session)

    def _build_polymorphic_query(self, base_stmt):
        """
        Build query with polymorphic joins based on table_name.

        Joins transaction_queue with respective source tables:
        - SERVICE_CONFIG → service_configs
        - SERVICE_CONFIG_DOCKERFILE → service_config_dockerfile_workflows
        - ALERT_CONFIG → alert_configs
        - INFRASTRUCTURE → infrastructure_mst
        - KONG_ROUTE → kong_route_configs
        - PIPELINE → pipeline_mst

        Args:
            base_stmt: Base select statement for TransactionQueueModel

        Returns:
            Statement with polymorphic outerjoin and options to load joined data
        """
        # Add outerjoin for each possible table type
        stmt = base_stmt.outerjoin(
            ServiceConfigModel,
            and_(
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG,
                TransactionQueueModel.transaction_code == ServiceConfigModel.code
            )
        ).outerjoin(
            ServiceConfigDockerfileWorkflowModel,
            and_(
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
                TransactionQueueModel.transaction_code == ServiceConfigDockerfileWorkflowModel.code
            )
        ).outerjoin(
            AlertConfigModel,
            and_(
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.ALERT_CONFIG,
                TransactionQueueModel.transaction_code == AlertConfigModel.code
            )
        ).outerjoin(
            InfrastructureMstModel,
            and_(
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE,
                TransactionQueueModel.transaction_code == InfrastructureMstModel.code
            )
        ).outerjoin(
            KongRouteConfigModel,
            and_(
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.KONG_ROUTE,
                TransactionQueueModel.transaction_code == KongRouteConfigModel.code
            )
        ).outerjoin(
            # KONG_ROUTE covers TWO shapes. The per-route flow points transaction_code
            # at kong_route_configs; the gateway flow points it at a route GROUP, which
            # is the grain plugins and terragrunt entries live at. Both joins are kept
            # because both shapes exist — only one can match a given row, since the
            # codes are distinctly prefixed (KRC_ vs KRG_).
            #
            # Without this, gateway rows resolved to NULL and the deploy loop read no
            # environment or region off them — the same columns it reads off a
            # service_config for a settings change.
            KongRouteGroupModel,
            and_(
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.KONG_ROUTE,
                TransactionQueueModel.transaction_code == KongRouteGroupModel.code
            )
        ).outerjoin(
            # ...and a THIRD shape: the scope-keyed gateway row, whose
            # transaction_code is a service_configs code. Prefixes stay disjoint
            # (KRC_ / KRG_ / SC_), so at most one of the three can match a row.
            KongScopeConfigModel,
            and_(
                gateway_rows_clause(),
                TransactionQueueModel.transaction_code == KongScopeConfigModel.code
            )
        ).outerjoin(
            PipelineMstModel,
            and_(
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.PIPELINE,
                TransactionQueueModel.transaction_code == PipelineMstModel.code
            )
        )

        return stmt

    # Statuses meaning "queued, not shipped yet". DEPLOYED rows are history and
    # must not be read as pending, or already-live routes show as changes.
    #
    # Two sets, deliberately. REUSE must never adopt a FAILED row — a retry has to
    # start a fresh one — but DISPLAY must include it, or a failed deploy makes the
    # group read as clean while its rows sit INITIATED in the tables, with no way
    # back to the change. Same split as search_draft_by_transaction's include_failed.
    #
    # PR_REJECTED sits with FAILED for the same reason: a gateway PR closed on
    # GitHub without merging settles the row there (sync_pr_status_from_github),
    # but the change never landed — hiding the row made the saved routes vanish
    # from the tab and the changes modal while still undeployed in the tables.
    # Visible again, the delta renders as pending and the next deploy re-ships it.
    _PENDING_STATUSES = (
        TransactionQueueStatusEnum.DRAFT,
        TransactionQueueStatusEnum.APPROVED,
    )
    # SUBMIT is VISIBLE but deliberately not in _PENDING_STATUSES: the reuse
    # query above upserts onto a pending row, and a submitted change is under
    # review — rewriting it would swap the content out from under the reviewer.
    # It has to stay visible, though. The tab's read merges these rows into the
    # route list, so a set without SUBMIT made every submitted route vanish from
    # the Gateway tab at the exact moment it went to review — while History,
    # which reads the queue without a status filter, kept showing it.
    _VISIBLE_PENDING_STATUSES = _PENDING_STATUSES + (
        TransactionQueueStatusEnum.SUBMIT,
        TransactionQueueStatusEnum.FAILED,
        TransactionQueueStatusEnum.PR_REJECTED,
    )

    # Shipped but not finished: a PR exists, or Temporal is mid-flight. NOT pending
    # (the user cannot still edit it) and NOT deployed (it is not in the gateway
    # file yet), so it needs its own set.
    #
    # A group in one of these must be read-only. Editing it looks harmless but
    # corrupts the next change set: the tab rebuilds "what is deployed" by undoing
    # the PENDING delta, so an in-flight one is not undone and is treated as
    # already live. If that deploy then fails or its PR is closed, those routes sit
    # in the database, absent from the file, and no future delta will ever add them.
    _IN_FLIGHT_STATUSES = (
        TransactionQueueStatusEnum.STARTING_DEPLOYMENT,
        TransactionQueueStatusEnum.COMMIT,
        TransactionQueueStatusEnum.PR_RAISED,
        TransactionQueueStatusEnum.PR_DRAFT,
        TransactionQueueStatusEnum.PR_APPROVED,
        TransactionQueueStatusEnum.PR_MERGED,
        TransactionQueueStatusEnum.DEPLOYING,
        TransactionQueueStatusEnum.PROVISIONING,
        TransactionQueueStatusEnum.BUILDING,
        TransactionQueueStatusEnum.CHECKOUT,
        TransactionQueueStatusEnum.VERIFICATION,
    )

    # ── scope-keyed gateway rows ─────────────────────────────────────────────
    #
    # A gateway change covers one SCOPE — (service, environment, region) — because
    # that is exactly what one terragrunt gateway file covers. One row therefore
    # carries EVERY changed group, and transaction_code holds a service_configs
    # code from that scope.
    #
    # Every lookup below resolves that code back THROUGH service_configs instead
    # of matching it directly, and the direction is the whole point. A scope can
    # hold several service_configs rows — the unique key also spans alb_selection,
    # infra type and vendor — so "which code is THE code for this scope" has no
    # single answer. Asking instead "does the config this row points at belong to
    # my scope" never needs one: whichever code was stored, its own columns place
    # it in exactly one scope. A service running both ECS and EKS therefore reads
    # the same queue row from either node, which matching on the code cannot do.

    def _scope_config_codes(
        self,
        tenant_code: str,
        services_mst_code: str,
        environment,
        geo_loc_mst_code: str,
    ):
        """The service_configs codes covering one gateway scope, as a subquery.

        Deliberately does NOT filter is_deleted. The config row is a scope
        pointer, not the subject of the change — a gateway change never depended
        on it. Replacing a service's config (switching infra type, say) soft
        deletes the old row, and excluding it here would orphan a perfectly valid
        queued change whose routes are untouched.
        """
        return (
            select(ServiceConfigModel.code)
            .where(
                ServiceConfigModel.tenant_mst_code == tenant_code,
                ServiceConfigModel.services_mst_code == services_mst_code,
                ServiceConfigModel.environment == environment,
                ServiceConfigModel.geo_loc_mst_code == geo_loc_mst_code,
            )
            .scalar_subquery()
        )

    async def get_pending_for_scope(
        self,
        tenant_code: str,
        services_mst_code: str,
        environment,
        geo_loc_mst_code: str,
    ) -> List[TransactionQueueModel]:
        """EVERY user's undeployed gateway row for one scope, newest first.

        Not filtered by user, and that is deliberate. The Gateway tab has to see a
        teammate's pending slice for three separate reasons: the save-time delta
        recompute subtracts their claimed paths (a delta that included them would
        either trip the overlap 409 or move their work into this user's row), an
        in-flight row of theirs must make the tab read-only, and the server's own
        overlap guard compares against exactly the rows a user filter would drop.
        Callers tag rows with is_mine rather than hiding them — "absent" reads as
        "free to edit", which is the one thing it must not mean.

        FAILED counts as pending: the change did not ship, the tables still hold
        it, and hiding it would strand the user with no way back to it.
        """
        stmt = (
            select(self.model)
            .where(
                gateway_rows_clause(),
                self.model.transaction_code.in_(
                    self._scope_config_codes(
                        tenant_code, services_mst_code, environment, geo_loc_mst_code
                    )
                ),
                self.model.status.in_(
                    self._VISIBLE_PENDING_STATUSES + self._IN_FLIGHT_STATUSES
                ),
                self.model.is_deleted == False,
            )
            .order_by(self.model.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_in_flight_for_scope(
        self,
        tenant_code: str,
        services_mst_code: str,
        environment,
        geo_loc_mst_code: str,
    ) -> Optional[TransactionQueueModel]:
        """The scope's shipped-but-unfinished row, if it has one — whoever owns it.

        Scope-level rather than per group: one deploy writes ONE gateway file
        covering every group in the scope, so a second deploy started meanwhile
        races a second PR against that same file. ANY user's in-flight row
        therefore blocks the whole scope, not just their own groups.

        See _IN_FLIGHT_STATUSES for why editing during one is corrupting rather
        than merely confusing.
        """
        return (await self.session.execute(
            select(self.model).where(
                gateway_rows_clause(),
                self.model.transaction_code.in_(
                    self._scope_config_codes(
                        tenant_code, services_mst_code, environment, geo_loc_mst_code
                    )
                ),
                self.model.status.in_(self._IN_FLIGHT_STATUSES),
                self.model.is_deleted == False,
            ).order_by(self.model.created_at.desc()).limit(1)
        )).scalars().first()

    async def find_submitted_for_scope(
        self,
        tenant_code: str,
        user_code: str,
        services_mst_code: str,
        environment,
        geo_loc_mst_code: str,
    ) -> Optional[TransactionQueueModel]:
        """This user's gateway row for a scope that is under review, if any.

        The gateway half of the lane lock, and the mirror of
        ApprovalRepository.find_live_for_resource — which excludes add_route
        rows precisely because the gateway lane polices itself. This is that
        policing; without it nothing did.

        SUBMIT only. DRAFT is someone still typing and APPROVED is handled by
        upsert_pending_for_scope, which demotes it back to DRAFT so the stale
        decision cannot ride along. SUBMIT is the one state that can neither be
        rewritten (a reviewer is looking at it) nor reused, and the upsert's
        reuse query therefore skips it — leaving it to insert a SECOND live row
        for one scope. Nothing downstream represents two: the service page and
        the MCP preview each pick one pending request per lane, so the older
        row vanishes from view while still sitting in review.

        Scoped to the user, unlike the settings lock, which is service-wide.
        Gateway rows are per author by design (see upsert_pending_for_scope),
        so one author's review must not freeze a colleague's edits.
        """
        return (await self.session.execute(
            select(self.model).where(
                gateway_rows_clause(),
                self.model.user_code == user_code,
                self.model.transaction_code.in_(
                    self._scope_config_codes(
                        tenant_code, services_mst_code, environment, geo_loc_mst_code
                    )
                ),
                self.model.status == TransactionQueueStatusEnum.SUBMIT,
                self.model.is_deleted == False,
            ).order_by(self.model.created_at.desc()).limit(1)
        )).scalars().first()

    async def upsert_pending_for_scope(
        self,
        transaction_code: str,
        tenant_code: str,
        user_code: str,
        services_mst_code: str,
        environment,
        geo_loc_mst_code: str,
        snapshot: Dict[str, Any],
        display_name: str,
    ) -> TransactionQueueModel:
        """One pending row per (scope, user): overwrite it if present, else insert.

        The existing row is found by SCOPE, never by transaction_code. An earlier
        save may have stored a different service_configs code from the same scope
        — both are equally valid pointers — and matching on the code directly
        would miss it and insert a second row for one gateway file.

        Two users editing the same service each keep their OWN row, so the tab can
        show each of them their own diff and a deploy ships only the deploying
        user's slice.

        The snapshot is replaced wholesale, never merged. The client recomputes
        every group's delta against the DEPLOYED baseline on each save, so the
        payload is always this user's complete pending set for the scope — a
        group edited yesterday and still undeployed is in it again today. Merging
        would resurrect groups the user has since undone.
        """
        import uuid

        existing = (await self.session.execute(
            select(self.model).where(
                gateway_rows_clause(),
                self.model.user_code == user_code,
                self.model.transaction_code.in_(
                    self._scope_config_codes(
                        tenant_code, services_mst_code, environment, geo_loc_mst_code
                    )
                ),
                # FAILED is reusable HERE and nowhere else. A gateway change
                # exists only in this row until its PR merges, so a failed
                # deploy leaves the routes with no other copy — starting a
                # fresh row (what the settings path does, deliberately) would
                # strand them beside a second one and show the group twice on
                # the tab. Settings can start fresh because deploy already
                # wrote its values into service_configs; gateway cannot.
                #
                # Reusing it puts the change back in DRAFT, so recovery runs
                # through submit and approve like any other edit — never the
                # silent revive-to-APPROVED this replaced.
                self.model.status.in_(
                    self._PENDING_STATUSES + (TransactionQueueStatusEnum.FAILED,)
                ),
                self.model.is_deleted == False,
            ).order_by(self.model.created_at.desc()).limit(1)
        )).scalars().first()

        if existing:
            existing.config_snapshot = snapshot
            # Re-stamped so the row keeps pointing at a code that still resolves:
            # the config it was created against may since have been replaced.
            existing.transaction_code = transaction_code
            # A pre-flip row still says KONG_ROUTE; every save converges it to
            # the new SERVICE_CONFIG + add_route identity.
            existing.table_name = WorkflowSourceTableEnum.SERVICE_CONFIG
            existing.case_ref_code = "add_route"
            existing.name = display_name[:255]
            existing.display_name = display_name[:255]
            existing.updated_at = func.now()
            # New content means any decision on this row was made about something
            # else, so the row goes back to being a draft and the decision is
            # dropped with it. Two rows reach here in that state:
            #
            #   FAILED    a deploy that did not land. Recovery is an ordinary
            #             edit — save, submit, approve — instead of the silent
            #             revive-to-APPROVED that used to do it behind the gate.
            #   APPROVED  an edit after approval. Rewriting the snapshot under a
            #             live approval broke its seal, and deploy then refused
            #             the row permanently with nothing saying why.
            #
            # Leaving the seal or decided_by behind would let a later read claim
            # an approver signed off on bytes they never saw.
            if existing.status != TransactionQueueStatusEnum.DRAFT:
                existing.status = TransactionQueueStatusEnum.DRAFT
                existing.decided_by = None
                existing.decided_at = None
                existing.decision_comment = None
                existing.approved_snapshot_hash = None
            self.session.add(existing)
            await self.session.flush()
            return existing

        row = self.model(
            code=f"queue-{uuid.uuid4().hex[:12]}",
            name=display_name[:255],
            user_code=user_code,
            tenant_code=tenant_code,
            transaction_code=transaction_code,
            # SERVICE_CONFIG, not KONG_ROUTE: the row's transaction_code IS a
            # service_configs code, and case_ref_code="add_route" is what says
            # "gateway change". Readers accept the old KONG_ROUTE value until
            # pre-flip rows are cleared.
            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
            case_ref_code="add_route",
            # DRAFT, like every other change. Gateway rows used to be born
            # APPROVED so they could skip the service-config approval gate, and a
            # route change reached production with nobody having seen it. Every
            # deploy path already refuses anything that is not APPROVED, so
            # entering as a draft IS the gate: the row cannot ship until a
            # decision moves it.
            status=TransactionQueueStatusEnum.DRAFT,
            config_snapshot=snapshot,
            display_name=display_name[:255],
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def clear_pending_for_scope(
        self,
        tenant_code: str,
        user_code: str,
        services_mst_code: str,
        environment,
        geo_loc_mst_code: str,
    ) -> Optional[str]:
        """Drop THIS USER's pending row for a scope. Returns the code, or None.

        For a save that leaves nothing changed — every edit undone across every
        group. Scoped to the user: one user abandoning their edits must not erase
        a teammate's queued change on the same service.

        Soft delete, so the row survives as history and stops matching the pending
        lookup, which is what makes the user's slice read as clean.

        Clears FAILED rows too: undoing the change is exactly how you abandon a
        failed deploy, and leaving it behind would keep the scope looking dirty.
        """
        existing = (await self.session.execute(
            select(self.model).where(
                gateway_rows_clause(),
                self.model.user_code == user_code,
                self.model.transaction_code.in_(
                    self._scope_config_codes(
                        tenant_code, services_mst_code, environment, geo_loc_mst_code
                    )
                ),
                self.model.status.in_(self._VISIBLE_PENDING_STATUSES),
                self.model.is_deleted == False,
            )
        )).scalars().all()

        cleared = None
        for row in existing:
            row.is_deleted = True
            row.is_active = False
            row.deleted_at = func.now()
            self.session.add(row)
            cleared = row.code
        if cleared:
            await self.session.flush()
        return cleared

    async def resolve_item_environment(self, item) -> Optional[str]:
        """The queue item's environment, from the most reliable source available.

        Three levels — needed because `source_entity` is NOT a relationship
        (only get_pending_items_for_user attaches it; get_by_id returns bare
        rows), and prod-vs-stage ROUTING must never silently miss:
          1. the attached source row (when present)
          2. config_snapshot["environment"] (server-stored at queue time)
          3. query the source table by transaction_code (service_config /
             infrastructure / kong route-group routing)
        Returns the lowercase environment string, or None if truly unknowable.
        """
        src = getattr(item, "source_entity", None)
        if src is not None:
            env = getattr(src, "environments_enum", None) or getattr(src, "environment", None)
            if env:
                return (env.value if hasattr(env, "value") else str(env)).lower()

        snap_env = (item.config_snapshot or {}).get("environment")
        if snap_env:
            return str(snap_env).lower()

        from app.core.enum import WorkflowSourceTableEnum
        if item.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
            row = (await self.db.execute(
                select(InfrastructureMstModel.environments_enum).where(
                    InfrastructureMstModel.code == item.transaction_code
                )
            )).scalar_one_or_none()
            if row:
                return (row.value if hasattr(row, "value") else str(row)).lower()
        elif item.table_name in (
            WorkflowSourceTableEnum.SERVICE_CONFIG,
            WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
        ):
            from app.db.models.service_config_model import ServiceConfigModel
            row = (await self.db.execute(
                select(ServiceConfigModel.environment).where(
                    ServiceConfigModel.code == item.transaction_code
                )
            )).scalar_one_or_none()
            if row:
                return (row.value if hasattr(row, "value") else str(row)).lower()
        elif item.table_name == WorkflowSourceTableEnum.KONG_ROUTE and item.transaction_code:
            from app.repository.kong_route_groups_repository import KongRouteGroupsRepository
            routing = await KongRouteGroupsRepository(self.db).resolve_routing(item.transaction_code)
            if routing and routing.get("environment"):
                return str(routing["environment"]).lower()
        return None

    async def get_pending_items_for_user(
        self,
        user_code: str,
        tenant_code: Optional[str] = None,
        environment: Optional[str] = None
    ) -> List[TransactionQueueModel]:
        """
        Get all pending queue items for a user.

        Args:
            user_code: User code
            tenant_code: Optional tenant filter
            environment: Optional environment filter

        Returns:
            List of pending queue items
        """
        filters = [
            TransactionQueueModel.user_code == user_code,
            TransactionQueueModel.status == TransactionQueueStatusEnum.APPROVED,
            TransactionQueueModel.is_deleted == False
        ]

        if tenant_code:
            filters.append(TransactionQueueModel.tenant_code == tenant_code)
        if environment:
            filters.append(TransactionQueueModel.environment == environment)

        stmt = (
            select(TransactionQueueModel)
            .where(and_(*filters))
            .order_by(TransactionQueueModel.created_at.desc())
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_gateway_request_for_deploy(
        self, transaction_code: str, tenant_code: str
    ) -> Optional["TransactionQueueModel"]:
        """The one APPROVED gateway (add_route) row for this service+env, whoever
        authored it — resolved by SCOPE, not by exact transaction_code.

        A gateway row stores ANY service_configs code in its scope (see
        KongRouteConfigService._scope_config_code), which need not be the code
        the caller passes — so an exact match would miss it. Resolving the
        caller's code to its scope (service + env + geo) and matching any code in
        that scope mirrors the save/read paths and finds the row whichever config
        code was stamped. Parity with variables/settings: NO user filter — a
        change is approved once and any can_deploy holder may ship it, not only
        its author. The submit-time lane lock keeps this to one row;
        `case_ref_code = 'add_route'` separates it from the settings/variables
        rows that share the same transaction_code.
        """
        sc = (await self.session.execute(
            select(ServiceConfigModel).where(ServiceConfigModel.code == transaction_code)
        )).scalars().first()
        if sc is None:
            return None
        scope_codes = self._scope_config_codes(
            tenant_code, sc.services_mst_code, sc.environment, sc.geo_loc_mst_code
        )
        stmt = (
            select(TransactionQueueModel)
            .where(
                TransactionQueueModel.transaction_code.in_(scope_codes),
                TransactionQueueModel.tenant_code == tenant_code,
                TransactionQueueModel.case_ref_code == "add_route",
                TransactionQueueModel.status == TransactionQueueStatusEnum.APPROVED,
                TransactionQueueModel.is_deleted == False,
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def get_items_for_pr(
        self,
        pr_number: int
    ) -> List[TransactionQueueModel]:
        """
        Get all queue items associated with a PR.

        Args:
            pr_number: GitHub PR number

        Returns:
            List of queue items for the PR
        """
        stmt = (
            select(TransactionQueueModel)
            .join(
                TransactionQueueWorkflowMappingModel,
                TransactionQueueWorkflowMappingModel.transaction_queue_code == TransactionQueueModel.code
            )
            .join(
                GitopsWorkflowDetailModel,
                TransactionQueueWorkflowMappingModel.gitops_workflow_code == GitopsWorkflowDetailModel.code
            )
            .where(GitopsWorkflowDetailModel.pr_number == pr_number)
            .where(TransactionQueueModel.is_deleted == False)
            .order_by(TransactionQueueModel.created_at.asc())
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_latest_pr_refs_for_queue_codes(
        self,
        queue_codes: List[str],
    ) -> List[dict]:
        """
        Each queue row's NEWEST PR reference (number, repo, url), via the
        queue↔workflow mapping. Rows with no PR-bearing workflow are absent.

        Latest only, by design: a redeployed row maps to every workflow it ever
        shipped through, and syncing an OLD closed PR would flip the row to
        pr_rejected while its current PR is still open — update_pr_status writes
        by pr_number across all mapped rows.
        """
        if not queue_codes:
            return []
        stmt = (
            select(
                TransactionQueueWorkflowMappingModel.transaction_queue_code,
                GitopsWorkflowDetailModel.pr_number,
                GitopsWorkflowDetailModel.pr_url,
                GitopsWorkflowDetailModel.git_repository,
            )
            .join(
                GitopsWorkflowDetailModel,
                TransactionQueueWorkflowMappingModel.gitops_workflow_code == GitopsWorkflowDetailModel.code,
            )
            .where(TransactionQueueWorkflowMappingModel.transaction_queue_code.in_(queue_codes))
            .where(GitopsWorkflowDetailModel.pr_number.isnot(None))
            .order_by(TransactionQueueWorkflowMappingModel.id.desc())
        )
        rows = (await self.session.execute(stmt)).all()
        latest: dict[str, dict] = {}
        for queue_code, pr_number, pr_url, git_repository in rows:
            # Rows arrive newest-first; keep only the first seen per queue code.
            if queue_code not in latest:
                latest[queue_code] = {
                    "queue_code": queue_code,
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                    "git_repository": git_repository,
                }
        return list(latest.values())

    async def get_queue_codes_for_pr(
        self,
        pr_number: int,
        git_repository: str
    ) -> List[str]:
        """
        Get queue codes associated with a PR + repository.

        Lightweight query: gitops_workflow_detail → mapping → transaction_queue codes.
        No polymorphic joins — just returns codes for further lookup.

        Args:
            pr_number: GitHub PR number
            git_repository: GitHub repository (e.g., "owner/repo")

        Returns:
            List of queue codes
        """
        stmt = (
            select(TransactionQueueModel.code)
            .join(
                TransactionQueueWorkflowMappingModel,
                TransactionQueueWorkflowMappingModel.transaction_queue_code == TransactionQueueModel.code
            )
            .join(
                GitopsWorkflowDetailModel,
                TransactionQueueWorkflowMappingModel.gitops_workflow_code == GitopsWorkflowDetailModel.code
            )
            .where(
                GitopsWorkflowDetailModel.pr_number == pr_number,
                GitopsWorkflowDetailModel.git_repository == git_repository,
                TransactionQueueModel.is_deleted == False
            )
            .distinct()
        )

        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

    async def update_status_by_pr_and_repository(
        self,
        pr_number: int,
        git_repository: str,
        new_status: TransactionQueueStatusEnum
    ) -> int:
        """
        Update transaction queue status for all items linked to a PR + repository.

        Resolves queue codes via the workflow mapping table, then bulk-updates status.

        Args:
            pr_number: GitHub PR number
            git_repository: GitHub repository (e.g., "owner/repo")
            new_status: New status to set

        Returns:
            Number of items updated
        """
        queue_codes = await self.get_queue_codes_for_pr(pr_number, git_repository)
        if not queue_codes:
            return 0
        return await self.bulk_update_status_by_codes(queue_codes, new_status)

    async def get_selected_queues_by_codes(
        self,
        queue_codes: List[str],
        tenant_code: str
    ) -> List[TransactionQueueModel]:
        """
        Fetch queues by their codes with polymorphic source entity joins.

        Same pattern as get_selected_queues but uses queue codes instead of IDs.

        Args:
            queue_codes: List of queue codes to fetch
            tenant_code: Tenant code for isolation

        Returns:
            List of queue items with source_entity attached
        """
        if not queue_codes:
            return []

        filters = [
            TransactionQueueModel.code.in_(queue_codes),
            TransactionQueueModel.tenant_code == tenant_code,
            TransactionQueueModel.is_deleted == False
        ]

        base_stmt = (
            select(TransactionQueueModel)
            .where(and_(*filters))
            .order_by(TransactionQueueModel.created_at.asc())
        )

        # Add polymorphic joins
        stmt = self._build_polymorphic_query(base_stmt)

        stmt = stmt.add_columns(
            ServiceConfigModel,
            ServiceConfigDockerfileWorkflowModel,
            AlertConfigModel,
            InfrastructureMstModel,
            KongRouteConfigModel,
            PipelineMstModel
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        queue_items = []
        for row in rows:
            queue_item = row[0]

            if queue_item.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG:
                queue_item.source_entity = row[1]
            elif queue_item.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE:
                queue_item.source_entity = row[2]
            elif queue_item.table_name == WorkflowSourceTableEnum.ALERT_CONFIG:
                queue_item.source_entity = row[3]
            elif queue_item.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
                queue_item.source_entity = row[4]
            elif queue_item.table_name == WorkflowSourceTableEnum.KONG_ROUTE:
                queue_item.source_entity = row[5]
            elif queue_item.table_name == WorkflowSourceTableEnum.PIPELINE:
                queue_item.source_entity = row[6]
            else:
                queue_item.source_entity = None

            queue_items.append(queue_item)

        return queue_items

    # Statuses that mean the row's config has been submitted to deploy — the
    # baseline for the settings-diff. PR_MERGED/DEPLOYED are the true "live in
    # AWS" markers but arrive only after a (often manual) merge, so we also
    # treat PR_RAISED/PR_APPROVED as deployed: the diff clears as soon as the
    # deploy PR is raised instead of making the user wait for the merge.
    _DEPLOYED_STATUSES = [
        TransactionQueueStatusEnum.PR_RAISED,
        TransactionQueueStatusEnum.PR_APPROVED,
        TransactionQueueStatusEnum.PR_MERGED,
        TransactionQueueStatusEnum.DEPLOYED,
        TransactionQueueStatusEnum.APPLIED_SUCCESSFULLY,
    ]

    async def has_settings_queue_item(
        self,
        transaction_code: str,
        tenant_code: Optional[str] = None,
    ) -> bool:
        """True when the LATEST settings-change queue row is deployable —
        i.e. the newest update_service item is DRAFT/APPROVED/FAILED.

        Gates the settings diff so it only shows when a redeploy can actually
        run. Uses the LATEST row's status (not "any"): once a deploy flips the
        newest item to STARTING_DEPLOYMENT/PR_RAISED/DEPLOYED, the diff hides —
        even if older leftover drafts still exist. FAILED is included so a
        failed deploy keeps the diff visible (changes never went live, so the
        user must be able to see and retry them). Scoped to update_service so
        Kong add_route rows don't count.
        """
        filters = [
            TransactionQueueModel.transaction_code == transaction_code,
            TransactionQueueModel.case_ref_code == "update_service",
            TransactionQueueModel.is_deleted == False,
        ]
        if tenant_code:
            filters.append(TransactionQueueModel.tenant_code == tenant_code)
        stmt = (
            select(TransactionQueueModel.status)
            .where(and_(*filters))
            .order_by(TransactionQueueModel.created_at.desc())
            .limit(1)
        )
        latest_status = (await self.session.execute(stmt)).scalar_one_or_none()
        return latest_status in (
            TransactionQueueStatusEnum.DRAFT,
            TransactionQueueStatusEnum.APPROVED,
            TransactionQueueStatusEnum.FAILED,
        )

    async def get_latest_deployed_by_transaction_code(
        self,
        transaction_code: str,
        tenant_code: Optional[str] = None,
        settings_rows_only: bool = False,
    ) -> Optional[TransactionQueueModel]:
        """Latest successfully-deployed queue row for an entity.

        Used as the "last deployed config" baseline for the settings-diff. A
        deploy that reached AWS leaves the row DEPLOYED (temporal) or PR_MERGED
        / APPLIED_SUCCESSFULLY (Atlantis); its config_snapshot is what is live.

        settings_rows_only: a service's transaction_code is shared by its
        KONG_ROUTE rows and its update_variables rows, and whichever deployed
        LAST wins the unfiltered query. A caller reading the snapshot as a
        settings baseline (fields, sidecars) must skip those — their snapshots
        hold route deltas or an S3 pointer, and "latest deployed" silently
        stops meaning "latest deployed settings" the day another lane ships.
        """
        from app.core.enum import WorkflowSourceTableEnum

        filters = [
            TransactionQueueModel.transaction_code == transaction_code,
            TransactionQueueModel.status.in_(self._DEPLOYED_STATUSES),
            TransactionQueueModel.is_deleted == False,
        ]
        if settings_rows_only:
            filters.append(
                TransactionQueueModel.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG
            )
            # add_route is excluded alongside update_variables: gateway rows
            # now live on SERVICE_CONFIG too, and their snapshots hold route
            # deltas, not settings.
            filters.append(
                or_(
                    TransactionQueueModel.case_ref_code.is_(None),
                    TransactionQueueModel.case_ref_code.notin_(
                        ("update_variables", "add_route")
                    ),
                )
            )
        if tenant_code:
            filters.append(TransactionQueueModel.tenant_code == tenant_code)

        stmt = (
            select(TransactionQueueModel)
            .where(and_(*filters))
            .order_by(TransactionQueueModel.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_items_by_status(
        self,
        status: TransactionQueueStatusEnum,
        user_code: Optional[str] = None,
        tenant_code: Optional[str] = None,
        limit: int = 100
    ) -> List[TransactionQueueModel]:
        """
        Get queue items by status.

        Args:
            status: Queue item status
            user_code: Optional user filter
            tenant_code: Optional tenant filter
            limit: Maximum items to return

        Returns:
            List of queue items
        """
        filters = [
            TransactionQueueModel.status == status,
            TransactionQueueModel.is_deleted == False
        ]

        if user_code:
            filters.append(TransactionQueueModel.user_code == user_code)
        if tenant_code:
            filters.append(TransactionQueueModel.tenant_code == tenant_code)

        stmt = (
            select(TransactionQueueModel)
            .where(and_(*filters))
            .order_by(TransactionQueueModel.created_at.desc())
            .limit(limit)
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_user_queue_with_counts(
        self,
        user_code: str,
        tenant_code: Optional[str] = None
    ) -> Tuple[
        List[Tuple[TransactionQueueModel, Optional[str], Optional[EnvironmentEnum], Optional[str], Optional[EnvironmentEnum], Optional[str], Any]],
        int,
        int
    ]:
        """
        Get user's queue items with status counts and latest pipeline run status.

        Join chain for pipeline status (direct 1-hop):
        transaction_queue.transaction_code → pipeline_mst.transaction_code →
        latest pipeline_run_track (by created_at DESC LIMIT 1)

        Returns:
            Tuple of (items, pending_count, pr_raised_count)
            Each item: (queue_item, infra_geo, infra_env, kong_geo, kong_env, pipeline_run_status, pipeline_build_stages)
        """
        from app.db.models.pipeline_run_track_model import PipelineRunTrackModel
        from sqlalchemy import literal_column

        base_filters = [
            TransactionQueueModel.user_code == user_code,
            TransactionQueueModel.is_deleted == False
        ]
        if tenant_code:
            base_filters.append(TransactionQueueModel.tenant_code == tenant_code)

        # LATERAL subquery: latest pipeline_run_track per queue item.
        #
        # Resolution order:
        #   1) Direct linkage via run_track.transaction_queue_code @> [queue.code]
        #      — works for both service deploys and infra-apply runs (the
        #      orchestrators set this array when triggering Jenkins).
        #   2) Fallback: pipeline_mst.transaction_code = queue.transaction_code
        #      — preserves behavior for older runs that pre-date the queue-code
        #      linkage.
        #
        # Infra-apply uses one shared pipeline_mst per (tenant, env) with a
        # synthetic transaction_code (e.g. INFRA_APPLY_aslam_stage), so the
        # queue.transaction_code = pipeline_mst.transaction_code path NEVER
        # matches for those rows — only the queue-code linkage finds them.
        latest_run = (
            select(
                PipelineRunTrackModel.status.label("run_status"),
                PipelineRunTrackModel.build_stages.label("run_build_stages"),
                PipelineRunTrackModel.created_at.label("run_created_at"),
                PipelineRunTrackModel.deploy_result.label("run_deploy_result"),
            )
            .join(
                PipelineMstModel,
                PipelineRunTrackModel.pipeline_mst_code == PipelineMstModel.code,
            )
            .where(
                and_(
                    PipelineRunTrackModel.is_deleted == False,
                    or_(
                        PipelineRunTrackModel.transaction_queue_code.contains(
                            cast(
                                func.jsonb_build_array(TransactionQueueModel.code),
                                JSONB,
                            )
                        ),
                        and_(
                            PipelineMstModel.transaction_code == TransactionQueueModel.transaction_code,
                            PipelineMstModel.table_name == TransactionQueueModel.table_name,
                        ),
                    ),
                )
            )
            .order_by(
                # Prefer THIS item's own run (precise queue-code link) over the
                # per-service pipeline_mst fallback — otherwise every queue item that
                # shares a transaction_code (e.g. all gateway deploys of a service)
                # resolves to the same newest run, showing duplicate PRs across cards.
                PipelineRunTrackModel.transaction_queue_code.contains(
                    cast(func.jsonb_build_array(TransactionQueueModel.code), JSONB)
                ).desc(),
                PipelineRunTrackModel.created_at.desc(),
            )
            .limit(1)
            .correlate(TransactionQueueModel)
            .lateral("latest_run")
        )

        # Get items with geo/env from infra or kong + pipeline run status
        item_filters = base_filters
        stmt = (
            select(
                TransactionQueueModel,
                InfrastructureMstModel.geo_loc_mst_code.label("infra_geo_loc_mst_code"),
                InfrastructureMstModel.environments_enum.label("infra_environment"),
                # Three shapes of KONG_ROUTE row, coalesced into two columns the
                # caller already knows: the per-route flow points transaction_code
                # at kong_route_configs, while BOTH gateway shapes — the older
                # per-group one and the scope-keyed one — are folded into the
                # "group" pair. Folding here rather than adding columns keeps the
                # selected tuple 12 wide, which is what the caller unpacks.
                KongRouteConfigModel.geo_loc_mst_code.label("kong_geo_loc_mst_code"),
                KongRouteConfigModel.environments_enum.label("kong_environment"),
                func.coalesce(
                    KongRouteGroupModel.geo_loc_mst_code,
                    KongScopeConfigModel.geo_loc_mst_code,
                ).label("kong_group_geo_loc_mst_code"),
                func.coalesce(
                    KongRouteGroupModel.environments_enum,
                    KongScopeConfigModel.environment,
                ).label("kong_group_environment"),
                # Which service a gateway item belongs to. Exposed because
                # transaction_code alone cannot say: neither a KRG_ nor an SC_ code
                # means anything to a caller, so anything filtering the queue by
                # service had to guess. This is also what the Gateway tab matches
                # its own pending row on.
                func.coalesce(
                    KongRouteGroupModel.services_mst_code,
                    KongScopeConfigModel.services_mst_code,
                    KongRouteConfigModel.services_mst_code,
                ).label("kong_service_mst_code"),
                latest_run.c.run_status.label("pipeline_run_status"),
                latest_run.c.run_build_stages.label("pipeline_build_stages"),
                latest_run.c.run_created_at.label("pipeline_run_created_at"),
                latest_run.c.run_deploy_result.label("pipeline_deploy_result"),
            )
            .outerjoin(
                InfrastructureMstModel,
                and_(
                    TransactionQueueModel.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE,
                    TransactionQueueModel.transaction_code == InfrastructureMstModel.code,
                )
            )
            .outerjoin(
                KongRouteConfigModel,
                and_(
                    TransactionQueueModel.table_name == WorkflowSourceTableEnum.KONG_ROUTE,
                    TransactionQueueModel.transaction_code == KongRouteConfigModel.code,
                )
            )
            .outerjoin(
                KongRouteGroupModel,
                and_(
                    TransactionQueueModel.table_name == WorkflowSourceTableEnum.KONG_ROUTE,
                    TransactionQueueModel.transaction_code == KongRouteGroupModel.code,
                )
            )
            .outerjoin(
                KongScopeConfigModel,
                and_(
                    gateway_rows_clause(),
                    TransactionQueueModel.transaction_code == KongScopeConfigModel.code,
                )
            )
            .outerjoin(latest_run, literal_column("true"))
            .where(and_(*item_filters))
            .order_by(TransactionQueueModel.created_at.desc())
        )
        result = await self.session.execute(stmt)
        items = list(result.all())

        # Count pending
        pending_stmt = (
            select(func.count())
            .select_from(TransactionQueueModel)
            .where(and_(
                *base_filters,
                TransactionQueueModel.status == TransactionQueueStatusEnum.APPROVED
            ))
        )
        pending_result = await self.session.execute(pending_stmt)
        pending_count = pending_result.scalar() or 0

        # Count PR raised
        pr_stmt = (
            select(func.count())
            .select_from(TransactionQueueModel)
            .where(and_(
                *base_filters,
                TransactionQueueModel.status == TransactionQueueStatusEnum.PR_RAISED
            ))
        )
        pr_result = await self.session.execute(pr_stmt)
        pr_raised_count = pr_result.scalar() or 0

        return items, pending_count, pr_raised_count

    async def check_duplicate(
        self,
        user_code: str,
        service_config_code: str,
        environment: str
    ) -> Optional[TransactionQueueModel]:
        """
        Check if a pending item already exists for this config+env.

        Args:
            user_code: User code
            service_config_code: Service config code
            environment: Environment

        Returns:
            Existing queue item if found, None otherwise
        """
        stmt = (
            select(TransactionQueueModel)
            .where(and_(
                TransactionQueueModel.user_code == user_code,
                TransactionQueueModel.service_config_code == service_config_code,
                TransactionQueueModel.environment == environment,
            TransactionQueueModel.status == TransactionQueueStatusEnum.APPROVED
        ))
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def check_duplicate_infra(
        self,
        user_code: str,
        service_name: str,
        environment: str
    ) -> Optional[TransactionQueueModel]:
        """
        Check if a pending infra item already exists for this name+env.

        Used for standalone infrastructure items (no service_config_code).

        Args:
            user_code: User code
            service_name: Infrastructure name (from config_snapshot)
            environment: Environment

        Returns:
            Existing queue item if found, None otherwise
        """
        stmt = (
            select(TransactionQueueModel)
            .where(and_(
                TransactionQueueModel.user_code == user_code,
                TransactionQueueModel.service_config_code.is_(None),  # Infra items have no service_config_code
                TransactionQueueModel.environment == environment,
                TransactionQueueModel.status == TransactionQueueStatusEnum.APPROVED,
                TransactionQueueModel.config_snapshot['service_name'].astext == service_name
            ))
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def bulk_update_status(
        self,
        item_ids: List[int],
        status: TransactionQueueStatusEnum,
        pr_number: Optional[int] = None,
        pr_url: Optional[str] = None,
        git_branch: Optional[str] = None,
        commit_sha: Optional[str] = None,
        gitops_workflow_id: Optional[int] = None
    ) -> int:
        """
        Bulk update status and PR info for multiple items.

        Args:
            item_ids: List of item IDs to update
            status: New status
            pr_number: PR number (optional)
            pr_url: PR URL (optional)
            git_branch: Git branch (optional)
            commit_sha: Commit SHA (optional)
            gitops_workflow_id: GitOps workflow ID (optional)

        Returns:
            Number of items updated
        """
        if not item_ids:
            return 0

        # Fetch items
        stmt = (
            select(TransactionQueueModel)
            .where(TransactionQueueModel.id.in_(item_ids))
        )
        result = await self.session.execute(stmt)
        items = list(result.scalars().all())

        # Update each item
        for item in items:
            self.append_status_event(item, status)
            item.status = status
            if pr_number is not None:
                item.pr_number = pr_number
            if pr_url is not None:
                item.pr_url = pr_url
            if git_branch is not None:
                item.git_branch = git_branch
            if commit_sha is not None:
                item.commit_sha = commit_sha
            if gitops_workflow_id is not None:
                item.gitops_workflow_id = gitops_workflow_id

            self.session.add(item)

        await self.session.flush()
        return len(items)

    # Statuses a row can be in while its pull request is open. Only these may
    # be returned to APPROVED when the PR closes without merging — anything
    # else (a draft, a submitted request, a deployed row) was not riding that
    # PR and must not be moved by its fate.
    _PR_IN_FLIGHT = (
        TransactionQueueStatusEnum.STARTING_DEPLOYMENT,
        TransactionQueueStatusEnum.COMMIT,
        TransactionQueueStatusEnum.CHECKOUT,
        TransactionQueueStatusEnum.PR_DRAFT,
        TransactionQueueStatusEnum.PR_RAISED,
        TransactionQueueStatusEnum.PR_APPROVED,
    )

    def _pr_rejected_to_approved(self, item: TransactionQueueModel) -> bool:
        """A closed-without-merge PR returns its rows to APPROVED — same rule
        as a failed deploy (update_queue_status's FAILED branch), for the same
        reason: the change never landed, so nothing about what the reviewer
        approved has changed, and parking it at PR_REJECTED stranded it —
        deploy accepts APPROVED rows only, and nothing ever moved a row out of
        PR_REJECTED. The closure is not lost: it is written to the row's
        history, the gitops_workflow_detail row keeps the PR's own state, and
        the seal stays intact so the retry verifies against the same decision.

        NOT for prod. The environments part ways at the shipping point: on
        qa/stage nothing moves until the PR merges, so a closed PR means the
        change never landed and APPROVED is simply the truth again. On prod
        the raised PR IS the outcome — the live service_configs row was
        written at PR creation (_save_settings_for_batch), merging is manual
        ops on their own schedule — so by the time a prod PR closes, devlift
        already counts the change as landed. Re-arming it as APPROVED would
        offer a redeploy of values the record says are live. A prod row keeps
        PR_REJECTED, with the closure on its history; if the change is still
        wanted, it is raised again as a new request.

        Returns True when the row was moved."""
        from datetime import datetime, timezone

        if item.status not in self._PR_IN_FLIGHT:
            return False
        was = getattr(item.status, "value", str(item.status))
        environment = (item.config_snapshot or {}).get("environment")
        if environment == "prod":
            item.status = TransactionQueueStatusEnum.PR_REJECTED
            item.history = (item.history or []) + [{
                "at": datetime.now(timezone.utc).isoformat(),
                "by": "rule",
                "event": "pr-rejected",
                "comment": "pull request closed",
            }]
            return False
        item.status = TransactionQueueStatusEnum.APPROVED
        # JSONB does not see in-place mutation: reassign, never append.
        item.history = (item.history or []) + [{
            "at": datetime.now(timezone.utc).isoformat(),
            "by": "rule",
            "event": "pr-rejected",
            "comment": f"pull request closed without merging while {was}; "
                       f"returned to approved so it can be deployed again",
        }]
        return True

    @staticmethod
    def append_status_event(item: TransactionQueueModel, status: TransactionQueueStatusEnum) -> None:
        """History entry for a PIPELINE status transition — every move a row
        makes belongs on its record, not just the approval-flow ones (created /
        submitted / approved / ...), which the approval service writes itself.

        Event name is the status, kebab-cased ("pr_raised" → "pr-raised"), so
        the History tab can label the common ones and render the rest verbatim.
        No-op when the status is not actually changing — a Temporal retry or an
        idempotent re-apply must not duplicate the timeline.
        """
        from datetime import datetime, timezone

        if item.status == status:
            return
        # JSONB does not see in-place mutation: reassign, never append.
        item.history = (item.history or []) + [{
            "at": datetime.now(timezone.utc).isoformat(),
            "by": "pipeline",
            "event": getattr(status, "value", str(status)).replace("_", "-"),
        }]

    async def bulk_update_status_by_codes(
        self,
        queue_codes: List[str],
        status: TransactionQueueStatusEnum
    ) -> int:
        """
        Bulk update status for multiple items by their queue codes using direct UPDATE query.

        Args:
            queue_codes: List of queue codes to update
            status: New status

        Returns:
            Number of items updated
        """
        if not queue_codes:
            return 0

        from datetime import datetime, timezone

        if status == TransactionQueueStatusEnum.PR_REJECTED:
            # Row path, not the bulk UPDATE: a closed-without-merge PR sends
            # its rows back to APPROVED with a history event, and only rows
            # that were actually riding the PR — see _pr_rejected_to_approved.
            rows = (
                await self.session.execute(
                    select(TransactionQueueModel).where(
                        TransactionQueueModel.code.in_(queue_codes)
                    )
                )
            ).scalars().all()
            moved = sum(1 for r in rows if self._pr_rejected_to_approved(r))
            await self.session.flush()
            return moved

        # Row path rather than a bulk UPDATE: each transition goes onto the
        # row's history, and JSONB appends need the row loaded.
        rows = (
            await self.session.execute(
                select(TransactionQueueModel).where(
                    TransactionQueueModel.code.in_(queue_codes)
                )
            )
        ).scalars().all()
        for row in rows:
            self.append_status_event(row, status)
            row.status = status
            row.status_last_updated_at = datetime.now(timezone.utc)
        await self.session.flush()

        return len(rows)

    async def bulk_soft_delete_by_codes(self, queue_codes: List[str]) -> int:
        """
        Bulk soft delete queue items by their codes (set is_deleted=True).

        Args:
            queue_codes: List of queue codes to soft delete

        Returns:
            Number of items updated
        """
        if not queue_codes:
            return 0

        from sqlalchemy import update
        from datetime import datetime, timezone

        stmt = (
            update(TransactionQueueModel)
            .where(TransactionQueueModel.code.in_(queue_codes))
            .values(
                is_deleted=True,
                status_last_updated_at=datetime.now(timezone.utc),
            )
        )

        result = await self.session.execute(stmt)
        await self.session.flush()

        return result.rowcount

    async def update_status(self, queue_id: int, status: str) -> None:
        """Update the status of a single transaction queue item by ID."""
        stmt = (
            update(TransactionQueueModel)
            .where(TransactionQueueModel.id == queue_id)
            .values(status=status)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def get_entity_refs_by_ids(
        self,
        queue_ids: List[int],
    ) -> List[Dict[str, Any]]:
        """Return transaction_code + table_name + case_ref_code for the given queue IDs."""
        rows = (
            await self.session.execute(
                select(
                    TransactionQueueModel.transaction_code,
                    TransactionQueueModel.table_name,
                    TransactionQueueModel.case_ref_code,
                )
                .where(TransactionQueueModel.id.in_(queue_ids))
            )
        ).all()
        return [
            {
                "transaction_code": r.transaction_code,
                "table_name": r.table_name,
                "case_ref_code": r.case_ref_code,
            }
            for r in rows
        ]

    async def get_entity_refs_by_codes(
        self,
        queue_codes: List[str],
    ) -> List[Dict[str, Any]]:
        """Return transaction_code + table_name + case_ref_code for the given queue codes.

        Code-keyed counterpart of ``get_entity_refs_by_ids`` — the PR workflow
        stages its status decisions by queue *code*, so resolving the source
        resource of a staged item needs this shape.
        """
        rows = (
            await self.session.execute(
                select(
                    TransactionQueueModel.transaction_code,
                    TransactionQueueModel.table_name,
                    TransactionQueueModel.case_ref_code,
                )
                .where(TransactionQueueModel.code.in_(queue_codes))
            )
        ).all()
        return [
            {
                "transaction_code": r.transaction_code,
                "table_name": r.table_name,
                "case_ref_code": r.case_ref_code,
            }
            for r in rows
        ]

    async def get_prs_for_user(
        self,
        user_code: str,
        tenant_code: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get distinct PRs created by user from queue items.

        Groups items by PR number and returns PR-level info.

        Args:
            user_code: User code
            tenant_code: Optional tenant filter

        Returns:
            List of PR info dicts with item counts
        """
        filters = [
            TransactionQueueModel.user_code == user_code,
            TransactionQueueModel.status.in_([
                TransactionQueueStatusEnum.PR_RAISED,
                TransactionQueueStatusEnum.APPROVED,
                TransactionQueueStatusEnum.PR_APPROVED,
                TransactionQueueStatusEnum.PR_REJECTED
            ]),
            TransactionQueueModel.is_deleted == False
        ]

        if tenant_code:
            filters.append(TransactionQueueModel.tenant_code == tenant_code)

        # Get all items with PRs via mapping table
        stmt = (
            select(TransactionQueueModel, GitopsWorkflowDetailModel)
            .join(
                TransactionQueueWorkflowMappingModel,
                TransactionQueueWorkflowMappingModel.transaction_queue_code == TransactionQueueModel.code
            )
            .join(
                GitopsWorkflowDetailModel,
                TransactionQueueWorkflowMappingModel.gitops_workflow_code == GitopsWorkflowDetailModel.code
            )
            .where(and_(*filters))
            .where(GitopsWorkflowDetailModel.pr_number.isnot(None))
            .order_by(GitopsWorkflowDetailModel.created_at.desc())
        )

        result = await self.session.execute(stmt)
        rows = list(result.all())

        # Group by PR number
        pr_map: Dict[int, Dict[str, Any]] = {}
        status_priority = {
            TransactionQueueStatusEnum.APPROVED.value:0,
            TransactionQueueStatusEnum.PR_RAISED.value:1
        }
        for item, workflow in rows:
            pr_num = workflow.pr_number
            if pr_num is None:
                continue
            if pr_num not in pr_map:
                pr_map[pr_num] = {
                    "pr_number": pr_num,
                    "pr_url": workflow.pr_url,
                    "git_branch": workflow.git_branch,
                    "git_repository": workflow.git_repository,
                    "status": item.status.value,
                    "items_count": 0,
                    "environments": set(),
                    "infra_types": set(),
                    "items": [],
                    "created_at": workflow.created_at or item.created_at
                }

            existing_status = pr_map[pr_num]["status"]
            if status_priority.get(item.status.value, 0) > status_priority.get(existing_status, 0):
                pr_map[pr_num]["status"] = item.status.value

            pr_map[pr_num]["items_count"] += 1
            snapshot = item.config_snapshot or {}
            environment = snapshot.get("environment")
            infra_type = snapshot.get("infra_type")
            if environment:
                pr_map[pr_num]["environments"].add(environment)
            if infra_type:
                pr_map[pr_num]["infra_types"].add(infra_type)
            pr_map[pr_num]["items"].append({
                "id": item.__dict__.get("id"),
                "code": item.__dict__.get("code"),
                "user_code": item.__dict__.get("user_code"),
                "transaction_code": item.__dict__.get("transaction_code"),
                "table_name": item.__dict__.get("table_name"),
                "config_snapshot": item.__dict__.get("config_snapshot"),
                "display_name": item.__dict__.get("display_name"),
                "case_ref_code": item.__dict__.get("case_ref_code"),
                "status": item.__dict__.get("status"),
                "status_last_updated_at": item.__dict__.get("status_last_updated_at"),
                "tenant_code": item.__dict__.get("tenant_code"),
                "created_at": item.__dict__.get("created_at"),
                "updated_at": item.__dict__.get("updated_at")
            })

        # Convert sets to lists for JSON serialization
        pr_list = []
        for pr_info in pr_map.values():
            pr_info["environments"] = list(pr_info["environments"])
            pr_info["infra_types"] = list(pr_info["infra_types"])
            pr_list.append(pr_info)

        return pr_list

    async def soft_delete_item(self, item_id: int) -> Optional[TransactionQueueModel]:
        """
        Soft delete a queue item.

        Args:
            item_id: Item ID to delete

        Returns:
            Updated item if found, None otherwise
        """
        item = await self.get_by_id(item_id)
        if item and item.status == TransactionQueueStatusEnum.APPROVED:
            item.soft_delete()
            self.session.add(item)
            await self.session.flush()
            await self.session.refresh(item)
            return item
        return None

    async def update_pr_status(
        self,
        pr_number: int,
        new_status: TransactionQueueStatusEnum
    ) -> int:
        """
        Update status for all items with a specific PR number.

        Args:
            pr_number: PR number
            new_status: New status to set

        Returns:
            Number of items updated
        """
        items = await self.get_items_for_pr(pr_number)

        for item in items:
            if new_status == TransactionQueueStatusEnum.PR_REJECTED:
                self._pr_rejected_to_approved(item)
            else:
                item.status = new_status
            self.session.add(item)

        await self.session.flush()
        return len(items)

    async def get_selected_queues(
        self,
        user_code: str,
        tenant_code: str,
        selected_queue_ids: List[int],
        statuses: Optional[List[TransactionQueueStatusEnum]] = None
    ) -> List[TransactionQueueModel]:
        """
        Fetch selected queues based on tenant, user code, statuses, and selected queue IDs.

        Includes polymorphic joins with source tables based on table_name.

        Args:
            user_code: User code
            tenant_code: Tenant code
            selected_queue_ids: List of queue IDs to fetch
            statuses: Optional list of statuses to filter by (if None, fetches all statuses)

        Returns:
            List of queue items matching the criteria with joined source table data
        """
        # Deliberately NOT filtered by user_code: a change is approved once and
        # any deployer holding can_deploy on the service may ship it, not only
        # its author. The ids are explicit and tenant-scoped, and can_deploy is
        # already enforced at the deploy endpoints — ownership is the wrong gate
        # here and dropped it once an approver could no longer deploy an author's
        # approved settings/gateway change.
        filters = [
            TransactionQueueModel.tenant_code == tenant_code,
            TransactionQueueModel.id.in_(selected_queue_ids),
            TransactionQueueModel.is_deleted == False
        ]

        if statuses:
            filters.append(TransactionQueueModel.status.in_(statuses))

        # Build base statement
        base_stmt = (
            select(TransactionQueueModel)
            .where(and_(*filters))
            .order_by(TransactionQueueModel.created_at.desc())
        )

        # Add polymorphic joins
        stmt = self._build_polymorphic_query(base_stmt)

        # Add columns from all joined tables to the select
        stmt = stmt.add_columns(
            ServiceConfigModel,
            ServiceConfigDockerfileWorkflowModel,
            AlertConfigModel,
            InfrastructureMstModel,
            KongRouteConfigModel,
            PipelineMstModel
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        # Process results: attach joined data to TransactionQueueModel instances
        queue_items = []
        for row in rows:
            queue_item = row[0]  # TransactionQueueModel

            # Attach the appropriate joined entity based on table_name
            if queue_item.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG:
                queue_item.source_entity = row[1]  # ServiceConfigModel
            elif queue_item.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE:
                queue_item.source_entity = row[2]  # ServiceConfigDockerfileWorkflowModel
            elif queue_item.table_name == WorkflowSourceTableEnum.ALERT_CONFIG:
                queue_item.source_entity = row[3]  # AlertConfigModel
            elif queue_item.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
                queue_item.source_entity = row[4]  # InfrastructureMstModel
            elif queue_item.table_name == WorkflowSourceTableEnum.KONG_ROUTE:
                queue_item.source_entity = row[5]  # KongRouteConfigModel
            elif queue_item.table_name == WorkflowSourceTableEnum.PIPELINE:
                queue_item.source_entity = row[6]  # PipelineMstModel
            else:
                queue_item.source_entity = None

            queue_items.append(queue_item)

        return queue_items

    async def get_all_queues_for_user(
        self,
        user_code: str,
        tenant_code: str,
        status: Optional[TransactionQueueStatusEnum] = None
    ) -> List[TransactionQueueModel]:
        """
        Fetch all queues for a user based on tenant, user code, and status.

        Includes polymorphic joins with source tables based on table_name.

        Args:
            user_code: User code
            tenant_code: Tenant code
            status: Optional status filter (if None, fetches all statuses)

        Returns:
            List of all queue items for the user with joined source table data
        """
        filters = [
            TransactionQueueModel.user_code == user_code,
            TransactionQueueModel.tenant_code == tenant_code,
            TransactionQueueModel.is_deleted == False
        ]

        if status:
            filters.append(TransactionQueueModel.status == status)

        # Build base statement
        base_stmt = (
            select(TransactionQueueModel)
            .where(and_(*filters))
            .order_by(TransactionQueueModel.created_at.desc())
        )

        # Add polymorphic joins
        stmt = self._build_polymorphic_query(base_stmt)

        # Add columns from all joined tables to the select
        stmt = stmt.add_columns(
            ServiceConfigModel,
            ServiceConfigDockerfileWorkflowModel,
            AlertConfigModel,
            InfrastructureMstModel,
            KongRouteConfigModel,
            PipelineMstModel
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        # Process results: attach joined data to TransactionQueueModel instances
        queue_items = []
        for row in rows:
            queue_item = row[0]  # TransactionQueueModel

            # Attach the appropriate joined entity based on table_name
            if queue_item.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG:
                queue_item.source_entity = row[1]  # ServiceConfigModel
            elif queue_item.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE:
                queue_item.source_entity = row[2]  # ServiceConfigDockerfileWorkflowModel
            elif queue_item.table_name == WorkflowSourceTableEnum.ALERT_CONFIG:
                queue_item.source_entity = row[3]  # AlertConfigModel
            elif queue_item.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
                queue_item.source_entity = row[4]  # InfrastructureMstModel
            elif queue_item.table_name == WorkflowSourceTableEnum.KONG_ROUTE:
                queue_item.source_entity = row[5]  # KongRouteConfigModel
            elif queue_item.table_name == WorkflowSourceTableEnum.PIPELINE:
                queue_item.source_entity = row[6]  # PipelineMstModel
            else:
                queue_item.source_entity = None

            queue_items.append(queue_item)

        return queue_items

    async def update_artifact_s3_key(
        self,
        code: str,
        artifact_s3_key: str
    ) -> Optional[TransactionQueueModel]:
        """
        Update the artifact S3 key for a queue item after successful file upload.

        This is called after HCL files are uploaded to S3 to track where they are stored.

        Args:
            code: Queue item code
            artifact_s3_key: S3 object key (e.g., "sqs/payment-queue.hcl")

        Returns:
            Updated queue item if found, None otherwise
        """
        stmt = select(TransactionQueueModel).where(TransactionQueueModel.code == code)
        result = await self.session.execute(stmt)
        item = result.scalar_one_or_none()

        if item:
            item.script_access_key = artifact_s3_key
            self.session.add(item)
            await self.session.flush()
            await self.session.refresh(item)
            return item
        return None

    async def bulk_approve_by_ids(
        self,
        queue_ids: List[int],
        user_code: str,
        tenant_code: str
    ) -> int:
        """
        Bulk update status to APPROVED for multiple items by their IDs.

        Only updates items that belong to the specified user and tenant.

        Args:
            queue_ids: List of queue IDs to approve
            user_code: User code for ownership validation
            tenant_code: Tenant code for isolation

        Returns:
            Number of items updated
        """
        if not queue_ids:
            return 0

        from sqlalchemy import update
        from datetime import datetime, timezone

        # Execute direct UPDATE query with user/tenant validation
        stmt = (
            update(TransactionQueueModel)
            .where(
                and_(
                    TransactionQueueModel.id.in_(queue_ids),
                    TransactionQueueModel.user_code == user_code,
                    TransactionQueueModel.tenant_code == tenant_code,
                    TransactionQueueModel.is_deleted == False
                )
            )
            .values(
                status=TransactionQueueStatusEnum.APPROVED,
                status_last_updated_at=datetime.now(timezone.utc)
            )
        )

        result = await self.session.execute(stmt)
        await self.session.flush()

        return result.rowcount

    async def search_draft_by_transaction(
        self,
        transaction_code: str,
        table_name: WorkflowSourceTableEnum,
        tenant_code: str,
        user_code: str,
        case_ref_code: Optional[str] = None,
        include_failed: bool = False,
        snapshot_scope: Optional[dict] = None,
    ) -> Optional[TransactionQueueModel]:
        """
        Search for the latest DRAFT or APPROVED queue item by transaction code and table name.

        If multiple entries exist, returns the most recently created one.

        Args:
            transaction_code: Code of the source entity
            table_name: Source table enum (SERVICE_CONFIG, INFRASTRUCTURE, etc.)
            tenant_code: Tenant code for isolation
            user_code: User code
            case_ref_code: Optional case reference code to narrow the search
                           (e.g. 'user_management' vs 'database_creation' on the same server)
            snapshot_scope: Extra config_snapshot fields the item must match, e.g.
                           {"environment": "stage", "geo_loc_mst_code": "region-..."}.
                           For a caller whose transaction_code does not identify the
                           item on its own. Unused today — the gateway flow keys on
                           the route group, which already means one (service, env,
                           region) — but kept so a future caller with the same
                           problem has it.
            include_failed: When True, also matches FAILED items so a failed
                           deploy can be retried. Default False keeps the
                           draft-REUSE path (add_item_to_queue) from ever
                           reusing a failed row — it must always start a fresh
                           draft. Only the redeploy path passes True.

        Returns:
            Latest matching queue item if found, None otherwise
        """
        allowed_statuses = [
            TransactionQueueStatusEnum.DRAFT,
            TransactionQueueStatusEnum.APPROVED,
        ]
        if include_failed:
            allowed_statuses.append(TransactionQueueStatusEnum.FAILED)
        conditions = [
            TransactionQueueModel.transaction_code == transaction_code,
            TransactionQueueModel.table_name == table_name,
            TransactionQueueModel.tenant_code == tenant_code,
            TransactionQueueModel.user_code == user_code,
            TransactionQueueModel.status.in_(allowed_statuses),
            TransactionQueueModel.is_deleted == False
        ]

        if case_ref_code:
            conditions.append(TransactionQueueModel.case_ref_code == case_ref_code)


        # Narrow the match by config_snapshot fields, when the caller needs it.
        # A missing key WIDENS rather than narrows — deliberate, but it is why a
        # caller relying on this must be sure the field is always present.
        for _key, _value in (snapshot_scope or {}).items():
            if _value is None:
                continue
            conditions.append(
                TransactionQueueModel.config_snapshot[_key].astext == str(_value)
            )

        stmt = (
            select(TransactionQueueModel)
            .options(joinedload(TransactionQueueModel.ticket))
            .where(and_(*conditions))
            .order_by(TransactionQueueModel.created_at.desc())
            .limit(1)
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def search_by_ticket_code(
        self,
        ticket_code: str,
        tenant_code: str,
        user_code: str
    ) -> Optional[TransactionQueueModel]:
        """
        Search for the latest APPROVED queue item by ticket code.

        Args:
            ticket_code: Ticket code to search for
            tenant_code: Tenant code for isolation
            user_code: User code

        Returns:
            Latest approved queue item if found, None otherwise
        """
        stmt = (
            select(TransactionQueueModel)
            .where(and_(
                TransactionQueueModel.ticket_code == ticket_code,
                TransactionQueueModel.tenant_code == tenant_code,
                TransactionQueueModel.user_code == user_code,
                # TransactionQueueModel.status == TransactionQueueStatusEnum.APPROVED,
                TransactionQueueModel.is_deleted == False
            ))
            .order_by(TransactionQueueModel.created_at.desc())
            .limit(1)
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # Statuses that indicate an active deployment pipeline is in flight.
    # Used by get_latest_by_transaction_codes(active_pipeline_only=True) to skip
    # idle resources (deployed, failed, draft, approved, …) from canvas polling.
    ACTIVE_PIPELINE_STATUSES = {
        TransactionQueueStatusEnum.PR_RAISED,
        TransactionQueueStatusEnum.STARTING_PLANNING,
        TransactionQueueStatusEnum.PLANNING,
        TransactionQueueStatusEnum.PLANNED_SUCCESSFULLY,
        TransactionQueueStatusEnum.PLAN_FAILED,
        TransactionQueueStatusEnum.STARTING_APPLYING,
        TransactionQueueStatusEnum.APPLYING,
        TransactionQueueStatusEnum.APPLIED_SUCCESSFULLY,
        TransactionQueueStatusEnum.APPLY_FAILED,
        TransactionQueueStatusEnum.DEPLOYING,
        # legacy Jenkins pipeline stages
        TransactionQueueStatusEnum.CHECKOUT,
        TransactionQueueStatusEnum.BUILDING,
        TransactionQueueStatusEnum.PROVISIONING,
        TransactionQueueStatusEnum.VERIFICATION,
    }

    async def get_latest_by_transaction_codes(
        self,
        transaction_codes: list[str],
        table_name: WorkflowSourceTableEnum,
        tenant_code: str,
        active_pipeline_only: bool = False,
    ) -> dict[str, TransactionQueueModel]:
        """
        Fetch the latest (most recent) queue item for each transaction_code.

        active_pipeline_only=True: only return rows whose status is in
        ACTIVE_PIPELINE_STATUSES — idle resources (deployed, failed, draft, etc.)
        are excluded so callers don't populate or poll them unnecessarily.
        """
        if not transaction_codes:
            return {}

        # Subquery: max created_at per transaction_code
        latest_sq = (
            select(
                TransactionQueueModel.transaction_code,
                func.max(TransactionQueueModel.created_at).label("max_created"),
            )
            .where(and_(
                TransactionQueueModel.transaction_code.in_(transaction_codes),
                TransactionQueueModel.table_name == table_name,
                TransactionQueueModel.tenant_code == tenant_code,
                TransactionQueueModel.is_deleted == False,
            ))
            .group_by(TransactionQueueModel.transaction_code)
            .subquery()
        )

        outer_filters = [
            TransactionQueueModel.table_name == table_name,
            TransactionQueueModel.tenant_code == tenant_code,
            TransactionQueueModel.is_deleted == False,
        ]
        if active_pipeline_only:
            outer_filters.append(
                TransactionQueueModel.status.in_(self.ACTIVE_PIPELINE_STATUSES)
            )

        stmt = (
            select(TransactionQueueModel)
            .join(
                latest_sq,
                and_(
                    TransactionQueueModel.transaction_code == latest_sq.c.transaction_code,
                    TransactionQueueModel.created_at == latest_sq.c.max_created,
                ),
            )
            .where(and_(*outer_filters))
        )

        result = await self.session.execute(stmt)
        rows = result.scalars().all()
        return {row.transaction_code: row for row in rows}
