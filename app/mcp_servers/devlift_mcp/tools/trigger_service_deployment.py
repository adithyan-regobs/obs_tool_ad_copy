"""trigger_service_deployment tool — deploy an EKS service draft.

PATH B tool. Delegates to dispatcher.trigger_service_deployment_handler.
Covers: eks_service and any future service_config resources.

For infrastructure resources (s3_bucket, postgres_server) use
trigger_resource_deployment instead.
"""

from app.mcp_servers.devlift_mcp.dispatcher import trigger_service_deployment_handler


async def trigger_service_deployment_impl(
    resource_type: str,
    is_code_committed: bool = False,
    project_id: str | None = None,
    draft_id: str | None = None,
    sync_envs: bool = False,
    source_case_ref_code: str | None = None,
    source_draft_id: str | None = None,
    env_mapping: dict[str, str] | None = None,
) -> dict:
    return await trigger_service_deployment_handler(
        resource_type=resource_type,
        is_code_committed=is_code_committed,
        project_id=project_id,
        draft_id=draft_id,
        sync_envs=sync_envs,
        source_case_ref_code=source_case_ref_code,
        source_draft_id=source_draft_id,
        env_mapping=env_mapping,
    )
