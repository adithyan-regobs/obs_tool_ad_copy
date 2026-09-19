"""start_resource_edit / apply_resource_edit — change a setting on a resource
that already exists, without re-asking the whole form.

start is read-only: it returns the current values and says which fields may
change. apply performs the write, and only after the user has confirmed a
before/after summary.

Neither tool takes a product, environment or region. Placement is read from the
row being edited, so there is no input on this path that can move a resource.
"""

from app.mcp_servers.devlift_mcp.dispatcher import (
    apply_resource_edit_handler,
    start_resource_edit_handler,
)


async def start_resource_edit_impl(
    resource_name: str,
    environment: str | None = None,
    product: str | None = None,
) -> dict:
    return await start_resource_edit_handler(
        resource_name=resource_name,
        environment=environment,
        product=product,
    )


async def apply_resource_edit_impl(
    edit_id: str,
    changes: dict,
    confirmed: bool = False,
    project_id: str | None = None,
) -> dict:
    return await apply_resource_edit_handler(
        edit_id=edit_id,
        changes=changes,
        confirmed=confirmed,
        project_id=project_id,
    )
