"""view_service_settings — every field + value, grouped like the Settings tab,
draft values overlaid on live ones."""

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
    "language_ref_code": None,
    "sync_status": "NEVER_SYNCED",
    "config": {
        "namespace": "sample-mcp-service",
        "alb_selection": "existing_alb",
        "cluster_name": "eks-stage",
        "compute": "on-demand",
        "auth_mode": "pod_identity",
        "create_ecr": True,
        "generate_dockerfile": False,
        "port": None,
        "repository": None,
    },
}


def _draft(config: dict, root: dict | None = None, status="draft"):
    item = _item("queue-111", "update service:sample-mcp-service region-aspora-mumbai", status)
    item["resource_code"] = SC
    item["config_snapshot"] = {"config": config, **(root or {})}
    return item


class Harness:
    def __init__(self, *, live=None, approvals=None, redis_entries=None):
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
            return await dispatcher.view_service_settings_handler(**kwargs)


def _field(result, section, name):
    sec = next(s for s in result["sections"] if s["title"] == section)
    return next(f for f in sec["fields"] if f["field"] == name)


@pytest.mark.asyncio
async def test_live_only_when_no_pending_request():
    h = Harness(approvals=[])
    result = await h.run(ticket_code=TICKET)
    assert result["status"] == "success"
    assert result["pending_request"] is None
    titles = [s["title"] for s in result["sections"]]
    assert titles == ["Placement", "Manifest", "AWS Resource Provisioning"] or "Placement" in titles
    assert _field(result, "Placement", "environment") == {"field": "environment", "value": "stage", "source": "live"}
    assert _field(result, "Placement", "namespace")["value"] == "sample-mcp-service"
    assert _field(result, "Manifest", "compute")["value"] == "on-demand"
    assert _field(result, "AWS Resource Provisioning", "create_ecr")["value"] is True
    # Unset fields are not listed (port, repository are None live).
    assert all(f["field"] != "port" for s in result["sections"] for f in s["fields"])
    assert result["message"].endswith("all live.")
    assert result["next_action"]["type"] == "present_settings"


@pytest.mark.asyncio
async def test_draft_values_overlay_live_and_are_marked():
    draft = _draft(
        {"repository": "Regobs/x", "branches": ["main"], "port": "8080", "cpu_requested": "2",
         "hpa": {"enabled": False}, "generate_dockerfile": True, "custom_iam_policies": ["s3"]},
        root={"language_name": "Go", "language_version": "1.24", "language_ref_code": "GO_1_24"},
    )
    h = Harness(approvals=[draft])
    result = await h.run(ticket_code=TICKET)
    assert result["pending_request"]["queue_code"] == "queue-111"
    assert _field(result, "Repository", "repository") == {"field": "repository", "value": "Regobs/x", "source": "draft"}
    assert _field(result, "Repository", "branches")["value"] == ["main"]
    assert _field(result, "Manifest", "port")["source"] == "draft"
    assert _field(result, "Manifest", "hpa")["value"] == {"enabled": False}
    assert _field(result, "Dockerfile", "language_version") == {"field": "language_version", "value": "1.24", "source": "draft"}
    assert _field(result, "Dockerfile", "generate_dockerfile")["value"] is True  # draft wins over live False
    assert _field(result, "AWS Resource Provisioning", "custom_iam_policies")["value"] == ["s3"]
    # Live-only values stay live.
    assert _field(result, "Placement", "namespace")["source"] == "live"
    assert "from the draft request" in result["message"]


@pytest.mark.asyncio
async def test_boolean_false_is_still_listed():
    h = Harness(approvals=[])
    result = await h.run(ticket_code=TICKET)
    assert _field(result, "Dockerfile", "generate_dockerfile") == {"field": "generate_dockerfile", "value": False, "source": "live"}


@pytest.mark.asyncio
async def test_approvals_failure_is_non_fatal():
    h = Harness()
    h.list_approvals.side_effect = ObsToolAPIError(500, "boom", "/approvals")
    result = await h.run(ticket_code=TICKET)
    assert result["status"] == "success"
    assert result["pending_request"] is None


@pytest.mark.asyncio
async def test_403_is_permission_error():
    h = Harness()
    h.get_service_config.side_effect = ObsToolAPIError(403, "denied", "/service-configs/by-code/x")
    result = await h.run(ticket_code=TICKET)
    assert result["status"] == "error"
    assert result["reason"] == "permission_denied"


@pytest.mark.asyncio
async def test_nothing_given():
    h = Harness(redis_entries=[])
    result = await h.run()
    assert result["reason"] == "missing_target"


def test_sections_cover_every_form_field_once():
    seen = [k for _, keys in dispatcher._SETTINGS_SECTIONS for k in keys]
    assert len(seen) == len(set(seen))
    for key in ("repository", "branches", "cpu_requested", "port", "hpa", "custom_iam_policies", "auth_mode", "namespace"):
        assert key in seen


@pytest.mark.asyncio
async def test_name_resolves_from_database_when_no_draft_or_request_exists():
    """A service whose drafts were all discarded is still readable by name."""
    from types import SimpleNamespace
    h = Harness(redis_entries=[], approvals=[])
    h.db_service_row = SimpleNamespace(code="svc-db", name="sample-mcp-service")
    h.db_configs = [SimpleNamespace(code=SC, environment=SimpleNamespace(value="stage"), geo_loc_mst_code="region-aspora-mumbai")]
    result = await h.run(service_name="sample-mcp-service")
    assert result["status"] == "success"
    assert h.get_service_config.await_args.kwargs["service_config_code"] == SC
    h.list_approvals.assert_awaited_once_with(jwt_token="jwt", resource_code=SC)


@pytest.mark.asyncio
async def test_two_database_configs_ask_to_choose():
    from types import SimpleNamespace
    h = Harness(redis_entries=[], approvals=[])
    h.db_service_row = SimpleNamespace(code="svc-db", name="demo")
    h.db_configs = [
        SimpleNamespace(code="sc-demo-stage-1", environment=SimpleNamespace(value="stage"), geo_loc_mst_code="geo-mumbai"),
        SimpleNamespace(code="sc-demo-prod-1", environment=SimpleNamespace(value="prod"), geo_loc_mst_code="geo-london"),
    ]
    result = await h.run(service_name="demo")
    assert result["reason"] == "ambiguous_target"
    assert [o["value"] for o in result["next_action"]["options"]] == ["sc-demo-stage-1", "sc-demo-prod-1"]
    assert "stage" in result["next_action"]["options"][0]["label"]


# ── gateway routes are opt-in ────────────────────────────────────────────────
# Asked "what are the config for wealth in core stage", the reply carried the
# settings AND every Kong route — dozens of regex paths that buried the answer.
# Routes belong to the Gateway tab; "show the config" is not a request for them.

GATEWAY_STATE = {"groups": [
    {"http_method": "GET", "route_group_key": "wealth-service-routes",
     "plugins": ["jwt"], "regex_priority": 0,
     "paths": [{"route_path": "~/wealth/v1/portfolio$", "code": "r1"},
               {"route_path": "~/wealth/v1/prices/chart$", "code": "r2"}]},
    {"http_method": "POST", "route_group_key": "wealth-callback",
     "plugins": [], "regex_priority": 0,
     "paths": [{"route_path": "~/wealth/v1/external/payment/callback$", "code": "r3"}]},
]}


def _with_gateway(h):
    h.get_gateway_state = AsyncMock(return_value=GATEWAY_STATE)
    original = h.patches

    def patches():
        out = original()
        for p in out:
            target = getattr(p, "new", None)
            if isinstance(target, MagicMock) and hasattr(target, "get_service_config"):
                target.get_gateway_state = h.get_gateway_state
        return out

    h.patches = patches
    return h


@pytest.mark.asyncio
async def test_routes_are_withheld_by_default_but_counted():
    h = _with_gateway(Harness(approvals=[]))
    result = await h.run(ticket_code=TICKET)

    assert result["gateway_routes"] == []          # not handed over
    assert result["gateway_route_count"] == 2      # ... but the user is told
    text = result["next_action"]["instruction"]
    assert "Do NOT list the gateway routes" in text
    assert "include_gateway=true" in text          # and how to get them


@pytest.mark.asyncio
async def test_routes_are_returned_when_asked_for():
    h = _with_gateway(Harness(approvals=[]))
    result = await h.run(ticket_code=TICKET, include_gateway=True)

    assert len(result["gateway_routes"]) == 2
    assert result["gateway_route_count"] == 2
    text = result["next_action"]["instruction"]
    assert "Gateway routes" in text and "Method" in text
    assert "Do NOT list" not in text


@pytest.mark.asyncio
async def test_a_service_with_no_routes_says_nothing_about_them():
    h = Harness(approvals=[])
    h.get_gateway_state = AsyncMock(return_value={"groups": []})
    result = await h.run(ticket_code=TICKET)

    assert result["gateway_route_count"] == 0
    assert "gateway" not in result["next_action"]["instruction"].lower()
