"""
Admin Dashboard (deploy tracker) API

Aggregated deployment statistics for the admin-only Admin Dashboard view:
KPI totals, breakdowns by target resource type / environment / deploy kind,
and the most-deployed services — all over an explicit [date_from, date_to]
window. The deployment list itself stays on GET /deployments/history and the
single-deployment detail on GET /deployments/history/{workflow_id}; this
module only serves aggregates.

Data model recap (same grouping rule as deployment_history.py): one
deployment = the set of pipeline_run_track rows sharing a
vendor_deployment_id, resolved through pipeline_mst to the polymorphic
resource tables. The deploy-kind predicates are imported from
deployment_history.DEPLOY_TYPE_SQL so the two surfaces can never disagree.

Resource semantics (user-confirmed): "gateway" is a deploy KIND, not a
resource — a gateway-route deploy counts under the OWNING SERVICE's compute
platform (its service_config.infrastructuretype_ref_code, e.g. EKS/ECS),
resolved via kong_route_{configs,groups}.services_mst_code.

Access: org owners always; plus the emails in DEPLOY_TRACKER_ADMIN_EMAILS.
This is the server-side boundary — the frontend allowlist
(NEXT_PUBLIC_DEPLOY_TRACKER_ALLOWED_EMAILS) only hides the menu entry.
"""
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.api.v1.endpoints.deployment_history import DEPLOY_TYPE_SQL, _TQ_CODES_ARRAY
from app.core.config import settings
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.schemas.deploy_tracker_schemas import (
    CountBucket,
    DeployTrackerStatsResponse,
    DeployTrackerTotals,
    SlackReportRequest,
    SlackReportResponse,
    TopService,
    TopUser,
)

router = APIRouter()


def _admin_emails() -> set[str]:
    return {
        e.strip().lower()
        for e in (settings.deploy_tracker_admin_emails or "").split(",")
        if e.strip()
    }


async def require_deploy_tracker_admin(
    current_user_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
) -> Tuple[UserMstModel, TenantsMstModel]:
    """Org owners (user_mst.is_org_owner) or the DEPLOY_TRACKER_ADMIN_EMAILS
    allowlist. Checked against the DB user rather than re-parsing the JWT so
    all three token paths (Clerk / MCP JWT / MCP OAuth) get the same answer."""
    user, tenant = current_user_tenant
    email = (user.email_id or "").strip().lower()
    if user.is_org_owner or (email and email in _admin_emails()):
        return user, tenant
    raise HTTPException(
        status_code=403,
        detail="Access denied: the admin dashboard is restricted to admins",
    )


# One row per deployment (vendor_deployment_id) in the window, carrying
# everything the aggregations need. The resolved joins mirror
# deployment_history.py's Phase-1 CTE. resource_type comes from the target's
# infrastructuretype_ref (ECS/EKS/S3/SQS/…): service configs and infra rows
# carry it directly; KONG_ROUTE runs resolve it via the route's owning
# service (LATERAL picks one of that service's configs, preferring the
# route's own environment) — gateway is a deploy kind, never a resource.
_STATS_SQL = f"""
    WITH resolved AS (
        SELECT
            rt.vendor_deployment_id AS vid,
            rt.created_at AS created_at,
            rt.status::text AS run_status,
            COALESCE(sc.environment::text, im.environments_enum::text, kr.environments_enum::text, kg.environments_enum::text) AS env,
            COALESCE(sm.name, im.name, kr.api_name, kg.api_name) AS service_name,
            COALESCE(sc.infrastructuretype_ref_code, im.infrastructuretype_ref_code, ksc.infrastructuretype_ref_code) AS resource_type,
            uq.user_code AS user_code,
            {DEPLOY_TYPE_SQL['gateway']} AS is_gateway,
            {DEPLOY_TYPE_SQL['settings']} AS is_settings,
            {DEPLOY_TYPE_SQL['variables']} AS is_variables,
            {DEPLOY_TYPE_SQL['infra']} AS is_infra
        FROM pipeline_run_track rt
        JOIN pipeline_mst p ON p.code = rt.pipeline_mst_code
        LEFT JOIN service_configs sc     ON p.table_name::text = 'SERVICE_CONFIG' AND sc.code = p.transaction_code
        LEFT JOIN infrastructure_mst im  ON p.table_name::text = 'INFRASTRUCTURE' AND im.code = p.transaction_code
        LEFT JOIN kong_route_configs kr  ON p.table_name::text = 'KONG_ROUTE'     AND kr.code = p.transaction_code
        LEFT JOIN kong_route_groups  kg  ON p.table_name::text = 'KONG_ROUTE'     AND kg.code = p.transaction_code
        LEFT JOIN services_mst sm        ON sm.code = COALESCE(sc.services_mst_code, kr.services_mst_code, kg.services_mst_code)
        LEFT JOIN LATERAL (
            SELECT x.infrastructuretype_ref_code
            FROM service_configs x
            WHERE x.services_mst_code = COALESCE(kr.services_mst_code, kg.services_mst_code)
            ORDER BY (x.environment::text = COALESCE(kr.environments_enum::text, kg.environments_enum::text)) DESC
            LIMIT 1
        ) ksc ON COALESCE(kr.code, kg.code) IS NOT NULL
        LEFT JOIN LATERAL (
            SELECT tq3.user_code
            FROM jsonb_array_elements_text({_TQ_CODES_ARRAY}) tc2(code)
            JOIN transaction_queue tq3 ON tq3.code = tc2.code
            WHERE tq3.user_code IS NOT NULL
            LIMIT 1
        ) uq ON true
        WHERE p.tenant_code = :tenant
          AND p.pipeline_vendor_mst_code = :vendor_code
          AND rt.vendor_deployment_id IS NOT NULL
          AND rt.is_deleted = false
          AND rt.created_at >= :date_from
          AND rt.created_at <= :date_to
    )
    SELECT
        r.vid AS vid,
        CASE
            WHEN bool_or(r.run_status IN ('FAILED','CANCELLED')) THEN 'FAILED'
            WHEN bool_or(r.run_status = 'TIMEOUT') THEN 'TIMED_OUT'
            WHEN bool_or(r.run_status IN ('RUNNING','PENDING')) THEN 'RUNNING'
            ELSE 'COMPLETED'
        END AS agg_status,
        MAX(r.env) AS env,
        MAX(r.service_name) AS service_name,
        MAX(r.resource_type) AS resource_type,
        MAX(r.user_code) AS user_code,
        bool_or(r.is_gateway) AS is_gateway,
        bool_or(r.is_settings) AS is_settings,
        bool_or(r.is_variables) AS is_variables,
        bool_or(r.is_infra) AS is_infra
    FROM resolved r
    GROUP BY r.vid
"""

_TYPE_FLAGS = (("gateway", "is_gateway"), ("settings", "is_settings"),
               ("variables", "is_variables"), ("infra", "is_infra"))


def _totals(status_counter: Counter) -> DeployTrackerTotals:
    return DeployTrackerTotals(
        deployments=sum(status_counter.values()),
        completed=status_counter.get("COMPLETED", 0),
        failed=status_counter.get("FAILED", 0),
        running=status_counter.get("RUNNING", 0),
        timed_out=status_counter.get("TIMED_OUT", 0),
    )


async def _compute_stats(
    db: AsyncSession,
    tenant_code: str,
    date_from: datetime,
    date_to: datetime,
    deploy_type: Optional[str],
    include_previous: bool,
) -> DeployTrackerStatsResponse:
    """The aggregation shared by GET /stats and the Slack report."""
    params = {
        "tenant": tenant_code,
        "vendor_code": f"pv-temporal-{tenant_code}",
        "date_from": date_from,
        "date_to": date_to,
    }
    rows = (await db.execute(text(_STATS_SQL), params)).all()

    flag_col = dict(_TYPE_FLAGS).get(deploy_type) if deploy_type else None
    if flag_col:
        rows = [r for r in rows if getattr(r, flag_col)]

    statuses: Counter = Counter(r.agg_status for r in rows)
    by_resource: Counter = Counter((r.resource_type or "other") for r in rows)
    by_environment: Counter = Counter((r.env or "unknown") for r in rows)
    # A deployment can carry more than one kind (a multi-deploy ships settings
    # + variables + gateway at once), so these counts may sum past the total —
    # each bucket answers "how many deployments touched this kind".
    by_type: Counter = Counter()
    for r in rows:
        for type_key, col in _TYPE_FLAGS:
            if getattr(r, col):
                by_type[type_key] += 1

    services: dict[str, dict] = {}
    service_resources: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if not r.service_name:
            continue
        s = services.setdefault(r.service_name, {"count": 0, "completed": 0, "failed": 0})
        s["count"] += 1
        if r.agg_status == "COMPLETED":
            s["completed"] += 1
        elif r.agg_status in ("FAILED", "TIMED_OUT"):
            s["failed"] += 1
        if r.resource_type:
            service_resources[r.service_name][r.resource_type] += 1
    top_services = [
        TopService(
            name=name,
            resource=(service_resources[name].most_common(1)[0][0] if service_resources[name] else None),
            **counts,
        )
        for name, counts in sorted(services.items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]
    ]

    # Most active deployers — per-deployment user comes from the first queue
    # item that carries one (same attribution rule as the list's Triggered by).
    users: dict[str, dict] = {}
    for r in rows:
        if not r.user_code:
            continue
        u = users.setdefault(r.user_code, {"count": 0, "completed": 0, "failed": 0})
        u["count"] += 1
        if r.agg_status == "COMPLETED":
            u["completed"] += 1
        elif r.agg_status in ("FAILED", "TIMED_OUT"):
            u["failed"] += 1
    top_user_codes = [c for c, _ in sorted(users.items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]]
    user_names: dict[str, UserMstModel] = {}
    if top_user_codes:
        for u_row in (await db.execute(
            select(UserMstModel).where(UserMstModel.code.in_(top_user_codes))
        )).scalars():
            user_names[u_row.code] = u_row
    top_users = [
        TopUser(
            code=c,
            name=(
                f"{user_names[c].first_name} {user_names[c].last_name}".strip()
                if c in user_names else "Unknown user"
            ),
            email=user_names[c].email_id if c in user_names else None,
            **users[c],
        )
        for c in top_user_codes
    ]

    previous_totals = None
    if include_previous:
        span = date_to - date_from
        prev_params = {**params, "date_from": date_from - span, "date_to": date_from}
        prev_rows = (await db.execute(text(_STATS_SQL), prev_params)).all()
        if flag_col:
            prev_rows = [r for r in prev_rows if getattr(r, flag_col)]
        previous_totals = _totals(Counter(r.agg_status for r in prev_rows))

    return DeployTrackerStatsResponse(
        totals=_totals(statuses),
        previous_totals=previous_totals,
        by_resource=[CountBucket(key=k, count=v) for k, v in by_resource.most_common()],
        by_environment=[CountBucket(key=k, count=v) for k, v in by_environment.most_common()],
        by_type=[CountBucket(key=k, count=v) for k, v in by_type.most_common()],
        top_services=top_services,
        top_users=top_users,
    )


@router.get("/stats", response_model=DeployTrackerStatsResponse)
async def get_deploy_tracker_stats(
    date_from: datetime = Query(..., description="Window start (ISO 8601)"),
    date_to: datetime = Query(..., description="Window end (ISO 8601)"),
    deploy_type: Optional[str] = Query(None, description="gateway | settings | variables | infra"),
    include_previous: bool = Query(False, description="Also aggregate the same-length window before date_from"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(require_deploy_tracker_admin),
):
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="date_to must not be before date_from")
    _, tenant = current_user_tenant
    return await _compute_stats(db, tenant.code, date_from, date_to, deploy_type, include_previous)


# ── Slack report ──────────────────────────────────────────────────────────────

def _short_resource(key: str) -> str:
    short = re.sub(r"_?INFRASTRUCTURE\s*TYPE_?REF$", "", key, flags=re.IGNORECASE)
    short = re.sub(r"_?INFRASTRUCTURETYPE_?REF$", "", short, flags=re.IGNORECASE)
    return (short or key).replace("_", " ").strip().upper()


# Workspace custom emoji used by the report (upload the matching dt-*.png
# pack via Slack → Customize Workspace → Emoji, names without colons). Until
# uploaded, Slack shows the literal shortcode text.
_E = {
    "success": ":dt-success:",
    "failed": ":dt-failed:",
    "running": ":dt-running:",
    "resource": ":dt-resource:",
    "env": ":dt-env:",
    "type": ":dt-type:",
    "services": ":dt-services:",
    "users": ":dt-users:",
}

_TYPE_TITLES = {
    "gateway": "Gateway updates",
    "settings": "Settings updates",
    "variables": "Environment updates",
    "infra": "Infrastructure updates",
}


def _fmt_day(dt: datetime) -> str:
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


# Left color stripe per group (legacy-attachment `color`), matching each
# section's icon color from the dt-* emoji pack.
_GROUP_COLORS = {
    "resource": "#a78bfa",
    "env": "#2dd4bf",
    "type": "#fbbf24",
    "services": "#f59e0b",
    "users": "#93c5fd",
}


def _build_report_blocks(
    stats: DeployTrackerStatsResponse,
    date_from: datetime,
    date_to: datetime,
    requested_by: str,
) -> tuple[list[dict], list[dict], str]:
    """Block Kit layout: header/status in the main blocks (success rate keeps
    its bar); every group rides as its own attachment so Slack draws a left
    color stripe matching the group's icon. Counts only in breakdowns.
    Returns (blocks, attachments, fallback_text)."""
    t = stats.totals
    finished = t.completed + t.failed + t.timed_out
    rate_pct = (t.completed / finished * 100) if finished else 0.0
    rate = f"{rate_pct:.0f}%" if finished else "—"
    range_str = f"{_fmt_day(date_from)} – {_fmt_day(date_to)}"

    def bar(value: int, max_value: int, slots: int = 8) -> str:
        """Proportional GREEN bar built from the dt-bar-on/off custom emoji —
        slim strips on a transparent canvas, so a run of them reads as one
        continuous thin colored bar (plain mrkdwn text can't be colored).
        Non-zero shows at least one filled segment."""
        if max_value <= 0 or value <= 0:
            return ":dt-bar-off:" * slots
        filled = max(1, min(slots, round(value / max_value * slots)))
        return ":dt-bar-on:" * filled + ":dt-bar-off:" * (slots - filled)

    status_line = f"*{t.deployments} deployments*   ·   {_E['success']} {t.completed}   ·   {_E['failed']} {t.failed + t.timed_out}"
    if t.running:
        status_line += f"   ·   {_E['running']} {t.running}"
    rate_line = f"Success rate   {bar(round(rate_pct), 100, 10)}   {rate}" if finished else None

    blocks: list[dict] = [
        # :devlift-logo: is the workspace's custom emoji — renders as the
        # devlift logo in front of the title (emoji: True is what enables
        # custom-emoji resolution in plain_text header blocks).
        {"type": "header", "text": {"type": "plain_text", "text": ":devlift-logo:  Devlift Deploy Report", "emoji": True}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"{range_str}   ·   requested by {requested_by or 'devlift'}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(x for x in (status_line, rate_line) if x)}},
    ]

    # Breakdowns show counts only (the bar stays on success rate alone).
    # Plain stacked sections behind dividers — NOT attachments (their colored
    # stripe forces Slack's un-removable "Added by <app>" footer). The left
    # color rail is faked instead with the dt-rail-* custom emoji: every
    # content line starts with a full-height colored bar segment, so stacked
    # lines read as a rail in the section's icon color.
    _RAIL = {
        "resource": ":dt-rail-resource:",
        "env": ":dt-rail-env:",
        "type": ":dt-rail-type:",
        "services": ":dt-rail-services:",
        "users": ":dt-rail-users:",
    }

    def rows(group: str, buckets: list, label_of) -> str:
        return "\n".join(f"{_RAIL[group]} {label_of(b)} — {b.count}" for b in buckets)

    def section(text_: str) -> list[dict]:
        return [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn", "text": text_}}]

    if stats.by_resource:
        blocks += section(f"*{_E['resource']} By resource*\n" + rows("resource", stats.by_resource, lambda b: _short_resource(b.key)))
    if stats.by_environment:
        blocks += section(f"*{_E['env']} By environment*\n" + rows("env", stats.by_environment, lambda b: b.key))
    if stats.by_type:
        blocks += section(f"*{_E['type']} By deploy type*\n" + rows("type", stats.by_type, lambda b: _TYPE_TITLES.get(b.key, b.key)))
    if stats.top_services:
        svc = "\n".join(f"{_RAIL['services']} *{i}.*  {s.name} — {s.count}" for i, s in enumerate(stats.top_services, start=1))
        blocks += section(f"*{_E['services']} Top services*\n{svc}")
    if stats.top_users:
        usr = "\n".join(f"{_RAIL['users']} *{i}.*  {u.name} — {u.count}" for i, u in enumerate(stats.top_users, start=1))
        blocks += section(f"*{_E['users']} Top users*\n{usr}")

    attachments: list[dict] = []
    fallback = (
        f"Devlift Deploy Report {range_str}: {t.deployments} deployments, "
        f"{t.completed} succeeded, {t.failed + t.timed_out} failed"
        + (f", {t.running} in progress" if t.running else "")
    )
    return blocks, attachments, fallback


@router.post("/report-slack", response_model=SlackReportResponse)
async def send_slack_report(
    body: SlackReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(require_deploy_tracker_admin),
):
    """Post the dashboard summary for [date_from, date_to] as text into the
    configured Slack channel, via the DEDICATED report bot (separate token
    from the main Slack app)."""
    token = (settings.deploy_tracker_slack_bot_token or "").strip()
    channel = (settings.deploy_tracker_slack_channel or "").strip()
    if not token or not channel:
        raise HTTPException(
            status_code=503,
            detail="Slack reporting is not configured — set DEPLOY_TRACKER_SLACK_BOT_TOKEN and DEPLOY_TRACKER_SLACK_CHANNEL",
        )
    if body.date_to < body.date_from:
        raise HTTPException(status_code=422, detail="date_to must not be before date_from")

    user, tenant = current_user_tenant
    stats = await _compute_stats(db, tenant.code, body.date_from, body.date_to, None, False)
    blocks, attachments, fallback = _build_report_blocks(
        stats, body.date_from, body.date_to,
        requested_by=f"{user.first_name} {user.last_name}".strip(),
    )

    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient

    try:
        await AsyncWebClient(token=token).chat_postMessage(channel=channel, text=fallback, blocks=blocks, attachments=attachments)
    except SlackApiError as e:
        err = e.response.get("error", "unknown_error") if getattr(e, "response", None) else "unknown_error"
        raise HTTPException(status_code=502, detail=f"Slack rejected the report: {err}")
    return SlackReportResponse(ok=True, channel=channel)
