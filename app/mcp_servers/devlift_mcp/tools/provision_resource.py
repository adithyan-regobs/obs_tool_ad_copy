"""provision_resource tool — DEPRECATED.

Provisioning is now driven end-to-end by the chatbot. The LLM calls the new
`chat` tool until the chatbot signals isReady=true, then calls
`trigger_resource_deployment(ticket_code=...)` which both creates the draft
and runs the deployment pipeline in one shot.

The implementation below is preserved (commented out) for reference. The tool
is no longer registered in server.py.
"""

# from app.mcp_servers.devlift_mcp.dispatcher import provision_resource_handler
#
#
# async def provision_resource_impl(
#     resource_type: str,
#     attributes: dict,
#     product: str,
#     environment: str,
#     geo_location: str,
#     project_id: str | None = None,
#     draft_id: str | None = None,
# ) -> dict:
#     return await provision_resource_handler(
#         resource_type=resource_type,
#         attributes=attributes,
#         product=product,
#         environment=environment,
#         geo_location=geo_location,
#         project_id=project_id,
#         draft_id=draft_id,
#     )
