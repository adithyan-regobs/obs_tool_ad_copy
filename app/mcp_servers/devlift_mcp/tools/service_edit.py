"""edit_service_configuration tool — start an edit session on an existing
service without re-asking the whole form.

Loads the service's effective configuration (pending request values over the
live row), maps it to the EKS form's answers, and opens a NEW chatbot session
pre-filled with them. The user then only states what changes; the ordinary
`chat` loop and `create_service_and_save_draft` finish the job, producing a
draft whose diff holds just the changed fields.

`section='gateway'` opens the Kong routes form instead (placement and service
pre-filled), the way the web's Gateway tab sits next to the Settings tab; the
result is saved by the same `create_service_and_save_draft` as the gateway
half of the service's draft.
"""

from app.mcp_servers.devlift_mcp.dispatcher import start_service_edit_handler


async def edit_service_configuration_impl(
    service_name: str | None = None,
    queue_code: str | None = None,
    ticket_code: str | None = None,
    service_config_code: str | None = None,
    section: str | None = None,
    route_action: str | None = None,
) -> dict:
    return await start_service_edit_handler(
        service_name=service_name,
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_config_code=service_config_code,
        section=section,
        route_action=route_action,
    )
