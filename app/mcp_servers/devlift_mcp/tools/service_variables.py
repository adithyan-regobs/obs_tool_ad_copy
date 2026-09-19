"""open_variables_editor tool - hand the user the Variables tab of a service.

Variable and secret VALUES never pass through the MCP server, the model or
the chatbot: the agreement is that secrets are managed only by the secret
service, and the browser talks to it directly. So this tool does not take
values. It resolves the service and returns the dashboard deep link to its
Variables tab; the user adds or edits there and comes back to submit.
"""

from app.mcp_servers.devlift_mcp.dispatcher import open_variables_editor_handler


async def open_variables_editor_impl(
    service_name: str | None = None,
    ticket_code: str | None = None,
    service_config_code: str | None = None,
) -> dict:
    return await open_variables_editor_handler(
        service_name=service_name,
        ticket_code=ticket_code,
        service_config_code=service_config_code,
    )
