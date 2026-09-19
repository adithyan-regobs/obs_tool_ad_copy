"""Kong gateway routes through the existing tools: the kong_route_form result
recognised by `chat`, saved by create_service_and_save_draft as the gateway
half of the draft, opened by edit_service_configuration(section='gateway'),
and shown next to the configuration by the read / review tools."""

from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp import service_payloads as sp
from app.mcp_servers.devlift_mcp.auth import AuthContext
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError
from app.mcp_servers.devlift_mcp.tools import chat as chat_tool
from tests.unit.mcp_servers.test_service_approval import Harness as ApprovalHarness, _item as _approval_item
from tests.unit.mcp_servers.test_service_details import Harness as DetailsHarness, _req, TICKET
from tests.unit.mcp_servers.test_service_edit import Harness as EditHarness, SC

AUTH = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x.io", clerk_user_id="")
GW_TICKET = "mcp-u1-gw000001"

GROUP = {
    "service_mst_code": "svc-1",
    "service_name": "ad-test-9999",
    "applications_mst_code": "app-core",
    "product_name": "Core",
    "environment": "stage",
    "geo_loc_mst_code": "region-aspora-mumbai",
    "http_method": "GET",
    "secured": True,
    "route_group_key": None,
    "regex_priority": 0,
    "plugins": None,
    "paths": ["~/api/v1/users$", "~/api/v1/orders$"],
}

STATE_EMPTY = {"service_mst_code": "svc-1", "service_name": "ad-test-9999", "environment": "stage",
               "geo_loc_mst_code": "region-aspora-mumbai", "groups": []}

STATE_WITH_GET = {
    **STATE_EMPTY,
    "groups": [{
        "code": "KRG_1", "route_group_key": "ad-test-9999", "http_method": "GET",
        "plugins": ["JWT"], "regex_priority": 0, "is_service_owner": True,
        "updated_at": "2026-09-10T10:00:00Z",
        "paths": [{"code": "KRC_1", "route_path": "~/api/v1/users$", "creation_status": "ACTIVE"}],
        "pending": [],
    }],
}

STORED_GROUPS = [{
    "group_code": None, "route_group_key": "ad-test-9999", "http_method": "GET",
    "paths": [{"action": "add", "code": None, "route_path": "~/api/v1/orders$"}],
    "plugins_before": ["JWT"], "plugins_after": ["JWT"],
    "regex_priority_before": 0, "regex_priority_after": 0,
    "desired_paths": [], "plugins": ["JWT"], "regex_priority": 0, "updated_at": None,
}]


# ── builders ─────────────────────────────────────────────────────────────────

def test_new_secured_group_gets_service_name_tag_and_jwt():
    save, summary = sp.build_gateway_group_save(GROUP, STATE_EMPTY)
    assert save["code"] is None and save["updated_at"] is None
    assert save["route_group_key"] == "ad-test-9999"          # web: plain name for the first group
    assert save["http_method"] == "GET"
    assert save["plugins"] == ["JWT"]                          # secured → JWT injected
    assert save["regex_priority"] == 0
    assert [p["route_path"] for p in save["paths"]] == ["~/api/v1/users$", "~/api/v1/orders$"]
    assert save["delta"]["paths"] == [
        {"action": "add", "code": None, "route_path": "~/api/v1/users$"},
        {"action": "add", "code": None, "route_path": "~/api/v1/orders$"},
    ]
    assert save["delta"]["plugins_before"] == [] and save["delta"]["plugins_after"] == ["JWT"]
    assert summary["new_group"] is True and summary["added_paths"] == GROUP["paths"]


def test_public_group_after_plain_name_taken_gets_open_suffix():
    group = {**GROUP, "secured": False, "http_method": "POST", "paths": ["~/health$"]}
    save, summary = sp.build_gateway_group_save(group, STATE_WITH_GET)
    assert save["route_group_key"] == "ad-test-9999-open"
    assert save["plugins"] == []
    assert summary["secured"] is False


def test_existing_group_keeps_live_paths_and_skips_duplicates():
    save, summary = sp.build_gateway_group_save(GROUP, STATE_WITH_GET)
    assert save["code"] == "KRG_1" and save["updated_at"] == "2026-09-10T10:00:00Z"
    assert [p["route_path"] for p in save["paths"]] == ["~/api/v1/users$", "~/api/v1/orders$"]
    assert save["paths"][0]["code"] == "KRC_1"
    assert save["delta"]["paths"] == [{"action": "add", "code": None, "route_path": "~/api/v1/orders$"}]
    assert save["delta"]["plugins_before"] == ["JWT"] and save["delta"]["plugins_after"] == ["JWT"]
    assert summary["added_paths"] == ["~/api/v1/orders$"]
    assert summary["already_present"] == ["~/api/v1/users$"]


def test_nothing_new_returns_none():
    group = {**GROUP, "paths": ["~/api/v1/users$"]}
    save, summary = sp.build_gateway_group_save(group, STATE_WITH_GET)
    assert save is None
    assert summary["already_present"] == ["~/api/v1/users$"]


def test_extra_plugins_and_priority_are_moves_on_the_group():
    group = {**GROUP, "paths": ["~/api/v1/users$"], "plugins": ["User ID Injection"], "regex_priority": 10}
    save, _ = sp.build_gateway_group_save(group, STATE_WITH_GET)
    assert save is not None
    assert save["plugins"] == ["JWT", "User ID Injection"]
    assert save["regex_priority"] == 10
    assert save["delta"]["paths"] == []
    assert save["delta"]["regex_priority_before"] == 0 and save["delta"]["regex_priority_after"] == 10


def test_auth_mismatch_on_existing_tag_is_a_conflict():
    group = {**GROUP, "secured": False, "route_group_key": "ad-test-9999"}
    with pytest.raises(sp.GatewayConflict):
        sp.build_gateway_group_save(group, STATE_WITH_GET)


def test_gateway_prefill_uses_option_labels():
    prefill = sp.build_gateway_prefill(service_name="ad-test-9999", product_name="Core", environment="stage", geo_name="Mumbai")
    assert prefill == {"product": "Core", "environment": "Stage", "geo_location": "Mumbai", "service_name": "ad-test-9999 - Core"}


def test_summarize_and_count_stored_groups():
    items = sp.summarize_gateway_groups(STORED_GROUPS)
    assert items == [{
        "route_group_key": "ad-test-9999", "http_method": "GET", "secured": True,
        "added": ["~/api/v1/orders$"], "removed": [], "changed": [],
    }]
    assert sp.count_gateway_changes(STORED_GROUPS) == 1
    moved = [{**STORED_GROUPS[0], "plugins_after": ["JWT", "User ID Injection"], "regex_priority_after": 5}]
    assert sp.count_gateway_changes(moved) == 3


def test_summarize_state_as_cards():
    cards = sp.summarize_gateway_state(STATE_WITH_GET)
    assert cards == [{
        "http_method": "GET", "secured": True, "route_group_key": "ad-test-9999",
        "plugins": [], "regex_priority": 0,
        "paths": [{"route_path": "~/api/v1/users$", "deployed": True}],
        "pending": [],
    }]


def _state_with_paths(count: int) -> dict:
    """One GET group holding `count` live paths."""
    return {
        **STATE_EMPTY,
        "groups": [{
            "code": "KRG_1", "route_group_key": "ad-test-9999", "http_method": "GET",
            "plugins": ["JWT"], "regex_priority": 0, "is_service_owner": True,
            "updated_at": "2026-09-10T10:00:00Z",
            "paths": [
                {"code": f"KRC_{i}", "route_path": f"~/api/v1/r{i}$", "creation_status": "ACTIVE"}
                for i in range(count)
            ],
            "pending": [],
        }],
    }


def test_preview_caps_paths_per_group_and_counts_the_rest():
    [card] = sp.preview_gateway_cards(sp.summarize_gateway_state(_state_with_paths(24)))
    assert len(card["paths"]) == sp.GATEWAY_PATH_PREVIEW == 10
    assert card["paths_total"] == 24
    assert card["paths_more"] == 14
    assert card["paths"][0]["route_path"] == "~/api/v1/r0$"
    assert card["paths"][-1]["route_path"] == "~/api/v1/r9$"


def test_preview_leaves_a_short_group_whole():
    [card] = sp.preview_gateway_cards(sp.summarize_gateway_state(STATE_WITH_GET))
    assert card["paths_more"] == 0
    assert card["paths_total"] == len(card["paths"]) == 1


def test_summarize_state_keeps_every_path_so_clash_detection_sees_them():
    """The cap is display-only. `_priority_action` decides whether the incoming
    paths clash by walking `card['paths']`, so a card trimmed at the source
    would report "nothing clashes" against the eleventh path onwards."""
    cards = sp.summarize_gateway_state(_state_with_paths(24))
    assert len(cards[0]["paths"]) == 24

    # A path that only exists past the preview window still has to be found.
    group = {"http_method": "GET", "route_group_key": "ad-test-other", "paths": ["~/api/v1/r20$"]}
    action = chat_tool._priority_action({"type": "ask_user", "field_id": "regex_priority"}, group, cards)
    assert "THESE PATHS CLASH" in action["instruction"]
    assert "ad-test-9999" in action["instruction"]


def test_priority_clash_is_missed_when_the_trimmed_cards_are_used():
    """Guard the split itself: feeding the preview into the clash check is
    exactly the bug this arrangement avoids."""
    trimmed = sp.preview_gateway_cards(sp.summarize_gateway_state(_state_with_paths(24)))
    group = {"http_method": "GET", "route_group_key": "ad-test-other", "paths": ["~/api/v1/r20$"]}
    action = chat_tool._priority_action({"type": "ask_user", "field_id": "regex_priority"}, group, trimmed)
    assert "THESE PATHS CLASH" not in action["instruction"]


# ── chat: a gateway result goes to create_service_and_save_draft ─────────────

GATEWAY_RESULT = {
    "status": "deployed", "message": "Here's your Kong Gateway Routes — please review",
    "isReady": True, "form_id": "kong_route_form", "gateway_group": GROUP,
    "collected_data": {"method": "GET"}, "missing_fields": {"required": [], "optional": []},
    "suggestions": [], "invalid_fields": [],
}


def test_gateway_result_next_action_is_the_draft_tool_not_deploy():
    action = chat_tool._build_next_action(dict(GATEWAY_RESULT))
    assert action["type"] == "create_service_and_save_draft"
    assert "gateway" in action["instruction"].lower()


@pytest.mark.asyncio
async def test_chat_caches_gateway_result_with_kind_marker():
    cache = AsyncMock(return_value=True)
    with patch.object(chat_tool, "get_auth_context", AsyncMock(return_value=AUTH)), \
         patch.object(chat_tool, "mint_internal_jwt", return_value="jwt"), \
         patch.object(chat_tool, "post_chat", AsyncMock(return_value=dict(GATEWAY_RESULT))), \
         patch.object(chat_tool, "cache_chatbot_result", cache):
        result = await chat_tool.chat_impl(message="done", ticket_code=GW_TICKET)
    payload = cache.await_args.args[2]
    assert payload["kind"] == "gateway"
    assert payload["gateway_group"]["paths"] == GROUP["paths"]
    assert "gateway_group" not in result
    assert result["next_action"]["type"] == "create_service_and_save_draft"


# ── create_service_and_save_draft on a gateway ticket ────────────────────────

class DraftHarness:
    def __init__(self, *, state=None, config_row=SimpleNamespace(code=SC), redis_entries=None, approval="default"):
        self.state = STATE_EMPTY if state is None else state
        self.config_row = config_row
        self.redis_entries = redis_entries if redis_entries is not None else []
        self.get_gateway_state = AsyncMock(return_value=self.state)
        self.save_gateway_draft = AsyncMock(return_value={
            "ok": True,
            "approval": (
                {"code": "queue-gw-1", "status": "draft", "case_ref_code": "add_route", "changes": {"groups": STORED_GROUPS}}
                if approval == "default" else approval
            ),
            "detail": None,
        })
        self.add_draft = AsyncMock(return_value=True)
        self.update_draft = AsyncMock(return_value=True)

    def patches(self):
        h = self

        @asynccontextmanager
        async def session():
            yield MagicMock(name="db")

        config_repo = MagicMock()
        config_repo.get_by_tenant_service_env_geo_loc = AsyncMock(return_value=h.config_row)
        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.get_gateway_state = self.get_gateway_state
        client.save_gateway_draft = self.save_gateway_draft
        cached = {"kind": "gateway", "form_id": "kong_route_form", "gateway_group": GROUP, "collected_data": {}}
        return [
            patch.object(dispatcher, "AsyncSessionLocal", session),
            patch.object(dispatcher, "get_auth_context", AsyncMock(return_value=AUTH)),
            patch("app.repository.service_config_repository.ServiceConfigRepository", return_value=config_repo),
            patch("app.mcp_servers.devlift_mcp.chatbot_client.get_cached_chatbot_result", AsyncMock(return_value=cached)),
            patch("app.mcp_servers.devlift_mcp._internal_jwt.mint_internal_jwt", return_value="jwt"),
            patch("app.mcp_servers.devlift_mcp.obs_tool_client", client),
            patch.object(dispatcher, "add_draft", self.add_draft),
            patch.object(dispatcher, "update_draft_by_id", self.update_draft),
            patch.object(dispatcher, "get_all_drafts", AsyncMock(return_value=self.redis_entries)),
        ]

    async def run(self):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await dispatcher.create_service_and_save_draft_handler(ticket_code=GW_TICKET, project_id="proj-1")


@pytest.mark.asyncio
async def test_gateway_ticket_saves_gateway_half_and_asks_what_next():
    h = DraftHarness()
    result = await h.run()

    assert result["status"] == "success"
    assert result["action"] == "gateway_draft_saved"
    assert result["service_config_code"] == SC
    assert result["queue_code"] == "queue-gw-1" and result["queue_status"] == "draft"
    assert result["group"]["added_paths"] == GROUP["paths"]
    assert result["gateway_changes"][0]["added"] == ["~/api/v1/orders$"]
    assert result["change_count"] == 1
    assert "2 paths added" in result["message"]
    assert result["next_action"]["type"] == "present_gateway_draft"
    instr = result["next_action"]["instruction"]
    assert "Submit for review" in instr and "section='gateway'" in instr and "keep editing" in instr
    assert "edit_service_configuration(service_name='ad-test-9999')" in instr  # no config ticket known

    h.get_gateway_state.assert_awaited_once_with(jwt_token="jwt", service_config_code=SC)
    kwargs = h.save_gateway_draft.await_args.kwargs
    assert kwargs["service_config_code"] == SC
    assert kwargs["gateway_groups"][0]["route_group_key"] == "ad-test-9999"
    assert kwargs["gateway_groups"][0]["plugins"] == ["JWT"]
    h.add_draft.assert_awaited_once()
    entry = h.add_draft.await_args.args[2]
    assert entry["transaction_code"] == SC and entry["gateway_queue_code"] == "queue-gw-1"
    assert entry["gateway_ticket_code"] == GW_TICKET and entry["ticket_code"] == GW_TICKET


@pytest.mark.asyncio
async def test_gateway_save_joins_existing_configuration_entry():
    existing = {"draft_id": "d1", "ticket_code": "mcp-u1-cfg", "queue_code": "queue-cfg", "queue_status": "draft",
                "transaction_code": SC, "identifier": "ad-test-9999", "status": "pending"}
    h = DraftHarness(redis_entries=[existing])
    result = await h.run()
    assert result["status"] == "success"
    assert result["configuration_ticket_code"] == "mcp-u1-cfg"
    assert "chat(ticket_code='mcp-u1-cfg')" in result["next_action"]["instruction"]
    h.add_draft.assert_not_awaited()
    updated = h.update_draft.await_args.args[3]
    assert updated["queue_code"] == "queue-cfg"                 # the settings row stays the handle
    assert updated["gateway_queue_code"] == "queue-gw-1"
    assert updated["gateway_ticket_code"] == GW_TICKET


@pytest.mark.asyncio
async def test_gateway_paths_already_live_is_no_changes():
    h = DraftHarness(state={**STATE_WITH_GET, "groups": [{**STATE_WITH_GET["groups"][0], "paths": [
        {"code": "KRC_1", "route_path": "~/api/v1/users$"}, {"code": "KRC_2", "route_path": "~/api/v1/orders$"}]}]})
    result = await h.run()
    assert result["status"] == "no_changes"
    assert "already on **ad-test-9999**" in result["message"]
    h.save_gateway_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_without_configuration_in_that_scope_is_not_found():
    h = DraftHarness(config_row=None)
    result = await h.run()
    assert result["status"] == "error" and result["reason"] == "not_found"
    h.get_gateway_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_permission_refusal_is_final():
    h = DraftHarness()
    h.save_gateway_draft.side_effect = ObsToolAPIError(403, "You may not edit the gateway of this service", "/transaction/kong-gateway/x")
    result = await h.run()
    assert result["status"] == "error" and result["reason"] == "permission_denied"
    h.add_draft.assert_not_awaited()


# ── edit_service_configuration(section='gateway') ────────────────────────────

class GatewayEditHarness(EditHarness):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.get_gateway_state = AsyncMock(return_value=STATE_WITH_GET)

    def patches(self):
        # The edit harness's client has no gateway read; a second patch of the
        # same module, entered last, wins.
        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.get_service_config = self.get_service_config
        client.list_approvals = self.list_approvals
        client.get_gateway_state = self.get_gateway_state
        return super().patches() + [patch("app.mcp_servers.devlift_mcp.obs_tool_client", client)]

    async def run(self, **kwargs):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await dispatcher.start_service_edit_handler(**kwargs)


@pytest.mark.asyncio
async def test_edit_gateway_opens_kong_form_prefilled_with_placement_and_service():
    h = GatewayEditHarness()
    result = await h.run(service_name="ad-test-9999", section="gateway")

    assert result["status"] == "success"
    assert result["section"] == "gateway"
    assert result["service_config_code"] == SC
    assert result["existing_routes"][0]["route_group_key"] == "ad-test-9999"
    assert "1 route group(s), 1 path(s). Pick the card" in result["message"]
    assert result["next_action"]["type"] == "continue_chat"   # no section from the chatbot in this harness
    assert result["ticket_code"] in result["next_action"]["instruction"]

    kwargs = h.post_select_form.await_args.kwargs
    assert kwargs["form_id"] == "kong_route_form"
    assert kwargs["prefill"] == {"product": "core", "environment": "Stage", "geo_location": "Mumbai", "service_name": "ad-test-9999 - core"}


@pytest.mark.asyncio
async def test_edit_default_section_is_still_the_eks_form():
    h = GatewayEditHarness()
    result = await h.run(service_name="ad-test-9999")
    assert result["status"] == "success" and "section" not in result
    assert h.post_select_form.await_args.kwargs["form_id"] == "eks_service_form"


@pytest.mark.asyncio
async def test_edit_unknown_section_is_refused():
    h = GatewayEditHarness()
    result = await h.run(service_name="ad-test-9999", section="variables")
    assert result["status"] == "error" and result["reason"] == "invalid_section"
    h.post_select_form.assert_not_awaited()


# ── read tools: the gateway row next to the settings row ─────────────────────

def _gateway_req(code, status, by="Yahiya"):
    item = _req(code, status, changes={"groups": STORED_GROUPS}, by=by)
    item["case_ref_code"] = "add_route"
    item["display_name"] = "Gateway routes - sample-mcp-service"
    return item


@pytest.mark.asyncio
async def test_preview_shows_configuration_and_gateway_halves():
    settings = _req("queue-111", "draft", {"port": {"from": None, "to": "8080"}})
    settings["case_ref_code"] = "update_service"
    h = DetailsHarness(approvals=[settings, _gateway_req("queue-gw", "draft")])
    result = await h.run(ticket_code=TICKET)
    assert result["pending_request"]["queue_code"] == "queue-111"
    assert result["pending_request"]["changes"] == {"port": {"from": None, "to": "8080"}}
    assert result["gateway_request"]["queue_code"] == "queue-gw"
    assert result["gateway_request"]["change_count"] == 1
    assert result["gateway_request"]["changes"][0]["added"] == ["~/api/v1/orders$"]
    assert "1 configuration change(s) and 1 gateway route change(s) pending" in result["message"]
    assert "Gateway routes" in result["next_action"]["instruction"]


@pytest.mark.asyncio
async def test_preview_never_reads_a_gateway_row_as_a_field_diff():
    h = DetailsHarness(approvals=[_gateway_req("queue-gw", "draft")])
    result = await h.run(ticket_code=TICKET)
    assert result["pending_request"] is None
    assert result["gateway_request"]["change_count"] == 1
    assert "1 gateway route change(s) pending" in result["message"]


@pytest.mark.asyncio
async def test_inbox_folds_the_two_rows_of_one_change_set():
    settings = _approval_item("queue-a", "demo", "submit")
    settings["case_ref_code"] = "update_service"
    gateway = _approval_item("queue-a-gw", "demo", "submit", changes={"groups": STORED_GROUPS})
    gateway["case_ref_code"] = "add_route"
    gateway["display_name"] = "Gateway routes - demo"
    h = ApprovalHarness(submitted=[settings, gateway, _approval_item("queue-b", "other", "submit")])
    result = await h.run(dispatcher.list_pending_approvals_handler)
    rows = result["waiting_for_review"]
    assert [r["queue_code"] for r in rows] == ["queue-a", "queue-b"]
    assert rows[0]["change_count"] == 2 and rows[0]["includes"] == ["configuration", "gateway"]
    assert rows[1]["includes"] == ["configuration"]
    assert result["message"].startswith("2 requests waiting")


@pytest.mark.asyncio
async def test_review_attaches_the_gateway_sibling():
    settings = _approval_item("queue-a", "demo", "submit")
    settings["case_ref_code"] = "update_service"
    gateway = _approval_item("queue-a-gw", "demo", "submit", changes={"groups": STORED_GROUPS})
    gateway["case_ref_code"] = "add_route"
    h = ApprovalHarness(submitted=[settings, gateway], single=settings)
    result = await h.run(dispatcher.review_service_request_handler, queue_code="queue-a")
    assert result["status"] == "success"
    assert result["queue_code"] == "queue-a"
    assert result["changes"] == {"port": {"from": None, "to": "8080"}}
    assert result["gateway_changes"][0]["added"] == ["~/api/v1/orders$"]
    assert result["change_count"] == 2 and result["includes"] == ["configuration", "gateway"]
    assert "(configuration and gateway routes)" in result["message"]


@pytest.mark.asyncio
async def test_edit_gateway_turns_the_forms_first_section_into_one_dialog():
    h = GatewayEditHarness()
    h.post_select_form.return_value = {
        **h.post_select_form.return_value,
        "section": {"title": "Route", "fields": [
            {"field_id": "method", "label": "HTTP Method", "type": "dropdown", "required": True, "multi": False,
             "options": [{"text": m, "value": m} for m in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")], "hint": None, "default": None},
            {"field_id": "secured", "label": "Authentication", "type": "dropdown", "required": True, "multi": False,
             "options": [{"text": "Auth - JWT required", "value": "Auth - JWT required"}, {"text": "No Auth - public", "value": "No Auth - public"}], "hint": None, "default": None},
        ]},
    }
    result = await h.run(service_name="ad-test-9999", section="gateway")
    action = result["next_action"]
    assert action["type"] == "ask_section"
    assert [f["field_id"] for f in action["fields"]] == ["method", "secured"]
    assert action["ticket_code"] == result["ticket_code"]
    assert "existing_routes" in action["instruction"]          # cards first
    # The path is asked for, and sent, in Kong regex form — the same input the
    # Gateway tab takes and what gets stored verbatim. The model must not
    # convert a plain path itself; the form rejects one and the rejection
    # carries devlift's own suggestion for the user to confirm.
    assert "~/api/v1/users$" in action["instruction"]
    assert "Never convert a plain path yourself" in action["instruction"]
    assert "Pick the card" in result["message"]


@pytest.mark.asyncio
async def test_submit_response_carries_both_halves_like_the_web_changes_view():
    from tests.unit.mcp_servers.test_service_request import Harness as RequestHarness, _item as _req_item

    settings = _req_item("queue-111", "sample-mcp-service", "submit", changes={"cpu_requested": {"from": "1m", "to": "0.4m"}})
    settings["case_ref_code"] = "update_service"
    gateway = _req_item("queue-111-gw", "sample-mcp-service", "submit", changes={"groups": STORED_GROUPS})
    gateway["case_ref_code"] = "add_route"
    h = RequestHarness(listing=[settings, gateway], action_result={"ok": True, "approval": settings, "detail": "submitted"})
    result = await h.run(dispatcher.submit_service_request_handler, queue_code="queue-111")

    assert result["status"] == "success"
    assert result["changes"] == {"cpu_requested": {"from": "1m", "to": "0.4m"}}
    assert result["gateway_changes"][0]["added"] == ["~/api/v1/orders$"]
    assert result["gateway_queue_code"] == "queue-111-gw"
    assert result["change_count"] == 2
    assert "Gateway routes" in result["next_action"]["instruction"]


# ── a second card must not drop the first (obs_tool replaces the row's groups) ──

PENDING_POST = {
    "group_code": None, "route_group_key": "ad-test-9999", "http_method": "POST",
    "paths": [{"action": "add", "code": None, "route_path": "~/api/v1/orders$"}],
    "plugins_before": [], "plugins_after": ["JWT"], "regex_priority_before": 0, "regex_priority_after": 0,
    "desired_paths": [{"code": None, "route_path": "~/api/v1/orders$"}], "plugins": ["JWT"], "regex_priority": 0, "updated_at": None,
}


def test_pending_entry_round_trips_to_a_group_save():
    save = sp.pending_entry_to_save(PENDING_POST)
    assert save["code"] is None and save["http_method"] == "POST" and save["plugins"] == ["JWT"]
    assert save["paths"] == [{"code": None, "route_path": "~/api/v1/orders$"}]
    assert save["delta"]["paths"] == PENDING_POST["paths"]
    assert save["delta"]["plugins_after"] == ["JWT"]


def test_new_card_is_batched_with_the_pending_ones():
    new_save, _ = sp.build_gateway_group_save({**GROUP, "http_method": "GET", "secured": False, "paths": ["~/api/v1/orders/status$"]}, STATE_EMPTY)
    batch = sp.merge_gateway_batch(new_save, [PENDING_POST])
    assert [(g["route_group_key"], g["http_method"]) for g in batch] == [("ad-test-9999", "POST"), ("ad-test-9999", "GET")]
    assert batch[0]["delta"]["paths"][0]["route_path"] == "~/api/v1/orders$"
    assert batch[1]["delta"]["paths"][0]["route_path"] == "~/api/v1/orders/status$"


def test_same_card_saved_twice_merges_its_moves():
    again, _ = sp.build_gateway_group_save({**GROUP, "http_method": "POST", "paths": ["~/api/v1/orders/(?<id>[^/]+)$"]}, STATE_EMPTY)
    batch = sp.merge_gateway_batch(again, [PENDING_POST])
    assert len(batch) == 1
    only = batch[0]
    assert [a["route_path"] for a in only["delta"]["paths"]] == ["~/api/v1/orders$", "~/api/v1/orders/(?<id>[^/]+)$"]
    assert [p["route_path"] for p in only["paths"]] == ["~/api/v1/orders$", "~/api/v1/orders/(?<id>[^/]+)$"]
    assert only["delta"]["plugins_before"] == []


@pytest.mark.asyncio
async def test_gateway_save_carries_the_earlier_pending_card():
    h = DraftHarness()
    pending_row = {
        "code": "queue-gw-1", "status": "draft", "case_ref_code": "add_route", "resource_code": SC,
        "you": {"mine": True}, "config_snapshot": {"groups": [PENDING_POST]},
    }
    h.list_approvals = AsyncMock(return_value={"approvals": [pending_row], "total": 1})
    original = h.patches

    def patches():
        ps = original()
        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.get_gateway_state = h.get_gateway_state
        client.save_gateway_draft = h.save_gateway_draft
        client.list_approvals = h.list_approvals
        return ps + [patch("app.mcp_servers.devlift_mcp.obs_tool_client", client)]

    h.patches = patches
    result = await h.run()
    assert result["status"] == "success"
    sent = h.save_gateway_draft.await_args.kwargs["gateway_groups"]
    assert [(g["route_group_key"], g["http_method"]) for g in sent] == [("ad-test-9999", "POST"), ("ad-test-9999", "GET")]
