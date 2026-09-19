"""get_service_configuration tool — the service details page, read-only.

Returns what the web's service details page and its preview tab show: the
live configuration for one environment/geo, and the pending change request
with its from/to diff (computed live for a draft, frozen once submitted).

Two calls with the caller's identity:
    GET /service-configs/by-code/{sc}   can_view_settings
    GET /approvals?resource_code={sc}   your own drafts, or requests you may act on
"""

from app.mcp_servers.devlift_mcp.dispatcher import get_service_configuration_handler


async def get_service_configuration_impl(
    service_name: str | None = None,
    queue_code: str | None = None,
    ticket_code: str | None = None,
    service_config_code: str | None = None,
) -> dict:
    return await get_service_configuration_handler(
        service_name=service_name,
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_config_code=service_config_code,
    )
