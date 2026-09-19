"""edit_service_configuration — prefill mapping and the edit-session start."""

from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp import service_payloads as sp
from app.mcp_servers.devlift_mcp.auth import AuthContext
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError
from tests.unit.mcp_servers.test_service_request import REDIS_ENTRY, TICKET

AUTH = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x.io", clerk_user_id="")
SC = "sc-svc-stage-abc"

LIVE_CONFIG = {
    "repository": "Vance-Club/devops-sandbox-golang",
    "branches": ["main"],
    "cpu_requested": "1",
    "cpu_limit": "2",
    "memory_requested": "1",
    "memory_limit": "2",
    "port": "8080",
    "health": "/health",
    "service_path": "/api",
    "alb_schema": "internal",
    "compute": "on-demand",
    "hpa": {"enabled": False},
    "replica_count": "2",
    "create_ecr": True,
    "create_secrets": True,
    "create_ssm": False,
    "create_argo": True,
    "auth_mode": "pod_identity",
    "generate_dockerfile": True,
    "dockerfile_path": "Dockerfile",
    "go_use_aws_secrets": False,
    "custom_iam_policies": ["s3", "sqs", "dynamodb", "ses"],
    "build_args": [{"name": "APP_ENV", "value": "prod"}],
    "namespace": "ad-test-9999-service",
    "cluster_name": "eks-stage",
    "xms": None,
    "build_path": None,
}


# ── prefill mapping ──────────────────────────────────────────────────────────

def test_prefill_maps_config_to_form_answers():
    prefill = sp.build_edit_prefill(
        LIVE_CONFIG,
        service_name="ad-test-9999",
        service_type="API",
        product_name="core",
        environment="stage",
        geo_name="Mumbai",
        resource_group_name="Default service group",
        language_name="Go",
        language_version="1.24",
    )
    assert prefill["product"] == "core"
    assert prefill["environment"] == "Stage"
    assert prefill["geo_location"] == "Mumbai"
    assert prefill["resource_group"] == "Default service group"
    assert prefill["service_name"] == "ad-test-9999"
    assert prefill["service_type"] == "API"
    assert prefill["repository"] == "Vance-Club/devops-sandbox-golang"
    assert prefill["branches"] == ["main"]
    assert prefill["language"] == "Go" and prefill["version"] == "1.24"
    assert prefill["cpu_requested"] == "1" and prefill["cpu_limit"] == "2"
    assert prefill["port"] == "8080"
    assert prefill["hpa_enabled"] == "false" and prefill["replica_count"] == "2"
    assert "min_replicas" not in prefill
    assert prefill["create_ecr"] == "true" and prefill["create_ssm"] == "false"
    assert prefill["generate_dockerfile"] == "true"
    assert prefill["go_use_aws_secrets"] == "false"
    assert prefill["custom_iam_policies"] == ["s3", "sqs", "dynamodb", "ses"]  # multi-select, whole list
    assert prefill["build_args"] == {"APP_ENV": "prod"}
    # Untouched / unanswerable keys are not form fields and are dropped.
    for key in ("namespace", "cluster_name", "xms", "build_path"):
        assert key not in prefill


def test_prefill_hpa_enabled_carries_min_max_not_replica_count():
    cfg = {**LIVE_CONFIG, "hpa": {"enabled": True, "min_replicas": 1, "max_replicas": 4}}
    prefill = sp.build_edit_prefill(cfg, service_name="x", service_type="API", product_name="core",
                                    environment="qa", geo_name="Mumbai", resource_group_name="rg",
                                    language_name="Go", language_version="1.24")
    assert prefill["hpa_enabled"] == "true"
    assert prefill["min_replicas"] == "1" and prefill["max_replicas"] == "4"
    assert "replica_count" not in prefill
    assert prefill["environment"] == "QA"


@pytest.mark.parametrize("name,code,expected", [
    ("Go 1.24", "GO_1_24", "Go"),
    ("Python 3.10", "PYTHON_3_10", "Python"),
    ("Java 17", "JAVA-MAVEN_17", "Java Maven"),
    ("Java 17", "JAVA_17", "Java Gradle"),
    ("Node.js 20", "NODE_20", "Node.js"),
    (None, None, None),
])
def test_language_base_name(name, code, expected):
    assert sp.language_base_name(name, code) == expected


# ── handler ──────────────────────────────────────────────────────────────────

LIVE_ROW = {
    "code": SC,
    "name": "ad-test-9999",
    "services_mst_code": "svc-1",
    "environment": "stage",
    "geo_loc_mst_code": "region-aspora-mumbai",
    "language_ref_code": "GO_1_24",
    "config": LIVE_CONFIG,
}


class Harness:
    def __init__(self, *, approvals=None, redis_entries=None):
        self.redis_entries = (
            [dict(REDIS_ENTRY, transaction_code=SC, identifier="ad-test-9999")]
            if redis_entries is None else redis_entries
        )
        self.get_service_config = AsyncMock(return_value=LIVE_ROW)
        self.list_approvals = AsyncMock(return_value={"approvals": approvals or [], "total": len(approvals or [])})
        self.post_select_form = AsyncMock(return_value={
            "status": "pending",
            "message": "Loaded the current settings of **ad-test-9999**. Tell me what to change.",
            "missing_fields": {"required": [], "optional": []},
            "prefilled_fields": ["product", "environment"],
        })
        self.cache_edit_origin = AsyncMock(return_value=True)
        self.mark_session_prefilled = AsyncMock(return_value=True)
        # DB rows: services_mst join, geo name, language_ref
        # Mirrors the services_mst join: name, type, product name, group name,
        # then the two codes the placement guard compares against.
        self.svc_row = ("ad-test-9999", "API", "core", "Default service group",
                        "app-core", "rg-default")
        self.geo_row = ("Mumbai",)
        self.lang_row = ("Go 1.24", "1.24")

    def _session_factory(self):
        h = self

        @asynccontextmanager
        async def session():
            db = MagicMock(name="db")
            async def execute(stmt, params=None):
                sql = str(stmt)
                res = MagicMock()
                if "services_mst" in sql:
                    res.fetchone.return_value = h.svc_row
                elif "geo_loc_mst" in sql:
                    res.fetchone.return_value = h.geo_row
                elif "language_ref" in sql:
                    res.fetchone.return_value = h.lang_row
                else:
                    res.scalars.return_value.all.return_value = []
                    res.fetchone.return_value = None
                return res
            db.execute = AsyncMock(side_effect=execute)
            yield db

        return session

    def patches(self):
        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.get_service_config = self.get_service_config
        client.list_approvals = self.list_approvals
        services_repo = MagicMock()
        services_repo.find_by_name_for_mcp = AsyncMock(return_value=None)
        return [
            patch.object(dispatcher, "get_auth_context", AsyncMock(return_value=AUTH)),
            patch("app.mcp_servers.devlift_mcp._internal_jwt.mint_internal_jwt", return_value="jwt"),
            patch("app.mcp_servers.devlift_mcp.obs_tool_client", client),
            patch("app.mcp_servers.devlift_mcp.chatbot_client.post_select_form", self.post_select_form),
            # The edit pins its placement in Redis so the save can refuse a
            # move; capture it here instead of reaching a real connection.
            patch("app.mcp_servers.devlift_mcp.chatbot_client.cache_edit_origin", self.cache_edit_origin),
            # Edit and clone claim the language-template slot as they open, so
            # a session that already holds a service's values is never offered
            # defaults in place of them.
            patch("app.mcp_servers.devlift_mcp.chatbot_client.mark_session_prefilled",
                  self.mark_session_prefilled),
            patch.object(dispatcher, "get_all_drafts", AsyncMock(return_value=self.redis_entries)),
            patch.object(dispatcher, "AsyncSessionLocal", self._session_factory()),
            patch.object(dispatcher, "ServicesMstRepository", return_value=services_repo),
        ]

    async def run(self, **kwargs):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await dispatcher.start_service_edit_handler(**kwargs)


@pytest.mark.asyncio
async def test_edit_starts_prefilled_session_and_returns_new_ticket():
    h = Harness()
    result = await h.run(service_name="ad-test-9999")

    assert result["status"] == "success"
    assert result["ticket_code"].startswith("mcp-u1-")
    assert result["ticket_code"] != TICKET
    assert result["service_config_code"] == SC
    assert result["editing_request"] is None
    assert result["next_action"]["type"] == "continue_chat"
    assert result["ticket_code"] in result["next_action"]["instruction"]

    h.post_select_form.assert_awaited_once()
    kw = h.post_select_form.await_args.kwargs
    assert kw["form_id"] == "eks_service_form"
    assert kw["ticket_code"] == result["ticket_code"]
    assert kw["jwt_token"] == "jwt"
    prefill = kw["prefill"]
    assert prefill["service_name"] == "ad-test-9999"
    assert prefill["product"] == "core"
    assert prefill["environment"] == "Stage"
    assert prefill["geo_location"] == "Mumbai"
    assert prefill["resource_group"] == "Default service group"
    assert prefill["language"] == "Go" and prefill["version"] == "1.24"
    assert prefill["repository"] == "Vance-Club/devops-sandbox-golang"  # trusted as-is
    assert prefill["cpu_requested"] == "1"


@pytest.mark.asyncio
async def test_pending_draft_values_overlay_live_for_editing():
    draft = {"code": "queue-9", "status": "draft", "resource_code": SC, "requested_at": "2026-09-12T06:00:00Z",
             "config_snapshot": {"config": {"cpu_requested": "3", "port": "9090"}, "language_ref_code": "GO_1_23"},
             "you": {"mine": True}}
    h = Harness(approvals=[draft])
    h.lang_row = ("Go 1.23", "1.23")
    result = await h.run(service_name="ad-test-9999")
    assert result["editing_request"] == {"queue_code": "queue-9", "status": "draft"}
    prefill = h.post_select_form.await_args.kwargs["prefill"]
    assert prefill["cpu_requested"] == "3"   # draft wins
    assert prefill["port"] == "9090"
    assert prefill["cpu_limit"] == "2"       # untouched by the draft → live
    assert prefill["version"] == "1.23"


@pytest.mark.asyncio
async def test_403_on_config_is_permission_error():
    h = Harness()
    h.get_service_config.side_effect = ObsToolAPIError(403, "denied", "/service-configs/by-code/x")
    result = await h.run(service_name="ad-test-9999")
    assert result["status"] == "error"
    assert result["reason"] == "permission_denied"
    h.post_select_form.assert_not_awaited()


@pytest.mark.asyncio
async def test_chatbot_failure_is_reported():
    h = Harness()
    h.post_select_form.side_effect = RuntimeError("chatbot down")
    result = await h.run(service_name="ad-test-9999")
    assert result["status"] == "error"
    assert "chatbot down" in result["message"]


@pytest.mark.asyncio
async def test_unknown_service():
    h = Harness(redis_entries=[])
    result = await h.run(service_name="ghost")
    assert result["status"] == "error"
    assert result["reason"] == "not_found"
