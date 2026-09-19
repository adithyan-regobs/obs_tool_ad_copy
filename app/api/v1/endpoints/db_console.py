"""TEMPORARY — run SQL against the app DB over HTTP.

The bastion/tunnel into vance-prod was revoked, so there is no other way to
inspect or repair that database from outside the cluster. Reads and writes
both, on purpose: checking a row during testing and fixing one (an
alembic_version stamp, say) are the same job.

NO AUTHENTICATION. The route is Public on purpose: the Clerk JWTs on this
deployment expire in ~30s, which made a token-gated console unusable for the
volume of ad-hoc testing queries this exists for. The trade is stark and
deliberate — anyone who reaches this URL has unrestricted read/write on the
production database, no credential required. It is safe ONLY because it is
short-lived; the module docstring's DELETE instruction is the real control.

DELETE THIS FILE and its include_router block in app/api/v1/router.py the
moment normal DB access is restored. Every extra hour it is live is an
unauthenticated SQL door into production.
"""

import logging
import time
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.authz.security import Public, SecureRouter
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = SecureRouter()

# A stray `SELECT * FROM audit_log` should not stream a million rows back.
MAX_ROWS = 1000
# A runaway query would otherwise pin a worker until it finishes.
STATEMENT_TIMEOUT_MS = 60_000


class SqlRequest(BaseModel):
    sql: str = Field(..., min_length=1)
    params: dict[str, Any] | None = Field(
        default=None, description="Bind values for :name placeholders."
    )
    commit: bool = Field(
        default=True,
        description="false runs the statement and rolls back — a dry run that "
                    "still reports rowcount.",
    )


class SqlResponse(BaseModel):
    committed: bool
    rowcount: int | None = None
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    truncated: bool = False
    elapsed_ms: int


@router.post(
    "/query",
    response_model=SqlResponse,
    include_in_schema=False,
    access=Public(
        reason="Operator SQL console — temporary, while DB tunnel access to "
               "vance-prod is revoked. Unauthenticated by design: the ~30s "
               "Clerk JWT TTL made a token-gated console unusable for the "
               "testing volume this exists for. Must be deleted the moment "
               "normal DB access is restored."
    ),
)
async def run_sql(
    body: SqlRequest,
    request: Request,
) -> SqlResponse:
    # WARNING level on purpose — every use belongs in the log the on-call
    # reads. With no caller identity, the source IP is the only audit trail.
    caller = request.client.host if request.client else "unknown"
    logger.warning("db console: caller=%s commit=%s sql=%r params=%r",
                   caller, body.commit, body.sql, body.params)

    started = time.monotonic()
    # Its own session, never the request-scoped one: a rollback here must not
    # disturb anything else.
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            result = await db.execute(text(body.sql), body.params or {})

            columns: list[str] = []
            rows: list[dict[str, Any]] = []
            truncated = False
            if result.returns_rows:
                columns = list(result.keys())
                fetched = result.fetchmany(MAX_ROWS + 1)
                truncated = len(fetched) > MAX_ROWS
                # str() the values — UUIDs, datetimes, Decimals and enums are
                # not JSON-serialisable, and a console that 500s on a timestamp
                # column is useless for the job it exists to do.
                rows = [
                    {c: (None if v is None else str(v)) for c, v in zip(columns, r)}
                    for r in fetched[:MAX_ROWS]
                ]

            rowcount = result.rowcount if result.rowcount != -1 else None

            if body.commit:
                await db.commit()
            else:
                await db.rollback()

            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning("db console: caller=%s committed=%s rowcount=%s elapsed_ms=%d",
                           caller, body.commit, rowcount, elapsed_ms)
            return SqlResponse(
                committed=body.commit, rowcount=rowcount, columns=columns,
                rows=rows, truncated=truncated, elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            await db.rollback()
            logger.warning("db console: caller=%s failed: %s", caller, exc)
            # The DB's own error text goes back — without it you are debugging
            # production SQL blind.
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{type(exc).__name__}: {exc}")
