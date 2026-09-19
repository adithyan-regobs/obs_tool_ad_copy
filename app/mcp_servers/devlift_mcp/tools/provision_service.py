"""provision_service tool — DEPRECATED.

EKS service provisioning will move to the chatbot once
`eks_service_creation_form.json` is added there. Until then, the tool is
unregistered and service deployments via MCP are temporarily unavailable.

The implementation below is preserved (commented out) for reference.
"""

# from app.mcp_servers.devlift_mcp.dispatcher import provision_service_handler
#
#
# async def provision_service_impl(
#     resource_type: str,
#     attributes: dict,
#     product: str,
#     environment: str,
#     geo_location: str,
#     project_id: str | None = None,
#     draft_id: str | None = None,
# ) -> dict:
#     return await provision_service_handler(
#         resource_type=resource_type,
#         attributes=attributes,
#         product=product,
#         environment=environment,
#         geo_location=geo_location,
#         project_id=project_id,
#         draft_id=draft_id,
#     )
