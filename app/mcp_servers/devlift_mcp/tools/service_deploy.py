"""deploy_service_request tool — ship an APPROVED service change (the web's
Deploy button).

Two calls to obs_tool's own routes, in the web's order:
    1. POST /approvals/{code}/deploy          the gate: approved, can_deploy, seal
    2. POST /deployments/multiple-deploy      the settings row + the approved
                                              gateway routes, one Temporal batch
The tool asks for confirmation itself (a Deploy / Cancel question; on
production the user types the service name) before either call, then hands
the workflow to get_deployment_status for progress.
"""

from app.mcp_servers.devlift_mcp.dispatcher import deploy_service_request_handler


async def deploy_service_request_impl(
    queue_code: str | None = None,
    service_name: str | None = None,
    ticket_code: str | None = None,
    confirmed: bool | None = None,
    confirm_service_name: str | None = None,
    project_id: str | None = None,
) -> dict:
    return await deploy_service_request_handler(
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
        confirmed=bool(confirmed),
        confirm_service_name=confirm_service_name,
        project_id=project_id,
    )
