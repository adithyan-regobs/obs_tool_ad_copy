"""describe_data_schema tool - the read-only view catalog for query_data.

Static: the catalog lives in code (db_query/catalog.py) and mirrors the views
created by migration 168. Auth is still required so the tool cannot be used to
map the data model without a DevLift login.
"""

from app.mcp_servers.devlift_mcp.auth import get_auth_context
from app.mcp_servers.devlift_mcp.db_query.catalog import describe_schema


async def describe_data_schema_impl() -> dict:
    auth_ctx = await get_auth_context()
    if auth_ctx is None:
        return {
            "status": "error",
            "message": (
                "Authentication required. Run the 'authenticate' tool first "
                "to log in via your browser."
            ),
        }

    return {
        "status": "ok",
        **describe_schema(),
        "message": "Read-only views available to query_data. All are scoped to your account.",
        "next_action": {
            "type": "write_query",
            "instruction": (
                "Write ONE SELECT over these views that answers the user's "
                "question, then call query_data(sql=..., purpose=...). Follow "
                "`rules`. Do not call describe_data_schema again in this "
                "conversation unless a query fails on an unknown column."
            ),
        },
    }
