"""Run a guarded SELECT against the mcp_ro views for one tenant.

Guards, in the order they apply:
    1. sql_guard   - shape, keyword, function and table-name checks
    2. READ ONLY   - the transaction refuses writes
    3. set_config  - app.tenant_code is what every mcp_ro view filters on
    4. timeout     - SET LOCAL statement_timeout
    5. search_path - unqualified names resolve in mcp_ro only
    6. role grants - DB_RO_USER can SELECT from mcp_ro.* and nothing else
    7. row cap     - the wrapped query carries LIMIT max_rows + 1

Nothing is ever committed. Every call is logged with user and tenant.
"""

import logging
import time
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import app.db.models  # noqa: F401  registers every table on Base.metadata
from app.core.config import settings
from app.db.models.base_model import Base
from app.db.session import READ_ONLY_CREDENTIALS_CONFIGURED, read_only_async_engine
from app.mcp_servers.devlift_mcp.db_query.catalog import SCHEMA_NAME, allowed_view_names
from app.mcp_servers.devlift_mcp.db_query.sql_guard import SqlRejected, guard_sql

logger = logging.getLogger(__name__)

ALLOWED_VIEWS: frozenset[str] = allowed_view_names()

# Base table names may not appear anywhere in a query, even as aliases: the
# views are the only surface. A view that shares its name with a base table
# (service_configs) stays allowed: search_path is pinned to mcp_ro, so the bare
# name resolves to the view, and the only way to reach the table is to qualify
# it with `public`, which is denied here.
DENIED_WORDS: frozenset[str] = (frozenset(Base.metadata.tables) - ALLOWED_VIEWS) | frozenset(
    {"public", "pg_catalog", "pg_toast", "information_schema", "alembic_version"}
)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    return str(value)


def _db_error(exc: DBAPIError) -> tuple[str, bool]:
    """(LLM-facing message, is_timeout). SQLAlchemy wraps the asyncpg error twice."""
    orig = getattr(exc, "orig", None) or exc
    cause = getattr(orig, "__cause__", None) or orig
    name = type(cause).__name__
    detail = (str(cause) or "").strip().splitlines()
    first = detail[0] if detail else name
    is_timeout = name == "QueryCanceledError" or "statement timeout" in first.lower()
    return f"{name}: {first}", is_timeout


def _rejected(message: str) -> dict:
    return {
        "status": "error",
        "reason": "sql_rejected",
        "message": message,
        "allowed_views": sorted(ALLOWED_VIEWS),
        "next_action": {
            "type": "fix_sql",
            "instruction": (
                "Rewrite the query using only the allowed views and plain "
                "SELECT syntax, then call query_data again. Call "
                "describe_data_schema if you need the column list."
            ),
        },
    }


async def run_read_only_query(
    *,
    sql: str,
    tenant_code: str,
    user_code: str,
    purpose: Optional[str] = None,
) -> dict:
    max_rows = int(settings.mcp_data_query_max_rows)
    timeout_ms = int(settings.mcp_data_query_timeout_ms)

    try:
        guarded = guard_sql(
            sql,
            allowed_tables=ALLOWED_VIEWS,
            schema=SCHEMA_NAME,
            max_rows=max_rows,
            denied_words=DENIED_WORDS,
        )
    except SqlRejected as exc:
        logger.warning(
            "mcp query_data rejected: user=%s tenant=%s reason=%s sql=%r",
            user_code, tenant_code, exc, sql,
        )
        return _rejected(str(exc))

    if not READ_ONLY_CREDENTIALS_CONFIGURED:
        logger.warning("mcp query_data: DB_RO_USER is not set; running with the application role")

    started = time.monotonic()
    try:
        async with read_only_async_engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(
                    text("SELECT set_config('app.tenant_code', :tenant, true)"),
                    {"tenant": tenant_code},
                )
                await conn.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
                await conn.execute(text(f"SET LOCAL search_path = {SCHEMA_NAME}"))
                # Driver-level execution: no bind-parameter parsing, so ':' in
                # casts or literals is passed through untouched.
                result = await conn.exec_driver_sql(guarded.wrapped_sql)
                columns = list(result.keys())
                fetched = result.fetchall()
            finally:
                await trans.rollback()
    except DBAPIError as exc:
        message, is_timeout = _db_error(exc)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "mcp query_data failed: user=%s tenant=%s elapsed_ms=%d error=%s sql=%r",
            user_code, tenant_code, elapsed_ms, message, guarded.sql,
        )
        if is_timeout:
            message = (
                f"The query ran longer than {timeout_ms // 1000}s and was cancelled. "
                "Add WHERE filters, aggregate, or lower the LIMIT."
            )
        return {
            "status": "error",
            "reason": "sql_error",
            "message": message,
            "next_action": {
                "type": "fix_sql",
                "instruction": (
                    "Read the database error, fix the query, and call query_data "
                    "again. After two failed retries, tell the user what you "
                    "could not look up instead of trying further."
                ),
            },
        }
    except Exception as exc:  # connection refused, pool timeout, ...
        logger.exception("mcp query_data: unexpected failure user=%s tenant=%s", user_code, tenant_code)
        return {
            "status": "error",
            "reason": "query_failed",
            "message": f"Could not run the query: {type(exc).__name__}. Try again in a moment.",
        }

    elapsed_ms = int((time.monotonic() - started) * 1000)
    truncated = len(fetched) > max_rows
    rows = [
        {col: _jsonable(val) for col, val in zip(columns, row)}
        for row in fetched[:max_rows]
    ]
    logger.info(
        "mcp query_data ok: user=%s tenant=%s rows=%d truncated=%s elapsed_ms=%d tables=%s purpose=%r sql=%r",
        user_code, tenant_code, len(rows), truncated, elapsed_ms, list(guarded.tables), purpose, guarded.sql,
    )

    message = f"{len(rows)} row(s) returned."
    if truncated:
        message += f" The result was cut at {max_rows} rows; narrow the query to see the rest."
    return {
        "status": "ok",
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "max_rows": max_rows,
        "elapsed_ms": elapsed_ms,
        "message": message,
        "next_action": {
            "type": "answer_user",
            "instruction": (
                "Answer the user's question in plain language from `rows`. Do "
                "not show SQL, *_code or id values, or raw column names. If "
                "`truncated` is true, say the list was cut short and offer to "
                "narrow it. If `rows` is empty, say nothing matched for their "
                "account."
            ),
        },
    }
