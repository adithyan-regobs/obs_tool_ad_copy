"""
Approval Repository

Reads and locks transaction_queue rows for the change-approval flow.

Deliberately separate from TransactionQueueRepository: that one serves the
GitOps deploy pipeline and is in production. Approval is a new surface shipping
in the next API release, so it gets its own queries and nothing existing is
edited.

This layer never decides who may see what — OpenFGA does, and the service hands
down the resulting list of resource codes.
"""

from datetime import timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import WorkflowSourceTableEnum
from app.db.models.transaction_queue_model import (
    TransactionQueueModel,
    TransactionQueueStatusEnum,
)
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.user_mst_model import UserMstModel


# Statuses a change is IN FLIGHT: from starting deployment until it lands as
# DEPLOYED (or falls back to APPROVED on failure). The lane lock covers these
# too — the moment deploy starts, the review statuses clear, and without this
# a second user could save + submit a new change against a service whose
# previous change is mid-pipeline.
DEPLOY_IN_FLIGHT_STATUSES = (
    TransactionQueueStatusEnum.STARTING_DEPLOYMENT,
    TransactionQueueStatusEnum.COMMIT,
    TransactionQueueStatusEnum.CHECKOUT,
    TransactionQueueStatusEnum.PR_DRAFT,
    TransactionQueueStatusEnum.PR_RAISED,
    TransactionQueueStatusEnum.PR_APPROVED,
    TransactionQueueStatusEnum.PR_MERGED,
    TransactionQueueStatusEnum.DEPLOYING,
    TransactionQueueStatusEnum.PROVISIONING,
    TransactionQueueStatusEnum.BUILDING,
    TransactionQueueStatusEnum.VERIFICATION,
    TransactionQueueStatusEnum.STARTING_PLANNING,
    TransactionQueueStatusEnum.PLANNING,
    TransactionQueueStatusEnum.PLANNED_SUCCESSFULLY,
    TransactionQueueStatusEnum.STARTING_APPLYING,
    TransactionQueueStatusEnum.APPLYING,
    TransactionQueueStatusEnum.STARTING_APPROVAL,
)

# Under review or cleared to deploy — a decision is outstanding, and it is a
# human's to make. These hold the lane for as long as they exist.
REVIEW_LANE_STATUSES = (
    TransactionQueueStatusEnum.SUBMIT,
    TransactionQueueStatusEnum.APPROVED,
)

# What holds the service's ONE review-and-ship lane: under review, cleared to
# deploy, or actually deploying.
LIVE_LANE_STATUSES = (
    *REVIEW_LANE_STATUSES,
    *DEPLOY_IN_FLIGHT_STATUSES,
)

# How long a row may sit in a DEPLOY_IN_FLIGHT status before the lane lock
# stops believing it is running.
#
# Nothing writes a terminal status on a deploy that dies: the Temporal workflow
# sets STARTING_DEPLOYMENT, its create-PR activity sets PR_RAISED, and the next
# write is DEPLOYED or FAILED at the very end. A workflow that crashes, or a
# create-PR-only change from before auto-deploy existed, leaves the row in an
# in-flight status permanently — and the lock then refuses every save on that
# service for as long as the row exists. 255 rows across 220 services were in
# exactly that state, the oldest since January.
#
# 72h rather than something tighter because the pipeline's own longest
# legitimate wait is 24h — the stuck-plan and stuck-approval timers both
# auto-terminate there — so three days is past anything that could still be
# alive. Measured from status_last_updated_at, so a deploy that is actually
# progressing keeps renewing its claim.
#
# SUBMIT and APPROVED are deliberately NOT bounded. Their messages are true and
# actionable ("deploy it, or send it back through review"); only the in-flight
# branch claims a deployment is running, and that is the claim that goes stale.
DEPLOY_STALE_AFTER = timedelta(hours=72)


class ApprovalRepository:
    """Data access for change requests held in transaction_queue."""

    # A service's change can be to its settings or to its gateway routes. Both
    # carry the same service_configs code in transaction_code, so a query over
    # both table_names is a query about one service — which is what a reviewer
    # is asked to decide on.
    APPROVABLE_TABLES: Tuple[WorkflowSourceTableEnum, ...] = (
        WorkflowSourceTableEnum.SERVICE_CONFIG,
        WorkflowSourceTableEnum.KONG_ROUTE,
    )

    # What the approvals inbox shows. Anything past APPROVED belongs to the
    # GitOps flow and is no longer an approval decision.
    INBOX_STATUSES: Tuple[TransactionQueueStatusEnum, ...] = (
        TransactionQueueStatusEnum.DRAFT,
        TransactionQueueStatusEnum.SUBMIT,
        TransactionQueueStatusEnum.APPROVED,
        TransactionQueueStatusEnum.REJECTED,
    )

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session
        self.model = TransactionQueueModel

    async def get_by_code(
        self, queue_code: str, tenant_code: Optional[str] = None
    ) -> Optional[TransactionQueueModel]:
        """One request by its code, optionally constrained to a tenant."""
        filters = [self.model.code == queue_code]
        if tenant_code:
            filters.append(self.model.tenant_code == tenant_code)
        stmt = select(self.model).where(*filters)
        return (await self.session.execute(stmt)).scalars().first()

    async def get_locked_by_code(
        self, queue_code: str
    ) -> Optional[TransactionQueueModel]:
        """Same, but SELECT ... FOR UPDATE.

        Every decision takes this lock, so two reviewers acting at the same
        instant cannot both win.
        """
        stmt = (
            select(self.model)
            .where(
                self.model.code == queue_code,
                # A soft-deleted row is gone from every read, so it must be
                # gone from every VERB too — without this, anyone holding a
                # binned row's code could still discard/submit/decide it (a
                # second discard was appending duplicate history events).
                self.model.is_deleted.isnot(True),
            )
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def lock_change_set(
        self,
        transaction_code: str,
        tenant_code: str,
        statuses: Tuple[TransactionQueueStatusEnum, ...],
        user_code: Optional[str] = None,
        exclude_case_ref: Optional[str] = None,
    ) -> List[TransactionQueueModel]:
        """Every row of ONE AUTHOR's change to this service, locked for a verb.

        `exclude_case_ref` leaves one lane's row unlocked — discard needs the
        variables row free while devlift-secret soft-deletes it on our behalf
        (see ApprovalService.discard); everything else is taken as usual.

        A change spans rows — settings, gateway routes, variables — and they are
        one decision, so they move together or not at all. Locking them in ONE
        statement is what makes that true under concurrency.

        Scoped to the author (the POC's model): drafts are per user, and a verb
        must never move someone else's work. Without this filter, one person's
        Submit swept every colleague's half-finished draft on the same service
        into review — which is exactly what happened on UAT, four authors deep.
        The lane stays shared at the SERVICE level (one live request at a time,
        enforced at submit); only the rows are per author.

        Ordered by id so every caller takes the rows in the same order, which is
        the cheap way to keep two of them from deadlocking on each other.
        """
        stmt = (
            select(self.model)
            .where(
                self.model.transaction_code == transaction_code,
                self.model.tenant_code == tenant_code,
                self.model.table_name.in_(self.APPROVABLE_TABLES),
                self.model.status.in_(statuses),
                self.model.is_deleted.isnot(True),
            )
            .order_by(self.model.id)
            .with_for_update()
        )
        if user_code is not None:
            stmt = stmt.where(self.model.user_code == user_code)
        if exclude_case_ref is not None:
            stmt = stmt.where(self.model.case_ref_code != exclude_case_ref)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_change_set(
        self,
        transaction_code: str,
        tenant_code: str,
        statuses: Tuple[TransactionQueueStatusEnum, ...],
        user_code: Optional[str] = None,
    ) -> List[TransactionQueueModel]:
        """lock_change_set without the lock — a plain read of one author's rows.

        For the step BEFORE a verb takes its locks when that step calls
        another service which writes to the same rows (discard's call into
        devlift-secret, which soft-deletes the variables row). Holding FOR
        UPDATE across that call would block the other service's UPDATE on our
        own lock while we wait for its reply.
        """
        stmt = (
            select(self.model)
            .where(
                self.model.transaction_code == transaction_code,
                self.model.tenant_code == tenant_code,
                self.model.table_name.in_(self.APPROVABLE_TABLES),
                self.model.status.in_(statuses),
                self.model.is_deleted.isnot(True),
            )
            .order_by(self.model.id)
        )
        if user_code is not None:
            stmt = stmt.where(self.model.user_code == user_code)
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_live_by_other_author(
        self,
        transaction_code: str,
        tenant_code: str,
        user_code: str,
    ) -> Optional[TransactionQueueModel]:
        """Someone ELSE's request on this service that is under review or
        awaiting deploy — the POC's one-live-request rule, asked at submit.

        Drafts are per author and unlimited; the REVIEW LANE is shared. A second
        submission while another is live would give a reviewer two competing
        versions of the same service, and deploy an ordering problem neither
        author chose. So the second submitter waits, told whose request holds
        the lane.
        """
        stmt = (
            select(self.model)
            .where(
                self.model.transaction_code == transaction_code,
                self.model.tenant_code == tenant_code,
                self.model.table_name.in_(self.APPROVABLE_TABLES),
                self.model.status.in_(
                    (TransactionQueueStatusEnum.SUBMIT, TransactionQueueStatusEnum.APPROVED)
                ),
                self.model.user_code != user_code,
                self.model.is_deleted.isnot(True),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def find_live_for_resource(
        self,
        transaction_code: str,
        tenant_code: str,
        table_name: WorkflowSourceTableEnum = WorkflowSourceTableEnum.SERVICE_CONFIG,
    ) -> Optional[TransactionQueueModel]:
        """A request on this resource that is under review or awaiting deploy.

        SUBMIT and APPROVED only — a DRAFT is not live, it is someone still
        typing, and the save path is allowed to keep updating it.

        This backs the lane lock: one live request per (resource, table). The
        live add-to-queue path reuses a DRAFT *or* APPROVED row for the same
        service, so without this a save after approval would quietly rewrite an
        approved snapshot and invalidate its seal.

        Gateway (add_route) rows are excluded: they sit on SERVICE_CONFIG now
        but belong to the gateway lane, whose save path handles its own live
        rows (upsert_pending_for_scope demotes them back to DRAFT). When they
        carried KONG_ROUTE they never matched this query, and a live route
        change must not start refusing settings saves.

        A deploy that stopped moving more than DEPLOY_STALE_AFTER ago is not
        live and does not hold the lane — see that constant for why. Review
        states (SUBMIT, APPROVED) are never aged out.
        """
        # coalesce because status_last_updated_at is only written when a status
        # actually changes; a row that has never moved carries NULL there and
        # its age is the age of the row.
        last_moved = func.coalesce(
            self.model.status_last_updated_at, self.model.created_at
        )
        stmt = (
            select(self.model)
            .where(
                self.model.transaction_code == transaction_code,
                self.model.tenant_code == tenant_code,
                self.model.table_name == table_name,
                or_(
                    self.model.case_ref_code.is_(None),
                    self.model.case_ref_code != "add_route",
                ),
                self.model.is_deleted.isnot(True),
                or_(
                    self.model.status.in_(REVIEW_LANE_STATUSES),
                    and_(
                        self.model.status.in_(DEPLOY_IN_FLIGHT_STATUSES),
                        last_moved > func.now() - DEPLOY_STALE_AFTER,
                    ),
                ),
            )
            .order_by(self.model.created_at.desc())
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_candidates(
        self,
        tenant_code: str,
        status: Optional[str] = None,
        resource_code: Optional[str] = None,
        table_names: Optional[Tuple[WorkflowSourceTableEnum, ...]] = None,
    ) -> List[TransactionQueueModel]:
        """Requests that COULD appear in an inbox — candidates, not results.

        This layer never decides who may see what. The service takes the
        distinct resources off these rows and asks OpenFGA about those.

        Narrowed by tenant and status before FGA is involved: `status=submit`
        is the review queue, a handful of rows even in a large tenant. Asking
        FGA to enumerate every service a user could approve would be a far
        bigger question than asking about the few that actually have something
        waiting.
        """
        filters = [
            self.model.tenant_code == tenant_code,
            # Both halves of a service's change. Filtering to SERVICE_CONFIG hid
            # the gateway row from the reviewer entirely: it was submitted, it
            # was waiting on them, and it appeared nowhere.
            self.model.table_name.in_(table_names or self.APPROVABLE_TABLES),
            # isnot(True), not == False: BaseModel gives is_deleted a
            # Python-side default only, so rows written outside the ORM hold
            # NULL and `== False` would silently drop them.
            self.model.is_deleted.isnot(True),
            self.model.status.in_(self.INBOX_STATUSES),
        ]
        if status:
            filters.append(self.model.status == status)
        if resource_code:
            filters.append(self.model.transaction_code == resource_code)

        stmt = select(self.model).where(*filters).order_by(self.model.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_all_for_resource(
        self,
        transaction_code: str,
        tenant_code: str,
        table_names: Optional[Tuple[WorkflowSourceTableEnum, ...]] = None,
        limit: int = 15,
    ) -> List[TransactionQueueModel]:
        """EVERY request ever raised against one resource, whatever its status.

        Deliberately not INBOX_STATUSES. The inbox is a work queue and stops at
        the states someone can still act on; a history is the opposite question
        — what has happened to this service — and a deployed or cancelled
        request is exactly the part of that story the inbox drops.

        Oldest last: the reader wants the most recent thing first.
        """
        stmt = (
            select(self.model)
            .where(
                self.model.tenant_code == tenant_code,
                self.model.table_name.in_(table_names or self.APPROVABLE_TABLES),
                self.model.transaction_code == transaction_code,
                # isnot(True), not == False: rows written outside the ORM hold
                # NULL, and `== False` would silently drop them.
                self.model.is_deleted.isnot(True),
            )
            .order_by(self.model.created_at.desc())
            # Bounded: a long-lived service accumulates requests without bound,
            # and each one carries its full history and snapshot. Newest first,
            # so the cut falls on the oldest — the part nobody scrolls to.
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def service_name_for_config(self, transaction_code: str) -> Optional[str]:
        """The service's name, for a service_config code.

        A refusal has to name the thing it is about, and `sc-4f94eb02-…-prod-…`
        names it only to the database. One join, on a path that runs once per
        Save click.
        """
        stmt = (
            select(ServicesMstModel.name)
            .join(
                ServiceConfigModel,
                ServiceConfigModel.services_mst_code == ServicesMstModel.code,
            )
            .where(ServiceConfigModel.code == transaction_code)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def names_for_user_codes(self, codes: Iterable[str]) -> Dict[str, str]:
        """user_code -> a name a person recognises.

        Every actor in a request is recorded by code, because that is what
        survives a rename. The history is read by humans, though, and a column
        of UUIDs tells them nothing — so the codes are resolved in one query
        here rather than per row at render time.
        """
        wanted = {c for c in codes if c}
        if not wanted:
            return {}

        stmt = select(
            UserMstModel.code, UserMstModel.first_name,
            UserMstModel.last_name, UserMstModel.email_id,
        ).where(UserMstModel.code.in_(wanted))

        out: Dict[str, str] = {}
        for code, first, last, email in (await self.session.execute(stmt)).all():
            full = " ".join(p for p in (first, last) if p).strip()
            out[code] = full or email or code
        return out
