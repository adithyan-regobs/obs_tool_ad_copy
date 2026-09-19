"""The reviewer's tools: inbox, review, approve, reject (request-changes), revoke."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp.auth import AuthContext
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError

AUTH = AuthContext(user_code="rev1", tenant_code="aspora", user_email="r@x.io", clerk_user_id="")


def _item(code, name, status, *, mine=False, can_approve=True, can_deploy=False, changes=None, env="stage", by="Yahiya", at="2026-09-12T10:00:00Z"):
    return {
        "code": code,
        "status": status,
        "display_name": f"update service:{name} region-aspora-mumbai",
        "resource_code": f"sc-{name}-{env}-1",
        "config_snapshot": {"environment": env},
        "changes": changes if changes is not None else {"port": {"from": None, "to": "8080"}},
        "requested_by": "u1",
        "requested_by_name": by,
        "requested_at": at,
        "history": [{"event": "submitted", "by": "u1", "by_name": by, "at": at, "comment": ""}],
        "you": {"mine": mine, "can_approve": can_approve, "can_deploy": can_deploy, "can_write_settings": False},
    }


class Harness:
    def __init__(self, *, submitted=None, approved=None, single=None, action_result=None):
        self.submitted = submitted or []
        self.approved = approved or []
        self.single = single
        self.list_approvals = AsyncMock(side_effect=self._list)
        self.get_approval = AsyncMock(side_effect=self._get)
        self.approval_action = AsyncMock(
            return_value=action_result if action_result is not None
            else {"ok": True, "approval": _item("queue-1", "demo", "approved"), "detail": "approved"}
        )
        self.update_draft = AsyncMock(return_value=True)

    async def _list(self, *, jwt_token, status=None, resource_code=None):
        items = {"submit": self.submitted, "approved": self.approved}.get(status, self.submitted + self.approved)
        if status is None and self.single is not None:
            # The unfiltered list is how a request is opened now — the caller
            # reads it from here instead of GET /approvals/{code}, which only
            # answers approvers. `single` is that row, so it has to be in it.
            codes = {i.get("code") for i in items}
            if self.single.get("code") not in codes:
                items = items + [self.single]
        return {"approvals": items, "total": len(items)}

    async def _get(self, *, jwt_token, queue_code):
        if self.single is None:
            raise ObsToolAPIError(404, f"change request {queue_code} not found", f"/approvals/{queue_code}")
        return self.single

    def patches(self):
        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.list_approvals = self.list_approvals
        client.get_approval = self.get_approval
        client.approval_action = self.approval_action
        return [
            patch.object(dispatcher, "get_auth_context", AsyncMock(return_value=AUTH)),
            patch("app.mcp_servers.devlift_mcp._internal_jwt.mint_internal_jwt", return_value="jwt"),
            patch("app.mcp_servers.devlift_mcp.obs_tool_client", client),
            patch.object(dispatcher, "get_all_drafts", AsyncMock(return_value=[])),
            patch.object(dispatcher, "update_draft_by_id", self.update_draft),
        ]

    async def run(self, handler, **kwargs):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await handler(**kwargs)


# ── list ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_groups_by_status_and_filters_by_can_approve():
    h = Harness(
        submitted=[_item("queue-a", "demo", "submit"), _item("queue-b", "other", "submit", can_approve=False)],
        approved=[_item("queue-c", "payments", "approved", mine=True)],
    )
    result = await h.run(dispatcher.list_pending_approvals_handler)
    assert result["status"] == "success"
    assert [i["queue_code"] for i in result["waiting_for_review"]] == ["queue-a"]
    assert result["waiting_for_review"][0]["service_name"] == "demo"
    assert result["waiting_for_review"][0]["change_count"] == 1
    assert [i["queue_code"] for i in result["approved_by_you"]] == ["queue-c"]
    assert result["approved_by_you"][0]["mine"] is True
    assert result["message"] == "1 request waiting for your review, 1 approved by you (can still be revoked)."
    assert result["next_action"]["type"] == "present_list"


@pytest.mark.asyncio
async def test_list_empty_inbox():
    h = Harness()
    result = await h.run(dispatcher.list_pending_approvals_handler)
    assert result["waiting_for_review"] == [] and result["approved_by_you"] == []
    assert result["message"] == "Nothing is waiting for your review."


# ── review ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_by_queue_code_shows_diff_and_decisions():
    h = Harness(single=_item("queue-a", "demo", "submit", changes={"port": {"from": None, "to": "8080"}, "cpu_limit": {"from": "1", "to": "2"}}))
    result = await h.run(dispatcher.review_service_request_handler, queue_code="queue-a")
    assert result["status"] == "success"
    assert result["service_name"] == "demo"
    assert result["change_count"] == 2
    assert result["changes"]["cpu_limit"] == {"from": "1", "to": "2"}
    assert result["decisions_available"] == ["approve", "reject"]
    assert result["history"][0]["event"] == "submitted"
    assert "You can: approve, reject." in result["message"]
    assert result["next_action"]["type"] == "present_request"


@pytest.mark.asyncio
async def test_review_own_submission_without_approval_right_offers_withdraw():
    h = Harness(single=_item("queue-a", "demo", "submit", mine=True, can_approve=False))
    result = await h.run(dispatcher.review_service_request_handler, queue_code="queue-a")
    assert result["decisions_available"] == ["withdraw"]


@pytest.mark.asyncio
async def test_review_approved_offers_revoke_and_deploy_by_flags():
    h = Harness(single=_item("queue-a", "demo", "approved", can_deploy=True))
    result = await h.run(dispatcher.review_service_request_handler, queue_code="queue-a")
    assert result["decisions_available"] == ["revoke", "deploy"]


@pytest.mark.asyncio
async def test_review_by_service_name_resolves_from_inbox():
    h = Harness(submitted=[_item("queue-z", "payments-api", "submit")], single=_item("queue-z", "payments-api", "submit"))
    result = await h.run(dispatcher.review_service_request_handler, service_name="payments-api")
    assert result["status"] == "success"
    assert result["queue_code"] == "queue-z"
    # Opened through the list, never the approver-only single-request endpoint.
    h.get_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_not_yours_is_not_found():
    h = Harness(single=None)
    result = await h.run(dispatcher.review_service_request_handler, queue_code="queue-x")
    assert result["status"] == "error"
    assert result["reason"] == "not_found"
    assert "not be one you can see" in result["message"]


@pytest.mark.asyncio
async def test_review_nothing_given():
    h = Harness()
    result = await h.run(dispatcher.review_service_request_handler)
    assert result["reason"] == "missing_target"


# ── decisions ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approve_calls_verb_and_reports_approved():
    h = Harness()
    result = await h.run(dispatcher.approve_service_request_handler, queue_code="queue-1", comment="LGTM")
    assert result["status"] == "success"
    assert result["action"] == "approve"
    assert result["queue_status"] == "approved"
    assert "approved" in result["message"]
    h.approval_action.assert_awaited_once_with(jwt_token="jwt", queue_code="queue-1", verb="approve", comment="LGTM")


@pytest.mark.asyncio
async def test_approve_by_name_looks_in_submitted():
    h = Harness(submitted=[_item("queue-7", "demo", "submit")])
    result = await h.run(dispatcher.approve_service_request_handler, service_name="demo")
    assert result["status"] == "success"
    assert h.list_approvals.await_args_list[0].kwargs == {"jwt_token": "jwt", "status": "submit"}
    assert h.approval_action.await_args.kwargs["queue_code"] == "queue-7"


@pytest.mark.asyncio
async def test_reject_requires_reason_before_any_call():
    h = Harness()
    result = await h.run(dispatcher.reject_service_request_handler, queue_code="queue-1", reason="   ")
    assert result["status"] == "error"
    assert result["reason"] == "missing_reason"
    h.approval_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_calls_request_changes_with_reason():
    h = Harness(action_result={"ok": True, "approval": _item("queue-1", "demo", "draft"), "detail": "sent back"})
    result = await h.run(dispatcher.reject_service_request_handler, queue_code="queue-1", reason="port must be 8080")
    assert result["status"] == "success"
    assert result["action"] == "reject"
    assert result["queue_status"] == "draft"
    assert result["reason_given"] == "port must be 8080"
    assert "sent back" in result["message"] and "Reason: port must be 8080" in result["message"]
    h.approval_action.assert_awaited_once_with(jwt_token="jwt", queue_code="queue-1", verb="request-changes", comment="port must be 8080")


@pytest.mark.asyncio
async def test_revoke_calls_verb_and_reports_submit():
    h = Harness(action_result={"ok": True, "approval": _item("queue-1", "demo", "submit"), "detail": "revoked"})
    result = await h.run(dispatcher.revoke_approval_handler, queue_code="queue-1")
    assert result["status"] == "success"
    assert result["action"] == "revoke"
    assert result["queue_status"] == "submit"
    assert h.approval_action.await_args.kwargs["verb"] == "revoke"


@pytest.mark.asyncio
async def test_revoke_by_name_looks_in_approved():
    h = Harness(approved=[_item("queue-9", "demo", "approved")], action_result={"ok": True, "approval": _item("queue-9", "demo", "submit"), "detail": ""})
    result = await h.run(dispatcher.revoke_approval_handler, service_name="demo")
    assert result["status"] == "success"
    assert h.list_approvals.await_args_list[0].kwargs == {"jwt_token": "jwt", "status": "approved"}


@pytest.mark.asyncio
async def test_self_approval_refusal_is_final():
    h = Harness()
    h.approval_action.side_effect = ObsToolAPIError(403, "self-approval is not allowed for this resource group", "/approvals/queue-1/approve")
    result = await h.run(dispatcher.approve_service_request_handler, queue_code="queue-1")
    assert result["status"] == "error"
    assert result["reason"] == "permission_denied"
    assert "self-approval" in result["message"]


@pytest.mark.asyncio
async def test_revoke_during_deploy_conflict_is_verbatim():
    h = Harness()
    h.approval_action.side_effect = ObsToolAPIError(409, "a deployment of this change is in flight", "/approvals/queue-1/revoke")
    result = await h.run(dispatcher.revoke_approval_handler, queue_code="queue-1")
    assert result["reason"] == "conflict"
    assert "in flight" in result["message"]


def test_clean_service_label():
    assert dispatcher._clean_service_label({"display_name": "update service:demo region-aspora-mumbai"}) == "demo"
    assert dispatcher._clean_service_label({"display_name": "Something else"}) == "Something else"
    assert dispatcher._clean_service_label({"resource_code": "sc-x"}) == "sc-x"
