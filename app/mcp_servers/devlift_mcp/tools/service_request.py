"""service_request tools — the author's own change request in the review lane.

    submit_service_request    draft  -> submit   (freezes the diff, notifies approvers)
    withdraw_service_request  submit -> draft    (submitter pulls it back)
    discard_service_request   draft  -> gone     (author bins the draft)

Each is one call to obs_tool's own `/approvals/{queue_code}/{verb}` route with
the caller's identity, so the lane's state, ownership and OpenFGA rules run
exactly as they do for the web. The reviewer verbs (approve / reject /
request-changes / revoke) live in `service_approval.py`; deploy in
`service_deploy.py`.
"""

from app.mcp_servers.devlift_mcp.dispatcher import (
    discard_service_request_handler,
    submit_service_request_handler,
    withdraw_service_request_handler,
)


async def submit_service_request_impl(
    queue_code: str | None = None,
    ticket_code: str | None = None,
    service_name: str | None = None,
    comment: str | None = None,
) -> dict:
    return await submit_service_request_handler(
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
        comment=comment,
    )


async def withdraw_service_request_impl(
    queue_code: str | None = None,
    ticket_code: str | None = None,
    service_name: str | None = None,
    comment: str | None = None,
) -> dict:
    return await withdraw_service_request_handler(
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
        comment=comment,
    )


async def discard_service_request_impl(
    queue_code: str | None = None,
    ticket_code: str | None = None,
    service_name: str | None = None,
) -> dict:
    return await discard_service_request_handler(
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
    )
