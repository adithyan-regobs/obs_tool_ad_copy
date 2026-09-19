"""service_approval tools — the reviewer's side of the review lane.

    list_pending_approvals     what is waiting for THIS caller (and what they approved)
    review_service_request     one request: author, frozen diff, history, decisions
    approve_service_request    submit -> approved
    reject_service_request     submit -> draft, back to the author with a reason
                               (the product's "Reject"; calls the request-changes
                               route — the terminal reject is not exposed)
    revoke_approval            approved -> submit

Each decision is one call to obs_tool's `/approvals/{code}/{verb}` route with
the caller's identity; the lane's state, permission and self-approval rules
run there. The author's own verbs live in `service_request.py`.
"""

from app.mcp_servers.devlift_mcp.dispatcher import (
    approve_service_request_handler,
    list_pending_approvals_handler,
    reject_service_request_handler,
    review_service_request_handler,
    revoke_approval_handler,
)


async def list_pending_approvals_impl() -> dict:
    return await list_pending_approvals_handler()


async def review_service_request_impl(
    queue_code: str | None = None,
    service_name: str | None = None,
) -> dict:
    return await review_service_request_handler(queue_code=queue_code, service_name=service_name)


async def approve_service_request_impl(
    queue_code: str | None = None,
    service_name: str | None = None,
    comment: str | None = None,
) -> dict:
    return await approve_service_request_handler(
        queue_code=queue_code, service_name=service_name, comment=comment
    )


async def reject_service_request_impl(
    queue_code: str | None = None,
    service_name: str | None = None,
    reason: str | None = None,
) -> dict:
    return await reject_service_request_handler(
        queue_code=queue_code, service_name=service_name, reason=reason
    )


async def revoke_approval_impl(
    queue_code: str | None = None,
    service_name: str | None = None,
    comment: str | None = None,
) -> dict:
    return await revoke_approval_handler(
        queue_code=queue_code, service_name=service_name, comment=comment
    )
