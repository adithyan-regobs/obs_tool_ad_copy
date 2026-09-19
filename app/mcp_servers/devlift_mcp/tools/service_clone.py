"""clone_service tool — a new service with another service's
settings.

Loads the source service's effective configuration (pending request over the
live row) as EKS form answers, swaps in the new name (and optionally the
environment / geo), and opens a pre-filled chat session. "done", or any
differences the user states, reaches isReady; `create_service_and_save_draft`
then creates the new service, its base configuration and a draft holding the
copied settings. Settings only — variables, secrets and sidecars are not
copied.
"""

from app.mcp_servers.devlift_mcp.dispatcher import start_service_clone_handler


async def clone_service_impl(
    source_service_name: str | None = None,
    new_service_name: str | None = None,
    environment: str | None = None,
    geo_location: str | None = None,
    source_service_config_code: str | None = None,
) -> dict:
    return await start_service_clone_handler(
        source_service_name=source_service_name,
        source_service_config_code=source_service_config_code,
        new_service_name=new_service_name,
        environment=environment,
        geo_location=geo_location,
    )
