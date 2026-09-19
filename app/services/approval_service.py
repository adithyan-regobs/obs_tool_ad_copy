"""
Approval Service

Scope for this release: a reviewer sees the change requests they may act on,
and decides.

    submit ──approve─────────▶ approved      ready for the existing deploy flow
      │
      ├────request-changes───▶ draft         recoverable — submitter edits it
      │
      └────reject────────────▶ rejected      terminal, no path back

Getting a request INTO `submit` (draft -> submit) is deliberately not here yet;
seed rows directly for now. Withdraw, discard, revoke and the deploy seal check
come in a later pass.

WHO SEES WHAT IS DECIDED BY OPENFGA, NOT BY THIS DATABASE. The inbox is built
by asking ListObjects which service_configs the caller holds `can_approve` on,
then fetching only those rows. Nothing here infers permission from a column.
"""

import copy
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import fga
from app.core.authz.security import require
from app.core.enum import WorkflowSourceTableEnum
from app.db.models.transaction_queue_model import (
    TransactionQueueModel,
    TransactionQueueStatusEnum as S,
)
from app.repository.approval_repository import ApprovalRepository
from app.schemas.service_config_schemas import is_eks_infra, validate_service_path
from app.repository.approval_rule_repository import ApprovalRuleRepository

logger = logging.getLogger(__name__)


# The queue row is polymorphic; this is the only place it becomes an FGA object.
#
# KONG_ROUTE maps to service_config, not to a type of its own, and that is not a
# shortcut: a gateway row's transaction_code IS a service_configs code (see
# KongRouteConfigService._scope_config_code). Routes belong to the service they
# route to, so whoever may change that service's settings may change its routes,
# and one grant covers both — a separate FGA type would mean a second set of
# tuples describing the same relationship.
# What a row CHANGES, asked of case_ref_code rather than of table_name.
#
# table_name is a polymorphic pointer — it names the table transaction_code
# refers to — and a gateway row's transaction_code has always been a
# service_configs code, so gateway rows are being migrated to SERVICE_CONFIG
# to match. That migration makes table_name useless for telling a settings
# change from a gateway one: both become SERVICE_CONFIG. case_ref_code is what
# actually records the KIND of change, so every branch that asks "what sort of
# change is this?" asks it here.
GATEWAY_CASE_REF = "add_route"
VARIABLES_CASE_REF = "update_variables"
SETTINGS_CASE_REF = "update_service"

# The write relation each KIND of change needs, named per kind rather than
# reached by falling off the end of an if. Settings is spelled out like every
# other kind because it is one kind among several, not the absence of the
# others — an unrecognised case_ref_code must not silently borrow whatever the
# last branch happened to return.
#
# update_variables is deliberately absent: its relation depends on the KINDS
# inside the row (secrets, configs, or both), which only submit_permissions
# can read, so it is resolved there rather than by a lookup here.
WRITE_PERMISSION: Dict[str, Set[str]] = {
    SETTINGS_CASE_REF: {"can_write_settings"},
    GATEWAY_CASE_REF: {"can_write_gateway"},
}


FGA_TYPE: Dict[WorkflowSourceTableEnum, str] = {
    WorkflowSourceTableEnum.SERVICE_CONFIG: "service",
    WorkflowSourceTableEnum.KONG_ROUTE: "service",
    WorkflowSourceTableEnum.INFRASTRUCTURE: "infra_mst",
}

# Stored as columns on service_configs rather than inside its config JSONB,
# while the snapshot keeps them at the root. Both sides need lifting into the
# same shape before they can be compared — otherwise live reads as empty and
# every request reports a change it never made.
COLUMN_FIELDS = ("language_ref_code", "alb_selection")

# No rule row => no gate, which is today's behaviour everywhere.
DEFAULT_RULE = {
    "require_approval": False,
    "allow_self_approval": True,
    "allow_approver_edit": False,
}


def denied(relation: str, what: str) -> str:
    """The refusal, in one sentence, from one place.

    The POC's wording, with its parenthetical kept: the relation is the whole
    answer to "why not?", and it is what whoever grants access has to grant.

    Two departures from the POC, both because ours are UUIDs where its were
    readable. The subject is dropped — "You" already said who, and repeating it
    as user:07eacde1-… tells the reader nothing they did not know. The object is
    the service's NAME, since sc-4f94eb02-…-prod-0381b256 names it only to the
    database, and a person cannot act on an id they have never seen.
    """
    return (
        f"You don't have permission to do this operation "
        f"(you need {relation} on {what})"
    )


def target_name(row: TransactionQueueModel) -> str:
    """What to call the row's target in a message.

    The snapshot already carries the service's name — it was written there by
    the form that made the request — so the common case costs no query.
    """
    snapshot = row.config_snapshot or {}
    return (
        snapshot.get("service_name")
        or row.display_name
        or row.transaction_code
    )


def _now() -> str:
    """ISO string — for the JSONB history log."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    """datetime — for decided_at, which is a real TIMESTAMP column.
    Passing the ISO string here is a DataError, not a silent cast."""
    return datetime.now(timezone.utc)


# How long the deploy gate's mark is believed.
#
# The mark exists to cover the seconds between the gate passing and a Temporal
# activity moving the row off APPROVED. It cannot simply be trusted forever,
# because the gate runs BEFORE the deploy: the caller can pass the gate and
# then never deploy at all — the panel returns early on a missing resource code
# or unsaved edits, the multiple-deploy call can fail, the tab can be closed.
# The row is then still APPROVED, marked, with nothing running, and an
# unbounded check would refuse every revoke on it for good.
#
# A real deploy leaves APPROVED within seconds, after which the status check
# refuses a revoke on its own and this mark stops mattering. So a mark still
# sitting on an APPROVED row long afterwards means the deploy never took, and
# the change is revocable again. Generous enough to cover a badly backed-up
# worker, short enough that nobody is stuck waiting on it.
_DEPLOY_MARK_TTL = timedelta(minutes=15)


def deploy_in_flight(row) -> bool:
    """Is a deploy running for this row, as far as anything here can tell?

    One definition, used by revoke (which refuses on it) and by the caller's
    YouFlags (which hides the button on it). Two copies would drift, and the
    drift shows up as a button that is offered and then refused, or hidden on a
    change nobody is deploying.
    """
    started = getattr(row, "deploy_started_at", None)
    if started is None:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return _now_dt() - started < _DEPLOY_MARK_TTL


# Why a revoke was refused, in the words the person clicking would use.
#
# The old message printed the raw status — "request is starting_deployment —
# only an approved one can have its approval taken back". It named an internal
# enum the UI never shows, and it described the RULE rather than the situation,
# so the reader was left to work out whether they had done something wrong,
# whether they could retry, and whether the change was still going out. Worse,
# it reads as an invitation: "only an approved one can" sounds like the change
# just needs to be approved again.
#
# Keyed on the status the row is actually in, and saying only what happened.
# The earlier version also told the reader what to do instead ("stop the
# deployment", "close the pull request"); it was cut as noise — the person who
# just tried to revoke knows what they wanted, and a banner is not the place to
# hand out instructions for a different screen.
# Split deliberately. At STARTING_DEPLOYMENT the deploy has been triggered but
# nothing has been applied yet, and telling that person the change is "being
# deployed" overstates what has happened — they will read it as work already
# done and may not realise there is still something to stop. The rest are
# genuinely mid-flight.
_TRIGGERED = (S.STARTING_DEPLOYMENT,)
_RUNNING = (
    S.DEPLOYING, S.PROVISIONING, S.BUILDING,
    S.CHECKOUT, S.VERIFICATION, S.COMMIT,
)
_PR_OPEN = (S.PR_RAISED, S.PR_DRAFT, S.PR_APPROVED, S.PR_MERGED)

# The granular Atlantis plan/apply/approve states. Every one of these is a
# deploy that is under way, but calling them "being deployed right now" would
# repeat the overstatement this function exists to avoid: a change sitting at
# `planning` has had nothing applied to it. They get their own sentence, and
# they used to fall through to the vague catch-all instead.
_MID_DEPLOY = (
    S.STARTING_PLANNING, S.PLANNING, S.PLANNED_SUCCESSFULLY,
    S.STARTING_APPLYING, S.APPLYING, S.APPLIED_SUCCESSFULLY,
    S.STARTING_APPROVAL, S.APPROVED_SUCCESSFULLY,
)
# Their failures belong with the ordinary deploy failure, not with the states
# still in motion.
_MID_DEPLOY_FAILED = (S.PLAN_FAILED, S.APPLY_FAILED, S.APPROVAL_FAILED)


def _cannot_revoke(status) -> str:
    """One sentence saying what happened, and what to do instead."""
    if status in _TRIGGERED:
        return (
            "A deploy has already been started for this change, so its approval "
            "can no longer be taken back."
        )
    if status in _RUNNING:
        return (
            "This change is being deployed right now, so its approval can no "
            "longer be taken back."
        )
    if status in _PR_OPEN:
        return (
            "This change has already been raised as a pull request, so its "
            "approval can no longer be taken back."
        )
    if status == S.DEPLOYED:
        return (
            "This change has already been deployed, so its approval can no "
            "longer be taken back."
        )
    if status == S.SUBMIT:
        return (
            "This change is still waiting for a decision, so there is no "
            "approval to take back yet."
        )
    if status == S.DRAFT:
        return (
            "This change is back with its author as a draft, so there is no "
            "approval to take back."
        )
    if status in (S.REJECTED, S.PR_REJECTED):
        return "This change was rejected, so there is no approval to take back."
    if status in _MID_DEPLOY:
        return (
            "This change is part way through its deployment, so its approval "
            "can no longer be taken back."
        )
    if status == S.FAILED or status in _MID_DEPLOY_FAILED:
        return (
            "This change's deployment failed, so there is no approval to take "
            "back."
        )
    return (
        "This change has moved on since it was approved, so its approval can "
        "no longer be taken back."
    )


def _is_empty_change(changes) -> bool:
    """True when a row's frozen diff would put nothing in front of a reviewer.

    Settings and variables diffs are plain dicts — empty means empty. A gateway
    diff is `{"groups": [...]}` (compute_changes), a truthy dict even with no
    groups in it, so it is judged by its list: an empty list is "no route
    edits", which is nothing to review.
    """
    if not changes:
        return True
    if isinstance(changes, dict) and set(changes) == {"groups"}:
        return not changes["groups"]
    return False


def _event(
    by: str,
    event: str,
    comment: Optional[str] = None,
    changes: Optional[dict] = None,
) -> dict:
    """One line of the audit log: who did what, and when.

    `changes` is carried on the event rather than read off the row at display
    time. The row holds ONE diff, recomputed on every submit, so a request
    submitted, withdrawn, edited and submitted again would show its second diff
    against both submissions — the log would be quietly rewriting its own past.
    A copy on the event is what makes each line true when it was written.
    """
    e = {"at": _now(), "by": by, "event": event}
    if comment:
        e["comment"] = comment
    if changes:
        e["changes"] = changes
    return e


def seal(config_snapshot: Optional[dict]) -> str:
    """Fingerprint of the content being approved, recorded at approval.

    Canonical on purpose — key order or whitespace drift would otherwise break
    the seal on a record nobody touched. The deploy-time check that reads it
    lands in a later pass.
    """
    canonical = json.dumps(config_snapshot or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _gateway_entry(group: dict) -> dict:
    """One route group, stored the way every reader of this row expects it.

    The browser sends a group as it thinks of one — identity, the desired end
    state, and a `delta` describing the move:

        {code, route_group_key, http_method, plugins, regex_priority,
         paths: [...desired...],
         delta: {paths: [...actions...], plugins_before, plugins_after,
                 regex_priority_before, regex_priority_after}}

    Everything downstream reads a FLAT entry instead: the terragrunt generator
    takes route_group_key, http_method, regex_priority_after and paths[].action
    off the entry itself (see _apply_from_delta), and so do ensure_deploy_items
    and the Gateway tab's read. Storing the browser's shape verbatim left all of
    them looking one level above the data — the tab showed no pending routes at
    all, and a deploy would have found no actions and raised an empty PR.

    The desired state is kept ALONGSIDE the actions, under its own keys. The
    Kong tables used to hold it, because a save wrote them immediately; they are
    not written until deploy now, so this row is the only place it exists.
    """
    delta = group.get("delta") or {}
    return {
        # None when the change CREATES the group — there is nothing to point at.
        "group_code": group.get("code"),
        "route_group_key": group.get("route_group_key"),
        "http_method": (group.get("http_method") or "").upper(),
        # The change itself: path actions, the plugin move, the priority move.
        **delta,
        # The end state, for the tab's merged view and the table write at deploy.
        "desired_paths": group.get("paths") or [],
        "plugins": group.get("plugins") or [],
        "regex_priority": group.get("regex_priority") or 0,
        # The concurrency token this change was built against.
        "updated_at": group.get("updated_at"),
    }


def fga_ref(row: TransactionQueueModel) -> str:
    """OpenFGA object id for a queue row's target."""
    obj_type = FGA_TYPE.get(row.table_name)
    if obj_type is None:
        raise HTTPException(400, f"approval is not wired for {row.table_name} yet")
    return f"{obj_type}:{row.transaction_code}"


def submit_permissions(row: TransactionQueueModel) -> Set[str]:
    """What the SUBMITTER must hold to send this row for review.

    Settings and gateway rows are can_update, as always. A variables row
    (case_ref_code='update_variables', written by devlift-secret-config-
    manager's save path) is not: env writes are gated on can_write_secret /
    can_write_config — split so a tenant can grant config access without
    handing over secrets. Both services check the SAME OpenFGA store, so
    the tuples the save routes consulted answer here too. Accepting
    can_update instead would let someone with settings access submit a
    secret change the save routes would have refused them.

    Which of the two depends on what the request carries, read from the
    `kind` of each entry in `changes`. That kind is server-derived, not
    client data: the secrets service's kind-split save stamps type from the
    ROUTE (`/secrets/save` → secret, `/configs/save` → variable), explicitly
    overwriting anything the payload claimed — and each route was itself
    guarded by the matching permission when it stamped it. So "kind":
    "secret" is a server-recorded fact meaning can_write_secret was already
    verified for that entry at save; submit re-checks the same permission on
    the same object. A config-only change never demands secret access.

    Fails CLOSED: a variables row whose `changes` names no recognisable kind
    requires BOTH. The alternative is requiring neither — a row that moves
    on no permission at all is the one outcome this must never allow.

    Reviewer verbs are untouched: can_approve stays blanket per decision,
    like can_deploy — deciding a change is not writing its values.
    """
    if row.case_ref_code != VARIABLES_CASE_REF:
        # Per row KIND, from the table above — the model has no blanket
        # can_update any more, and the surfaces are separately grantable. It
        # matches what the SAVE routes already card on, so whoever could stage
        # the change can send it for review; nothing can be submitted that its
        # author could not have written.
        named = WRITE_PERMISSION.get(row.case_ref_code)
        if named is not None:
            return set(named)

        # Kinds with no entry of their own — the infrastructure case refs
        # (create_bucket, create_queue, database_creation, table_management).
        # They ask for can_write_settings because that is what they have always
        # asked for, and it is not a guess that can quietly widen: their FGA
        # object is infra_mst, a type the published model does not define, so
        # every check against one errors and fails closed until infra enters
        # the model. Give a kind its own entry above before relying on this.
        return {"can_write_settings"}

    kinds = {
        (item or {}).get("kind")
        for item in ((row.changes or {}).get("variables") or [])
    }
    needed: Set[str] = set()
    if "secret" in kinds:
        needed.add("can_write_secret")
    if "variable" in kinds:
        needed.add("can_write_config")
    return needed or {"can_write_secret", "can_write_config"}


class ApprovalService:
    """One method per action in the approvals UI."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ApprovalRepository(db)
        self.rule_repo = ApprovalRuleRepository(db)
        # Live config rows already fetched in THIS request. The history endpoint
        # diffs several drafts against the same service, and without this each
        # one re-runs the identical SELECT. Instance-scoped, so it lives exactly
        # as long as the request and can never serve a stale row to the next.
        self._live_configs: Dict[tuple, object] = {}

    async def actor_names(
        self, rows: Sequence[TransactionQueueModel]
    ) -> Dict[str, str]:
        """user_code -> display name for everyone who touched these requests.

        Resolved for the whole page in one query, so rendering a history of ten
        events does not become ten lookups. Codes with no matching user simply
        do not appear; callers fall back to the code itself.
        """
        codes: set = set()
        for row in rows:
            codes.add(row.user_code)
            codes.add(row.decided_by)
            for event in row.history or []:
                codes.add(event.get("by"))
        return await self.repo.names_for_user_codes(codes)

    # ── level 2: the requests ───────────────────────────────────────────────
    async def list_inbox(
        self,
        user: str,
        user_code: str,
        tenant_code: str,
        status: Optional[str] = None,
        resource_code: Optional[str] = None,
    ) -> List[tuple]:
        """Change requests the caller may act on — theirs, or theirs to review.

        Order matters, and it is NARROW IN SQL, THEN ASK FGA:

          1. the queue gives the candidates — bounded by tenant and status, so
             `status=submit` is the review queue and that is a small set
          2. take the DISTINCT services off those rows; several requests often
             share one service, so this is smaller still
          3. ONE list_objects per (permission, object type) says which services
             the caller may approve, and which they may deploy
          4. keep the rows the caller owns, may approve, or may deploy

        Step 3 used to be a batch_check per permission — one question per
        candidate service. That is capped at 100 so no caller can fan out
        unboundedly inside OpenFGA, and a tenant with 113 services holding open
        rows went straight through it: the batch 400'd, authz failed closed,
        every verdict came back False, and the approvals page rendered
        "Nothing waiting" with a 200 and no error, while the same user could
        approve the same change from the service panel — which asks about one
        service and stayed under the cap.

        Asking FGA to enumerate instead was avoided on the grounds that an org
        owner can approve hundreds of services with nothing waiting on them.
        True, and it is still one request rather than N: the size lands on
        OpenFGA, which is built for the traversal, instead of on a batch whose
        size this caller cannot control.

        A row the caller SUBMITTED is always theirs to see. Without that, a
        maker who holds no approver role sees an empty page and can never
        submit the draft they just saved.
        """
        rows = await self.repo.list_candidates(
            tenant_code=tenant_code, status=status, resource_code=resource_code
        )
        if not rows:
            return []

        # Distinct services, not one entry per row.
        refs: Dict[str, str] = {}
        for row in rows:
            refs.setdefault(row.transaction_code, fga_ref(row))

        # ASKED THE OTHER WAY ROUND — see fga.list_objects.
        #
        # This used to be two batch_checks, one question per service. That is
        # capped at 100 so no caller can fan out unboundedly inside OpenFGA, and
        # a tenant with 113 services carrying open rows blew straight through
        # it: the batch 400'd, authz failed closed, every verdict came back
        # False, and this page rendered "Nothing waiting" — 200, no error —
        # while the same user could approve the very same change from the
        # service panel, which asks about ONE service and so stayed under the
        # cap. A ceiling that only bites at scale, and silently.
        #
        # ListObjects asks once per (relation, type) whatever the size, and the
        # candidates are intersected with the answer. The cost moves to
        # OpenFGA, which is built for that traversal.
        #
        # PER TYPE, deliberately: a queue row's object is service or infra_mst
        # (FGA_TYPE), and infra_mst is not in the published model — asking for
        # it errors and returns empty, which must not take the service answers
        # down with it.
        codes = list(refs)
        obj_types = {ref.split(":", 1)[0] for ref in refs.values()}
        approved_objs: Set[str] = set()
        deployable_objs: Set[str] = set()
        for obj_type in obj_types:
            approved_objs.update(await fga.list_objects(user, "can_approve", obj_type))
            deployable_objs.update(await fga.list_objects(user, "can_deploy", obj_type))
        can_approve = {c for c in codes if refs[c] in approved_objs}
        can_deploy = {c for c in codes if refs[c] in deployable_objs}

        out: List[tuple] = []
        # can_approve, made TRUTHFUL PER ROW: on the caller's OWN row the
        # resource group's allow_self_approval rule decides — approve() already
        # refuses a forbidden self-approval, so a raw FGA yes here painted an
        # Approve button that could only 403. Rule cached per service: one
        # lookup per transaction_code, not per row.
        self_rule_cache: Dict[str, bool] = {}
        for row in rows:
            mine = row.user_code == user_code
            approvable = row.transaction_code in can_approve
            deployable = row.transaction_code in can_deploy
            if not (mine or approvable or deployable):
                continue
            # A draft is private to whoever wrote it — a reviewer has nothing to
            # review until it is submitted.
            if row.status == S.DRAFT and not mine:
                continue
            if approvable and mine:
                if row.transaction_code not in self_rule_cache:
                    rule = await self.rule_for(row)
                    self_rule_cache[row.transaction_code] = bool(
                        rule["allow_self_approval"]
                    )
                approvable = self_rule_cache[row.transaction_code]
            out.append((row, {
                "mine": mine,
                "can_approve": approvable,
                "can_deploy": deployable,
                "deploy_in_flight": deploy_in_flight(row),
            }))
        return out

    async def list_resource_history(
        self, user: str, user_code: str, tenant_code: str, resource_code: str,
        limit: int = 15,
    ) -> List[tuple]:
        """Every request ever raised against one service — the whole story.

        The inbox answers "what needs me?"; this answers "what has happened
        here?", which is a different set: a deployed change, a rejected one and
        a withdrawn one are all part of the record and none of them are in an
        inbox. So no status filter, and no hiding of rows the caller cannot act
        on — being able to see the service is what earns the log.

        Only a NEVER-SUBMITTED draft is held back, and only from people who did
        not write it: an unshown change is nobody else's business yet.

        Once a change has been submitted its record belongs to the service, and
        a later status of DRAFT does not take that back. Rejection (and
        withdrawal) return the row to DRAFT, so hiding every foreign draft
        erased whole reviewed requests from everyone but their author — the
        approver could not even find the approve/revoke/reject they had just
        performed. What stays private is work nobody has ever been asked about.
        """
        rows = await self.repo.list_all_for_resource(
            resource_code, tenant_code, limit=limit
        )
        if not rows:
            return []

        # One object, so one Check per permission rather than one per row.
        ref = fga_ref(rows[0])
        approve, deploy = await fga.batch_check(
            [(user, "can_approve", ref), (user, "can_deploy", ref)]
        )

        def _ever_submitted(row: TransactionQueueModel) -> bool:
            """Has this row ever been shown to a reviewer? The history is
            append-only, so a submit event never leaves it."""
            return any(
                isinstance(e, dict) and e.get("event") == "submitted"
                for e in (row.history or [])
            )

        out: List[tuple] = []
        for row in rows:
            mine = row.user_code == user_code
            if row.status == S.DRAFT and not mine and not _ever_submitted(row):
                continue
            out.append((row, {
                "mine": mine, "can_approve": approve, "can_deploy": deploy,
            }))
        return out

    async def draft_previews(
        self, rows: Sequence[TransactionQueueModel]
    ) -> Dict[str, dict]:
        """queue code -> a LIVE diff, for the rows that are still drafts.

        `changes` on the row is frozen at submit and describes a submission. A
        withdrawn request keeps that value while going back to being a draft,
        so serving it would show the author a diff labelled as submitted for a
        change nobody has been asked about — and, once they edit, one that no
        longer matches what is in the boxes.

        Computed on read rather than stored: a draft is still being written, so
        anything persisted here would be out of date by the next keystroke. The
        lane lock means at most one draft per service, so this is one extra
        query on a page that already made several.

        Returned rather than assigned onto the row, because assigning would
        mark it dirty and a later commit in the same session would persist a
        preview into the column that is supposed to be frozen.
        """
        out: Dict[str, dict] = {}
        for row in rows:
            if row.status == S.DRAFT:
                out[row.code] = await self.compute_changes(row)
        return out

    async def flags_for(
        self, user: str, user_code: str, row: TransactionQueueModel
    ) -> dict:
        """What the caller may do with ONE request — one batched Check.

        ListObjects is the right call for a list; for one known object a Check
        is cheaper and reads more plainly. The UI draws its buttons from this,
        so it holds no permission logic of its own.
        """
        ref = fga_ref(row)
        # The write right depends on WHAT this row changes — settings, gateway
        # routes and variables are separately grantable, and a single
        # can_write_settings answered for all three would have told a gateway
        # reviewer they may edit a row their gateway permission actually
        # governs (or refused one they could edit). submit_permissions already
        # maps a row to the relation(s) its kind requires, so this asks the
        # same question submit will ask rather than a second, looser one.
        needed = sorted(submit_permissions(row))
        results = await fga.batch_check(
            [(user, rel, ref) for rel in needed]
            + [(user, "can_approve", ref), (user, "can_deploy", ref)]
        )
        approve, deploy = results[len(needed):]
        mine = row.user_code == user_code
        # Truthful for THIS row: an author-approver's can_approve obeys the
        # resource group's allow_self_approval rule — approve() enforces it
        # anyway, and a raw FGA yes painted an Approve button that could only
        # 403 where the rule forbids self-approval.
        if approve and mine:
            approve = bool((await self.rule_for(row))["allow_self_approval"])
        flags = {
            "mine": mine,
            "can_approve": approve,
            "can_deploy": deploy,
            # State, not permission — see YouFlags.deploy_in_flight. The UI
            # hides Revoke on it; revoke() refuses on the same column, so the
            # button vanishing is the courtesy and the 409 is the guarantee.
            "deploy_in_flight": deploy_in_flight(row),
        }
        # Report under the relation's OWN name. Every key here is a relation
        # that exists in the model, so a flag can be traced to the tuple that
        # granted it; an invented umbrella name could not be. Only the
        # relation(s) this row's kind needs are asked, so the rest stay at
        # their default false — the caller knows the kind and reads the one
        # that applies. A variables row carrying secrets AND configs reports
        # both, and needs both, exactly as submit requires both.
        for rel, ok in zip(needed, results[: len(needed)]):
            flags[rel] = ok
        return flags

    async def flags_for_resource(self, user: str, resource_code: str) -> dict:
        """The same three permissions, for a service with no request yet.

        flags_for needs a row; this answers before one exists — which is when
        the UI has to know, because it mints a ticket on the way to the first
        save and a refusal that arrives afterwards leaves that ticket orphaned.

        `mine` is False by definition: there is nothing yet to own.
        """
        ref = f"service:{resource_code}"
        # Every write surface, not just settings: the tabs are separately
        # grantable, so one flag cannot answer for Gateway and Env too.
        # can_view_settings rides in the same batch so a caller can tell
        # "view only" from "no access" on Configuration (the CLI assistant's
        # permissions table). One batched round trip either way.
        (settings, gateway, secret, config, view_settings,
         approve, deploy) = await fga.batch_check(
            [(user, "can_write_settings", ref), (user, "can_write_gateway", ref),
             (user, "can_write_secret", ref), (user, "can_write_config", ref),
             (user, "can_view_settings", ref),
             (user, "can_approve", ref), (user, "can_deploy", ref)]
        )
        update = settings
        return {
            "mine": False,
            "can_write_settings": settings,
            "can_write_gateway": gateway,
            "can_write_secret": secret,
            "can_write_config": config,
            "can_view_settings": view_settings,
            "can_approve": approve,
            "can_deploy": deploy,
            # The refusal travels WITH the answer, so the caller shows the same
            # sentence the write endpoint would have raised rather than writing
            # a second one of its own that then drifts from it.
            "can_write_settings_reason": None if update else denied(
                "can_write_settings",
                await self.repo.service_name_for_config(resource_code)
                or resource_code,
            ),
        }

    # ── level 1: services with something to review ──────────────────────────
    async def list_services_awaiting(
        self, user: str, user_code: str, tenant_code: str
    ) -> List[dict]:
        """The approvals landing page: which services have changes for you.

        Grouped by service_config — a change is made against one service in one
        environment and region, and that is what the reviewer opens. The name,
        environment and region come from the queue row's own config_snapshot,
        which already carries them, so this needs no joins.
        """
        pairs = await self.list_inbox(
            user=user, user_code=user_code, tenant_code=tenant_code
        )

        # Only submitted and approved belong on this list. A draft is someone
        # still working — private to its author, nothing for anyone to act on —
        # and a rejected request is finished. Filtered here, before grouping, so
        # the counts describe only what is actually waiting rather than being
        # inflated by rows that are then hidden.
        #
        # APPROVERS ONLY, by decision: this page is the reviewer's inbox.
        # Authors follow their own change from the service panel (status line,
        # Slack DMs), and deployers deploy from there too — showing either here
        # made the page read as everyone's, which it is not.
        pairs = [
            (row, flags) for row, flags in pairs
            if row.status in (S.SUBMIT, S.APPROVED) and flags.get("can_approve")
        ]

        grouped: Dict[str, dict] = {}
        for row, flags in pairs:
            key = row.transaction_code
            snap = row.config_snapshot or {}
            entry = grouped.setdefault(key, {
                "resource_code": key,
                "services_mst_code": None,
                "service_name": None,
                "environment": None,
                "geo_loc_mst_code": None,
                "display_name": None,
                "pending_count": 0,
                "approved_count": 0,
                "total_count": 0,
                "latest_requested_at": None,
            })

            # Identity, taken from whichever half of the change carries it. A
            # gateway row's snapshot names things its own way — api_name rather
            # than service_name, service_mst_code rather than services_mst_code —
            # so a card built from the gateway row alone came up nameless. First
            # non-empty value wins, which is the settings row whenever there is
            # one, and the gateway row's equivalents when there is not.
            for field, value in (
                ("services_mst_code", snap.get("services_mst_code") or snap.get("service_mst_code")),
                ("service_name", snap.get("service_name") or snap.get("api_name")),
                ("environment", snap.get("environment")),
                ("geo_loc_mst_code", snap.get("geo_loc_mst_code")),
                ("display_name", row.display_name),
            ):
                if value and not entry[field]:
                    entry[field] = value

            # A change is ONE thing to review even when it is stored as two rows
            # — its settings and its gateway routes. Counting rows told the
            # reviewer "2 awaiting review" for a single change they would approve
            # with one click. The lane lock allows at most one live change per
            # service, so presence is the whole count.
            if row.status == S.SUBMIT:
                entry["pending_count"] = 1
            else:
                entry["approved_count"] = 1
            entry["total_count"] = entry["pending_count"] + entry["approved_count"]

            at = row.created_at.isoformat() if row.created_at else None
            if at and (entry["latest_requested_at"] or "") < at:
                entry["latest_requested_at"] = at

        # A VARIABLES-ONLY change set has no identity to donate: its snapshot
        # holds just the draft-file pointer, so the card came up with no
        # services_mst_code / name / env / region — and the approval drawer,
        # seeing no service code, declared the service "not created through
        # DevLift" and locked itself. The service_configs row (joined to
        # services_mst for the name) is the authority; one query backfills
        # every incomplete card.
        missing = [
            code for code, e in grouped.items()
            if not (e["services_mst_code"] and e["service_name"]
                    and e["environment"] and e["geo_loc_mst_code"])
        ]
        if missing:
            from sqlalchemy import text as _text

            rows_ = (await self.db.execute(_text(
                "SELECT sc.code, sc.services_mst_code, sm.name, "
                "       sc.environment, sc.geo_loc_mst_code "
                "FROM service_configs sc "
                "JOIN services_mst sm ON sm.code = sc.services_mst_code "
                "WHERE sc.code = ANY(:codes)"
            ), {"codes": missing})).all()
            for code, mst, name, env, geo in rows_:
                e = grouped.get(code)
                if not e:
                    continue
                e["services_mst_code"] = e["services_mst_code"] or mst
                e["service_name"] = e["service_name"] or name
                e["environment"] = e["environment"] or getattr(env, "value", env)
                e["geo_loc_mst_code"] = e["geo_loc_mst_code"] or geo
                # The card title too: "update variables : ..." is the ROW's
                # label, not the service's.
                if not e["display_name"] or str(e["display_name"]).startswith("update variables"):
                    e["display_name"] = name

        # Anything actually waiting first, then most recently touched.
        return sorted(
            grouped.values(),
            key=lambda e: (-e["pending_count"], e["latest_requested_at"] or ""),
        )

    # ── policy ──────────────────────────────────────────────────────────────
    async def rule_for(self, row: TransactionQueueModel) -> dict:
        """Approval policy for this row's resource group, or the defaults."""
        if row.table_name != WorkflowSourceTableEnum.SERVICE_CONFIG:
            return {**DEFAULT_RULE, "resource_group_mst_code": None}

        rg_code = await self.rule_repo.resource_group_of_service_config(
            row.transaction_code
        )
        if not rg_code:
            return {**DEFAULT_RULE, "resource_group_mst_code": None}

        rule = await self.rule_repo.get_for_resource_group(row.tenant_code, rg_code)
        if rule is None:
            return {**DEFAULT_RULE, "resource_group_mst_code": rg_code}
        return {
            "require_approval": rule.require_approval,
            "allow_self_approval": rule.allow_self_approval,
            "allow_approver_edit": rule.allow_approver_edit,
            "resource_group_mst_code": rg_code,
        }

    async def _get_locked(self, queue_code: str) -> TransactionQueueModel:
        """Every decision takes a row lock, so two reviewers deciding at the
        same instant cannot both win."""
        row = await self.repo.get_locked_by_code(queue_code)
        if row is None:
            raise HTTPException(404, f"change request {queue_code} not found")
        return row

    # ── the three decisions ─────────────────────────────────────────────────
    async def _change_set(
        self,
        row: TransactionQueueModel,
        statuses: Tuple[S, ...],
    ) -> List[TransactionQueueModel]:
        """Every row of this service's change, locked, including `row` itself.

        A change to a service can span two rows — its settings and its gateway
        routes — and to everyone involved it is ONE change: one thing submitted,
        one thing approved, one thing deployed. So every verb that moves it
        moves all of it. Approving half would ship routes whose settings nobody
        agreed to, or the reverse.

        Locked in a single statement, ordered by id: two people acting on the
        same service cannot each take a piece of it, and two concurrent callers
        take the rows in the same order rather than deadlocking.

        `row` is appended if the query somehow misses it — its own lock is
        already held by the caller, so acting on it is never a surprise.
        """
        members = await self.repo.lock_change_set(
            row.transaction_code, row.tenant_code, statuses=statuses,
            # The POC's model: a verb moves ONE author's rows. The anchor row
            # names the author, so approve/reject on a submitted request move
            # exactly what that submission contained — never a bystander's
            # draft that happened to share the service.
            user_code=row.user_code,
        )
        if all(r.code != row.code for r in members):
            members.append(row)
        return members

    async def approve(
        self, request: Request, user: str, user_code: str, queue_code: str,
        comment: str = "",
    ) -> TransactionQueueModel:
        """submit -> approved, for the WHOLE change. It becomes deployable.

        One decision covers the service's settings and its gateway routes: the
        reviewer was shown both and said yes to both, so both are sealed here.
        """
        row = await self._get_locked(queue_code)
        if row.status != S.SUBMIT:
            raise HTTPException(
                409, f"request is {row.status.value} — only submitted can be approved"
            )
        await require(
            request, user, "can_approve", fga_ref(row), 403,
            denied("can_approve", target_name(row)),
        )
        rule = await self.rule_for(row)
        if not rule["allow_self_approval"] and row.user_code == user_code:
            raise HTTPException(
                403,
                "You cannot approve your own request — this resource group "
                "requires a different approver",
            )
        change_set = await self._change_set(row, (S.SUBMIT,))
        for member in change_set:
            member.status = S.APPROVED
            member.decided_by = user_code
            member.decided_at = _now_dt()
            member.decision_comment = comment or None
            # Sealed PER ROW, over that row's own snapshot. The seal is what
            # deploy re-checks before it ships anything, and each row deploys by
            # its own path — a single hash across the pair could not say which
            # half had been tampered with, and would fail both for one edit.
            member.approved_snapshot_hash = seal(member.config_snapshot)
            # A fresh decision starts with nothing in flight. Without this a
            # row that was deployed, failed, sent back and approved again would
            # carry the old gate's marker and refuse every revoke thereafter.
            member.deploy_started_at = None
            member.history = (member.history or []) + [
                _event(user_code, "approved", comment)
            ]

        # The row the caller named, so the response describes what they acted on.
        return next((r for r in change_set if r.code == row.code), row)

    async def revoke(
        self, request: Request, user: str, user_code: str, queue_code: str,
        comment: str = "",
    ) -> TransactionQueueModel:
        """approved -> submit, for the WHOLE change. The decision is taken back.

        For an approver who approved by mistake, or learned something after
        deciding. It returns the change to WAITING, not to the author: nothing
        about the request itself is in question, only the decision — so the
        author has nothing to fix and no reason to be interrupted. Someone
        (possibly the same person) decides again.

        Only while it is still APPROVED. Once a deploy starts, the row leaves
        that status, and this refuses — by then the change is in a PR or already
        applied, and un-deciding it would say something untrue about what
        shipped. Withdrawing it from the pipeline is a different act, and a
        deploy that must not proceed is stopped in the pipeline, not here.

        The seal is DROPPED, along with decided_by/at/comment. It is a record of
        an approval that no longer exists, and leaving it behind would let a
        deploy verify against a decision that had been taken back.
        """
        row = await self._get_locked(queue_code)
        if row.status != S.APPROVED:
            raise HTTPException(409, _cannot_revoke(row.status))
        await require(
            request, user, "can_approve", fga_ref(row), 403,
            denied("can_approve", target_name(row)),
        )

        change_set = await self._change_set(row, (S.APPROVED,))
        # The status alone cannot say whether a deploy has begun. The gate
        # leaves the row APPROVED on purpose — validate_deployable_queue_items
        # and the multiple-deploy route both match that status exactly — so the
        # row stays APPROVED from the gate until a Temporal activity moves it,
        # seconds later. Revoking inside that gap does not stop the deploy; it
        # only erases the decision the deploy is carrying, which is how a change
        # reached production with a null seal and no approver on it.
        #
        # Checked across the change set, since the gate marks every member and
        # either half being in flight means the change is.
        in_flight = next((m for m in change_set if deploy_in_flight(m)), None)
        if in_flight is not None:
            raise HTTPException(409, _cannot_revoke(S.STARTING_DEPLOYMENT))
        for member in change_set:
            member.status = S.SUBMIT
            member.decided_by = None
            member.decided_at = None
            member.decision_comment = None
            member.approved_snapshot_hash = None
            member.history = (member.history or []) + [
                _event(user_code, "approval-revoked", comment)
            ]
        return next((r for r in change_set if r.code == row.code), row)

    async def request_changes(
        self, request: Request, user: str, user_code: str, queue_code: str,
        comment: str,
    ) -> TransactionQueueModel:
        """submit -> draft, for the WHOLE change. Recoverable: every value
        survives so the submitter edits and resubmits on the same records.

        Both halves come back. Sending only the named row back would leave the
        other still waiting on a reviewer who has already made their decision —
        and the author could not resubmit, because half their change would not
        be a draft.
        """
        if not comment.strip():
            raise HTTPException(400, "say what needs changing — a comment is required")
        row = await self._get_locked(queue_code)
        if row.status != S.SUBMIT:
            raise HTTPException(
                409, f"request is {row.status.value} — only submitted can be sent back"
            )
        await require(
            request, user, "can_approve", fga_ref(row), 403,
            denied("can_approve", target_name(row)),
        )
        change_set = await self._change_set(row, (S.SUBMIT,))
        for member in change_set:
            member.status = S.DRAFT
            member.decided_by = None
            member.decided_at = None
            member.decision_comment = comment
            member.approved_snapshot_hash = None
            member.history = (member.history or []) + [
                _event(user_code, "changes-requested", comment)
            ]
        return next((r for r in change_set if r.code == row.code), row)

    # ── the maker's half: save, submit, withdraw ────────────────────────────
    @staticmethod
    def _snapshot_changed(before: Optional[dict], after: Optional[dict]) -> bool:
        """Did this save actually alter the stored snapshot?

        The Save button sends every half of the form — settings, gateway and
        variables — on each press, whether or not that half was touched. A
        history entry per half per press made the timeline claim the settings
        and the routes were edited every time someone added a variable, which
        is a false record of what a reviewer is being asked to approve.

        `before` is read BEFORE the write, because the write commits: an
        earlier version of this read SQLAlchemy's attribute history afterwards,
        and a commit clears that history, so it saw "nothing changed" every
        time and silently stopped recording real edits. The audit trail going
        quiet is a worse failure than a duplicate line in it, so anything this
        cannot establish counts as a change.

        Compared on the STORED value, not on the request: the snapshot is
        enriched server-side (ids, codes, language_ref_code, config fallbacks)
        before it lands, so an untouched form still arrives looking different
        from what the client sent.

        Canonical JSON rather than ==, because dict ordering is not meaningful
        here and a re-serialised snapshot can reorder keys without anything
        having changed. List order IS kept significant — reordering routes or
        sidecars is a real edit.
        """
        if before is None:
            # No previous value to compare — record it rather than guess.
            return True
        return json.dumps(before, sort_keys=True, default=str) != json.dumps(
            after, sort_keys=True, default=str
        )

    async def save_draft(
        self,
        request: Request,
        user: str,
        user_code: str,
        tenant_code: str,
        transaction_code: str,
        config_snapshot: Optional[dict] = None,
        gateway_groups: Optional[List[dict]] = None,
        case_ref_code: Optional[str] = None,
        queue_code: Optional[str] = None,
        ticket_code: Optional[str] = None,
    ) -> TransactionQueueModel:
        """Save — parks the change as a DRAFT and writes nothing live.

        One Save for the whole service. Settings and gateway routes are one
        change to the person making it, so they arrive together and become one
        decision — but each half writes a row only if it actually changed:

            config_snapshot  ->  SERVICE_CONFIG row
            gateway_groups   ->  KONG_ROUTE row

        Neither touches a live table. service_configs keeps showing what is
        running, and so do kong_route_groups / kong_route_configs; both are
        written at deploy, after approval. That is what makes the live rows a
        truthful record — and therefore a baseline worth diffing against.

        Returns the SETTINGS row when there is one, otherwise the gateway row:
        the caller wants something to show, and a gateway-only save has only
        the one.
        """
        # No permission check here. Both callers are carded routes declaring the
        # relation for the half they carry — can_write_settings on
        # /transaction/service-settings, can_write_gateway on
        # /transaction/kong-gateway — against service:{transaction_code}, the
        # same object this method would have asked about, and the guard runs
        # before the handler.
        #
        # A second check would be a second round-trip for an answer already
        # given, and it could not be more precise than the cards: this method
        # sees both halves at once, while each route knows exactly which one it
        # is saving. The "shared method keeps its own check" argument does not
        # hold either — require() takes a FastAPI Request, so a non-HTTP caller
        # could not reach this code path in the first place.

        # ── the lane lock: one live request per resource ────────────────────
        # The live add-to-queue path reuses an existing DRAFT *or APPROVED* row
        # for the same service. Left alone, saving after an approval would
        # rewrite the approved snapshot in place: the seal would break, the
        # change would become undeployable, and the edit would be stranded on a
        # row its author can no longer submit. Refuse instead, and say what to
        # do about it.
        live = await self.repo.find_live_for_resource(transaction_code, tenant_code)
        if live is not None:
            from app.repository.approval_repository import DEPLOY_IN_FLIGHT_STATUSES

            state = getattr(live.status, "value", str(live.status))
            if live.status in DEPLOY_IN_FLIGHT_STATUSES:
                # Deploy started, review statuses cleared — but the pipeline
                # is mid-flight. A new save now would raise a change against
                # a service whose previous change has not landed yet.
                raise HTTPException(
                    409,
                    "A deployment is currently running for this service — "
                    "please wait until it completes before saving new changes.",
                )
            nudge = (
                "withdraw it or ask a reviewer to send it back"
                if state == "submit"
                else "deploy it, or send it back through review"
            )
            raise HTTPException(
                409,
                f"{transaction_code} already has a change {state} ({live.code}) — "
                f"{nudge} before starting another.",
            )

        # Imported here, not at module scope — this service pulls in the whole
        # deploy stack, and a top-level import makes a cycle.
        from app.services.transaction_queue_service import TransactionQueueService

        row = None
        if config_snapshot is not None:
            # Captured before the write, which commits — see _snapshot_changed.
            # None when this is a fresh draft, which the "created" branch below
            # handles on its own.
            before_settings = None
            prior = None
            if queue_code:
                prior = await self.repo.get_by_code(queue_code, tenant_code)
                if prior is not None and not prior.is_deleted:
                    before_settings = copy.deepcopy(prior.config_snapshot)

            # THE GUARANTEE against phantom settings requests: a settings half
            # whose diff against the live config is EMPTY changes nothing, so
            # no new row is created for it — whatever the client decided.
            # Save sends every half it believes changed, and one wrong
            # "changed" flag used to park an empty request that then had to be
            # submitted, reviewed and approved to no effect. The check lives
            # here, server-side, because the client's belief has been wrong
            # before (a null-vs-absent sidecar comparison) and will be wrong
            # again; the server is the only place that can promise it.
            #
            # CREATION only. An existing draft is still updated even when the
            # update empties its diff — the author reverting their edits back
            # to the live values is a real action on a real row, and skipping
            # the write would freeze the stale content they just undid.
            #
            # The probe row is never added to the session: compute_changes
            # only reads its columns and queries by its codes.
            if prior is None:
                probe = TransactionQueueModel(
                    transaction_code=transaction_code,
                    tenant_code=tenant_code,
                    user_code=user_code,
                    table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                    case_ref_code=case_ref_code or SETTINGS_CASE_REF,
                    config_snapshot=config_snapshot,
                )
                if not await self.compute_changes(probe):
                    logger.info(
                        "save_draft: settings half for %s matches the live "
                        "config — not queued (gateway half unaffected)",
                        transaction_code,
                    )
                    config_snapshot = None
            elif (
                prior.status == S.DRAFT
                and prior.user_code == user_code
                and not prior.is_deleted
            ):
                # UPDATE of the author's own draft. The probe above only covered
                # creation, on the argument that reverting edits back to the live
                # values is a real action on a real row. It is — but the honest
                # record of that revert is a CLEARED draft, not an empty one:
                # an empty draft has no future except being submitted, put in
                # front of a reviewer with nothing to decide, and failing at
                # deploy with "no diff detected". The draft is discarded exactly
                # as the Discard verb would, so the history says what happened.
                # Only the author's own DRAFT: anything past draft is in the
                # review lane, where its own verbs apply.
                probe = TransactionQueueModel(
                    transaction_code=transaction_code,
                    tenant_code=tenant_code,
                    user_code=user_code,
                    table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                    case_ref_code=case_ref_code or SETTINGS_CASE_REF,
                    config_snapshot=config_snapshot,
                )
                if not await self.compute_changes(probe):
                    prior.is_deleted = True
                    prior.history = (prior.history or []) + [
                        _event(
                            user_code, "discarded",
                            "values reverted to what is deployed — draft cleared",
                        )
                    ]
                    logger.info(
                        "save_draft: update emptied the settings diff for %s — "
                        "draft %s cleared rather than left empty",
                        transaction_code, prior.code,
                    )
                    config_snapshot = None

        if config_snapshot is not None:
            row = await TransactionQueueService(self.db).add_item_to_queue(
                user_code=user_code,
                tenant_code=tenant_code,
                transaction_code=transaction_code,
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                config_snapshot=config_snapshot,
                case_ref_code=case_ref_code,
                queue_code=queue_code,
                ticket_code=ticket_code,
            )

        # add_item_to_queue reuses an open draft rather than stacking a second
        # one, so this is an edit as often as it is a create — and every one of
        # them is a change to a record someone will later be asked to approve.
        # Logging only the create left the audit trail saying a request was made
        # once and never touched again, however many times it was rewritten.
        #
        # JSONB does not see in-place mutation: reassign, never append.
        if row is not None:
            if not row.history:
                row.history = [_event(user_code, "created")]
            elif self._snapshot_changed(before_settings, row.config_snapshot):
                row.history = row.history + [_event(user_code, "edited")]

        gateway_row = None
        if gateway_groups is not None:
            gateway_row = await self._save_gateway_draft(
                user_code=user_code,
                tenant_code=tenant_code,
                transaction_code=transaction_code,
                groups=gateway_groups,
            )

        # The settings row is the one a caller expects back; a gateway-only save
        # has only the other. Both are in the same change set either way.
        return row or gateway_row

    async def _save_gateway_draft(
        self,
        user_code: str,
        tenant_code: str,
        transaction_code: str,
        groups: List[dict],
    ) -> Optional[TransactionQueueModel]:
        """The gateway half: a DRAFT queue row holding the route change.

        Nothing is written to kong_route_groups or kong_route_configs. Those are
        written at deploy, from this snapshot — which is also what the terragrunt
        generator already reads, so deferring the table writes changes nothing
        about what ships.

        The scope is DERIVED from transaction_code rather than sent by the
        client: the service_configs row names its service, environment and
        region, and its service names the api. One source, so the settings row
        and the gateway row cannot disagree about which service they describe.

        An empty `groups` means every route edit was undone — the row is dropped
        rather than stored empty, or a deploy of "no changes" would produce an
        empty PR.
        """
        from app.repository.transaction_queue_repository import TransactionQueueRepository
        from app.services.service_config_service import ServiceConfigService

        svc = ServiceConfigService(self.db)
        config = await svc.service_config_repository.get_by_code_and_tenant(
            transaction_code, tenant_code
        )
        if config is None:
            raise HTTPException(
                404,
                f"no service configuration {transaction_code} to attach gateway "
                f"routes to",
            )

        queue_repo = TransactionQueueRepository(self.db)

        if not groups:
            await queue_repo.clear_pending_for_scope(
                tenant_code=tenant_code,
                user_code=user_code,
                services_mst_code=config.services_mst_code,
                environment=config.environment,
                geo_loc_mst_code=config.geo_loc_mst_code,
            )
            return None

        service_name = await self.repo.service_name_for_config(transaction_code)
        env_value = getattr(config.environment, "value", config.environment)

        # Captured before the upsert — see _snapshot_changed. get_pending_for_scope
        # returns EVERY user's pending row for the scope, so pick this caller's:
        # the upsert only ever rewrites their own.
        before_gateway = None
        for prior in await queue_repo.get_pending_for_scope(
            tenant_code=tenant_code,
            services_mst_code=config.services_mst_code,
            environment=config.environment,
            geo_loc_mst_code=config.geo_loc_mst_code,
        ):
            if prior.user_code == user_code and not prior.is_deleted:
                before_gateway = copy.deepcopy(prior.config_snapshot)
                break

        # ── Correct the browser's delta against the LIVE Kong tables ────────
        # The delta is computed client-side against whatever baseline the tab
        # held when the user typed — and that baseline can be stale: after a
        # deploy, the paths query refetches (rows now carry KRC_ codes) while
        # the pending-rows query the baseline is rebuilt from may still be
        # cached. One real second-change save arrived claiming add for two
        # routes that were already deployed, with plugins_before=[] when JWT
        # was live — a false diff the reviewer then approved. The FILE stayed
        # correct only because the generator's primitives are idempotent.
        #
        # The tables are authoritative here — since save stopped writing them,
        # they hold exactly the deployed state — so every claim the browser
        # makes about "before" is checked against them: an add or edit whose
        # code already sits ACTIVE at that exact path is already live and is
        # dropped; plugins_before / regex_priority_before are overwritten with
        # the group's real values. A group whose actions all drop and whose
        # plugin and priority moves cancel says nothing and is dropped whole.
        entries = [_gateway_entry(g) for g in groups]
        normalized: List[dict] = []
        for entry in entries:
            group_code = entry.get("group_code")
            if group_code:
                from app.db.models.kong_route_config_model import KongRouteConfigModel
                from app.db.models.kong_route_group_model import KongRouteGroupModel
                from sqlalchemy import select as _select

                grp = (await self.db.execute(_select(KongRouteGroupModel).where(
                    KongRouteGroupModel.code == group_code,
                    KongRouteGroupModel.is_deleted.isnot(True),
                ))).scalars().first()
                if grp is not None:
                    live_paths = {
                        r.code: r.route_path
                        for r in (await self.db.execute(_select(KongRouteConfigModel).where(
                            KongRouteConfigModel.kong_route_group_id == grp.id,
                            KongRouteConfigModel.is_deleted.isnot(True),
                        ))).scalars().all()
                    }
                    kept = []
                    for a in entry.get("paths") or []:
                        code, path = a.get("code"), a.get("route_path")
                        act = (a.get("action") or "").lower()
                        if act in ("add", "edit") and code and live_paths.get(code) == path:
                            continue  # already live exactly like this
                        kept.append(a)
                    entry["paths"] = kept
                    entry["plugins_before"] = list(grp.plugins or [])
                    entry["regex_priority_before"] = grp.regex_priority or 0
            same_plugins = (entry.get("plugins_before") or []) == (entry.get("plugins_after") or [])
            same_priority = (entry.get("regex_priority_before") or 0) == (entry.get("regex_priority_after") or 0)
            if not entry.get("paths") and same_plugins and same_priority:
                continue  # nothing this group actually changes
            normalized.append(entry)

        if not normalized:
            # Every claimed change was already live — same outcome as an empty
            # groups list: no request is parked for a change that is not one.
            await queue_repo.clear_pending_for_scope(
                tenant_code=tenant_code,
                user_code=user_code,
                services_mst_code=config.services_mst_code,
                environment=config.environment,
                geo_loc_mst_code=config.geo_loc_mst_code,
            )
            return None

        # The lane lock, gateway half — the settings rule above, applied to the
        # lane find_live_for_resource deliberately leaves alone. A submitted row
        # cannot be rewritten under its reviewer, and the upsert would answer
        # that by inserting a second live row for the scope.
        submitted = await queue_repo.find_submitted_for_scope(
            tenant_code=tenant_code,
            user_code=user_code,
            services_mst_code=config.services_mst_code,
            environment=config.environment,
            geo_loc_mst_code=config.geo_loc_mst_code,
        )
        if submitted is not None:
            raise HTTPException(
                409,
                f"This service already has a gateway change submit "
                f"({submitted.code}) — withdraw it or ask a reviewer to send it "
                f"back before starting another.",
            )

        gateway_row = await queue_repo.upsert_pending_for_scope(
            transaction_code=transaction_code,
            tenant_code=tenant_code,
            user_code=user_code,
            services_mst_code=config.services_mst_code,
            environment=config.environment,
            geo_loc_mst_code=config.geo_loc_mst_code,
            snapshot={
                # Scope, carried for the generator — which must not have to join
                # anything to know which file it is editing.
                "service_mst_code": config.services_mst_code,
                "api_name": service_name,
                "environment": env_value,
                "geo_loc_mst_code": config.geo_loc_mst_code,
                "groups": normalized,
            },
            display_name=f"Gateway routes - {service_name or transaction_code}",
        )

        # The gateway row is logged like the settings one. The repository method
        # is shared with the gateway tab's own save path and writes no history,
        # so it is written here — without it the History tab shows the settings
        # half being created and edited while the routes appear from nowhere at
        # submit. An empty log means this upsert just inserted the row.
        if not gateway_row.history:
            gateway_row.history = [_event(user_code, "created")]
        elif self._snapshot_changed(before_gateway, gateway_row.config_snapshot):
            gateway_row.history = gateway_row.history + [
                _event(user_code, "edited")
            ]
        return gateway_row

    async def compute_changes(self, row: TransactionQueueModel) -> dict:
        """from/to diff of this request against what is really running.

            from  =  the live service_configs row
            to    =  this request's config_snapshot

        Save writes NOTHING to service_configs, so that row still holds the
        values actually deployed — which makes it the real baseline, and makes
        it read-only input here. It is written at deploy, not before.

        The comparison itself is ServiceConfigService.diff_config_dicts, the
        same engine behind the settings screen's diff, so the field list,
        unit normalisation and nested-group handling stay in one place.

        A GATEWAY row is different in kind: its diff was never derived. The
        client computed it against what is deployed — knowledge the server does
        not hold, since deployed routes live in a terragrunt file rather than a
        table — and sent it to be stored. So the change record IS the snapshot's
        groups; running the settings diff over it would compare route deltas
        against a service config and report nonsense.
        """
        # Variables are a SERVICE_CONFIG row too, told apart by their case. Their
        # diff arrives already made: devlift-secret-config-manager builds it at
        # save time from the merged draft file and stores it here. Running the
        # settings differ over their snapshot would replace a list of the keys
        # that move with one bogus row called "draft_file" holding an S3 path —
        # the snapshot is deliberately nothing but a pointer, because
        # config_snapshot is copied verbatim into the audit table and key names
        # and values must not be.
        if row.case_ref_code == VARIABLES_CASE_REF:
            return row.changes or {}

        if row.case_ref_code == GATEWAY_CASE_REF:
            snapshot = row.config_snapshot or {}
            return {"groups": snapshot.get("groups") or []}

        from app.services.service_config_service import ServiceConfigService

        svc = ServiceConfigService(self.db)
        key = (row.transaction_code, row.tenant_code)
        if key not in self._live_configs:
            self._live_configs[key] = (
                await svc.service_config_repository.get_by_code_and_tenant(
                    row.transaction_code, row.tenant_code
                )
            )
        live_row = self._live_configs[key]

        # The diff walks the PROPOSAL's keys and nothing else (see
        # diff_config_dicts), so what matters here is handing it the payload:
        # the nested `config` block, which holds exactly what the form manages.
        # The snapshot's root is queue identity plus pipeline extras
        # (sidecar_config, env_variables, pendingChanges) — none of it a
        # settings decision, all of it an "addition" if it reached the walk.
        snapshot = row.config_snapshot or {}
        nested = snapshot.get("config")
        live = dict((live_row.config or {}) if live_row else {})

        if isinstance(nested, dict):
            proposed = dict(nested)
            # Snapshot-only routing keys, never persisted to the live config.
            # The diff walks the proposal, so anything here that the live row
            # can never hold reads as a change on EVERY request — ci_provider
            # showed "— → jenkins" to reviewers on submissions that never
            # touched it. The form's own diff (configDiffKeys) excludes the
            # same keys, for the same reason; the two lists must agree or the
            # save and the reviewer count different changes.
            for routing_key in ("ci_provider", "pendingChanges",
                                "pendingchanges", "pending_changes"):
                proposed.pop(routing_key, None)
        else:
            # A snapshot from before the payload was nested: form fields and
            # identity share the root and nothing structural tells them apart.
            # Restrict to keys the live config also has — additions cannot be
            # reported for these rows, which is the honest trade; saving again
            # rewrites the snapshot in the nested shape and lifts the limit.
            proposed = {k: v for k, v in snapshot.items() if k in live}

        # sidecar_config needs NORMALISING before the generic column lift: the
        # snapshot carries the form's full display objects (name, enabled flag,
        # a dozen default_advanced_options), while the live column stores
        # minimal overrides [{sidecar_config_code, cpu, ram}]. Compared raw,
        # the shapes differ on every request — a permanent phantom change. The
        # deployable truth of a sidecar is "which ones are ON, at what size",
        # so both sides reduce to exactly that; an entry toggled off is not a
        # sidecar, it is the memory of one.
        def _sidecars(raw) -> list:
            out = []
            for sc in raw or []:
                if not isinstance(sc, dict) or not sc.get("sidecar_config_code"):
                    continue
                # The live column stores only deployed (enabled) overrides and
                # carries no flag; snapshot entries say so explicitly.
                if sc.get("enabled") is False:
                    continue
                entry = {
                    "sidecar": sc["sidecar_config_code"],
                    "cpu": str(sc.get("cpu") or ""),
                    "ram": str(sc.get("ram") or ""),
                }
                # Advanced options count only as DEVIATIONS from the catalog
                # defaults. The form sends all ~14 defaults with every save,
                # touched or not — freezing them would bury the decision under
                # a constant block. An edit away from a default is a decision;
                # the default itself is furniture.
                defaults = {
                    o.get("key"): str(o.get("value"))
                    for o in (sc.get("default_advanced_options") or [])
                    if isinstance(o, dict) and o.get("key") and o.get("value") is not None
                }
                deviations = {
                    k: v
                    for k, v in (
                        (o.get("key"), str(o.get("value")))
                        for o in (sc.get("advanced_options") or [])
                        if isinstance(o, dict) and o.get("key") and o.get("value") is not None
                    )
                    if defaults.get(k) != v
                }
                if deviations:
                    entry["options"] = dict(sorted(deviations.items()))
                # Log collection defaults to on; only an explicit OFF is a
                # statement. Absent and on compare equal on purpose.
                if sc.get("datadog_logs_enabled") is False:
                    entry["logs_enabled"] = False
                out.append(entry)
            return sorted(out, key=lambda x: x["sidecar"])

        # Only when the snapshot CARRIES the key — the merge-patch rule again:
        # a proposal that says nothing about sidecars changes nothing about
        # them, however many the live column holds. The form sends the key
        # only once its sidecar list is hydrated and non-empty; "remove them
        # all" still arrives explicitly, because disabling a sidecar keeps its
        # entry (enabled: false) in the list — an absent key only ever means
        # "this save does not speak for sidecars".
        #
        # The frozen entry is STRUCTURED — real lists on both sides, never fed
        # to the walker, whose repr-stringification is fine for scalars but
        # turns a list of sidecars into one opaque blob. The UI renders this
        # key as its own block (added / removed / updated per sidecar), which
        # needs the objects intact. Display names ride along for that block
        # only — the snapshot's display objects carry them, and a removed
        # sidecar's name is usually still there as its enabled:false entry.
        sidecar_change: dict | None = None
        if "sidecar_config" in snapshot:
            # The baseline is the LAST DEPLOYED snapshot when there is one: it
            # holds the full display objects, so a deployed option edit is
            # comparable — set once, deployed, it stops re-reporting on every
            # later request. The live column — minimal {code, cpu, ram}
            # overrides — is the fallback for services that never deployed a
            # sidecar save, and reads as "everything at defaults", which is
            # what an optionless deploy record honestly is.
            latest_deployed = (
                await svc.queue_repository.get_latest_deployed_by_transaction_code(
                    row.transaction_code, row.tenant_code, settings_rows_only=True
                )
            )
            deployed_raw = (
                (latest_deployed.config_snapshot or {}).get("sidecar_config")
                if latest_deployed
                else None
            )
            baseline_raw = (
                deployed_raw
                if deployed_raw is not None
                else (getattr(live_row, "sidecar_config", None) if live_row else None)
            )
            proposed_sc = _sidecars(snapshot.get("sidecar_config"))
            live_sc = _sidecars(baseline_raw)
            if proposed_sc != live_sc:
                # Snapshot last, so the proposal's spelling of a name wins.
                names = {
                    sc.get("sidecar_config_code"): sc.get("name")
                    for source in (baseline_raw, snapshot.get("sidecar_config"))
                    for sc in (source or [])
                    if isinstance(sc, dict) and sc.get("name")
                }

                def _named(entries: list) -> list:
                    return [
                        {**e, **({"name": names[e["sidecar"]]} if e["sidecar"] in names else {})}
                        for e in entries
                    ]

                sidecar_change = {"from": _named(live_sc), "to": _named(proposed_sc)}
        proposed.pop("sidecar_config", None)
        live.pop("sidecar_config", None)

        # The columns service_configs keeps OUTSIDE its config JSONB. The live
        # side must be read from the column or the proposal's value compares
        # against nothing and always reads as a change. Root fallback covers
        # the flat legacy shape, where the intersection above drops them.
        for column in COLUMN_FIELDS:
            if proposed.get(column) is None and snapshot.get(column) is not None:
                proposed[column] = snapshot[column]
            if proposed.get(column) is not None and live_row is not None:
                if getattr(live_row, column, None) is not None:
                    live[column] = getattr(live_row, column)

        changes = svc.diff_config_dicts(proposed, live)
        if sidecar_change is not None:
            changes["sidecar_config"] = sidecar_change
        return changes

    async def submit(
        self, request: Request, user: str, user_code: str, queue_code: str,
        comment: str = "",
    ) -> TransactionQueueModel:
        """draft -> submit, for the WHOLE change — THIS AUTHOR's whole change.

        A service's settings, routes and variables are one change to whoever
        made them, so submitting sends all of that author's draft rows for the
        service, not only the one whose code was passed. Submitting half would
        ask a reviewer to decide on a service while the rest of the same edit
        sat invisible in a draft.

        The POC's model, both halves: drafts are per author and a verb never
        moves a bystander's work; the REVIEW LANE is per service — one live
        request at a time, whoever's — enforced just below.

        The diff is FROZEN here, per row. Computing it at read time would let it
        drift after the fact — a reviewer must see what they are deciding on,
        not a recomputation against whatever is deployed by then.
        """
        row = await self._get_locked(queue_code)
        if row.status != S.DRAFT:
            raise HTTPException(
                409, f"request is {row.status.value} — only a draft can be submitted"
            )

        # The review lane is shared even though drafts are not — the POC's
        # one-live-request rule. Two submissions on one service would hand a
        # reviewer competing versions of the same thing, and hand deploy an
        # ordering neither author chose. The second submitter is told whose
        # request holds the lane and waits for it to finish.
        other = await self.repo.find_live_by_other_author(
            row.transaction_code, row.tenant_code, row.user_code
        )
        if other is not None:
            names = await self.repo.names_for_user_codes({other.user_code})
            who = names.get(other.user_code, other.user_code)
            raise HTTPException(
                409,
                f"{other.code} by {who} is already {other.status.value} on "
                f"{target_name(row)} — one request at a time; let it finish "
                f"(deploy, reject or withdraw) first.",
            )

        change_set = await self._change_set(row, (S.DRAFT,))

        # Field checks land HERE, not on the draft save: a draft is allowed to
        # be incomplete, and submit is where the change stops being the
        # author's and becomes something a reviewer is asked to decide on.
        for member in change_set:
            snapshot = member.config_snapshot
            if not isinstance(snapshot, dict):
                continue
            raw = snapshot.get("service_path")
            if isinstance(raw, str) and raw.strip():
                try:
                    # Trailing /* is refused for EKS only: it breaks a
                    # Kubernetes ingress, while an ALB path pattern uses that
                    # form routinely (and the ECS form appends the wildcard
                    # itself). The snapshot names its own infrastructure type.
                    validate_service_path(
                        raw,
                        reject_slash_wildcard=is_eks_infra(
                            snapshot.get("infrastructuretype_ref_code")
                        ),
                    )
                except ValueError as exc:
                    raise HTTPException(422, f"{target_name(member)}: {exc}")

        # Per ROW, not one check for the set: every row still names the same
        # service, but no longer the same permission — a variables row needs
        # can_write_secret / can_write_config where settings and gateway rows
        # need can_update (see submit_permissions). Deduped, so the common
        # case — settings + routes, same object, same permission — stays one
        # OpenFGA call. All of it is checked before anything moves: a caller
        # holding part of what the set needs must not strand half a change
        # in review.
        checked: Set[Tuple[str, str]] = set()
        for member in change_set:
            ref = fga_ref(member)
            for permission in submit_permissions(member):
                if (permission, ref) in checked:
                    continue
                checked.add((permission, ref))
                await require(
                    request, user, permission, ref, 403,
                    denied(permission, target_name(member)),
                )

        # THE GUARANTEE against an empty request reaching a reviewer: the diff
        # is computed for every row first, and if none of them changes anything
        # the submit is refused before a single row moves.
        #
        # This is the server's promise, not the client's. The form marks itself
        # dirty on any touch — reselecting the same dropdown value counts — and
        # its "changed" flag has been wrong in both directions before (a diff
        # that read dirty when clean, then a flag that reads dirty when nothing
        # changed). save_draft's own probe only runs on CREATION; an existing
        # draft updated to an empty diff was written as-is, submitted, put in
        # front of an approver with nothing to decide, and failed at deploy
        # with "no diff detected". Submit is where the change stops being the
        # author's, so submit is where "is there anything here?" is asked.
        diffs = [await self.compute_changes(member) for member in change_set]
        if all(_is_empty_change(d) for d in diffs):
            raise HTTPException(
                409,
                f"Nothing has changed on {target_name(row)} compared to what is "
                f"deployed, so there is nothing to review. Make an edit before "
                f"submitting, or discard the draft.",
            )

        for member, changes in zip(change_set, diffs):
            member.changes = changes
            member.status = S.SUBMIT
            member.decided_by = None
            member.decided_at = None
            member.decision_comment = None
            member.approved_snapshot_hash = None
            # The diff goes on the event too. `changes` is the CURRENT one and is
            # overwritten by the next submit; this copy is what THIS submission
            # contained, which is what the history has to be able to say.
            member.history = (member.history or []) + [
                _event(user_code, "submitted", comment, changes=member.changes)
            ]

        # The row the caller named, so the response describes what they acted on.
        return next((r for r in change_set if r.code == row.code), row)

    async def discard(
        self, request: Request, user: str, user_code: str, queue_code: str,
    ) -> Optional[TransactionQueueModel]:
        """Throw a DRAFT away. The POC folds this into withdraw — withdrawing a
        draft is the discard — kept as its own verb here because the two read
        as different intentions and deserve different buttons.

        Author-only and draft-only: a draft is private work, so only its author
        may bin it, and anything past draft is in the shared world where the
        lane's own verbs (withdraw, reject) apply. Soft delete, the same
        convention retire_variable_draft uses — reversible in the database,
        gone from every read.

        The WHOLE change, like every other verb: settings and gateway rows are
        binned here; the variables row, whose draft lives half in the queue
        row and half in a staged S3 file only devlift-secret may touch, is
        binned by devlift-secret on our request (_discard_variables_draft).
        It used to be left for the browser to clear from the Variables tab,
        which only worked when that tab happened to be mounted — a discard
        from the Settings tab removed the settings and kept the variables.

        Order matters, and the locks are the reason.

          1. Lock OUR rows (settings, gateway) — but not the variables row.
             devlift-secret soft-deletes that one on our behalf, and doing it
             while this transaction held FOR UPDATE on the same row would
             block its UPDATE on our own lock while we waited for its reply.
          2. Call devlift-secret. It is the half that cannot be rolled back,
             so it runs before anything of ours changes: a failure there
             leaves the whole draft intact and the caller sees the error.
             Holding our locks across the call is what keeps a concurrent
             submit from moving the settings rows under us — it waits, then
             finds them gone.
          3. Bin our rows. A failure here leaves only the settings draft,
             which a second click removes: the secret call is idempotent.

        Returns None when the clicked row WAS the variables row (a
        variables-only draft): devlift-secret retired it, and this session's
        copy is stale — there is no record to hand back.
        """
        row = await self.repo.get_by_code(queue_code)
        if row is None or row.is_deleted:
            raise HTTPException(404, f"change request {queue_code} not found")
        if row.status != S.DRAFT:
            raise HTTPException(
                409, f"request is {row.status.value} — only a draft can be discarded"
            )
        if row.user_code != user_code:
            raise HTTPException(403, "only the author can discard their draft")
        # 1. Our rows, locked. The variables row stays free (see above).
        own_rows = await self.repo.lock_change_set(
            row.transaction_code, row.tenant_code, statuses=(S.DRAFT,),
            user_code=row.user_code, exclude_case_ref=VARIABLES_CASE_REF,
        )
        if row.case_ref_code != VARIABLES_CASE_REF and all(
            r.code != row.code for r in own_rows
        ):
            # Moved between the plain read above and the lock — submitted or
            # binned from another tab. Nothing has been touched yet.
            raise HTTPException(409, "this draft changed under you — reload and try again")

        # Unlocked read of the whole set: to learn whether a variables row
        # exists (its lock is exactly what we must not take), and to ask every
        # row's write permission, as submit does — the clicked row first:
        # settings -> can_write_settings, routes -> can_write_gateway,
        # variables -> the kinds it holds. Discarding a draft deletes
        # its staged secrets and never-deployed rows, so it is an act on each
        # lane it touches — a discard pressed on the settings row must still
        # hold the secret/config write right for the variables it removes.
        # Owning the draft is not that right, least of all for a user whose
        # role was just revoked.
        members = await self.repo.list_change_set(
            row.transaction_code, row.tenant_code, statuses=(S.DRAFT,),
            user_code=row.user_code,
        )
        checked: Set[Tuple[str, str]] = set()
        for member in [row, *members]:
            ref = fga_ref(member)
            for permission in sorted(submit_permissions(member) or {"can_write_settings"}):
                if (permission, ref) in checked:
                    continue
                checked.add((permission, ref))
                await require(
                    request, user, permission, ref, 403,
                    denied(permission, target_name(member)),
                )
        if row.case_ref_code == VARIABLES_CASE_REF or any(
            m.case_ref_code == VARIABLES_CASE_REF for m in members
        ):
            # 2. devlift-secret bins the variables half, or nothing happens.
            await self._discard_variables_draft(row)

        # 3. Ours.
        for member in own_rows:
            # The model's own soft_delete, not hand-set flags: it also stamps
            # deleted_at, which the flags alone forgot — a binned row should
            # say WHEN it was binned.
            member.soft_delete()
            member.history = (member.history or []) + [
                _event(user_code, "discarded")
            ]
        if row.case_ref_code == VARIABLES_CASE_REF:
            return None
        return next((r for r in own_rows if r.code == row.code), row)

    async def _discard_variables_draft(self, row: TransactionQueueModel) -> None:
        """Ask devlift-secret to bin the author's variables draft — file, rows
        and queue row. Any failure is the caller's 502: nothing of ours has
        changed yet, so the draft is exactly as it was."""
        from app.integrations.secret_config_client import SecretConfigClient

        try:
            result = await SecretConfigClient(timeout=15).discard_variables_draft(
                transaction_code=row.transaction_code,
                user_code=row.user_code,
                tenant_code=row.tenant_code,
            )
        except Exception as exc:  # noqa: BLE001 — every failure maps to one answer
            body = getattr(getattr(exc, "response", None), "text", "")
            logger.error(
                "discard %s: variables draft could not be cleared for %s (%r) %s",
                row.code, row.transaction_code, exc, body[:300],
            )
            raise HTTPException(
                502,
                "The variables draft could not be cleared, so nothing was "
                "discarded — please try again.",
            )
        logger.info(
            "discard %s: variables draft cleared for %s by user %s -> %s",
            row.code, row.transaction_code, row.user_code, result,
        )

    async def withdraw(
        self, request: Request, user: str, user_code: str, queue_code: str,
        comment: str = "",
    ) -> TransactionQueueModel:
        """submit -> draft, for the WHOLE change, pulled back by whoever sent it.

        Distinct from request-changes, which is the reviewer pushing it back.
        Only the submitter may withdraw: taking a change out of someone else's
        review queue is not a reviewer's decision to make.

        It undoes a submit, so it has to undo all of one — submit moves the
        settings row and the gateway row together, and withdrawing only the
        named one would strand the other half in the approver's queue: the
        author would see a draft they could edit while a reviewer still saw a
        request waiting on them, for the same service.
        """
        row = await self._get_locked(queue_code)
        if row.status != S.SUBMIT:
            raise HTTPException(
                409, f"request is {row.status.value} — only a submitted one can be withdrawn"
            )
        if row.user_code != user_code:
            raise HTTPException(403, "only the submitter can withdraw a request")

        change_set = await self._change_set(row, (S.SUBMIT,))

        for member in change_set:
            # Someone else's submitted row against the same service is theirs to
            # withdraw, not this caller's — the ownership check above guards the
            # named row, and this guards the rest of the set.
            if member.user_code != user_code:
                continue
            member.status = S.DRAFT
            member.history = (member.history or []) + [
                _event(user_code, "withdrawn", comment)
            ]

        return next((r for r in change_set if r.code == row.code), row)

    # ── the end of the line: deploy what was approved ───────────────────────
    def verify_seal(self, row: TransactionQueueModel) -> bool:
        """Does the record still hold what was approved?

        False means config_snapshot changed after the approval — an edit made
        straight against the database, bypassing the flow entirely. That is
        precisely what the seal exists to catch.
        """
        if not row.approved_snapshot_hash:
            return False
        return row.approved_snapshot_hash == seal(row.config_snapshot)

    async def deploy(
        self, request: Request, user: str, user_code: str, tenant_code: str,
        queue_code: str,
    ):
        """approved -> deployed, but only if the seal still holds.

        The deploy itself is the existing Temporal path, untouched. What is new
        is the gate in front of it: FGA says who may deploy, and the seal says
        the bytes are the ones that were approved.
        """
        row = await self._get_locked(queue_code)
        if row.status != S.APPROVED:
            raise HTTPException(
                409, f"request is {row.status.value} — only an approved one can be deployed"
            )
        await require(
            request, user, "can_deploy", fga_ref(row), 403,
            denied("can_deploy", target_name(row)),
        )
        if not self.verify_seal(row):
            row.history = (row.history or []) + [_event(user_code, "seal-broken")]
            await self.db.commit()
            raise HTTPException(
                409,
                "This request changed after it was approved, so it will not be "
                "deployed. Send it back through review.",
            )

        # NOTHING is written to the live configuration here. This endpoint is
        # the gate — it answers "may this person deploy this exact, approved
        # change?" — and a gate that also mutated service_configs would make
        # the live row describe a deployment that had not happened yet, and
        # would keep describing it if the deploy then failed.
        #
        # The live row moves at each lane's real shipping point instead:
        #   prod      — after the PR is raised
        #               (ScriptPRWorkflowService._save_settings_for_batch)
        #   qa/stage  — after the deployment succeeds
        #               (save_deployed_settings, on the orchestrator's success
        #                path only, so a partial batch moves nothing)
        # Both go through ResourceSettingsSaverHandler, so there is one way to
        # write the live row rather than two that can drift.
        # Mark the change in flight, for the WHOLE change set. This is what
        # revoke reads: the status cannot serve, because it stays APPROVED
        # until a Temporal activity flips it seconds from now, and every deploy
        # path matches APPROVED exactly. Marking only the named row would leave
        # the other half of a settings+gateway change revocable while it ships.
        started = _now_dt()
        for member in await self._change_set(row, (S.APPROVED,)):
            member.deploy_started_at = started
        row.history = (row.history or []) + [_event(user_code, "deploy-started")]
        await self.db.commit()

        # Deliberately does NOT start the deployment. The caller runs the
        # existing deploy immediately after this returns, and doing both here
        # would ship the same change twice.
        #
        # So this endpoint is the GATE, not the deploy: it answers "may this
        # person deploy this exact, unmodified, approved change?" and makes the
        # approved values live. Everything after — HCL, pull request, Atlantis,
        # status writeback — stays with the pipeline that already owns it.
        return row

    async def reject(
        self, request: Request, user: str, user_code: str, queue_code: str,
        comment: str,
    ) -> TransactionQueueModel:
        """submit -> rejected, for the WHOLE change. Terminal — use
        request_changes unless the change should never happen.

        Refusing one row and leaving the other submitted would keep the service
        in review for a change that has been turned down.
        """
        if not comment.strip():
            raise HTTPException(400, "a reject needs a comment — tell the submitter why")
        row = await self._get_locked(queue_code)
        if row.status != S.SUBMIT:
            raise HTTPException(
                409, f"request is {row.status.value} — only submitted can be rejected"
            )
        await require(
            request, user, "can_approve", fga_ref(row), 403,
            denied("can_approve", target_name(row)),
        )
        change_set = await self._change_set(row, (S.SUBMIT,))
        for member in change_set:
            member.status = S.REJECTED
            member.decided_by = user_code
            member.decided_at = _now_dt()
            member.decision_comment = comment
            member.history = (member.history or []) + [
                _event(user_code, "rejected", comment)
            ]
        return next((r for r in change_set if r.code == row.code), row)
