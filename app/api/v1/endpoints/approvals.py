"""
Change Approval Endpoints

A NEW surface. The existing transaction-queue routes are untouched and keep
working exactly as they do in production; the approvals page talks to these.

    GET   /approvals/services                landing page — services with
                                             changes you may review
    GET   /approvals                         the requests themselves
    GET   /approvals/{code}                  one request with its history
    POST  /approvals/{code}/approve          submit -> approved
    POST  /approvals/{code}/revoke           approved -> submit (undo a decision)
    POST  /approvals/{code}/discard          draft -> gone (author bins their own work)
    POST  /approvals/{code}/request-changes  submit -> draft (recoverable)
    POST  /approvals/{code}/reject           submit -> rejected (terminal)

Getting a request INTO `submit` is not part of this release — seed rows for
now. Withdraw, discard, revoke and the deploy-time seal check follow later.

Routes hold no authorization logic and run no queries: they call
ApprovalService, which calls require(), which asks OpenFGA. The cards are
AuthenticationOnly because the resource being checked lives inside the stored
record — not in the URL or body — so a route-level guard cannot resolve it in
advance.
"""

import logging
from typing import Optional, Tuple

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user_and_tenant, get_db
from app.core.authz.security import (
    AuthenticationOnly,
    Authorization,
    SecureRouter,
    authenticated_user,
    mark_checked,
)
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.db.models.user_mst_model import UserMstModel
from app.schemas.approval_schemas import (
    ActionResponse,
    ApprovalItem,
    ApprovalListResponse,
    ApprovalServiceListResponse,
    ApprovalServiceSummary,
    DecisionIn,
    DeployResponse,
    GatewayDraftSaveIn,
    RequiredCommentIn,
    SettingsDraftSaveIn,
    YouFlags,
)
from app.services.approval_service import ApprovalService, fga_ref, target_name

logger = logging.getLogger(__name__)

router = SecureRouter()

UserAndTenant = Tuple[UserMstModel, TenantsMstModel]


def _service_label(row: TransactionQueueModel) -> str:
    """The bare service name for a Slack DM's "Service :" field.

    target_name() serves UI row titles, where a gateway row reads
    "Gateway routes - <service>" and a variables row "update variables :
    <service> ..." — right for a list, wrong after "Service :", which must
    name only the service. The snapshot's own service_name is the clean
    source; the prefixes are stripped only as a fallback."""
    snap = row.config_snapshot or {}
    name = snap.get("service_name")
    if name:
        return str(name)
    name = str(row.display_name or row.transaction_code or "")
    low = name.lower()
    if low.startswith("gateway routes") and "-" in name:
        return name.split("-", 1)[1].strip() or name
    if low.startswith("update variables") and ":" in name:
        tail = name.split(":", 1)[1].strip()
        return tail.split(" ")[0] or name
    return name


async def _notify_context(db: AsyncSession, row: TransactionQueueModel) -> tuple:
    """(service_name, environment, region) for a DM's field lines.

    Settings and gateway snapshots carry all three; a VARIABLES row stores
    only its draft-file pointer — for it (and any other gap) the
    service_configs row answers, joined to services_mst for the bare name.
    Best effort: a lookup failure degrades to the display-name fallback,
    never blocks the notification."""
    snap = row.config_snapshot or {}
    name = snap.get("service_name")
    env = snap.get("environment")
    region = snap.get("geo_loc_mst_code")
    if not (name and env and region):
        try:
            from sqlalchemy import text as _text

            r = (await db.execute(_text(
                "SELECT sm.name, sc.environment, sc.geo_loc_mst_code "
                "FROM service_configs sc "
                "JOIN services_mst sm ON sm.code = sc.services_mst_code "
                "WHERE sc.code = :c"
            ), {"c": row.transaction_code})).first()
            if r:
                name = name or r[0]
                env = env or getattr(r[1], "value", r[1])
                region = region or r[2]
        except Exception:
            logger.warning(
                "notify context lookup failed for %s", row.transaction_code,
                exc_info=True,
            )
    return str(name or _service_label(row)), str(env or ""), str(region or "")


def _actor_name(db_user: UserMstModel, fallback: str) -> str:
    """The decision-maker's display name for a Slack message."""
    full = f"{db_user.first_name or ''} {db_user.last_name or ''}".strip()
    return full or db_user.email_id or fallback


def _fire_notification(coro) -> None:
    """Run a Slack notification after the response, fire-and-forget.

    create_task, not await: the decision is already committed, and a Slack or
    authz outage must neither 500 this request nor slow it down. The notifier
    catches its own errors, so a lost task can at worst be silent."""
    import asyncio

    try:
        asyncio.create_task(coro)
    except RuntimeError:  # no running loop — tests calling endpoints directly
        logger.warning("approval notification skipped: no running event loop")


def _to_item(
    row: TransactionQueueModel,
    flags: dict,
    names: Optional[dict] = None,
    previews: Optional[dict] = None,
) -> ApprovalItem:
    """Model row + resolved permissions -> the shape the UI renders.

    `names` maps user_code -> display name (see ApprovalService.actor_names).
    Every actor is stored as a code, so without it the UI can only print UUIDs.

    `previews` maps queue code -> a live diff for rows that are still drafts
    (see ApprovalService.draft_previews). The stored `changes` is frozen at
    submit, which is right for a submitted request and stale for one that was
    withdrawn back to a draft and edited since.

    Both optional, and both fall back to what the row already holds, so a
    caller that resolved neither still gets a valid record.
    """
    names = names or {}
    return ApprovalItem(
        id=row.id,
        code=row.code,
        status=getattr(row.status, "value", str(row.status)),
        resource_type=getattr(row.table_name, "value", row.table_name),
        # The discriminator WITHIN a table: variables rows are SERVICE_CONFIG
        # too, told apart only by case_ref_code='update_variables'. Without it
        # the UI has no way to label or count the three kinds of change.
        case_ref_code=row.case_ref_code,
        resource_code=row.transaction_code,
        display_name=row.display_name,
        requested_by=row.user_code,
        requested_by_name=names.get(row.user_code),
        # Passed through as datetimes — Pydantic serialises them, same as every
        # other schema here. Stringifying first would fight the field type.
        requested_at=row.created_at,
        changes=(previews or {}).get(row.code, row.changes or {}),
        config_snapshot=row.config_snapshot or {},
        decided_by=row.decided_by,
        decided_by_name=names.get(row.decided_by),
        decided_at=row.decided_at,
        decision_comment=row.decision_comment,
        # The stored event is left intact and the name added beside it: the log
        # is append-only, and `by` is what a later reader can still resolve.
        history=[
            {**event, "by_name": names.get(event.get("by"))}
            for event in (row.history or [])
        ],
        you=YouFlags(**flags),
    )


async def _commit_and_respond(
    db: AsyncSession, service: ApprovalService, user: str, user_code: str,
    row: TransactionQueueModel, detail: str,
) -> ActionResponse:
    """Every decision ends the same way: commit, re-resolve the caller's flags
    so the UI can redraw its buttons, return the updated record."""
    await db.commit()
    if row is None:
        # A save that queued NOTHING — the settings half matched the live
        # config and the guard declined to park an empty request, and there
        # was no gateway half either. That is a success, not an error: the
        # caller's edits equal what is already running, so there is no record
        # to return. ActionResponse.approval is Optional for exactly this
        # shape of outcome (see the schema's own comment on the gateway
        # empty-list drop).
        return ActionResponse(
            ok=True, approval=None,
            detail="nothing changed — your values match what is already "
                   "deployed, so no request was queued",
        )
    flags = await service.flags_for(user, user_code, row)
    names = await service.actor_names([row])
    previews = await service.draft_previews([row])
    return ActionResponse(
        ok=True, approval=_to_item(row, flags, names, previews), detail=detail
    )


# ── level 1: the landing page ───────────────────────────────────────────────
# Declared BEFORE /{queue_code} so "services" is not swallowed as a code.
@router.get(
    "/services",
    response_model=ApprovalServiceListResponse,
    summary="Services with changes you can review",
    access=AuthenticationOnly(reason="visibility comes from OpenFGA ListObjects"),
)
async def list_services_awaiting(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """One row per service_config you hold `can_approve` on that has requests.

    Click one, then call GET /approvals?resource_code=<resource_code> for its
    change requests.
    """
    db_user, tenant = user_tenant
    rows = await ApprovalService(db).list_services_awaiting(
        user=user, user_code=db_user.code, tenant_code=tenant.code
    )
    mark_checked(request)
    return ApprovalServiceListResponse(
        services=[ApprovalServiceSummary(**r) for r in rows], total=len(rows)
    )


# Declared BEFORE /{queue_code}, like /services, so the literal path is not
# read as a queue code.
@router.get(
    "/permissions",
    response_model=YouFlags,
    summary="What you may do to one service",
    access=AuthenticationOnly(reason="the check itself is the authorization"),
)
async def resource_permissions(
    request: Request,
    resource_code: str = Query(..., description="service_config code"),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
):
    """Answers before anything is created.

    The settings form mints a ticket on its way to the first save, so a
    permission refusal that only arrives with the save leaves that ticket
    behind in the name of someone who was not allowed to make it. This lets the
    form ask first. It is convenience, not enforcement — every write endpoint
    still calls require() for itself.
    """
    flags = await ApprovalService(db).flags_for_resource(user, resource_code)
    mark_checked(request)
    return YouFlags(**flags)


# Declared BEFORE /{queue_code}, like /services, so "history" is not read as a
# queue code.
@router.get(
    "/history",
    response_model=ApprovalListResponse,
    summary="Every change request ever raised against one service",
    access=AuthenticationOnly(reason="per-record visibility resolved in the service"),
)
async def list_resource_history(
    request: Request,
    resource_code: str = Query(..., description="service_config code"),
    limit: int = Query(
        15, ge=1, le=100,
        description="Most recent requests to return. The history is bounded — "
                    "an old service has more of them than anyone reads.",
    ),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """The service's recent record, not just what is still open.

    GET /approvals stops at the states someone can still act on, because it
    feeds a work queue. A history has the opposite job: a deployed, rejected or
    withdrawn request is exactly the part of the story a queue drops, so this
    returns every row and lets the reader see how the service got here.
    """
    db_user, tenant = user_tenant
    service = ApprovalService(db)
    pairs = await service.list_resource_history(
        user=user,
        user_code=db_user.code,
        tenant_code=tenant.code,
        resource_code=resource_code,
        limit=limit,
    )
    mark_checked(request)
    names = await service.actor_names([row for row, _ in pairs])
    # OWN drafts only. The history now shows a foreign draft that was once
    # submitted (its reviewed record is public), but a live preview of what its
    # author is editing RIGHT NOW is not — the frozen per-event diffs are what
    # the log is made of.
    previews = await service.draft_previews(
        [row for row, flags in pairs if flags.get("mine")]
    )
    items = [_to_item(row, flags, names, previews) for row, flags in pairs]
    return ApprovalListResponse(approvals=items, total=len(items))


# ── level 2: the requests ───────────────────────────────────────────────────
@router.get(
    "",
    response_model=ApprovalListResponse,
    summary="Change requests you submitted, or can act on",
    access=AuthenticationOnly(reason="per-record visibility resolved in the service"),
)
async def list_approvals(
    request: Request,
    status: Optional[str] = Query(
        None, description="draft | submit | approved | rejected"
    ),
    resource_code: Optional[str] = Query(
        None, description="Only requests against this resource"
    ),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """Drafts stay private to whoever wrote them. Everything else appears if it
    is yours, or you may approve it, or you may deploy it."""
    db_user, tenant = user_tenant
    pairs = await ApprovalService(db).list_inbox(
        user=user,
        user_code=db_user.code,
        tenant_code=tenant.code,
        status=status,
        resource_code=resource_code,
    )
    mark_checked(request)
    # One lookup for the whole page rather than one per row.
    service = ApprovalService(db)
    names = await service.actor_names([row for row, _ in pairs])
    previews = await service.draft_previews([row for row, _ in pairs])
    items = [_to_item(row, flags, names, previews) for row, flags in pairs]
    return ApprovalListResponse(approvals=items, total=len(items))


@router.get(
    "/{queue_code}",
    response_model=ApprovalItem,
    summary="One change request",
    access=AuthenticationOnly(reason="visibility resolved against the stored record"),
)
async def get_approval(
    queue_code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    db_user, tenant = user_tenant
    service = ApprovalService(db)
    row = await service.repo.get_by_code(queue_code, tenant_code=tenant.code)
    if row is None:
        raise HTTPException(404, f"change request {queue_code} not found")

    flags = await service.flags_for(user, db_user.code, row)
    if not flags["can_approve"]:
        # 404 not 403 — do not confirm it exists to someone with no business
        # knowing that.
        raise HTTPException(404, f"change request {queue_code} not found")

    mark_checked(request)
    return _to_item(
        row, flags,
        await service.actor_names([row]),
        await service.draft_previews([row]),
    )


# ── the maker's half: draft saves, one endpoint per half ────────────────────
# These REPLACED the combined POST /approvals/draft (removed — the frontend's
# Save was its only caller). Same ApprovalService.save_draft underneath, so
# the permission (can_update), the lane lock and the history logging are
# identical — the split changes who carries the payload, not how it is
# decided. The rows still land in one change set: submit, approve and
# withdraw keep moving them together. Creates draft queue rows and NOTHING
# else — service_configs and the Kong route tables are written at deploy,
# after approval, so the live rows keep showing what actually runs.
#
# Mounted at /transaction (see router.py), OUTSIDE /approvals, because these
# are the maker writing a draft — not an approval verb.
transaction_router = SecureRouter()


@transaction_router.post(
    "/service-settings/{transaction_code}",
    response_model=ActionResponse,
    summary="Save the settings half of a change as a draft",
    # Declared HERE, on the route: OpenFGA can_write_settings on
    # service:{path param}, checked by the guard before the handler runs.
    # save_draft does NOT re-check — the card is the only check, and it is a
    # more precise one than the service method could make: this route knows it
    # is saving the settings half, while save_draft sees both halves at once.
    access=Authorization(
        permission="can_write_settings",
        obj_type="service",
        param="transaction_code",
        deny_status=403,
    ),
)
async def save_settings_draft(
    transaction_code: str,
    body: SettingsDraftSaveIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """The settings half of /approvals/draft, addressed by service_config code."""
    db_user, tenant = user_tenant
    service = ApprovalService(db)
    row = await service.save_draft(
        request, user, db_user.code, tenant.code,
        transaction_code=transaction_code,
        config_snapshot=body.config_snapshot,
        gateway_groups=None,
        case_ref_code=body.case_ref_code,
        queue_code=body.queue_code,
        ticket_code=body.ticket_code,
    )
    return await _commit_and_respond(
        db, service, user, db_user.code, row, "saved as draft (settings)"
    )


@transaction_router.post(
    "/kong-gateway/{transaction_code}",
    response_model=ActionResponse,
    summary="Save the gateway-routes half of a change as a draft",
    # Same object as service-settings — routes belong to the service they route
    # to (the FGA_TYPE comment in approval_service.py documents that choice) —
    # but a SEPARATE relation: can_write_gateway, not can_write_settings. The
    # two halves are granted independently, so holding one does not carry the
    # other. Like the settings route, save_draft does not re-check.
    access=Authorization(
        permission="can_write_gateway",
        obj_type="service",
        param="transaction_code",
        deny_status=403,
    ),
)
async def save_gateway_draft(
    transaction_code: str,
    body: GatewayDraftSaveIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """The gateway half of /approvals/draft, addressed by service_config code.

    An empty gateway_groups list is the undo: the pending Kong row is dropped,
    and the response carries no approval record because none remains.
    """
    db_user, tenant = user_tenant
    service = ApprovalService(db)
    row = await service.save_draft(
        request, user, db_user.code, tenant.code,
        transaction_code=transaction_code,
        config_snapshot=None,
        gateway_groups=body.gateway_groups,
    )
    if row is None:
        # Every route edit was undone — the save succeeded by REMOVING the
        # pending row. _commit_and_respond needs a record; there isn't one.
        await db.commit()
        return ActionResponse(
            ok=True, approval=None,
            detail="route edits cleared — nothing pending for the gateway",
        )
    return await _commit_and_respond(
        db, service, user, db_user.code, row, "saved as draft (gateway routes)"
    )


@router.post(
    "/{queue_code}/submit",
    response_model=ActionResponse,
    summary="Send a draft for review",
    access=AuthenticationOnly(reason="resource lives in the record; require() in the service"),
)
async def submit_approval(
    queue_code: str,
    body: DecisionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """draft -> submit. The from/to diff is frozen at this moment, so a
    reviewer decides on what was submitted rather than a recomputation."""
    db_user, _ = user_tenant
    service = ApprovalService(db)
    row = await service.submit(request, user, db_user.code, queue_code, body.comment)
    resp = await _commit_and_respond(
        db, service, user, db_user.code, row, "submitted for review"
    )
    if row is not None:
        # Values captured NOW, while the session is open — the task runs after
        # the response, when touching the ORM row would be unsafe.
        from app.services.slack.approval_notifier import notify_submitted

        # The submit moved the WHOLE change set (settings + gateway routes +
        # variables move together), so the DM itemises it per lane instead of
        # one flat count: "2 configuration changes, 1 gateway route,
        # 1 variable/secret". Rows are told apart by case_ref_code — every
        # lane is SERVICE_CONFIG in the table now.
        parts: list[str] = []
        try:
            from app.db.models.transaction_queue_model import (
                TransactionQueueStatusEnum as _S,
            )
            from app.services.approval_service import GATEWAY_CASE_REF

            members = await service._change_set(row, (_S.SUBMIT,))
            for m in members:
                ch = m.changes or {}
                if m.case_ref_code == GATEWAY_CASE_REF:
                    n = sum(
                        len(g.get("paths") or []) or 1
                        for g in (ch.get("groups") or [])
                        if isinstance(g, dict)
                    )
                    if n:
                        parts.append(f"{n} gateway route{'s' if n != 1 else ''}")
                elif m.case_ref_code == "update_variables":
                    n = len(ch.get("variables") or [])
                    if n:
                        parts.append(f"{n} variable{'s' if n != 1 else ''} & secrets")
                else:
                    n = len(ch)
                    if n:
                        parts.append(f"{n} configuration change{'s' if n != 1 else ''}")
        except Exception:  # the breakdown is decoration — never block the DM
            logger.warning("submit notification: change breakdown failed", exc_info=True)

        _svc, _env, _region = await _notify_context(db, row)
        _fire_notification(notify_submitted(
            queue_code=row.code,
            author_code=row.user_code,
            service_name=_svc,
            fga_object=fga_ref(row),
            actor_name=_actor_name(db_user, user),
            change_count=len(row.changes or {}),
            change_summary=", ".join(parts),
            tenant_code=row.tenant_code or "",
            environment=_env,
            region=_region,
        ))
    return resp


@router.post(
    "/{queue_code}/discard",
    response_model=ActionResponse,
    summary="Throw a draft away",
    access=AuthenticationOnly(reason="resource lives in the record; require() in the service"),
)
async def discard_approval(
    queue_code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """Draft-only and author-only. The WHOLE draft goes: settings and route
    rows here, and the variables half (staged file, never-deployed rows, the
    variables queue row) through devlift-secret, which owns it. One call from
    the client, whichever tab it was pressed on."""
    db_user, _ = user_tenant
    service = ApprovalService(db)
    row = await service.discard(request, user, db_user.code, queue_code)
    if row is None:
        # Variables-only draft: devlift-secret retired the clicked row itself,
        # so there is no record of ours to hand back.
        await db.commit()
        return ActionResponse(ok=True, approval=None, detail="draft discarded")
    return await _commit_and_respond(
        db, service, user, db_user.code, row, "draft discarded"
    )


@router.post(
    "/{queue_code}/withdraw",
    response_model=ActionResponse,
    summary="Pull your own request back out of review",
    access=AuthenticationOnly(reason="ownership checked in the service"),
)
async def withdraw_approval(
    queue_code: str,
    body: DecisionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """submit -> draft, by the submitter. A reviewer pushing it back is
    request-changes instead."""
    db_user, _ = user_tenant
    service = ApprovalService(db)
    row = await service.withdraw(request, user, db_user.code, queue_code, body.comment)
    resp = await _commit_and_respond(
        db, service, user, db_user.code, row, "withdrawn — back with you as a draft"
    )
    if row is not None:
        # Corrects the submit DM already sitting in the approvers' Slack —
        # otherwise "Approval requested" stands for a change nobody can review.
        from app.services.slack.approval_notifier import notify_withdrawn

        _svc, _env, _region = await _notify_context(db, row)
        _fire_notification(notify_withdrawn(
            queue_code=row.code,
            author_code=row.user_code,
            service_name=_svc,
            fga_object=fga_ref(row),
            actor_name=_actor_name(db_user, user),
            environment=_env,
            region=_region,
        ))
    return resp


# ── the three decisions ─────────────────────────────────────────────────────
@router.post(
    "/{queue_code}/approve",
    response_model=ActionResponse,
    summary="Approve a change request",
    access=AuthenticationOnly(reason="resource lives in the record; require() in the service"),
)
async def approve_approval(
    queue_code: str,
    body: DecisionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    db_user, _ = user_tenant
    service = ApprovalService(db)
    row = await service.approve(request, user, db_user.code, queue_code, body.comment)
    resp = await _commit_and_respond(
        db, service, user, db_user.code, row, "approved — ready to deploy"
    )
    if row is not None:
        from app.services.slack.approval_notifier import notify_decided

        from app.services.slack.approval_notifier import _service_panel_link

        # Approved means the author's next step is Deploy — link them to the
        # CANVAS service panel (not the approvals drawer), where that button is.
        snapshot = row.config_snapshot or {}
        _svc, _env, _region = await _notify_context(db, row)
        # Close the other approvers' open "Approval requested" DMs — one
        # colleague's approval ends the review for everyone.
        from app.services.slack.approval_notifier import notify_approvers_decided

        _fire_notification(notify_approvers_decided(
            verb="approved",
            queue_code=row.code,
            author_code=row.user_code,
            actor_code=db_user.code,
            service_name=_svc,
            fga_object=fga_ref(row),
            actor_name=_actor_name(db_user, user),
            comment=body.comment or "",
            environment=_env,
            region=_region,
        ))
        _fire_notification(notify_decided(
            verb="approved",
            queue_code=row.code,
            author_code=row.user_code,
            service_name=_svc,
            actor_name=_actor_name(db_user, user),
            actor_code=db_user.code,
            comment=body.comment or "",
            action_link=await _service_panel_link(
                row.tenant_code or "",
                row.transaction_code or "",
                environment=_env,
                app_code=str(
                    snapshot.get("applications_mst_code")
                    or snapshot.get("application_code") or ""
                ),
            ),
            action_label="Open the service to deploy",
            environment=_env,
            region=_region,
        ))
    return resp


@router.post(
    "/{queue_code}/revoke",
    response_model=ActionResponse,
    summary="Revoke an approval",
    access=AuthenticationOnly(reason="resource lives in the record; require() in the service"),
)
async def revoke_approval(
    queue_code: str,
    body: DecisionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """Approved by mistake, or something came up. Returns the change to WAITING
    rather than to its author: the request is not in question, only the
    decision. Refused once a deploy has started."""
    db_user, _ = user_tenant
    service = ApprovalService(db)
    row = await service.revoke(request, user, db_user.code, queue_code, body.comment)
    resp = await _commit_and_respond(
        db, service, user, db_user.code, row, "approval revoked — waiting for a decision again"
    )
    if row is not None:
        # Corrects the ✅ already in the author's Slack — their last word from
        # us must not stay "cleared to deploy" about a change that no longer is.
        from app.services.slack.approval_notifier import notify_decided

        _svc, _env, _region = await _notify_context(db, row)
        # The revoke REOPENS the review — tell the other approvers it is
        # waiting on them again, not just the author.
        from app.services.slack.approval_notifier import notify_approvers_decided

        _fire_notification(notify_approvers_decided(
            verb="approval-revoked",
            queue_code=row.code,
            author_code=row.user_code,
            actor_code=db_user.code,
            service_name=_svc,
            fga_object=fga_ref(row),
            actor_name=_actor_name(db_user, user),
            comment=body.comment or "",
            environment=_env,
            region=_region,
        ))
        _fire_notification(notify_decided(
            verb="approval-revoked",
            queue_code=row.code,
            author_code=row.user_code,
            service_name=_svc,
            actor_name=_actor_name(db_user, user),
            actor_code=db_user.code,
            comment=body.comment or "",
            environment=_env,
            region=_region,
        ))
    return resp


@router.post(
    "/{queue_code}/request-changes",
    response_model=ActionResponse,
    summary="Send it back to the submitter",
    access=AuthenticationOnly(reason="resource lives in the record; require() in the service"),
)
async def request_changes_approval(
    queue_code: str,
    body: RequiredCommentIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """Recoverable, unlike reject: it returns to the submitter as a draft with
    every value intact."""
    db_user, _ = user_tenant
    service = ApprovalService(db)
    row = await service.request_changes(
        request, user, db_user.code, queue_code, body.comment
    )
    resp = await _commit_and_respond(
        db, service, user, db_user.code, row, "sent back to the submitter"
    )
    if row is not None:
        from app.services.slack.approval_notifier import notify_decided

        from app.services.slack.approval_notifier import _service_panel_link

        # Sent back means the author's next step is EDIT — same canvas panel,
        # different verb on the button.
        snapshot = row.config_snapshot or {}
        _svc, _env, _region = await _notify_context(db, row)
        # The other approvers' open "Approval requested" DMs need closure too —
        # one colleague's rejection ends the review for everyone.
        from app.services.slack.approval_notifier import notify_approvers_decided

        _fire_notification(notify_approvers_decided(
            verb="rejected",
            queue_code=row.code,
            author_code=row.user_code,
            actor_code=db_user.code,
            service_name=_svc,
            fga_object=fga_ref(row),
            actor_name=_actor_name(db_user, user),
            comment=body.comment or "",
            environment=_env,
            region=_region,
        ))
        _fire_notification(notify_decided(
            verb="request-changes",
            queue_code=row.code,
            author_code=row.user_code,
            service_name=_svc,
            actor_name=_actor_name(db_user, user),
            actor_code=db_user.code,
            comment=body.comment or "",
            action_link=await _service_panel_link(
                row.tenant_code or "",
                row.transaction_code or "",
                environment=_env,
                app_code=str(
                    snapshot.get("applications_mst_code")
                    or snapshot.get("application_code") or ""
                ),
            ),
            action_label="Open the service to make the changes",
            environment=_env,
            region=_region,
        ))
    return resp


@router.post(
    "/{queue_code}/reject",
    response_model=ActionResponse,
    summary="Reject a change request (terminal)",
    access=AuthenticationOnly(reason="resource lives in the record; require() in the service"),
)
async def reject_approval(
    queue_code: str,
    body: RequiredCommentIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """There is no path back. Use request-changes unless the change should
    never happen."""
    db_user, _ = user_tenant
    service = ApprovalService(db)
    row = await service.reject(request, user, db_user.code, queue_code, body.comment)
    # No notification: the terminal reject is not part of the product yet (the
    # UI never calls it — its "Reject request" is request-changes above).
    return await _commit_and_respond(db, service, user, db_user.code, row, "rejected")


# ── the end of the line ─────────────────────────────────────────────────────
@router.post(
    "/{queue_code}/deploy",
    response_model=DeployResponse,
    summary="Deploy an approved change",
    access=AuthenticationOnly(reason="can_deploy resolved against the stored record"),
)
async def deploy_approval(
    queue_code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(authenticated_user),
    user_tenant: UserAndTenant = Depends(get_current_user_and_tenant),
):
    """The GATE in front of a deploy, not the deploy.

    Three questions, in order: is this change approved, may this person deploy
    it, and is it still the exact thing that was approved? Passing all three,
    the approved snapshot is written into service_configs — the only moment a
    proposal becomes the live configuration.

    It does NOT start the deployment. The caller runs the existing deploy right
    after this returns, so the pipeline that already owns HCL, pull requests and
    Atlantis keeps owning them; doing it here as well would ship twice.
    """
    db_user, tenant = user_tenant
    service = ApprovalService(db)
    row = await service.deploy(request, user, db_user.code, tenant.code, queue_code)
    flags = await service.flags_for(user, db_user.code, row)
    return DeployResponse(
        ok=True,
        approval=_to_item(row, flags, await service.actor_names([row])),
        deploy={},
        detail="cleared to deploy — approved values are now live",
    )
