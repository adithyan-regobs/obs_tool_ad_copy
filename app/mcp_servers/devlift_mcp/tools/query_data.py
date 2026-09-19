"""query_data tool - run a read-only SELECT over the mcp_ro views.

The LLM writes the SQL; the server validates it, pins the tenant, and executes
it in a READ ONLY transaction. See db_query/executor.py for the guard chain.
"""

from typing import Optional

from app.mcp_servers.devlift_mcp.auth import get_auth_context
from app.mcp_servers.devlift_mcp.db_query.executor import run_read_only_query


async def query_data_impl(*, sql: str, purpose: Optional[str] = None) -> dict:
    auth_ctx = await get_auth_context()
    if auth_ctx is None:
        return {
            "status": "error",
            "message": (
                "Authentication required. Run the 'authenticate' tool first "
                "to log in via your browser."
            ),
        }

    if not sql or not sql.strip():
        return {
            "status": "error",
            "reason": "sql_rejected",
            "message": "`sql` is empty. Pass one SELECT statement.",
        }

    return await run_read_only_query(
        sql=sql,
        tenant_code=auth_ctx.tenant_code,
        user_code=auth_ctx.user_code,
        purpose=purpose,
    )
