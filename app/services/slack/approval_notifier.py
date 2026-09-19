"""Slack DMs for the approval flow — submitted / approved / sent back.

The maker-checker flow used to move silently: an author submitted and nobody
was told, an approver decided and the author found out by refreshing. These
notifications close that gap the same way the deploy pipeline's
`send_p0_alert` does — bot token, email -> Slack-ID lookup, DM through
`conversations_open` — but for the approval verbs, on their OWN bot
(APPROVAL_SLACK_BOT_TOKEN), not the Socket Mode chat app's SLACK_BOT_TOKEN.

DMs ONLY, no channel (decided explicitly): the audience is exact.
  - submitted           -> everyone holding can_approve on the service,
                           enumerated through OpenFGA ListUsers (proxied via
                           devlift-secret-config-manager), which resolves
                           user_group membership and resource_group
                           inheritance — a tuple read would miss those.
  - approved / sent back -> the row's author. (The terminal reject endpoint
    is not part of the product yet, so it carries no notification.)

EVERY failure here is caught and logged, never raised — the callers fire this
with asyncio.create_task after the decision is committed, and a Slack or
authz outage must not 500, roll back, or delay a decision that already
happened. No token configured -> silent no-op (local dev).
"""

import logging

from sqlalchemy import select

logger = logging.getLogger(__name__)


def _client():
    """AsyncWebClient, or None when the bot token is not configured."""
    from slack_sdk.web.async_client import AsyncWebClient

    from app.core.config import settings

    token = settings.approval_slack_bot_token
    if not token:
        logger.info("approval notifier: APPROVAL_SLACK_BOT_TOKEN not set — skipping")
        return None
    return AsyncWebClient(token=token)


async def _approver_codes(fga_object: str) -> list[str]:
    """user_mst codes holding can_approve on the service — asked of
    devlift-secret-config-manager's internal list-users endpoint, which fronts
    OpenFGA's ListUsers (resolves user_group membership and resource_group
    inheritance). The call lives HERE, not in app/core/authz/fga.py: that
    module is deliberately check-only, and this is a best-effort audience
    lookup, not an authorization decision. Fails EMPTY — an authz outage must
    not break the submit that asked."""
    import httpx

    from app.core.config import settings

    url = settings.secret_service_url.rstrip("/") + "/api/v1/internal/authz/list-users"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"object": fga_object, "relation": "can_approve"},
                headers={"X-Internal-Key": settings.secret_service_internal_key},
            )
        resp.raise_for_status()
        return [str(u) for u in resp.json()["users"]]
    except Exception as exc:
        logger.warning(
            "approval notifier: list-users failed for %s — nobody to DM: %s",
            fga_object, exc,
        )
        return []


async def _emails_for(codes: list[str]) -> dict[str, str]:
    """user_mst code -> email, for the codes that resolve. Own session: the
    request's session is committed and possibly closed by the time this runs."""
    from app.db.models.user_mst_model import UserMstModel
    from app.db.session import AsyncSessionLocal

    if not codes:
        return {}
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(UserMstModel).where(
                    UserMstModel.code.in_(codes),
                    UserMstModel.is_deleted == False,  # noqa: E712
                )
            )
        ).scalars().all()
        return {u.code: u.email_id for u in rows if u.email_id}


async def _dm(slack_client, email: str, text: str, blocks: list | None = None) -> bool:
    """One DM, best effort. True when it was actually sent. `blocks` is the
    Block Kit body; `text` doubles as the notification-preview fallback."""
    from app.services.slack.user_mapper import SlackUserMapper

    try:
        mapper = SlackUserMapper(db=None, slack_client=slack_client)
        slack_id = await mapper.get_slack_user_id_by_email(email)
        if not slack_id:
            logger.info("approval notifier: no Slack user for %s — skipped", email)
            return False
        dm = await slack_client.conversations_open(users=[slack_id])
        await slack_client.chat_postMessage(
            channel=dm["channel"]["id"], text=text, blocks=blocks,
            # No unfurl: the deep link otherwise grows a big site-preview card
            # under every DM, which dwarfs the message itself.
            unfurl_links=False, unfurl_media=False,
        )
        return True
    except Exception as exc:
        logger.warning("approval notifier: DM to %s failed: %s", email, exc)
        return False


async def _approvals_link(tenant_code: str, resource_code: str = "") -> str | None:
    """Deep link into the approvals view. Shape and tenant-slug resolution are
    owned by app.services.frontend_links."""
    from app.services.frontend_links import approvals_link, tenant_url_slug

    return approvals_link(
        tenant_slug=await tenant_url_slug(tenant_code),
        resource_code=resource_code,
    )


async def _service_panel_link(
    tenant_code: str, resource_code: str = "", environment: str = "", app_code: str = "",
) -> str | None:
    """Deep link to the CANVAS with the service's panel open. This is the
    approved-DM link: the author's next step is Deploy, and the Deploy button
    lives on this panel. Shape owned by app.services.frontend_links."""
    from app.services.frontend_links import service_panel_link, tenant_url_slug

    return service_panel_link(
        tenant_slug=await tenant_url_slug(tenant_code),
        resource_code=resource_code,
        environment=environment,
        app_code=app_code,
    )


def _blocks(
    header: str, body: str, queue_code: str,
    *, service_name: str = "", environment: str = "", region: str = "",
    link: str | None = None, link_label: str = "", reason: str = "",
) -> list:
    """The team's approval-message template, everywhere the same:

        <header>
        _<body sentence, italic>_
        🖥️ Service : <name>
        🎯 Environment : <env>
        🌐 Region : <region>
        Request `<code>`
        ───────────────

    with an optional link line between body and fields, only when the reader
    has a next action behind it. The trailing divider keeps a stack of these
    DMs readable — without it consecutive messages run together."""
    body_text = f"_{body}_"
    if reason:
        body_text += f'\n> Reason: "{reason}"'
    if link:
        body_text += f"\n<{link}|{link_label or 'Open it'}>"
    fields = []
    if service_name:
        fields.append(f"🖥️ *Service* : {service_name}")
    if environment:
        fields.append(f"🎯 *Environment* : {environment}")
    if region:
        fields.append(f"🌐 *Region* : {region}")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body_text}},
    ]
    if fields:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(fields)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Request `{queue_code}`"}]})
    blocks.append({"type": "divider"})
    return blocks


async def notify_submitted(
    *, queue_code: str, author_code: str, service_name: str,
    fga_object: str, actor_name: str, change_count: int,
    change_summary: str = "", tenant_code: str = "",
    environment: str = "", region: str = "",
) -> None:
    """DM every approver of the service that a change awaits their review."""
    try:
        slack_client = _client()
        if slack_client is None:
            return

        approver_codes = await _approver_codes(fga_object)
        # The author never reviews their own change — no point telling them
        # to (they can hold can_approve via a group without it applying here).
        approver_codes = [c for c in approver_codes if c != author_code]
        if not approver_codes:
            logger.info(
                "approval notifier: no approvers found for %s — nobody to DM",
                fga_object,
            )
            return

        emails = await _emails_for(approver_codes)
        n = change_count
        text = f"{actor_name} submitted {service_name} for approval — request {queue_code}"
        # fga_object is "service:<resource_code>" — the drawer deep-link key.
        link = (
            await _approvals_link(tenant_code, fga_object.partition(":")[2])
            if tenant_code else None
        )
        # Per-lane breakdown when the caller computed one ("2 configuration
        # changes, 1 gateway route, 1 variable & secrets"); flat count as the
        # fallback so an older caller still reads sensibly.
        detail = (
            f" — {change_summary}" if change_summary
            else (f" — {n} change{'s' if n != 1 else ''}" if n else "")
        )
        blocks = _blocks(
            "📥 Approval requested",
            f"*{actor_name}* has submitted a change request"
            + (f" ({detail.lstrip(' —')})" if detail else "")
            + " and it is awaiting your review.",
            queue_code,
            service_name=service_name, environment=environment, region=region,
            link=link, link_label="Review the request",
        )
        sent = 0
        for code in approver_codes:
            email = emails.get(code)
            if email and await _dm(slack_client, email, text, blocks):
                sent += 1
        logger.info(
            "approval notifier: submitted %s — DMed %d of %d approver(s)",
            queue_code, sent, len(approver_codes),
        )
    except Exception as exc:
        logger.warning("approval notifier: notify_submitted failed: %s", exc)


_BROADCAST_TEXT = {
    "approved": (
        "✅ Request approved",
        "{actor} has reviewed and approved the pending change on this "
        "service. It is cleared for deployment — no further action is "
        "required from you.",
    ),
    "rejected": (
        "⛔ Request rejected",
        "{actor} has reviewed and rejected the pending change on this "
        "service. It has been returned to the author for revision — no "
        "further action is required from you.",
    ),
    # Unlike the two above, this REOPENS the review: the request returns to
    # waiting, so the other approvers are being asked again, not stood down.
    "approval-revoked": (
        "↩️ Approval revoked",
        "{actor} has revoked the approval of the pending change on this "
        "service. The request is awaiting review again.",
    ),
}


async def notify_approvers_decided(
    *, verb: str, queue_code: str, author_code: str, actor_code: str,
    service_name: str, fga_object: str, actor_name: str,
    comment: str = "", environment: str = "", region: str = "",
) -> None:
    """Tell the OTHER approvers a request they were asked to review is done.

    The submit DM asked every approver to review; once one of them decides —
    approve or reject — the rest still hold an open "Approval requested" in
    their Slack. This is its closure — same audience rule as submit (all
    can_approve holders), minus the author (they get their own DM) and minus
    the decider (it was their click)."""
    try:
        slack_client = _client()
        if slack_client is None:
            return
        header, template = _BROADCAST_TEXT.get(verb, (None, None))
        if template is None:
            logger.warning("approval notifier: unknown broadcast verb %r", verb)
            return
        codes = [
            c for c in await _approver_codes(fga_object)
            if c not in (author_code, actor_code)
        ]
        if not codes:
            return
        emails = await _emails_for(codes)
        text = f"{actor_name} {verb} the pending change on {service_name}"
        blocks = _blocks(
            header, template.format(actor=f"*{actor_name}*"), queue_code,
            service_name=service_name, environment=environment, region=region,
            reason=comment,
        )
        sent = 0
        for code in codes:
            email = emails.get(code)
            if email and await _dm(slack_client, email, text, blocks):
                sent += 1
        logger.info(
            "approval notifier: %s %s — DMed %d other approver(s)",
            verb, queue_code, sent,
        )
    except Exception as exc:
        logger.warning("approval notifier: notify_approvers_decided failed: %s", exc)


async def notify_withdrawn(
    *, queue_code: str, author_code: str, service_name: str,
    fga_object: str, actor_name: str,
    environment: str = "", region: str = "",
) -> None:
    """The corrective follow-up to notify_submitted: the approvers were told
    a change awaits them, and the author has since pulled it back — without
    this, the review request stays standing in their DMs forever."""
    try:
        slack_client = _client()
        if slack_client is None:
            return
        approver_codes = [
            c for c in await _approver_codes(fga_object) if c != author_code
        ]
        if not approver_codes:
            return
        emails = await _emails_for(approver_codes)
        text = f"{actor_name} withdrew {service_name} — nothing to review"
        blocks = _blocks(
            "↩️ Request withdrawn",
            f"*{actor_name}* has withdrawn their change request from review. "
            "No action is needed on your part — you will be notified if a "
            "revised request is submitted.",
            queue_code,
            service_name=service_name, environment=environment, region=region,
        )
        sent = 0
        for code in approver_codes:
            email = emails.get(code)
            if email and await _dm(slack_client, email, text, blocks):
                sent += 1
        logger.info(
            "approval notifier: withdrawn %s — DMed %d approver(s)",
            queue_code, sent,
        )
    except Exception as exc:
        logger.warning("approval notifier: notify_withdrawn failed: %s", exc)


_DECISION_TEXT = {
    "approved": (
        "✅ Request approved",
        "has approved your change request. It is now cleared for deployment.",
    ),
    # "Rejected", the product's word: this system's Reject IS request-changes
    # (recoverable — the change returns as a draft); there is no terminal
    # reject in the product.
    "request-changes": (
        "⛔ Request rejected",
        "has rejected your change request. It has been returned to you as a "
        "draft — please address the feedback and resubmit.",
    ),
    # The corrective follow-up for a ✅ already delivered: without it the
    # author's last word from us is "cleared to deploy" about a change that
    # no longer is.
    "approval-revoked": (
        "↩️ Approval revoked",
        "has revoked the approval. The request is pending review again — "
        "please hold any deployment.",
    ),
}


async def notify_decided(
    *, verb: str, queue_code: str, author_code: str,
    service_name: str, actor_name: str, comment: str = "",
    actor_code: str = "",
    action_link: str | None = None, action_label: str = "",
    environment: str = "", region: str = "",
) -> None:
    """DM the author that a reviewer decided on their change.

    `action_link`/`action_label` add a navigation line when the CALLER knows
    the reader has a next action behind it — the approve endpoint passes the
    service-panel link (Deploy is the next step); sent-back and revoked pass
    nothing, they are news."""
    try:
        # A decision is NEWS to the other party. A self-decision (an
        # author-approver approving/revoking their own change) has no other
        # party — DMing someone about their own click is noise, mirror of
        # the submit DM excluding the author.
        if actor_code and actor_code == author_code:
            logger.info(
                "approval notifier: %s %s — self-decision, no DM",
                verb, queue_code,
            )
            return
        slack_client = _client()
        if slack_client is None:
            return
        header, template = _DECISION_TEXT.get(verb, (None, None))
        if template is None:
            logger.warning("approval notifier: unknown verb %r — skipped", verb)
            return

        emails = await _emails_for([author_code])
        email = emails.get(author_code)
        if not email:
            logger.info(
                "approval notifier: author %s has no email — skipped", author_code
            )
            return

        body = f"*{actor_name}* " + template
        text = f"{header}: {service_name} — request {queue_code}"
        blocks = _blocks(
            header, body, queue_code,
            service_name=service_name, environment=environment, region=region,
            link=action_link, link_label=action_label, reason=comment,
        )
        if await _dm(slack_client, email, text, blocks):
            logger.info(
                "approval notifier: %s %s — author DMed", verb, queue_code
            )
    except Exception as exc:
        logger.warning("approval notifier: notify_decided failed: %s", exc)
