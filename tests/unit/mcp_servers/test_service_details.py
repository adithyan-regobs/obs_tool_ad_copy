"""get_service_configuration — live config + pending request preview."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp.auth import AuthContext
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError
from tests.unit.mcp_servers.test_service_request import REDIS_ENTRY, TICKET, _item

AUTH = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x.io", clerk_user_id="")
SC = "sc-svc-stage-abc"

LIVE = {
    "code": SC,
    "name": "sample-mcp-service",
    "environment": "stage",
    "geo_loc_mst_code": "region-aspora-mumbai",
    "infrastructure_mst_code": "infra-eks-1",
    "sync_status": "NEVER_SYNCED",
    "config": {"namespace": "sample-mcp-service", "alb_selection": "existing_alb", "cluster_name": "eks-stage", "port": None},
}


def _req(code, status, changes=None, requested_at="2026-09-12T06:00:00Z", by="Yahiya"):
    item = _item(code, "update service:sample-mcp-service region-aspora-mumbai", status, changes=changes)
    item["resource_code"] = SC
    item["requested_at"] = requested_at
    item["requested_by_name"] = by
    return item


class Harness:
    def __init__(self, *, redis_entries=None, live=None, approvals=None):
        self.redis_entries = [REDIS_ENTRY] if redis_entries is None else redis_entries
        self.get_service_config = AsyncMock(return_value=LIVE if live is None else live)
        self.list_approvals = AsyncMock(return_value={"approvals": approvals or [], "total": len(approvals or [])})

    def _session_factory(self):
        configs = getattr(self, "db_configs", [])
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def session():
            db = MagicMock(name="db")
            result = MagicMock()
            result.scalars.return_value.all.return_value = configs
            db.execute = AsyncMock(return_value=result)
            yield db

        return session

    def _services_repo(self):
        repo = MagicMock()
        repo.find_by_name_for_mcp = AsyncMock(return_value=getattr(self, "db_service_row", None))
        return repo

    def patches(self):
        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.get_service_config = self.get_service_config
        client.list_approvals = self.list_approvals
        return [
            patch.object(dispatcher, "get_auth_context", AsyncMock(return_value=AUTH)),
            patch("app.mcp_servers.devlift_mcp._internal_jwt.mint_internal_jwt", return_value="jwt"),
            patch("app.mcp_servers.devlift_mcp.obs_tool_client", client),
            patch.object(dispatcher, "get_all_drafts", AsyncMock(return_value=self.redis_entries)),
            patch.object(dispatcher, "AsyncSessionLocal", self._session_factory()),
            patch.object(dispatcher, "ServicesMstRepository", return_value=self._services_repo()),
        ]

    async def run(self, **kwargs):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await dispatcher.get_service_configuration_handler(**kwargs)


@pytest.mark.asyncio
async def test_by_ticket_returns_live_config_and_draft_diff():
    changes = {"port": {"from": None, "to": "8080"}, "repository": {"from": None, "to": "Regobs/x"}}
    h = Harness(approvals=[_req("queue-111", "draft", changes)])
    result = await h.run(ticket_code=TICKET)

    assert result["status"] == "success"
    assert result["service_config_code"] == "sc-svc-stage-abc"
    assert result["environment"] == "stage"
    assert result["cluster_name"] == "eks-stage"
    assert result["live_config"] == {"namespace": "sample-mcp-service", "alb_selection": "existing_alb", "cluster_name": "eks-stage"}
    assert result["pending_request"]["queue_code"] == "queue-111"
    assert result["pending_request"]["status"] == "draft"
    assert result["pending_request"]["change_count"] == 2
    assert result["pending_request"]["changes"] == changes
    assert result["pending_request"]["you"]["mine"] is True
    assert "2 configuration change(s) pending in a draft request by Yahiya" in result["message"]
    assert result["next_action"]["type"] == "present_configuration"
    h.get_service_config.assert_awaited_once_with(jwt_token="jwt", service_config_code="sc-svc-stage-abc")
    h.list_approvals.assert_awaited_once_with(jwt_token="jwt", resource_code="sc-svc-stage-abc")


@pytest.mark.asyncio
async def test_by_service_name_uses_redis_entry_first():
    h = Harness()
    result = await h.run(service_name="sample-mcp-service")
    assert result["status"] == "success"
    assert result["service_name"] == "sample-mcp-service"
    assert h.get_service_config.await_args.kwargs["service_config_code"] == "sc-svc-stage-abc"


@pytest.mark.asyncio
async def test_by_service_name_falls_back_to_approvals_listing():
    h = Harness(redis_entries=[], approvals=[_req("queue-5", "submit")])
    result = await h.run(service_name="sample-mcp-service")
    assert result["status"] == "success"
    assert h.get_service_config.await_args.kwargs["service_config_code"] == SC
    assert result["pending_request"]["status"] == "submit"


@pytest.mark.asyncio
async def test_no_pending_request():
    h = Harness(approvals=[])
    result = await h.run(ticket_code=TICKET)
    assert result["pending_request"] is None
    assert result["message"].endswith("No pending changes.")


@pytest.mark.asyncio
async def test_lane_request_preferred_over_finished_ones():
    h = Harness(approvals=[_req("queue-old", "deployed"), _req("queue-live", "submit"), _req("queue-rej", "rejected")])
    result = await h.run(ticket_code=TICKET)
    assert result["pending_request"]["queue_code"] == "queue-live"


@pytest.mark.asyncio
async def test_approvals_failure_is_non_fatal():
    h = Harness()
    h.list_approvals.side_effect = ObsToolAPIError(500, "boom", "/approvals")
    result = await h.run(ticket_code=TICKET)
    assert result["status"] == "success"
    assert result["pending_request"] is None
    assert result["live_config"]["cluster_name"] == "eks-stage"


@pytest.mark.asyncio
async def test_403_on_live_config_is_permission_error():
    h = Harness()
    h.get_service_config.side_effect = ObsToolAPIError(403, "can_view_settings denied", "/service-configs/by-code/x")
    result = await h.run(ticket_code=TICKET)
    assert result["status"] == "error"
    assert result["reason"] == "permission_denied"


@pytest.mark.asyncio
async def test_unknown_name_is_not_found():
    h = Harness(redis_entries=[], approvals=[])
    result = await h.run(service_name="ghost")
    assert result["status"] == "error"
    assert result["reason"] == "not_found"


@pytest.mark.asyncio
async def test_two_configs_ask_to_choose():
    a = _req("queue-a", "draft"); a["resource_code"] = "sc-a-stage-1"
    b = _req("queue-b", "submit"); b["resource_code"] = "sc-b-prod-1"
    h = Harness(redis_entries=[], approvals=[a, b])
    result = await h.run(service_name="sample-mcp-service")
    assert result["reason"] == "ambiguous_target"
    assert [o["value"] for o in result["next_action"]["options"]] == ["sc-a-stage-1", "sc-b-prod-1"]


@pytest.mark.asyncio
async def test_nothing_given():
    h = Harness(redis_entries=[])
    result = await h.run()
    assert result["reason"] == "missing_target"
