"""Read-only data-query fallback for the DevLift MCP server.

When no concrete tool answers a question about the user's DevLift data, the
client LLM reads the curated view catalog (`catalog.py`), writes a SELECT, and
the server validates (`sql_guard.py`) and executes it (`executor.py`) against
tenant-scoped views in the `mcp_ro` Postgres schema.
"""
