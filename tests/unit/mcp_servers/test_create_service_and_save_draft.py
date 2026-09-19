"""create_service_and_save_draft_handler — the three obs_tool calls, the
lookups that make it idempotent, and the error contract."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.enum import EnvironmentEnum
from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp.auth import AuthContext
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError
from tests.unit.mcp_servers.test_service_payloads import CACHED, LOCATOR_CAMEL

AUTH = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x.io", clerk_user_id="")
TICKET = "mcp-u1-deadbeef"
PROJECT = "proj-123456abcdef"
CLUSTER = ("infra-eks-1", [{"code": "infra-eks-1", "name": "EKS Stage", "locator": LOCATOR_CAMEL}])


class Harness:
    """Patches everything around the handler and records the HTTP calls."""

    def __init__(self, *, service_row=None, cluster=CLUSTER, existing_config=None, cached=None):
        self.service_row = service_row
        self.cluster = cluster
        self.existing_config = existing_config
        self.cached = CACHED if cached is None else cached
        self.create_service = AsyncMock(return_value={"service_code": "svc-new"})
        self.create_service_config = AsyncMock(
            return_value={"code": "sc-new-stage-abc", "config": {"ingress_group_order": 65}}
        )
        self.save_settings_draft = AsyncMock(
            return_value={
                "ok": True,
                "approval": {"code": "queue-1", "status": "draft", "changes": {"port": {"from": None, "to": "8080"}}},
                "detail": None,
            }
        )
        self.add_draft = AsyncMock(return_value=True)
        self.update_draft = AsyncMock(return_value=True)
        self.existing_redis_entries: list = []
        # Canvas placement context (what the web reads); empty by default so
        # the row locator is used, as before.
        self.canvas_eks_clusters: list = []

    def _session_factory(self):
        harness = self

        @asynccontextmanager
        async def session():
            yield MagicMock(name="db")

        return session

    def patches(self):
        services_repo = MagicMock()
        services_repo.find_by_name_for_mcp = AsyncMock(return_value=self.service_row)
        infra_repo = MagicMock()
        infra_repo.find_eks_cluster_for_mcp = AsyncMock(return_value=self.cluster)
        config_repo = MagicMock()
        # `config_repo_result` overrides the plain return value: an exception to
        # raise, or a list to walk call by call.
        override = getattr(self, "config_repo_result", None)
        if isinstance(override, list):
            self.config_repo_call = AsyncMock(side_effect=override)
        elif override is not None:
            self.config_repo_call = AsyncMock(side_effect=override)
        else:
            self.config_repo_call = AsyncMock(return_value=self.existing_config)
        config_repo.get_by_tenant_service_env_geo_loc = self.config_repo_call

        discovery = MagicMock()
        discovery.get_canvas_data = AsyncMock(
            return_value=SimpleNamespace(eksClusters=self.canvas_eks_clusters)
        )
        discovery.get_placement_context = AsyncMock(
            return_value=SimpleNamespace(geoLocations=[], accounts=[], cloudRegions=[])
        )

        client = MagicMock()
        client.ObsToolAPIError = ObsToolAPIError
        client.create_service = self.create_service
        client.create_service_config = self.create_service_config
        client.save_settings_draft = self.save_settings_draft

        return [
            patch.object(dispatcher, "AsyncSessionLocal", self._session_factory()),
            patch.object(dispatcher, "get_auth_context", AsyncMock(return_value=AUTH)),
            patch.object(dispatcher, "ServicesMstRepository", return_value=services_repo),
            patch.object(dispatcher, "InfrastructureMstRepository", return_value=infra_repo),
            patch.object(dispatcher, "VpcAndResourceDiscoveryService", return_value=discovery),
            patch("app.repository.service_config_repository.ServiceConfigRepository", return_value=config_repo),
            patch("app.mcp_servers.devlift_mcp.chatbot_client.get_cached_chatbot_result", AsyncMock(return_value=self.cached)),
            # None = a fresh create. Set `edit_origin` on the harness to run
            # the same handler as an edit session opened on that placement.
            patch("app.mcp_servers.devlift_mcp.chatbot_client.get_edit_origin",
                  AsyncMock(return_value=getattr(self, "edit_origin", None))),
            patch("app.mcp_servers.devlift_mcp._internal_jwt.mint_internal_jwt", return_value="jwt-token"),
            patch("app.mcp_servers.devlift_mcp.obs_tool_client", client),
            patch.object(dispatcher, "add_draft", self.add_draft),
            patch.object(dispatcher, "update_draft_by_id", self.update_draft),
            patch.object(dispatcher, "get_all_drafts", AsyncMock(return_value=self.existing_redis_entries)),
        ]

    async def run(self, ticket_code=TICKET, project_id=PROJECT, cluster_code=None):
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await dispatcher.create_service_and_save_draft_handler(
                ticket_code=ticket_code, project_id=project_id, cluster_code=cluster_code
            )


@pytest.mark.asyncio
async def test_happy_path_creates_service_config_and_draft():
    h = Harness()
    result = await h.run()

    assert result["status"] == "success"
    assert result["action"] == "created"
    assert result["service_code"] == "svc-new"
    assert result["service_config_code"] == "sc-new-stage-abc"
    assert result["queue_code"] == "queue-1"
    assert result["queue_status"] == "draft"
    assert result["cluster_name"] == "eks-stage"
    assert result["changes"] == {"port": {"from": None, "to": "8080"}}
    assert result["change_count"] == 1
    assert result["next_action"]["type"] == "present_draft"
    assert "Deployed → Requested" in result["next_action"]["instruction"]
    assert "trigger_resource_deployment" in result["next_action"]["instruction"]

    h.create_service.assert_awaited_once()
    assert h.create_service.await_args.kwargs["jwt_token"] == "jwt-token"
    assert h.create_service.await_args.kwargs["payload"]["service_name"] == "mcp-dryrun-api"

    h.create_service_config.assert_awaited_once()
    baseline = h.create_service_config.await_args.kwargs["payload"]
    assert baseline["services_mst_code"] == "svc-new"
    assert baseline["infrastructure_mst_code"] == "infra-eks-1"
    assert baseline["config"]["cluster_arn"] == LOCATOR_CAMEL["cluster_arn"]
    assert "repository" not in baseline["config"]

    h.save_settings_draft.assert_awaited_once()
    draft_kwargs = h.save_settings_draft.await_args.kwargs
    assert draft_kwargs["service_config_code"] == "sc-new-stage-abc"
    assert draft_kwargs["case_ref_code"] == "update_service"
    snapshot = draft_kwargs["config_snapshot"]
    assert snapshot["config"]["repository"] == "Regobs/chat-bot-POC"
    assert snapshot["ingress_group_order"] == 65
    assert snapshot["services_mst_code"] == "svc-new"

    h.add_draft.assert_awaited_once()
    entry = h.add_draft.await_args.args[2]
    assert entry["transaction_code"] == "sc-new-stage-abc"
    assert entry["queue_code"] == "queue-1"
    assert entry["services_mst_code"] == "svc-new"


@pytest.mark.asyncio
async def test_canvas_cluster_placement_wins_over_row_locator():
    """The web copies subnets / vpc / region from the canvas EKS node, so the
    baseline must use the same source when the canvas knows the cluster."""
    h = Harness()
    h.canvas_eks_clusters.append(SimpleNamespace(
        infrastructureMstCode="infra-eks-1",
        clusterArn=LOCATOR_CAMEL["cluster_arn"],
        clusterName="eks-stage",
        vpcId="vpc-canvas",
        cloudRegionId="cr-canvas",
        cloudRegion="ap-south-1",
        subnetIds=["subnet-canvas-a", "subnet-canvas-b", "subnet-canvas-c"],
    ))
    result = await h.run()
    assert result["status"] == "success"
    baseline = h.create_service_config.await_args.kwargs["payload"]["config"]
    assert baseline["subnet_ids"] == ["subnet-canvas-a", "subnet-canvas-b", "subnet-canvas-c"]
    assert "vpc_id" not in baseline  # web never sets it at creation
    assert baseline["cloud_region_id"] == "cr-canvas"
    assert baseline["cluster_arn"] == LOCATOR_CAMEL["cluster_arn"]


@pytest.mark.asyncio
async def test_canvas_miss_falls_back_to_row_locator():
    h = Harness()  # no canvas clusters
    result = await h.run()
    assert result["status"] == "success"
    baseline = h.create_service_config.await_args.kwargs["payload"]["config"]
    assert baseline["subnet_ids"] == LOCATOR_CAMEL["subnetIds"]
    assert "vpc_id" not in baseline


@pytest.mark.asyncio
async def test_rerun_updates_existing_redis_entry_instead_of_appending():
    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    existing = SimpleNamespace(code="sc-old-stage-xyz", config={"ingress_group_order": 75})
    h = Harness(service_row=service_row, existing_config=existing)
    h.existing_redis_entries.append({
        "draft_id": "keepme01", "transaction_code": "sc-old-stage-xyz", "queue_code": "queue-1",
        "ticket_code": "mcp-u1-older", "identifier": "mcp-dryrun-api", "status": "pending",
    })
    result = await h.run()
    assert result["status"] == "success"
    h.add_draft.assert_not_awaited()
    h.update_draft.assert_awaited_once()
    updated = h.update_draft.await_args.args[3]
    assert h.update_draft.await_args.args[2] == "keepme01"
    assert updated["draft_id"] == "keepme01"
    assert updated["ticket_code"] == TICKET  # latest conversation wins
    assert updated["queue_code"] == "queue-1"


@pytest.mark.asyncio
async def test_existing_service_and_config_only_saves_draft():
    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    existing = SimpleNamespace(code="sc-old-stage-xyz", config={"ingress_group_order": 75})
    h = Harness(service_row=service_row, existing_config=existing)
    result = await h.run()

    assert result["status"] == "success"
    assert result["action"] == "draft_saved"
    assert result["service_code"] == "svc-old"
    assert result["service_config_code"] == "sc-old-stage-xyz"
    h.create_service.assert_not_awaited()
    h.create_service_config.assert_not_awaited()
    assert h.save_settings_draft.await_args.kwargs["config_snapshot"]["ingress_group_order"] == 75


@pytest.mark.asyncio
async def test_existing_service_without_config_creates_config_only():
    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    h = Harness(service_row=service_row, existing_config=None)
    result = await h.run()

    assert result["action"] == "created"
    h.create_service.assert_not_awaited()
    h.create_service_config.assert_awaited_once()
    assert h.create_service_config.await_args.kwargs["payload"]["services_mst_code"] == "svc-old"


@pytest.mark.asyncio
async def test_missing_cache_asks_to_continue_chat():
    h = Harness(cached={})
    result = await h.run()
    assert result["status"] == "error"
    assert result["reason"] == "fields_incomplete"
    assert result["next_action"]["type"] == "continue_chat"
    h.create_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_resource_cache_is_refused():
    h = Harness(cached={"kind": "resource", "attribute_parameters": {"identifier": "b"}})
    result = await h.run()
    assert result["status"] == "error"
    assert result["reason"] == "wrong_tool"
    assert "trigger_resource_deployment" in result["next_action"]["instruction"]


@pytest.mark.asyncio
async def test_missing_project_id_returns_init_contract():
    h = Harness()
    result = await h.run(project_id=None)
    assert result["status"] == "project_init_required"
    assert result["project_id"].startswith("proj-")
    h.create_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_cluster_is_a_clear_error():
    h = Harness(cluster=(None, []))
    result = await h.run()
    assert result["status"] == "error"
    assert result["reason"] == "no_cluster"
    h.create_service.assert_not_awaited()


# Production registers several EKS clusters per environment and region; the
# form asks for none, so the user picks. Local has one each, which is why this
# path never showed up there.
THREE_CLUSTERS = (None, [
    {"code": "infra-a", "name": "EKS Stage Infrastructure Mumbai", "locator": {"cluster_name": "eks-a"}},
    {"code": "infra-b", "name": "EKS Staging Application Infrastructure Mumbai", "locator": {"cluster_name": "eks-b"}},
    {"code": "infra-c", "name": "EKS Staging Infrastructure Mumbai", "locator": {"cluster_name": "eks-c"}},
])


@pytest.mark.asyncio
async def test_several_clusters_asks_the_user_instead_of_failing():
    h = Harness(cluster=THREE_CLUSTERS)
    result = await h.run()

    assert result["status"] == "needs_cluster"          # a question, not an error
    assert result["reason"] == "ambiguous_cluster"
    assert [c["name"] for c in result["clusters"]] == [
        "EKS Stage Infrastructure Mumbai",
        "EKS Staging Application Infrastructure Mumbai",
        "EKS Staging Infrastructure Mumbai",
    ]
    assert result["next_action"]["type"] == "choose"
    assert [o["value"] for o in result["next_action"]["options"]] == ["infra-a", "infra-b", "infra-c"]
    assert "Do not pick for the user" in result["next_action"]["instruction"]
    assert "DevOps" not in result["message"]           # the user decides, not a ticket to someone else
    h.create_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_chosen_cluster_is_used():
    h = Harness(cluster=THREE_CLUSTERS)
    result = await h.run(cluster_code="infra-b")

    assert result["status"] == "success"
    assert result["cluster_name"] == "eks-b"
    baseline = h.create_service_config.await_args.kwargs["payload"]
    assert baseline["infrastructure_mst_code"] == "infra-b"


@pytest.mark.asyncio
async def test_a_service_that_already_runs_is_never_asked_again():
    """An edit / re-save: the configuration already records its cluster, so
    three candidates are not a question. Asking would invite the user to move
    a live service by accident."""
    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    existing = SimpleNamespace(
        code="sc-old-stage-xyz", config={}, infrastructure_mst_code="infra-b",
    )
    h = Harness(service_row=service_row, existing_config=existing, cluster=THREE_CLUSTERS)
    result = await h.run()

    assert result["status"] == "success"          # not needs_cluster
    assert result["cluster_name"] == "eks-b"      # the one it already runs on
    assert result["service_config_code"] == "sc-old-stage-xyz"
    h.create_service_config.assert_not_awaited()  # nothing re-created
    # The cluster must not be part of the key used to find it, or the lookup
    # could never learn which cluster it is.
    assert len(h.config_repo_call.await_args.args) == 7


@pytest.mark.asyncio
async def test_a_live_cluster_that_left_the_placement_is_still_honoured():
    """The cluster was deactivated or renamed out of the candidate list. The
    service still runs on it, so it is kept rather than re-asked."""
    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    existing = SimpleNamespace(
        code="sc-old-stage-xyz", config={}, infrastructure_mst_code="infra-retired",
    )
    h = Harness(service_row=service_row, existing_config=existing, cluster=THREE_CLUSTERS)
    result = await h.run()
    assert result["status"] == "success"
    h.create_service_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_configs_on_different_clusters_asks_instead_of_raising():
    """The row's unique key ends in the cluster, so a service can hold a
    configuration on two clusters of the same placement. The cluster-less
    lookup then raises rather than pick; that must become the question, not a
    500."""
    from sqlalchemy.exc import MultipleResultsFound

    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    h = Harness(service_row=service_row, cluster=THREE_CLUSTERS)
    h.config_repo_result = MultipleResultsFound()
    result = await h.run()

    assert result["status"] == "needs_cluster"
    assert [o["value"] for o in result["next_action"]["options"]] == ["infra-a", "infra-b", "infra-c"]
    h.create_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_configs_and_a_named_cluster_uses_the_full_key():
    """With the cluster named, the unique key is complete again, so the exact
    row is found and used."""
    from sqlalchemy.exc import MultipleResultsFound

    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    exact = SimpleNamespace(code="sc-on-b", config={}, infrastructure_mst_code="infra-b")
    h = Harness(service_row=service_row, cluster=THREE_CLUSTERS)
    h.config_repo_result = [MultipleResultsFound(), exact]   # bare call raises, keyed call succeeds
    result = await h.run(cluster_code="infra-b")

    assert result["status"] == "success"
    assert result["service_config_code"] == "sc-on-b"
    assert len(h.config_repo_call.await_args.args) == 8       # retried WITH the cluster


@pytest.mark.asyncio
async def test_moving_a_running_service_is_refused_out_loud():
    """Protecting a live service is right; doing it silently is not. Asking to
    put it on another cluster must not read as success."""
    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    existing = SimpleNamespace(code="sc-old", config={}, infrastructure_mst_code="infra-b")
    h = Harness(service_row=service_row, existing_config=existing, cluster=THREE_CLUSTERS)
    result = await h.run(cluster_code="infra-c")

    assert result["status"] == "error"
    assert result["reason"] == "cluster_change_not_supported"
    assert "EKS Staging Application Infrastructure Mumbai" in result["message"]   # where it runs
    assert "EKS Staging Infrastructure Mumbai" in result["message"]               # what was asked for
    h.save_settings_draft.assert_not_awaited()          # nothing was written


@pytest.mark.asyncio
async def test_an_unknown_cluster_is_still_caught_on_the_edit_path():
    """Validation must not be skipped just because the service already exists."""
    service_row = SimpleNamespace(code="svc-old", name="mcp-dryrun-api")
    existing = SimpleNamespace(code="sc-old", config={}, infrastructure_mst_code="infra-b")
    h = Harness(service_row=service_row, existing_config=existing, cluster=THREE_CLUSTERS)
    result = await h.run(cluster_code="infra-invented")
    assert result["reason"] == "unknown_cluster"


@pytest.mark.asyncio
async def test_a_cluster_outside_the_placement_is_refused():
    h = Harness(cluster=THREE_CLUSTERS)
    result = await h.run(cluster_code="infra-somewhere-else")
    assert result["status"] == "error"
    assert result["reason"] == "unknown_cluster"
    h.create_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_cluster_still_needs_no_question():
    h = Harness()
    result = await h.run()
    assert result["status"] == "success"
    assert result["cluster_name"] == "eks-stage"


@pytest.mark.asyncio
async def test_config_409_reuses_existing_code():
    h = Harness()
    h.create_service_config.side_effect = ObsToolAPIError(
        409, {"message": "exists", "config_code": "sc-dup-stage-1"}, "/service-configs"
    )
    result = await h.run()
    assert result["status"] == "success"
    assert result["service_config_code"] == "sc-dup-stage-1"
    assert h.save_settings_draft.await_args.kwargs["service_config_code"] == "sc-dup-stage-1"


@pytest.mark.asyncio
async def test_config_409_text_detail_is_parsed():
    h = Harness()
    h.create_service_config.side_effect = ObsToolAPIError(
        409, "Service configuration already exists (code sc-dup-stage-2) — use the update endpoint", "/service-configs"
    )
    result = await h.run()
    assert result["service_config_code"] == "sc-dup-stage-2"


@pytest.mark.asyncio
async def test_draft_403_is_final_permission_error():
    h = Harness()
    h.save_settings_draft.side_effect = ObsToolAPIError(403, "can_write_settings denied on demo", "/transaction/service-settings/x")
    result = await h.run()
    assert result["status"] == "error"
    assert result["reason"] == "permission_denied"
    assert "permission" in result["message"].lower()


@pytest.mark.asyncio
async def test_draft_409_lane_busy_surfaces_detail():
    h = Harness()
    h.save_settings_draft.side_effect = ObsToolAPIError(409, "sc-x already has a change submit (queue-9)", "/transaction/service-settings/x")
    result = await h.run()
    assert result["reason"] == "conflict"
    assert "queue-9" in result["message"]


@pytest.mark.asyncio
async def test_draft_noop_reports_no_changes():
    h = Harness()
    h.save_settings_draft.return_value = {"ok": True, "approval": None, "detail": "nothing changed — your values match"}
    result = await h.run()
    assert result["status"] == "no_changes"
    assert "nothing changed" in result["message"]


@pytest.mark.asyncio
async def test_create_service_duplicate_400_falls_back_to_lookup():
    h = Harness()
    h.create_service.side_effect = ObsToolAPIError(400, "service_name already exists in this application", "/services/create-service")
    # The re-lookup after the 400 finds the row created by the timed-out first call.
    found = SimpleNamespace(code="svc-found", name="mcp-dryrun-api")
    services_repo = MagicMock()
    services_repo.find_by_name_for_mcp = AsyncMock(side_effect=[None, found])
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in h.patches():
            stack.enter_context(p)
        stack.enter_context(patch.object(dispatcher, "ServicesMstRepository", return_value=services_repo))
        result = await dispatcher.create_service_and_save_draft_handler(ticket_code=TICKET, project_id=PROJECT)

    assert result["status"] == "success"
    assert result["service_code"] == "svc-found"
    assert h.create_service_config.await_args.kwargs["payload"]["services_mst_code"] == "svc-found"


@pytest.mark.asyncio
async def test_missing_required_chatbot_key_is_fields_incomplete():
    cached = copy.deepcopy(CACHED)
    cached["service_config"].pop("geo_loc_mst_code")
    h = Harness(cached=cached)
    result = await h.run()
    assert result["status"] == "error"
    assert result["reason"] == "fields_incomplete"
    assert "geo_loc_mst_code" in result["message"]


def test_environment_enum_accepts_chatbot_value():
    assert EnvironmentEnum("stage").value == "stage"


# ── the "what next" question adapts to what the user may do ──────────────────

def _draft_with(you: dict) -> dict:
    return {
        "ok": True,
        "detail": None,
        "approval": {
            "code": "queue-1", "status": "draft",
            "changes": {"port": {"from": None, "to": "8080"}},
            "you": you,
        },
    }


@pytest.mark.asyncio
async def test_author_without_rights_gets_the_plain_four_choices():
    h = Harness()
    h.save_settings_draft.return_value = _draft_with(
        {"mine": True, "can_approve": False, "can_deploy": False, "can_write_settings": True}
    )
    result = await h.run()
    instruction = result["next_action"]["instruction"]

    assert "Submit for review" in instruction
    assert "Submit, approve and deploy" not in instruction    # no rights, no shortcut
    assert "ONE AskUserQuestion" in instruction               # 4 options still fit
    assert result["you"]["can_approve"] is False


@pytest.mark.asyncio
async def test_approver_who_can_also_deploy_gets_the_full_path():
    h = Harness()
    h.save_settings_draft.return_value = _draft_with(
        {"mine": True, "can_approve": True, "can_deploy": True, "can_write_settings": True}
    )
    result = await h.run()
    instruction = result["next_action"]["instruction"]

    assert "Submit for review" in instruction                 # kept, not replaced
    assert "Submit, approve and deploy" in instruction
    # the three calls, in order
    assert instruction.index("submit_service_request") < instruction.index("approve_service_request")
    assert instruction.index("approve_service_request") < instruction.index("deploy_service_request")
    assert "confirmed=true" in instruction
    assert "PRODUCTION the deploy still asks" in instruction  # prod keeps its typed-name gate
    # Still a dialog with buttons: the 4 that fit, with "keep editing" moved
    # into the line above it rather than dropped.
    assert "ONE AskUserQuestion" in instruction
    assert "never a written list" in instruction
    assert "'Nothing, keep editing'" not in instruction
    assert "they can also just keep editing" in instruction
    assert result["you"]["can_deploy"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("you", [
    {"mine": True, "can_approve": True, "can_deploy": False, "can_write_settings": True},
    {"mine": True, "can_approve": False, "can_deploy": True, "can_write_settings": True},
])
async def test_one_right_alone_is_not_enough(you):
    """Approving without deploying, or deploying without approving, cannot take
    a change all the way — so the shortcut is not offered."""
    h = Harness()
    h.save_settings_draft.return_value = _draft_with(you)
    result = await h.run()
    assert "Submit, approve and deploy" not in result["next_action"]["instruction"]


# ── a language that never resolved ───────────────────────────────────────────
# The chatbot builds language_ref_code from the chosen version option's
# `value`. When that option list came back empty the lookup falls through to
# the raw LABEL — "1.23" where "GO_1_23" was meant. Nothing downstream would
# catch it: the draft saves, submit and approve pass, and it finally breaks the
# service_configs foreign key inside a Temporal activity, after the PR is
# raised. A real code always carries its language; the fallthrough never does.

def _cached_with_language(code):
    from copy import deepcopy
    cached = deepcopy(CACHED)
    cached["service_config"]["language_ref_code"] = code
    return cached


@pytest.mark.asyncio
async def test_a_bare_version_as_language_is_refused_before_anything_is_created():
    h = Harness(cached=_cached_with_language("1.23"))
    result = await h.run()

    assert result["status"] == "error"
    assert result["reason"] == "unknown_language"
    assert "1.23" in result["message"]
    # nothing was written — the point of refusing here rather than at deploy
    h.create_service.assert_not_awaited()
    h.create_service_config.assert_not_awaited()
    h.save_settings_draft.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["GO_1_24", "PYTHON_3_10", "JAVA-MAVEN_17", "NODEJS_20"])
async def test_real_language_codes_pass(code):
    h = Harness(cached=_cached_with_language(code))
    result = await h.run()
    assert result["status"] != "error", result.get("message")
    h.save_settings_draft.assert_awaited()


# ── Placement is identity, not a setting ────────────────────────────────────
# An edit session opens on one configuration row, but this handler resolves
# its target from the ANSWERS. Answer "prod" and it resolves elsewhere: it
# creates a configuration in the target placement, or writes the draft onto a
# DIFFERENT existing one — and both come back looking like a successful edit.
# ya-test-005 hit exactly this: the chatbot reported a promotion while its own
# collected values still read Stage / Mumbai.

ORIGIN = {
    "service_config_code": "sc-live-stage",
    "service_name": "mcp-dryrun-api",
    "environment": "stage",
    "geo_loc_mst_code": "region-aspora-mumbai",
    "infrastructure_mst_code": "infra-eks-1",
    "product_name": "core",
    "geo_name": "Mumbai",
    "resource_group_name": "Default service group",
    "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
    "resource_group_code": "154a9547-93aa-4589-baa7-402bc6d04288",
    "service_type": "API",
}


def _edit_harness(**cached_overrides):
    """An edit session on ORIGIN's placement, with the answers altered."""
    cached = copy.deepcopy(CACHED)
    cached["service_config"].update(cached_overrides.pop("service_config", {}))
    cached["create_service"].update(cached_overrides.pop("create_service", {}))
    h = Harness(cached=cached, service_row=SimpleNamespace(code="svc-1", name="mcp-dryrun-api"))
    h.edit_origin = ORIGIN
    return h


@pytest.mark.asyncio
async def test_editing_cannot_move_a_service_to_another_environment():
    h = _edit_harness(service_config={"environment": "prod"})
    result = await h.run()

    assert result["status"] == "error"
    assert result["reason"] == "placement_change_not_supported"
    assert any("environment" in c for c in result["changed"])
    # Nothing may be written — not the service, not the config, not the draft.
    h.create_service.assert_not_called()
    h.create_service_config.assert_not_called()
    h.save_settings_draft.assert_not_called()


@pytest.mark.asyncio
async def test_editing_cannot_move_a_service_to_another_region():
    h = _edit_harness(service_config={"geo_loc_mst_code": "region-aspora-london"})
    result = await h.run()

    assert result["reason"] == "placement_change_not_supported"
    assert any("region" in c for c in result["changed"])
    h.save_settings_draft.assert_not_called()


@pytest.mark.asyncio
async def test_editing_cannot_move_a_service_to_another_product():
    h = _edit_harness(create_service={"application_code": "some-other-product"})
    result = await h.run()

    assert result["reason"] == "placement_change_not_supported"
    assert any("product" in c for c in result["changed"])
    h.save_settings_draft.assert_not_called()


@pytest.mark.asyncio
async def test_the_refusal_says_what_an_edit_CAN_change():
    """A bare 'not supported' leaves the user stuck. The message has to hand
    back the thing they can actually do."""
    h = _edit_harness(service_config={"environment": "prod"})
    msg = (await h.run())["message"]

    assert "Nothing was changed." in msg
    for editable in ("repository", "port", "CPU and memory", "autoscaling"):
        assert editable in msg
    assert "second configuration" in msg
    # and the model must not be left free to retry or offer a move
    instruction = (await h.run())["next_action"]["instruction"]
    assert "Do NOT retry the save" in instruction


@pytest.mark.asyncio
async def test_an_edit_that_keeps_its_placement_saves_normally():
    """The guard must only catch moves — an ordinary edit still goes through."""
    h = _edit_harness()
    result = await h.run()

    assert result["status"] == "success"
    h.save_settings_draft.assert_awaited()


@pytest.mark.asyncio
async def test_a_fresh_create_has_no_origin_and_is_unaffected():
    """No edit origin means this ticket never came from edit_service_configuration,
    so any placement is legitimate — including a second config for prod."""
    h = Harness()
    h.edit_origin = None
    result = await h.run()

    assert result["status"] == "success"
    h.create_service_config.assert_awaited()


@pytest.mark.asyncio
async def test_editing_cannot_rename_the_service():
    """The service is looked up BY NAME, so a rename misses it and the handler
    builds an entirely new service — worse than a stray second config. The
    name is also the namespace and the ECR repository."""
    h = _edit_harness(create_service={"service_name": "mcp-dryrun-api-v2"})
    result = await h.run()

    assert result["reason"] == "placement_change_not_supported"
    assert any("name" in c for c in result["changed"])
    h.create_service.assert_not_called()
    h.create_service_config.assert_not_called()
    h.save_settings_draft.assert_not_called()


@pytest.mark.asyncio
async def test_editing_cannot_switch_api_to_worker():
    """service_type sets alb_selection, which is part of the row's lookup key —
    so flipping it resolves to a different configuration."""
    h = _edit_harness(create_service={"service_type": "Worker"})
    result = await h.run()

    assert result["reason"] == "placement_change_not_supported"
    assert any("service type" in c for c in result["changed"])
    h.save_settings_draft.assert_not_called()


@pytest.mark.asyncio
async def test_cloning_renames_on_purpose_and_is_not_blocked():
    """start_service_clone_handler mints its own ticket and writes no origin,
    so a clone's deliberate rename must still go through."""
    cached = copy.deepcopy(CACHED)
    cached["create_service"]["service_name"] = "mcp-dryrun-api-copy"
    h = Harness(cached=cached)
    h.edit_origin = None          # what a clone session looks like
    result = await h.run()

    assert result["status"] == "success"
    h.create_service.assert_awaited()
