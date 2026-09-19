"""trigger_resource_deployment tool — deploy an infrastructure resource.

Primary path is chatbot-driven: the LLM has chatted with chatbot via the `chat`
tool until isReady=true; this tool then takes the resolved data, creates the
infrastructure_mst row + queue + draft, and runs the deployment pipeline.

The legacy draft_id-based path (provision_resource → trigger_resource_deployment)
is retained for back-compat. Whichever the caller supplies wins; ticket_code
takes priority when both are present.
"""

from app.mcp_servers.devlift_mcp.dispatcher import (
    provision_and_trigger_from_ticket_handler,
    trigger_resource_deployment_handler,
)


async def trigger_resource_deployment_impl(
    resource_type: str | None = None,
    project_id: str | None = None,
    draft_id: str | None = None,
    ticket_code: str | None = None,
) -> dict:
    if ticket_code:
        return await provision_and_trigger_from_ticket_handler(
            ticket_code=ticket_code,
            project_id=project_id,
        )
    return await trigger_resource_deployment_handler(
        resource_type=resource_type,
        project_id=project_id,
        draft_id=draft_id,
    )
