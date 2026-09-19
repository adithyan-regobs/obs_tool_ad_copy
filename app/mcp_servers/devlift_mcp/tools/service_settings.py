"""view_service_settings tool — the Settings tab as text, read-only.

Every configuration field with its value, grouped as the web groups them
(Placement / Repository / Dockerfile / Manifest / AWS Resource Provisioning).
Shows the pending request's values when one exists (what the author is
editing) and the live values otherwise; each field says which.

Distinct from get_service_configuration, which is the PREVIEW: the pending
request's from/to diff. This one answers "what are the settings right now".
"""

from app.mcp_servers.devlift_mcp.dispatcher import view_service_settings_handler


async def view_service_settings_impl(
    service_name: str | None = None,
    queue_code: str | None = None,
    ticket_code: str | None = None,
    service_config_code: str | None = None,
    include_gateway: bool = False,
) -> dict:
    return await view_service_settings_handler(
        service_name=service_name,
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_config_code=service_config_code,
        include_gateway=include_gateway,
    )
