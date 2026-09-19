"""create_service_and_save_draft tool — EKS service creation + settings draft.

Chatbot-driven: the LLM has chatted via the `chat` tool until the EKS form
reports isReady=true; this tool then takes the resolved `create_service` +
`service_config` result and mirrors the web's "Create & Add" followed by the
Settings tab's Save:

    1. POST /services/create-service           (skipped when the service exists)
    2. POST /service-configs                   (baseline row; skipped when it exists)
    3. POST /transaction/service-settings/{sc} (the user's values, as a DRAFT)

It never submits, approves or deploys — those are the review lane's verbs and
belong to the follow-up tools.
"""

from app.mcp_servers.devlift_mcp.dispatcher import create_service_and_save_draft_handler


async def create_service_and_save_draft_impl(
    ticket_code: str | None = None,
    project_id: str | None = None,
    cluster_code: str | None = None,
) -> dict:
    return await create_service_and_save_draft_handler(
        ticket_code=ticket_code,
        project_id=project_id,
        cluster_code=cluster_code,
    )
