"""deploy_service_request — the web's Deploy button: confirm, gate, start,
remember for get_deployment_status."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp.auth import AuthContext
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError
from tests.unit.mcp_servers.test_service_gateway import STORED_GROUPS

AUTH = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x.io", clerk_user_id="")
SC = "sc-demo-stage-1"


def _row(code, *, status="approved", env="stage", gateway=False, can_deploy=True, in_flight=False, row_id=41):
    return {
        "id": row_id,
        "code": code,
        "status": status,
        "case_ref_code": "add_route" if gateway else "update_service",
        "display_name": f"Gateway routes - demo" if gateway else f"update service:demo region-aspora-mumbai",
        "resource_code": SC,
        "requested_by": "u1",
        "requested_by_name": "Yahiya",
        "requested_at": "2026-09-14T10:00:00Z",
        "config_snapshot": {"environment": env},
        "changes": {"groups": STORED_GROUPS} if gateway else {"cpu_requested": {"from": "1m", "to": "0.4m"}},
        "history": [],
        "you": {"mine": True, "can_approve": False, "can_deploy": can_deploy, "can_write_settings": True, "deploy_in_flight": in_flight},
    }


class Harness:
    def __init__(self, *, rows, single=None, redis_entries=None):
        self.rows = rows
        self.single = single if single is not None else rows[0]
        self.redis_entries = redis_entries if redis_entries is not None else []
        self.get_approval = AsyncMock(return_value=self.single)
        self.list_approvals = AsyncMock(side_effect=self._list)
        self.approval_action = AsyncMock(return_value={"ok": True, "approval": self.single, "deploy": {}, "detail": "cleared"})
        self.multiple_deploy = AsyncMock(return_value={"workflow_id": "multi-deploy-aspora-abc123", "status": "started"})
        self.add_draft = AsyncMock(return_value=True)
        self.update_draft = AsyncMock(return_value=True)

    async def _list(self, *, jwt_token, status=None, resource_code=None):
        items = [r for r in self.rows if (status is None or r["status"] == status)]
        return {"approvals": items, "total": len(items)}

    def patches(self):
        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.get_approval = self.get_approval
        client.list_approvals = self.list_approvals
        client.approval_action = self.approval_action
        client.multiple_deploy = self.multiple_deploy
        return [
            patch.object(dispatcher, "get_auth_context", AsyncMock(return_value=AUTH)),
            patch("app.mcp_servers.devlift_mcp._internal_jwt.mint_internal_jwt", return_value="jwt"),
            patch("app.mcp_servers.devlift_mcp.obs_tool_client", client),
            patch.object(dispatcher, "get_all_drafts", AsyncMock(return_value=self.redis_entries)),
            patch.object(dispatcher, "add_draft", self.add_draft),
            patch.object(dispatcher, "update_draft_by_id", self.update_draft),
        ]

    async def run(self, **kwargs):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await dispatcher.deploy_service_request_handler(**kwargs)


@pytest.mark.asyncio
async def test_first_call_asks_for_confirmation_with_both_halves():
    h = Harness(rows=[_row("queue-a"), _row("queue-a-gw", gateway=True, row_id=42)])
    result = await h.run(queue_code="queue-a")

    assert result["status"] == "needs_confirmation"
    assert result["shipped"] == ["configuration", "gateway"]
    assert result["changes"] == {"cpu_requested": {"from": "1m", "to": "0.4m"}}
    assert result["gateway_changes"][0]["added"] == ["~/api/v1/orders$"]
    assert result["production"] is False
    assert result["next_action"]["type"] == "confirm_deploy"
    assert "'Deploy' and 'Cancel'" in result["next_action"]["instruction"]
    assert "queue_code='queue-a'" in result["next_action"]["instruction"]
    h.approval_action.assert_not_awaited()
    h.multiple_deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_deploy_gates_then_starts_with_both_parts_and_remembers_it():
    h = Harness(rows=[_row("queue-a"), _row("queue-a-gw", gateway=True, row_id=42)])
    result = await h.run(queue_code="queue-a", confirmed=True, project_id="proj-1")

    assert result["status"] == "success"
    assert result["action"] == "deploy"
    assert result["workflow_id"] == "multi-deploy-aspora-abc123"
    assert "the configuration and the gateway routes" in result["message"]
    assert result["next_action"]["type"] == "poll_deployment"
    assert "/loop" in result["next_action"]["instruction"] and "get_deployment_status" in result["next_action"]["instruction"]

    h.approval_action.assert_awaited_once_with(jwt_token="jwt", queue_code="queue-a", verb="deploy")
    kwargs = h.multiple_deploy.await_args.kwargs
    assert kwargs["service_config_code"] == SC
    assert kwargs["item_ids"] == [41]              # the settings row only
    assert kwargs["include_gateway"] is True       # routes by code, server-resolved

    h.add_draft.assert_awaited_once()
    entry = h.add_draft.await_args.args[2]
    assert entry["deploy_in_progress"] is True and entry["deploy_mode"] == "temporal"
    assert entry["workflow_id"] == "multi-deploy-aspora-abc123"
    assert entry["transaction_code"] == SC and entry["identifier"] == "demo"
    assert entry["resource_type"] == "eks_service" and entry["queue_code"] == "queue-a"


@pytest.mark.asyncio
async def test_configuration_only_request_sends_no_gateway_part():
    h = Harness(rows=[_row("queue-a")])
    result = await h.run(queue_code="queue-a", confirmed=True)
    assert result["status"] == "success"
    assert result["shipped"] == ["configuration"]
    assert h.multiple_deploy.await_args.kwargs["include_gateway"] is False


@pytest.mark.asyncio
async def test_existing_redis_entry_is_updated_not_duplicated():
    existing = {"draft_id": "d1", "transaction_code": SC, "identifier": "demo", "status": "pending",
                "ticket_code": "mcp-u1-cfg", "queue_code": "queue-a", "queue_status": "approved"}
    h = Harness(rows=[_row("queue-a")], redis_entries=[existing])
    await h.run(queue_code="queue-a", confirmed=True)
    h.add_draft.assert_not_awaited()
    updated = h.update_draft.await_args.args[3]
    assert updated["workflow_id"] == "multi-deploy-aspora-abc123" and updated["queue_status"] == "deploying"


@pytest.mark.asyncio
async def test_production_needs_the_typed_service_name():
    h = Harness(rows=[_row("queue-p", env="prod")])
    asks = await h.run(queue_code="queue-p", confirmed=True)          # confirmed but nothing typed
    assert asks["status"] == "needs_confirmation" and asks["production"] is True
    assert "TYPE the service name" in asks["next_action"]["instruction"]
    wrong = await h.run(queue_code="queue-p", confirmed=True, confirm_service_name="demo-x")
    assert wrong["status"] == "error" and wrong["reason"] == "confirmation_mismatch"
    h.approval_action.assert_not_awaited()
    ok = await h.run(queue_code="queue-p", confirmed=True, confirm_service_name="Demo")
    assert ok["status"] == "success" and "promotion PR" in ok["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [("draft", "wrong_status"), ("submit", "wrong_status")])
async def test_not_approved_is_refused(status, reason):
    h = Harness(rows=[_row("queue-a", status=status)])
    result = await h.run(queue_code="queue-a", confirmed=True)
    assert result["status"] == "error" and result["reason"] == reason
    h.approval_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_deploy_right_and_in_flight_are_refused():
    h = Harness(rows=[_row("queue-a", can_deploy=False)])
    result = await h.run(queue_code="queue-a", confirmed=True)
    assert result["reason"] == "permission_denied"
    h = Harness(rows=[_row("queue-a", in_flight=True)])
    result = await h.run(queue_code="queue-a", confirmed=True)
    assert result["reason"] == "already_deploying"
    h.approval_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_broken_seal_at_the_gate_is_verbatim_and_stops():
    h = Harness(rows=[_row("queue-a")])
    h.approval_action.side_effect = ObsToolAPIError(
        409, "This request changed after it was approved, so it will not be deployed. Send it back through review.", "/approvals/queue-a/deploy"
    )
    result = await h.run(queue_code="queue-a", confirmed=True)
    assert result["status"] == "error"
    assert "changed after it was approved" in result["message"]
    h.multiple_deploy.assert_not_awaited()
    h.add_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_failure_leaves_redis_untouched():
    h = Harness(rows=[_row("queue-a")])
    h.multiple_deploy.side_effect = ObsToolAPIError(400, "Temporal is not enabled — multiple-deploy requires the Temporal deploy flow", "/deployments/multiple-deploy")
    result = await h.run(queue_code="queue-a", confirmed=True)
    assert result["status"] == "error"
    assert "Temporal is not enabled" in result["message"]
    h.add_draft.assert_not_awaited()
    h.update_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_by_service_name_finds_the_approved_request():
    h = Harness(rows=[_row("queue-a")])
    result = await h.run(service_name="demo")
    assert result["status"] == "needs_confirmation"
    assert h.list_approvals.await_args_list[0].kwargs == {"jwt_token": "jwt", "status": "approved"}


# ── approved by someone else ────────────────────────────────────────────────
# Reported from production: A holds write + deploy, B holds approve. B approves
# A's change, A deploys, and the tools answered "I can't find a request
# 'queue-...' that you may deploy" for a request A was entitled to ship. Both
# review and deploy opened it with GET /approvals/{code}, which answers only
# callers who may APPROVE — a different right, held by B. The web never had the
# bug because its Deploy button reads the row from the list and posts straight
# to /approvals/{code}/deploy; it never calls the single-request endpoint.

def _approved_by_someone_else(code="queue-b5ffa7c4876a"):
    row = _row(code)
    row["requested_by"] = "u1"          # ours
    row["decided_by"] = "approver-2"    # someone else approved it
    row["you"] = {
        "mine": True,
        "can_approve": False,           # we may NOT approve
        "can_deploy": True,             # but we MAY deploy
        "can_write_settings": True,
        "deploy_in_flight": False,
    }
    return row


@pytest.mark.asyncio
async def test_deploy_works_when_another_person_approved_it():
    h = Harness(rows=[_approved_by_someone_else()])
    # The approver-only endpoint would 404 us, as it does in production.
    h.get_approval = AsyncMock(side_effect=ObsToolAPIError(
        404, "change request not found", "/approvals/queue-b5ffa7c4876a"))
    result = await h.run(queue_code="queue-b5ffa7c4876a")
    assert result["status"] != "error", result.get("message")
    h.get_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_works_when_another_person_approved_it():
    h = Harness(rows=[_approved_by_someone_else()])
    h.get_approval = AsyncMock(side_effect=ObsToolAPIError(
        404, "change request not found", "/approvals/queue-b5ffa7c4876a"))
    with ExitStack() as stack:
        for patcher in h.patches():
            stack.enter_context(patcher)
        result = await dispatcher.review_service_request_handler(
            queue_code="queue-b5ffa7c4876a")
    assert result["status"] == "success", result.get("message")
    assert result["can_deploy"] is True
    assert result["can_approve"] is False
    h.get_approval.assert_not_awaited()
