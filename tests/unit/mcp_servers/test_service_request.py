"""submit / withdraw / discard — the author's own change request.

One HTTP call per verb via obs_tool_client.approval_action; the request is
resolved from a queue_code, the ticket's Redis entry, or the caller's own
requests by service name."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp.auth import AuthContext
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError

AUTH = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x.io", clerk_user_id="")
TICKET = "mcp-u1-deadbeef"

REDIS_ENTRY = {
    "draft_id": "a1b2c3d4",
    "project_id": "proj-1",
    "ticket_code": TICKET,
    "queue_code": "queue-111",
    "transaction_code": "sc-svc-stage-abc",
    "transaction_table": "service_config",
    "identifier": "sample-mcp-service",
    "resource_type": "eks_service",
    "status": "pending",
    "queue_status": "draft",
}


def _item(code, name, status, env="stage", changes=None):
    return {
        "code": code,
        "status": status,
        "display_name": name,
        "resource_code": f"sc-{name}-{env}-x",
        "config_snapshot": {"environment": env},
        "changes": changes or {},
        "you": {"mine": True, "can_approve": False, "can_deploy": False, "can_write_settings": True},
    }


class Harness:
    def __init__(self, *, redis_entries=None, listing=None, action_result=None):
        self.redis_entries = [REDIS_ENTRY] if redis_entries is None else redis_entries
        self.listing = {"approvals": listing or [], "total": len(listing or [])}
        self.approval_action = AsyncMock(
            return_value=action_result
            if action_result is not None
            else {"ok": True, "approval": _item("queue-111", "sample-mcp-service", "submit", changes={"port": {"from": "80", "to": "8080"}}), "detail": "submitted for review"}
        )
        self.list_approvals = AsyncMock(return_value=self.listing)
        self.update_draft = AsyncMock(return_value=True)

    def patches(self):
        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.approval_action = self.approval_action
        client.list_approvals = self.list_approvals
        return [
            patch.object(dispatcher, "get_auth_context", AsyncMock(return_value=AUTH)),
            patch("app.mcp_servers.devlift_mcp._internal_jwt.mint_internal_jwt", return_value="jwt"),
            patch("app.mcp_servers.devlift_mcp.obs_tool_client", client),
            patch.object(dispatcher, "get_all_drafts", AsyncMock(return_value=self.redis_entries)),
            patch.object(dispatcher, "update_draft_by_id", self.update_draft),
        ]

    async def run(self, handler, **kwargs):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await handler(**kwargs)


@pytest.mark.asyncio
async def test_submit_by_queue_code_calls_submit_and_returns_changes():
    h = Harness()
    result = await h.run(dispatcher.submit_service_request_handler, queue_code="queue-111", comment="please review")
    assert result["status"] == "success"
    assert result["action"] == "submit"
    assert result["queue_code"] == "queue-111"
    assert result["queue_status"] == "submit"
    assert result["service_name"] == "sample-mcp-service"
    assert result["changes"] == {"port": {"from": "80", "to": "8080"}}
    assert result["you"]["mine"] is True
    h.approval_action.assert_awaited_once_with(jwt_token="jwt", queue_code="queue-111", verb="submit", comment="please review")
    # The handle resolved without a listing; the only listing call is the
    # post-action sibling fetch (by resource_code, never by status).
    assert all("status" not in c.kwargs for c in h.list_approvals.await_args_list)
    # Redis entry kept in step.
    h.update_draft.assert_awaited_once()
    assert h.update_draft.await_args.args[3]["queue_status"] == "submit"


@pytest.mark.asyncio
async def test_submit_by_ticket_resolves_queue_from_redis():
    h = Harness()
    result = await h.run(dispatcher.submit_service_request_handler, ticket_code=TICKET)
    assert result["status"] == "success"
    assert h.approval_action.await_args.kwargs["queue_code"] == "queue-111"
    # The handle resolved without a listing; the only listing call is the
    # post-action sibling fetch (by resource_code, never by status).
    assert all("status" not in c.kwargs for c in h.list_approvals.await_args_list)


@pytest.mark.asyncio
async def test_submit_by_service_name_resolves_from_own_drafts():
    h = Harness(redis_entries=[], listing=[_item("queue-222", "sample-mcp-service", "draft")])
    result = await h.run(dispatcher.submit_service_request_handler, service_name="sample-mcp-service")
    assert result["status"] == "success"
    assert h.list_approvals.await_args_list[0].kwargs == {"jwt_token": "jwt", "status": "draft"}
    assert h.approval_action.await_args.kwargs["queue_code"] == "queue-222"


@pytest.mark.asyncio
async def test_service_name_matches_with_service_suffix():
    h = Harness(redis_entries=[], listing=[_item("queue-222", "demo-service", "draft")])
    result = await h.run(dispatcher.submit_service_request_handler, service_name="demo")
    assert result["status"] == "success"
    assert h.approval_action.await_args.kwargs["queue_code"] == "queue-222"


@pytest.mark.asyncio
async def test_service_name_matches_queue_display_name_format():
    h = Harness(redis_entries=[], listing=[_item("queue-444", "update service:sample-mcp-service region-aspora-mumbai", "draft")])
    result = await h.run(dispatcher.submit_service_request_handler, service_name="sample-mcp-service")
    assert result["status"] == "success"
    assert h.approval_action.await_args.kwargs["queue_code"] == "queue-444"
    # The user's own wording is kept in the message, not the row's display name.
    assert result["service_name"] == "sample-mcp-service"
    assert result["message"].startswith("**sample-mcp-service**")


@pytest.mark.parametrize(
    "candidate,wanted,expected",
    [
        ("demo", "demo", True),
        ("demo-service", "demo", True),
        ("demo", "demo-service", True),
        ("update service:demo region-aspora-mumbai", "demo", True),
        ("update service:demo-service region-x", "demo", True),
        ("update service:demo region-x", "demo-service", True),
        ("update service:demo-api region-x", "demo", False),
        ("other", "demo", False),
        (None, "demo", False),
    ],
)
def test_name_matches(candidate, wanted, expected):
    assert dispatcher._name_matches(candidate, wanted) is expected


@pytest.mark.asyncio
async def test_two_matches_ask_to_choose():
    h = Harness(redis_entries=[], listing=[_item("queue-1", "demo", "draft", env="stage"), _item("queue-2", "demo", "draft", env="prod")])
    result = await h.run(dispatcher.submit_service_request_handler, service_name="demo")
    assert result["status"] == "error"
    assert result["reason"] == "ambiguous_target"
    assert result["next_action"]["type"] == "choose"
    assert [o["value"] for o in result["next_action"]["options"]] == ["queue-1", "queue-2"]
    assert "stage" in result["next_action"]["options"][0]["label"]
    h.approval_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_match_is_not_found():
    h = Harness(redis_entries=[], listing=[])
    result = await h.run(dispatcher.submit_service_request_handler, service_name="ghost")
    assert result["status"] == "error"
    assert result["reason"] == "not_found"
    h.approval_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_nothing_given_asks_for_service():
    h = Harness()
    result = await h.run(dispatcher.submit_service_request_handler)
    assert result["status"] == "error"
    assert result["reason"] == "missing_target"


@pytest.mark.asyncio
async def test_withdraw_returns_draft_status():
    h = Harness(action_result={"ok": True, "approval": _item("queue-111", "sample-mcp-service", "draft"), "detail": "withdrawn"})
    result = await h.run(dispatcher.withdraw_service_request_handler, queue_code="queue-111", comment="need edits")
    assert result["status"] == "success"
    assert result["action"] == "withdraw"
    assert result["queue_status"] == "draft"
    assert "back with you" in result["message"]
    assert h.approval_action.await_args.kwargs["verb"] == "withdraw"


@pytest.mark.asyncio
async def test_withdraw_by_name_lists_submitted_requests():
    h = Harness(redis_entries=[], listing=[_item("queue-333", "sample-mcp-service", "submit")],
                action_result={"ok": True, "approval": _item("queue-333", "sample-mcp-service", "draft"), "detail": "withdrawn"})
    result = await h.run(dispatcher.withdraw_service_request_handler, service_name="sample-mcp-service")
    assert result["status"] == "success"
    assert h.list_approvals.await_args_list[0].kwargs == {"jwt_token": "jwt", "status": "submit"}


@pytest.mark.asyncio
async def test_discard_marks_redis_entry_and_reports_gone():
    h = Harness(action_result={"ok": True, "approval": None, "detail": "draft discarded"})
    result = await h.run(dispatcher.discard_service_request_handler, queue_code="queue-111")
    assert result["status"] == "success"
    assert result["action"] == "discard"
    assert result["queue_status"] == "discarded"
    assert "unchanged" in result["message"]
    assert h.approval_action.await_args.kwargs["comment"] is None
    assert h.update_draft.await_args.args[3]["status"] == "discarded"


@pytest.mark.asyncio
async def test_discarded_redis_entry_is_ignored_for_lookup():
    h = Harness(redis_entries=[{**REDIS_ENTRY, "status": "discarded"}], listing=[])
    result = await h.run(dispatcher.submit_service_request_handler, ticket_code=TICKET)
    assert result["status"] == "error"
    assert result["reason"] == "missing_target"


@pytest.mark.asyncio
async def test_409_wrong_state_is_verbatim_and_final():
    h = Harness()
    h.approval_action.side_effect = ObsToolAPIError(409, "request is submit — only a draft can be submitted", "/approvals/queue-111/submit")
    result = await h.run(dispatcher.submit_service_request_handler, queue_code="queue-111")
    assert result["status"] == "error"
    assert result["reason"] == "conflict"
    assert "only a draft can be submitted" in result["message"]


@pytest.mark.asyncio
async def test_403_not_author_is_permission_denied():
    h = Harness()
    h.approval_action.side_effect = ObsToolAPIError(403, "only the author can discard their draft", "/approvals/queue-111/discard")
    result = await h.run(dispatcher.discard_service_request_handler, queue_code="queue-111")
    assert result["status"] == "error"
    assert result["reason"] == "permission_denied"
    assert "only the author" in result["message"]


@pytest.mark.asyncio
async def test_404_unknown_request():
    h = Harness()
    h.approval_action.side_effect = ObsToolAPIError(404, "change request queue-999 not found", "/approvals/queue-999/submit")
    result = await h.run(dispatcher.submit_service_request_handler, queue_code="queue-999")
    assert result["status"] == "error"
    assert "not found" in result["message"]
