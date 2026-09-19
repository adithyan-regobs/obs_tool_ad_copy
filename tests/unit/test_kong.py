"""Every Kong test, in one file.

Deliberately not split across tests/unit/domain, tests/unit/plugins and
tests/unit/infra_chat_agent: Kong spans a policy, a validator, a chat
translator and two generators, and a change to the route model usually
touches several of them at once. Keeping them together means one file to
open and one run to trust.

Sections, in the order a route travels:
  1. route group naming policy   — what a group is called
  2. path overlap validator      — whether a path may be claimed
  3. chatbot gateway translator  — chat card -> v2 delta group
  4. script gen component v1     — delta -> terragrunt (single route)
  5. script gen component v2     — delta -> terragrunt (scope-keyed)
  6. chat/MCP staging adapter    — delta groups -> GatewaySaveRequest

The real-S3 upload test stays in tests/integration/plugins: it reaches AWS,
so it must not run in a unit sweep.
"""


# ============================================================================
# ROUTE GROUP NAMING POLICY
# (was tests/unit/domain/test_kong_route_group_naming.py)
# Unit tests for Kong route-group naming and the chat reply that announces it.
# ============================================================================
import inspect
import pytest

from app.domain.policies.kong_route_group_naming import (
    default_group_key,
    describe_group_choice,
    preload_tag,
    suggest_tags,
)


@pytest.mark.unit
def test_default_group_key_matches_what_the_generator_writes():
    # generate() appends `-service` before using the name as a group key, so the
    # suggestion has to carry the same suffix or it names a group nobody writes.
    assert default_group_key("goms") == "goms-service"
    assert default_group_key("goms-service") == "goms-service"
    assert default_group_key("  goms  ") == "goms-service"
    assert default_group_key("") == ""


@pytest.mark.unit
def test_suggestions_start_at_tag1():
    assert suggest_tags("goms")[0] == "goms-service-tag1"


@pytest.mark.unit
def test_suggestions_skip_taken_names_case_insensitively():
    taken = ["goms-service-tag1", "GOMS-SERVICE-TAG2"]
    assert suggest_tags("goms", taken)[0] == "goms-service-tag3"


@pytest.mark.unit
def test_preload_prefers_the_plain_service_name():
    # That group carries the terragrunt `service { }` block; leaving it unclaimed
    # makes the owner fall back to whichever tag sorts first.
    assert preload_tag("goms") == "goms-service"
    assert preload_tag("goms", ["goms-service"]) == "goms-service-tag1"


@pytest.mark.unit
def test_no_tag_reply_names_the_default_and_offers_one():
    msg = describe_group_choice("goms")
    assert "'goms-service'" in msg
    assert "service default" in msg
    assert "goms-service-tag1" in msg
    # The whole point of mentioning priority here: it is unreachable without a
    # group, and it is what resolves two groups claiming one path.
    assert "regex_priority" in msg


@pytest.mark.unit
def test_explicit_tag_reply_is_just_the_tag():
    # Nothing was chosen for the user, so there is nothing to warn them about.
    msg = describe_group_choice("goms", "payments-open")
    assert msg == "Route group: 'payments-open'."


@pytest.mark.unit
def test_suggestion_avoids_a_name_already_in_use():
    msg = describe_group_choice("goms", "", taken=["goms-service-tag1"])
    assert "goms-service-tag2" in msg
    assert "goms-service-tag1'" not in msg


# ============================================================================
# PATH OVERLAP VALIDATOR
# (was tests/unit/domain/test_kong_path_overlap_validator.py)
# Unit tests for the Kong cross-group path overlap rule.
# Renamed on merge to avoid a clash with another section: _Group -> _OverlapGroup, _Row -> _OverlapRow
# ============================================================================
import pytest

from app.domain.validators.kong_path_overlap_validator import (
    ExistingRoute,
    KongPathOverlapError,
    KongPathOverlapValidator,
)


def _live():
    """A gateway holding one path in two groups, plus an unanchored legacy route."""
    return [
        ExistingRoute("~/api/v1/users$", "GET", "payments", 0, "KRC_1"),
        ExistingRoute("~/api/v1/users$", "GET", "legacy", 500, "KRC_3"),
        # Terragrunt really does hold routes written without the trailing `$`.
        ExistingRoute("~/goblin-service/actuator/health", "GET", "health-open", 200, "KRC_2"),
        ExistingRoute("~/api/v1/orders$", "POST", "orders", 0, "KRC_4"),
    ]


def _validate(paths, method="GET", tag="new", priority=0, ignore_codes=()):
    return KongPathOverlapValidator.validate(
        paths=paths,
        http_method=method,
        route_group_key=tag,
        regex_priority=priority,
        existing=_live(),
        ignore_codes=ignore_codes,
    )


@pytest.mark.unit
def test_free_path_is_accepted():
    assert _validate(["~/api/v1/ping$"]) == []


@pytest.mark.unit
def test_equal_priority_is_refused():
    # No tie-break exists at equal priority, so Kong's choice is arbitrary.
    with pytest.raises(KongPathOverlapError) as exc:
        _validate(["~/goblin-service/actuator/health$"], priority=200)
    assert "health-open" in exc.value.errors[0]


@pytest.mark.unit
def test_higher_priority_is_an_override_not_a_conflict():
    overrides = _validate(["~/goblin-service/actuator/health$"], priority=201)
    assert [(o.their_tag, o.their_priority) for o in overrides] == [("health-open", 200)]


@pytest.mark.unit
def test_blocker_is_the_highest_owner_not_the_first():
    # 100 beats 'payments' at 0 but is shadowed by 'legacy' at 500, so it is
    # still refused — and the message must name the 500 it has to beat.
    with pytest.raises(KongPathOverlapError) as exc:
        _validate(["~/api/v1/users$"], priority=100)
    assert "'legacy' at priority 500" in exc.value.errors[0]
    assert "above 500" in exc.value.errors[0]

    assert _validate(["~/api/v1/users$"], priority=501)[0].their_tag == "legacy"


@pytest.mark.unit
def test_other_method_never_collides():
    # (tag, method) is the route group's identity; another method is a different
    # Kong route entirely.
    assert _validate(["~/api/v1/users$"], method="DELETE") == []
    assert _validate(["~/api/v1/orders$"], method="GET") == []


@pytest.mark.unit
def test_path_already_owned_by_the_target_group_is_a_no_op():
    # 'payments' already holds this path while 'legacy' shadows it at 500. That
    # override is pre-existing and deployed; re-saving must not be refused for it,
    # or an untouched path becomes un-saveable from either side.
    assert _validate(["~/api/v1/users$"], tag="payments") == []


@pytest.mark.unit
def test_unanchored_stored_path_is_still_found():
    # Stored without `$`, sent with one. Comparing a single spelling would report
    # the path as free while Kong already matches it.
    with pytest.raises(KongPathOverlapError):
        _validate(["~/goblin-service/actuator/health$"])


@pytest.mark.unit
def test_decompiled_incoming_path_is_compiled_before_comparing():
    # The Gateway tab sends human paths, chat/MCP sends Kong regexes.
    with pytest.raises(KongPathOverlapError):
        _validate(["/api/v1/users"])


@pytest.mark.unit
def test_ignore_codes_lets_a_route_move_between_groups():
    # A move keeps the row's code, so without this it would conflict with the
    # group it is leaving.
    assert _validate(["~/api/v1/users$"], ignore_codes=["KRC_1", "KRC_3"]) == []


@pytest.mark.unit
def test_all_or_nothing_on_a_mixed_batch():
    # One bad path refuses the whole batch — the caller must never be left with
    # half a group applied.
    with pytest.raises(KongPathOverlapError) as exc:
        _validate(["~/api/v1/ping$", "~/api/v1/users$"])
    assert len(exc.value.conflicts) == 1
    assert exc.value.conflicts[0].path == "~/api/v1/users$"


class _OverlapGroup:
    def __init__(self, key, priority):
        self.route_group_key, self.regex_priority = key, priority


class _OverlapRow:
    def __init__(self, path, method, group, code, api_name=""):
        self.route_path, self.http_method, self.route_group, self.code = path, method, group, code
        self.api_name = api_name


@pytest.mark.unit
def test_from_models_flattens_and_normalises():
    rows = [_OverlapRow("~/x$", "get", _OverlapGroup("tagA", 5), "c1")]
    flat = KongPathOverlapValidator.from_models(rows)
    assert (flat[0].http_method, flat[0].route_group_key, flat[0].regex_priority) == ("GET", "tagA", 5)


@pytest.mark.unit
def test_group_less_row_falls_back_to_its_own_services_default_group():
    # Legacy chat/MCP rows have no group but DO claim their path, under their own
    # service's default group — the same place the v2 generator puts them.
    rows = [_OverlapRow("~/y$", "GET", None, "c2", api_name="goms")]
    assert KongPathOverlapValidator.from_models(rows)[0].route_group_key == "goms-service"


@pytest.mark.unit
def test_group_less_rows_of_different_services_do_not_merge():
    # These rows are gateway-wide. Labelling every group-less row with one
    # caller-supplied tag would invent collisions between unrelated services.
    rows = [
        _OverlapRow("~/y$", "GET", None, "c1", api_name="goms"),
        _OverlapRow("~/y$", "GET", None, "c2", api_name="payments"),
    ]
    tags = {r.route_group_key for r in KongPathOverlapValidator.from_models(rows)}
    assert tags == {"goms-service", "payments-service"}


@pytest.mark.unit
def test_unnameable_row_is_dropped():
    # No group and no api_name: a conflict against a group we cannot name tells
    # the user nothing, so the row is dropped rather than guessed at.
    rows = [_OverlapRow("~/y$", "GET", None, "c2")]
    assert KongPathOverlapValidator.from_models(rows) == []
    assert KongPathOverlapValidator.from_models(rows, default_tag="fallback")[0].route_group_key == "fallback"


# ============================================================================
# CHATBOT GATEWAY TRANSLATOR (chat/MCP delta groups)
# (was tests/unit/infra_chat_agent/test_chatbot_gateway_translator.py)
# Unit tests for the chatbot gateway card → v2 delta entry.
# ============================================================================
import pytest

from app.mcp_servers.devlift_mcp.chatbot_gateway_translator import (
    AUTH_PLUGIN,
    build_gateway_group_delta,
)


def _card(**overrides):
    card = {
        "http_method": "GET",
        "secured": False,
        "route_group_key": "",
        "regex_priority": 0,
        "plugins": [],
        "paths": ["~/api/v1/users$"],
    }
    card.update(overrides)
    return card


def _build(api_name="goms", **overrides):
    return build_gateway_group_delta(_card(**overrides), api_name)


@pytest.mark.unit
def test_no_card_is_left_alone():
    # A non-kong form must pass through untouched.
    assert build_gateway_group_delta({}, "goms") is None
    assert build_gateway_group_delta(None, "goms") is None


@pytest.mark.unit
def test_compiles_each_path():
    g = _build(paths=["/api/v1/users", "~/logout$"])
    assert [p["route_path"] for p in g["paths"]] == ["~/api/v1/users$", "~/logout$"]


@pytest.mark.unit
def test_wraps_each_path_as_an_add():
    g = _build(paths=["~/a$", "~/b$"])
    assert g["paths"] == [
        {"action": "add", "route_path": "~/a$"},
        {"action": "add", "route_path": "~/b$"},
    ]
    assert g["desired_paths"] == [{"route_path": "~/a$"}, {"route_path": "~/b$"}]


@pytest.mark.unit
def test_secured_becomes_the_jwt_plugin():
    g = _build(secured=True, plugins=["User ID Injection"])
    assert g["plugins_after"] == [AUTH_PLUGIN, "User ID Injection"]
    assert AUTH_PLUGIN not in _build(secured=False)["plugins_after"]


@pytest.mark.unit
@pytest.mark.parametrize("value", [True, "true", "yes", "auth", "JWT"])
def test_secured_accepts_the_strings_a_dropdown_sends(value):
    assert AUTH_PLUGIN in _build(secured=value)["plugins_after"]


@pytest.mark.unit
def test_unrecognised_secured_value_defaults_to_public():
    # Defaulting to secured would put a JWT on routes meant to be open and read
    # as the gateway rejecting valid traffic.
    assert AUTH_PLUGIN not in _build(secured="maybe")["plugins_after"]


@pytest.mark.unit
def test_duplicate_paths_collapse():
    # The same path twice is one route; the second would be a duplicate HCL line.
    assert len(_build(paths=["/api/v1/users", "~/api/v1/users$"])["paths"]) == 1


@pytest.mark.unit
def test_blank_tag_falls_back_to_the_service_default_group():
    # Must match what the generator derives, or the routes deploy into a group
    # nobody writes.
    assert _build(route_group_key="")["route_group_key"] == "goms-service"


@pytest.mark.unit
def test_tag_and_priority_pass_through_on_both_keys():
    g = _build(route_group_key="goms-service-tag1", regex_priority=500)
    assert g["route_group_key"] == "goms-service-tag1"
    # The generator reads *_after; the DB writer falls back to the bare key.
    assert g["regex_priority"] == g["regex_priority_after"] == 500


@pytest.mark.unit
@pytest.mark.parametrize(
    "api_name,overrides,missing",
    [
        ("goms", {"http_method": ""}, "http_method"),
        ("goms", {"paths": []}, "paths"),
        ("", {}, "service_name"),
    ],
)
def test_incomplete_card_is_rejected_with_the_field_named(api_name, overrides, missing):
    # Raised here rather than deep inside the generator, where the message no
    # longer points at the chat turn that caused it.
    with pytest.raises(ValueError) as exc:
        build_gateway_group_delta(_card(**overrides), api_name)
    assert missing in str(exc.value)


# ============================================================================
# SCRIPT GEN COMPONENT v1
# (was tests/unit/plugins/test_aspora_kong_route_script_gen_component.py)
# Unit tests for AsporaKongRouteScriptGenComponent (the single-route `add_route` flow).
# ============================================================================
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_kong_route_script_gen_component import (
    AsporaKongRouteScriptGenComponent,
)

GET_CONTENT_TARGET = (
    "app.plugin.aspora.script_gen_components."
    "aspora_kong_route_script_gen_component.GitOpsHandler.get_content"
)

# Three services covering the array shapes the writer has to handle: populated,
# single-entry, and empty (no trailing comma to trip over).
SAMPLE_HCL_CONTENT = '''
kong_configs = {
  "user-api-service" = {
    routes = {
      "GET" = ["~/api/v1/users$", "~/api/v1/users/[0-9]+$"]
      "POST" = ["~/api/v1/users$"]
      "DELETE" = []
    }
  }
  "product-api-service" = {
    routes = {
      "GET" = ["~/api/v1/products$"]
      "POST" = []
    }
  }
  "empty-routes-api-service" = {
    routes = {
      "GET" = []
    }
  }
}
'''


class TestAsporaKongRouteScriptGenComponent:
    """Test cases for AsporaKongRouteScriptGenComponent"""

    @pytest.fixture
    def script_generator(self):
        return AsporaKongRouteScriptGenComponent()

    @pytest.fixture
    def file_location(self):
        return SimpleNamespace(
            repo="owner/repo",
            file_path="environment/core-prod-01/ap-south-1/gateway/terragrunt.hcl",
            base_branch="main",
            feature_branch="feature/test",
            target_branch="main",
            script_gen_key="gateway",
            queue_code="queue-001",
        )

    @pytest.fixture
    def workflow_context(self):
        return SimpleNamespace(
            skip_commit=False,
            staged_files=[],
            commit_messages={},
            script_gen_responses={1: {}},
        )

    @staticmethod
    def _queue(api_name, method, route):
        return {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"api_name": api_name, "method": method, "route": route},
        }

    async def _generate(self, script_generator, file_location, workflow_context,
                        api_name, method, route, existing=None):
        existing_file = existing if existing is not None else {
            "exists": True, "content": SAMPLE_HCL_CONTENT,
        }
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            return await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=self._queue(api_name, method, route),
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

    async def test_generate_adds_route_to_empty_array(
        self, script_generator, file_location, workflow_context
    ):
        """First route into an empty method array — no stray comma."""
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "empty-routes-api-service", "GET", "~/api/v1/test$",
        )

        assert '"GET" = ["~/api/v1/test$"]' in result

    async def test_generate_adds_route_to_non_empty_array(
        self, script_generator, file_location, workflow_context
    ):
        """A populated array keeps its entries and appends the new one last."""
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "product-api-service", "GET", "~/api/v1/products/new$",
        )

        assert "~/api/v1/products$" in result
        assert "~/api/v1/products/new$" in result
        assert result.index("~/api/v1/products$") < result.index("~/api/v1/products/new$")

    async def test_generate_adds_service_suffix_if_missing(
        self, script_generator, file_location, workflow_context
    ):
        """`user-api` resolves to the existing `user-api-service` entry."""
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "user-api", "DELETE", "~/api/v1/users/[0-9]+$",
        )

        assert '"user-api-service"' in result
        assert '"user-apiservice"' not in result
        assert '"DELETE" = ["~/api/v1/users/[0-9]+$"]' in result

    async def test_generate_raises_error_if_file_not_exists(
        self, script_generator, file_location, workflow_context
    ):
        """The gateway stack must exist before routes can be spliced in."""
        with pytest.raises(ValueError, match="does not exist"):
            await self._generate(
                script_generator, file_location, workflow_context,
                "user-api-service", "GET", "~/api/v1/new$",
                existing={"exists": False},
            )

    async def test_generate_creates_a_block_for_an_unknown_api(
        self, script_generator, file_location, workflow_context
    ):
        """An API with no kong_configs entry is CREATED, not rejected — the
        component grew this when new services started onboarding through it."""
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "brand-new-api-service", "GET", "~/api/v1/brand-new$",
        )

        assert '"brand-new-api-service"' in result
        assert "~/api/v1/brand-new$" in result
        # The services already in the file are untouched.
        assert '"user-api-service"' in result

    async def test_generate_raises_error_if_route_already_exists(
        self, script_generator, file_location, workflow_context
    ):
        """Adding a path the method already carries is a duplicate."""
        with pytest.raises(ValueError, match="already exists"):
            await self._generate(
                script_generator, file_location, workflow_context,
                "user-api-service", "GET", "~/api/v1/users$",
            )

    async def test_generate_preserves_other_api_routes(
        self, script_generator, file_location, workflow_context
    ):
        """Writing to one service leaves every other service's paths alone."""
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "user-api-service", "POST", "~/api/v1/users/bulk$",
        )

        assert "~/api/v1/products$" in result
        assert '"empty-routes-api-service"' in result
        assert "~/api/v1/users/[0-9]+$" in result

    async def test_generate_preserves_hcl_structure(
        self, script_generator, file_location, workflow_context
    ):
        """Braces stay balanced and the top-level block survives."""
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "user-api-service", "POST", "~/api/v1/users/bulk$",
        )

        assert "kong_configs = {" in result
        assert result.count("{") == result.count("}")
        assert result.count("[") == result.count("]")

    async def test_generate_with_special_characters_in_route(
        self, script_generator, file_location, workflow_context
    ):
        """Regex paths are written verbatim — no escaping, no mangling."""
        route = "~/api/v1/users/(?<id>[^/]+)/orders$"
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "user-api-service", "GET", route,
        )

        assert route in result

    async def test_generate_returns_string(
        self, script_generator, file_location, workflow_context
    ):
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "user-api-service", "DELETE", "~/api/v1/users/purge$",
        )

        assert isinstance(result, str)
        assert result

    async def test_generate_route_ordering(
        self, script_generator, file_location, workflow_context
    ):
        """New paths append to the end, so deployed order is stable."""
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "user-api-service", "GET", "~/api/v1/users/last$",
        )

        first = result.index("~/api/v1/users$")
        middle = result.index("~/api/v1/users/[0-9]+$")
        last = result.index("~/api/v1/users/last$")
        assert first < middle < last

    async def test_generate_creates_commit_and_updates_workflow_context(
        self, script_generator, file_location, workflow_context
    ):
        """The component stages the file and records its own response; the
        commit layer later replaces whatever is staged."""
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "user-api-service", "POST", "~/api/v1/users/staged$",
        )

        assert len(workflow_context.staged_files) == 1
        assert workflow_context.staged_files[0]["content"] == result
        assert "queue-001" in workflow_context.commit_messages["owner/repo|||main"]
        assert workflow_context.script_gen_responses[1]["gateway"]["original_content"] == result

    async def test_skip_commit_stages_nothing(
        self, script_generator, file_location, workflow_context
    ):
        """Preview runs still generate content but must not stage a commit."""
        workflow_context.skip_commit = True
        result = await self._generate(
            script_generator, file_location, workflow_context,
            "user-api-service", "POST", "~/api/v1/users/preview$",
        )

        assert "~/api/v1/users/preview$" in result
        assert workflow_context.staged_files == []
        assert workflow_context.commit_messages == {}


# ============================================================================
# SCRIPT GEN COMPONENT v2
# (was tests/unit/plugins/test_aspora_kong_route_script_gen_component_v2.py)
# Unit tests for AsporaKongRouteScriptGenComponentV2._apply_from_delta.
# Renamed on merge to avoid a clash with another section: _Group -> _OwnerGroup, _Row -> _OwnerRow
# ============================================================================
import pytest

from app.plugin.aspora.script_gen_components.aspora_kong_route_script_gen_component_v2 import (
    AsporaKongRouteScriptGenComponentV2,
    _find_block,
    _kc_bounds,
    add_route,
    find_owner_in_content,
    group_owns_service,
    read_deployed_group,
    remove_route,
)


# A gateway file shaped like the real one: an owner group carrying the service{}
# block, a second group pointing at it with existing_service, and a kong_plugins
# entry whose target_keys already hold a route belonging to ANOTHER service.
SAMPLE_HCL = '''terragrunt = {}
inputs = {
  kong_configs = {
    "goms-service" = {
      service = {
        host     = "alb.internal"
        protocol = "http"
        port     = 80
        path     = "/"
      }
      routes = {
        "GET" = ["~/goms/v1/ping$"]
      }
    }
    "goms-admin" = {
      existing_service = "goms-service"
      routes = {
        "POST" = ["~/goms/admin/old$", "~/goms/admin/keep$"]
      }
    }
  }
  kong_plugins = {
    routes_jwt_auth = {
      name        = "jwt"
      target      = "route"
      target_keys = ["other-service-get", "goms-admin-post"]
      config_json = jsonencode({})
    }
  }
}
'''

OWNER = "goms-service"


def _snapshot(*groups):
    """A scope-keyed config_snapshot carrying the given group entries."""
    return {
        "service_mst_code": "SVC_GOMS",
        "api_name": "goms",
        "environment": "stage",
        "geo_loc_mst_code": "GEO_MUM",
        "groups": list(groups),
    }


def _entry(key, method, paths, plugins_after=None, priority_after=0, plugins_before=None):
    return {
        "group_code": f"KRG_{key}_{method}",
        "route_group_key": key,
        "http_method": method,
        "paths": paths,
        "plugins_before": plugins_before or [],
        "plugins_after": plugins_after or [],
        "regex_priority_before": 0,
        "regex_priority_after": priority_after,
    }


TWO_GROUP_SNAPSHOT = _snapshot(
    _entry("goms-service", "GET",
           [{"action": "add", "route_path": "~/goms/v1/orders$"}],
           plugins_after=["JWT"]),
    _entry("goms-admin", "POST",
           [{"action": "edit", "old_path": "~/goms/admin/old$",
             "route_path": "~/goms/admin/new$"}],
           plugins_before=["JWT"], priority_after=100),
)


@pytest.fixture
def component():
    return AsporaKongRouteScriptGenComponentV2()


# The upstream a service block is built from. generate() resolves this from the
# gateway file's path; the reconcile just receives it, so a literal keeps these
# tests independent of the path-derivation rules.
SERVICE_CONFIG = {"host": "alb.internal", "protocol": "http", "port": 80, "path": "/"}


async def _apply(component, snapshot, content=SAMPLE_HCL, service_config=SERVICE_CONFIG):
    return await component._apply_from_delta(content, snapshot, OWNER, OWNER, service_config)


class TestApplyFromDeltaMultiGroup:
    """One row, many groups — the behaviour the scope-keyed flow depends on."""

    async def test_applies_changes_across_every_group(self, component):
        out = await _apply(component, TWO_GROUP_SNAPSHOT)

        first = read_deployed_group(out, "goms-service", "GET")
        second = read_deployed_group(out, "goms-admin", "POST")

        assert "~/goms/v1/orders$" in first["paths"]
        assert "~/goms/admin/new$" in second["paths"]
        assert "~/goms/admin/old$" not in second["paths"]

    async def test_leaves_untouched_paths_in_the_same_group(self, component):
        """A group's other paths are not collateral of editing one of them."""
        out = await _apply(component, TWO_GROUP_SNAPSHOT)

        assert "~/goms/v1/ping$" in read_deployed_group(out, "goms-service", "GET")["paths"]
        assert "~/goms/admin/keep$" in read_deployed_group(out, "goms-admin", "POST")["paths"]

    async def test_leaves_other_services_plugin_targets_alone(self, component):
        """
        The regression that once stripped JWT from 29 stage routes.

        target_keys holds routes belonging to services this deploy knows nothing
        about. Reconciling the list wholesale drops them; only the keys of the
        groups in this change may be touched.
        """
        out = await _apply(component, TWO_GROUP_SNAPSHOT)

        assert '"other-service-get"' in out

    async def test_group_order_does_not_change_the_output(self, component):
        """Each group edits its own kong_configs entry, so order cannot matter."""
        reversed_snapshot = {
            **TWO_GROUP_SNAPSHOT,
            "groups": list(reversed(TWO_GROUP_SNAPSHOT["groups"])),
        }

        assert await _apply(component, reversed_snapshot) == await _apply(component, TWO_GROUP_SNAPSHOT)

    async def test_reapplying_is_a_no_op(self, component):
        """
        A Temporal retry re-runs EVERY group from the start, over a file the
        failed attempt may already have written. Every primitive no-ops when the
        file already says the right thing, so the second pass must change nothing.
        """
        once = await _apply(component, TWO_GROUP_SNAPSHOT)
        twice = await _apply(component, TWO_GROUP_SNAPSHOT, content=once)

        assert twice == once


class TestApplyFromDeltaGroupLevelFields:
    """regex_priority and plugins belong to the group, not to any one path."""

    async def test_writes_priority_from_the_snapshot(self, component):
        out = await _apply(component, TWO_GROUP_SNAPSHOT)

        assert read_deployed_group(out, "goms-admin", "POST")["regex_priority"] == 100

    async def test_priority_only_change_needs_no_paths(self, component):
        """A group whose only edit is its priority carries no path actions."""
        out = await _apply(component, _snapshot(
            _entry("goms-admin", "POST", [], plugins_after=["JWT"],
                   plugins_before=["JWT"], priority_after=500),
        ))

        assert read_deployed_group(out, "goms-admin", "POST")["regex_priority"] == 500

    async def test_adds_and_removes_plugin_targets_per_group(self, component):
        out = await _apply(component, TWO_GROUP_SNAPSHOT)

        assert "JWT" in read_deployed_group(out, "goms-service", "GET")["plugins"]
        assert "JWT" not in read_deployed_group(out, "goms-admin", "POST")["plugins"]

    async def test_emptying_a_group_drops_its_plugin_targets(self, component):
        """
        Deleting every path removes the group entry, so its target key no longer
        names a live route. Keeping the plugin target would point at nothing.
        """
        out = await _apply(component, _snapshot(
            _entry("goms-admin", "POST",
                   [{"action": "delete", "route_path": "~/goms/admin/old$"},
                    {"action": "delete", "route_path": "~/goms/admin/keep$"}],
                   plugins_after=["JWT"], plugins_before=["JWT"]),
        ))

        assert '"goms-admin-post"' not in out


class TestApplyFromDeltaFailureModes:
    """Bad change sets must fail loudly rather than write a plausible file."""

    async def test_unknown_action_raises(self, component):
        with pytest.raises(ValueError, match="Unknown gateway action"):
            await _apply(component, _snapshot(
                _entry("goms-service", "GET",
                       [{"action": "frobnicate", "route_path": "~/x$"}]),
            ))

    async def test_edit_without_old_path_raises(self, component):
        """
        Without the deployed path there is no line to swap, and appending the new
        one alone would leave the old route live beside it.
        """
        with pytest.raises(ValueError, match="old_path"):
            await _apply(component, _snapshot(
                _entry("goms-admin", "POST",
                       [{"action": "edit", "route_path": "~/goms/admin/new$"}]),
            ))

    async def test_entry_without_route_path_is_skipped(self, component):
        """Malformed entry, but the rest of the change set still ships."""
        out = await _apply(component, _snapshot(
            _entry("goms-service", "GET",
                   [{"action": "add", "route_path": None},
                    {"action": "add", "route_path": "~/goms/v1/orders$"}]),
        ))

        assert "~/goms/v1/orders$" in read_deployed_group(out, "goms-service", "GET")["paths"]

    async def test_edit_of_a_path_not_in_the_file_becomes_an_add(self, component):
        """
        An earlier deploy already replaced the old line, so there is nothing to
        swap — the new path is simply ensured present rather than lost.
        """
        out = await _apply(component, _snapshot(
            _entry("goms-admin", "POST",
                   [{"action": "edit", "old_path": "~/goms/admin/never-existed$",
                     "route_path": "~/goms/admin/new$"}]),
        ))

        assert "~/goms/admin/new$" in read_deployed_group(out, "goms-admin", "POST")["paths"]


class TestApplyFromDeltaNewGroup:
    """A group with no kong_configs entry yet has to be created."""

    async def test_creates_a_missing_group_pointing_at_the_owner(self, component):
        out = await _apply(component, _snapshot(
            _entry("goms-reports", "GET",
                   [{"action": "add", "route_path": "~/goms/reports$"}]),
        ))

        created = read_deployed_group(out, "goms-reports", "GET")
        assert created["exists"]
        assert "~/goms/reports$" in created["paths"]
        # Non-owner groups borrow the owner's upstream instead of emitting a
        # second service{} block for the same service.
        assert 'existing_service = "goms-service"' in out


# A service whose owner is a TAG group, not the base name — the shape produced
# when a service's first gateway change created only tag groups. The base-named
# group exists and points AT the tag, which is legal and live.
TAG_OWNED_HCL = '''inputs = {
  kong_configs = {
    "jk-test-service-tag1" = {
      service = {
        host     = "alb.internal"
        protocol = "http"
        port     = 80
        path     = "/"
      }
      routes = {
        "GET" = ["~/api/v1/sample0$"]
      }
    }
    "jk-test-service" = {
      existing_service = "jk-test-service-tag1"
      routes = {
        "GET" = ["~/api/v1/sample1$"]
      }
    }
  }
}
'''


class _OwnerGroup:
    def __init__(self, key, is_owner):
        self.route_group_key = key
        self.is_service_owner = is_owner


class _OwnerRow:
    def __init__(self, key, is_owner=False):
        self.route_group_key = key
        self.route_group = _OwnerGroup(key, is_owner)


class TestOwnerGroupResolution:
    """Ownership is whatever the FILE says, because terraform keys
    kong_service.services by the owning group and follows existing_service
    exactly one hop."""

    def test_finds_the_group_carrying_the_service_block(self):
        assert find_owner_in_content(TAG_OWNED_HCL, ["jk-test-service-tag1"]) == "jk-test-service-tag1"

    def test_follows_existing_service_to_the_real_owner(self):
        # Asked about a group that owns nothing, it lands on the owner anyway.
        assert find_owner_in_content(TAG_OWNED_HCL, ["jk-test-service"]) == "jk-test-service-tag1"

    def test_returns_none_when_no_candidate_is_in_the_file(self):
        assert find_owner_in_content(TAG_OWNED_HCL, ["other-service"]) is None

    def test_group_owns_service_distinguishes_owner_from_pointer(self):
        assert group_owns_service(TAG_OWNED_HCL, "jk-test-service-tag1") is True
        assert group_owns_service(TAG_OWNED_HCL, "jk-test-service") is False

    def test_deployed_owner_survives_the_base_group_gaining_rows(self):
        """The regression: once the base-named group had rows, the resolver
        returned it and every new group pointed at a group owning nothing."""
        owner = AsporaKongRouteScriptGenComponentV2._owner_group(
            [_OwnerRow("jk-test-service-tag1", is_owner=True), _OwnerRow("jk-test-service")],
            "jk-test-service",
            ["jk-test-service-tag1", "jk-test-service"],
            content=TAG_OWNED_HCL,
        )
        assert owner == "jk-test-service-tag1"

    def test_without_the_file_the_recorded_owner_wins_over_the_name(self):
        owner = AsporaKongRouteScriptGenComponentV2._owner_group(
            [_OwnerRow("jk-test-service-tag1", is_owner=True), _OwnerRow("jk-test-service")],
            "jk-test-service",
            ["jk-test-service-tag1", "jk-test-service"],
        )
        assert owner == "jk-test-service-tag1"

    def test_new_service_absent_from_the_file_still_prefers_its_own_name(self):
        owner = AsporaKongRouteScriptGenComponentV2._owner_group(
            [], "brand-new-service", [],
            proposed_keys=["brand-new-service-tag1", "brand-new-service"],
            content=TAG_OWNED_HCL,
        )
        assert owner == "brand-new-service"

    def test_owner_absent_from_file_and_change_falls_back_to_a_written_group(self):
        """The shape that produced the reported file: route rows named a group
        the gateway never received, so the owner was a group nobody creates."""
        owner = AsporaKongRouteScriptGenComponentV2._owner_group(
            [_OwnerRow("ghost-service-tag1")], "ghost-service",
            ["ghost-service-tag1"],
            content=TAG_OWNED_HCL,
        )
        # Resolution alone still answers with the phantom group...
        assert owner == "ghost-service-tag1"
        # ...which is why generate() re-resolves it over the groups being written.


# The owner group plus a dependant, the shape every service has in the real file.
OWNED_HCL = '''inputs = {
  kong_configs = {
    "svc-a" = {
      service = {
        host     = "alb.internal"
        protocol = "http"
        port     = 80
        path     = "/"
      }
      routes = {
        "GET" = ["~/a$"]
      }
    }
    "svc-b" = {
      existing_service = "svc-a"
      routes = {
        "GET" = ["~/b$"]
      }
    }
  }
}
'''


class TestRemoveRouteKeepsTheOwner:
    """kong_service.services is keyed by the owning group and indexed without a
    fallback, so emptying the owner must not delete its entry."""

    def test_emptying_the_owner_keeps_its_service_block(self):
        out = remove_route(OWNED_HCL, "svc-a", "GET", "~/a$")

        assert group_owns_service(out, "svc-a")
        assert '"svc-a" = {' in out
        # the dependant's pointer still resolves
        assert 'existing_service = "svc-a"' in out
        assert find_owner_in_content(out, ["svc-b"]) == "svc-a"
        # the path itself is gone
        assert "~/a$" not in out

    def test_emptying_a_non_owner_still_drops_the_group(self):
        """Unchanged behaviour for groups that own nothing."""
        out = remove_route(OWNED_HCL, "svc-b", "GET", "~/b$")

        assert '"svc-b" = {' not in out
        assert group_owns_service(out, "svc-a")


# A file whose comments contain the characters the parser used to trip on.
COMMENTED_HCL = '''inputs = {
  kong_configs = {
    # a stray brace } and a lone quote " in prose
    "svc-a" = {
      service = {
        host     = "alb.internal"
        protocol = "http"
        port     = 80
        path     = "/"
      }
      routes = {
        "GET" = [
          # "~/disabled-on-purpose$",
          "~/a$"
        ]
      }
    }
    # "svc-ghost" = {
    #   existing_service = "svc-a"
    #   routes = { "GET" = ["~/ghost$"] }
    # }
  }
}
'''


class TestCommentsAreNotLiveHcl:
    """Comments carry braces, quotes and whole commented-out blocks. Parsing
    them as HCL corrupted the file or silently skipped every edit."""

    def test_a_brace_and_quote_in_a_comment_do_not_break_an_add(self):
        out = add_route(
            COMMENTED_HCL, "svc-a", "svc-a", "POST", "~/new$",
            {"host": "alb.internal", "protocol": "http", "port": 80, "path": "/"},
        )

        assert "~/new$" in read_deployed_group(out, "svc-a", "POST")["paths"]
        # the comment is still a comment, not a splice point
        assert "# a stray brace } and a lone quote" in out
        assert out.count('"svc-a" = {') == 1

    def test_a_commented_out_group_is_not_live(self):
        assert _find_block(COMMENTED_HCL, '"svc-ghost"', *_kc_bounds(COMMENTED_HCL)) is None
        assert not group_owns_service(COMMENTED_HCL, "svc-ghost")
        assert find_owner_in_content(COMMENTED_HCL, ["svc-ghost"]) is None

    def test_a_commented_out_path_is_not_resurrected(self):
        """It is disabled on purpose — rewriting the array must not re-add it."""
        out = add_route(
            COMMENTED_HCL, "svc-a", "svc-a", "GET", "~/c$",
            {"host": "alb.internal", "protocol": "http", "port": 80, "path": "/"},
        )

        paths = read_deployed_group(out, "svc-a", "GET")["paths"]
        assert paths == ["~/a$", "~/c$"]
        assert "~/disabled-on-purpose$" not in paths

    def test_an_unbalanced_quote_in_a_comment_does_not_freeze_the_parser(self):
        """The matcher used to stay 'inside a string' for the rest of the file,
        so every later edit became a silent no-op."""
        out = remove_route(COMMENTED_HCL, "svc-a", "GET", "~/a$")

        assert "~/a$" not in out
        assert out != COMMENTED_HCL


class TestRemoveRouteWithRegexBraces:
    """Kong paths are regexes, so `{` is a repetition count, not structure."""

    QUANT = OWNED_HCL.replace('"~/b$"', '"~/otp/(?<code>[0-9]{4,6})$"')

    def test_a_quantifier_path_does_not_leave_an_empty_array(self):
        """`"GET" = []` is not harmless: terraform builds a route with methods
        and no paths, which matches EVERY path on the service."""
        out = remove_route(self.QUANT, "svc-b", "GET", "~/otp/(?<code>[0-9]{4,6})$")

        assert '"GET" = []' not in out
        # svc-b owns nothing, so an emptied group is removed as before
        assert '"svc-b" = {' not in out
        assert group_owns_service(out, "svc-a")

    def test_repeated_delete_is_idempotent(self):
        once = remove_route(self.QUANT, "svc-b", "GET", "~/otp/(?<code>[0-9]{4,6})$")
        twice = remove_route(once, "svc-b", "GET", "~/otp/(?<code>[0-9]{4,6})$")

        assert once == twice

    def test_an_inline_group_still_keeps_its_braces(self):
        """The guard's real purpose: a hand-written one-liner must not lose a
        brace — it empties the array instead."""
        inline = '''inputs = {
  kong_configs = {
    "svc-a" = {
      service = { host = "alb.internal" }
      routes = { "GET" = ["~/a$"] }
    }
  }
}
'''
        out = remove_route(inline, "svc-a", "GET", "~/a$")

        assert '"GET" = []' in out
        assert out.count("{") == out.count("}")
        assert "~/a$" not in out


# ============================================================================
# GATEWAY CARD: ADD / REMOVE / RENAME PATHS
# service_payloads.build_gateway_group_save — the chat/MCP card turned into the
# GatewayGroupSave the Gateway tab posts. `paths` is the DESIRED state that
# _reconcile_paths reconciles the tables to; `delta` is the change record.
# ============================================================================
from app.mcp_servers.devlift_mcp import service_payloads as _sp

_STATE = {
    "service_name": "goms",
    "groups": [
        {
            "route_group_key": "goms",
            "http_method": "GET",
            "plugins": ["JWT"],
            "regex_priority": 0,
            "updated_at": "2026-09-15T00:00:00Z",
            "code": "KRG_1",
            "paths": [
                {"code": "KRC_A", "route_path": "~/api/v1/a$"},
                {"code": "KRC_B", "route_path": "~/api/v1/b$"},
            ],
        }
    ],
}


def _gw_card(**over):
    base = {
        "service_mst_code": "SVC",
        "service_name": "goms",
        "environment": "stage",
        "geo_loc_mst_code": "geo",
        "http_method": "GET",
        "secured": True,
        "route_group_key": "goms",
        "paths": [],
    }
    base.update(over)
    return base


def _gw_desired(save):
    return {p["route_path"]: p.get("code") for p in save["paths"]}


def _gw_actions(save):
    return [(a["action"], a.get("old_path"), a["route_path"]) for a in save["delta"]["paths"]]


@pytest.mark.unit
def test_removing_a_path_drops_it_from_the_desired_state():
    # _reconcile_paths removes whatever is absent from `paths`, so a removal is
    # expressed by leaving it out — and recorded so it is not a silent vanish.
    save, summary = _sp.build_gateway_group_save(
        _gw_card(remove_paths=["~/api/v1/a$"]), _STATE
    )
    assert "~/api/v1/a$" not in _gw_desired(save)
    assert "~/api/v1/b$" in _gw_desired(save), "an untouched path must survive"
    assert ("delete", None, "~/api/v1/a$") in _gw_actions(save)
    assert summary["removed_paths"] == ["~/api/v1/a$"]


@pytest.mark.unit
def test_renaming_a_path_keeps_its_code():
    # Same row, new path — not delete-and-recreate, so the route keeps identity.
    save, summary = _sp.build_gateway_group_save(
        _gw_card(edit_paths=[{"name": "~/api/v1/a$", "value": "~/api/v1/z$"}]), _STATE
    )
    d = _gw_desired(save)
    assert "~/api/v1/a$" not in d
    assert d["~/api/v1/z$"] == "KRC_A", "the rename must keep the original row"
    assert ("edit", "~/api/v1/a$", "~/api/v1/z$") in _gw_actions(save)
    assert summary["edited_paths"] == [{"from": "~/api/v1/a$", "to": "~/api/v1/z$"}]


@pytest.mark.unit
def test_add_remove_and_rename_in_one_card():
    save, _ = _sp.build_gateway_group_save(
        _gw_card(
            paths=["~/api/v1/new$"],
            remove_paths=["~/api/v1/b$"],
            edit_paths=[{"name": "~/api/v1/a$", "value": "~/api/v1/a2$"}],
        ),
        _STATE,
    )
    d = _gw_desired(save)
    assert set(d) == {"~/api/v1/a2$", "~/api/v1/new$"}
    assert d["~/api/v1/a2$"] == "KRC_A"
    acts = _gw_actions(save)
    assert ("edit", "~/api/v1/a$", "~/api/v1/a2$") in acts
    assert ("delete", None, "~/api/v1/b$") in acts
    assert ("add", None, "~/api/v1/new$") in acts


@pytest.mark.unit
def test_renaming_a_path_the_group_does_not_have_is_refused():
    with pytest.raises(_sp.GatewayConflict) as exc:
        _sp.build_gateway_group_save(
            _gw_card(edit_paths=[{"name": "~/nope$", "value": "~/x$"}]), _STATE
        )
    assert "~/nope$" in str(exc.value)


@pytest.mark.unit
def test_removing_a_path_the_group_does_not_have_is_refused():
    # Silently ignoring it would report success for a removal that never happened.
    with pytest.raises(_sp.GatewayConflict) as exc:
        _sp.build_gateway_group_save(_gw_card(remove_paths=["~/nope$"]), _STATE)
    assert "~/nope$" in str(exc.value)


@pytest.mark.unit
def test_a_card_that_only_removes_is_still_a_change():
    # `paths` is optional now, so a remove-only card must not read as "nothing".
    save, _ = _sp.build_gateway_group_save(_gw_card(remove_paths=["~/api/v1/a$"]), _STATE)
    assert save is not None


@pytest.mark.unit
def test_an_empty_card_is_rejected():
    with pytest.raises(_sp.ServicePayloadError):
        _sp.gateway_group_block({"gateway_group": _gw_card()})


@pytest.mark.unit
def test_a_card_with_only_removals_passes_validation():
    block = _sp.gateway_group_block({"gateway_group": _gw_card(remove_paths=["~/api/v1/a$"])})
    assert block["remove_paths"] == ["~/api/v1/a$"]


@pytest.mark.unit
def test_a_card_that_only_adds_a_plugin_passes_validation():
    """"Add User ID Injection to the orders group" touches no path. The guard
    used to refuse it with a message about paths, although the save layer had
    always handled it."""
    block = _sp.gateway_group_block(
        {"gateway_group": _gw_card(plugins=["User ID Injection"])})
    assert block["plugins"] == ["User ID Injection"]


@pytest.mark.unit
def test_the_empty_card_message_now_names_the_plugin_way_out():
    with pytest.raises(_sp.ServicePayloadError) as exc:
        _sp.gateway_group_block({"gateway_group": _gw_card()})
    assert "or a plugin for the route group" in str(exc.value)


@pytest.mark.unit
def test_a_plugin_only_card_becomes_a_save_that_moves_no_path():
    save, summary = _sp.build_gateway_group_save(
        _gw_card(plugins=["User ID Injection"]), _STATE)
    assert save is not None
    assert save["plugins"] == ["JWT", "User ID Injection"], "auth stays in front"
    assert save["delta"]["paths"] == [], "a plugin change moves nothing"
    assert _gw_desired(save) == {"~/api/v1/a$": "KRC_A", "~/api/v1/b$": "KRC_B"}
    assert summary["plugins"] == {"from": ["JWT"], "to": ["JWT", "User ID Injection"]}


@pytest.mark.unit
def test_a_plugin_the_group_already_has_is_still_nothing_to_save():
    state = {**_STATE, "groups": [
        {**_STATE["groups"][0], "plugins": ["JWT", "User ID Injection"]}]}
    save, _ = _sp.build_gateway_group_save(
        _gw_card(plugins=["User ID Injection"]), state)
    assert save is None


@pytest.mark.unit
def test_a_rename_to_the_same_path_is_not_a_change():
    save, _ = _sp.build_gateway_group_save(
        _gw_card(edit_paths=[{"name": "~/api/v1/a$", "value": "~/api/v1/a$"}]), _STATE
    )
    assert save is None, "renaming a path to itself changes nothing"


# ============================================================
# Plain path in chat: rejected, then suggested — never silently converted
# ============================================================
# The Gateway tab takes the Kong regex EXACTLY as stored (`pathFormatError` in
# GatewayContentV4.tsx). It stopped accepting plain paths on purpose: the
# compiled result was not what the user typed, and decompile/recompile is lossy
# (`~/minis/(?<proxy>.*)$` comes back as `~/minis/(?<rest>.+)$`). Chat holds the
# same contract, so the compiler is used only to OFFER a correction.

from app.mcp_servers.devlift_mcp.tools.chat import (  # noqa: E402
    _build_next_action as _next_action,
    _kong_path_candidates,
    _kong_path_suggestions,
)


def _rejected(field_id, value):
    return [{"field_id": field_id, "value": value, "error": "format"}]


@pytest.mark.unit
def test_plain_path_is_offered_back_in_kong_form():
    out = _kong_path_suggestions(_rejected("paths", ["/api/v1/users"]))
    assert "'/api/v1/users' -> '~/api/v1/users$'" in out


@pytest.mark.unit
def test_suggestion_handles_a_path_parameter():
    """The conversion the model reliably gets wrong if it tries it itself."""
    out = _kong_path_suggestions(_rejected("paths", ["/files/:id"]))
    assert "'~/files/(?<id>[^/]+)$'" in out


@pytest.mark.unit
def test_suggestion_tells_the_model_not_to_convert_or_assume():
    out = _kong_path_suggestions(_rejected("paths", ["/api/v1/users"]))
    assert "convert a path yourself" in out
    assert "do NOT assume the suggestion is right" in out


@pytest.mark.unit
def test_an_already_valid_path_is_not_suggested_back():
    """Rejected for some other reason — offering it unchanged would read as
    'you typed it wrong' next to an identical string."""
    assert _kong_path_suggestions(_rejected("paths", ["~/api/v1/users$"])) == ""


@pytest.mark.unit
def test_non_path_rejections_are_left_alone():
    assert _kong_path_suggestions(_rejected("tag", ["not a tag!"])) == ""


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [
    ("/a", ["/a"]),
    (["/a", "/b"], ["/a", "/b"]),
    ({"/a": "/b"}, ["/a", "/b"]),
    ([{"name": "/a", "value": "/b"}], ["/a", "/b"]),
    (None, []),
])
def test_every_field_shape_is_unwrapped(value, expected):
    """array, key_value-as-dict, key_value-as-pairs, and a bare item."""
    assert _kong_path_candidates(value) == expected


@pytest.mark.unit
def test_rename_suggests_both_sides():
    out = _kong_path_suggestions(_rejected("edit_paths", [{"name": "/old", "value": "/new"}]))
    assert "'/old' -> '~/old$'" in out and "'/new' -> '~/new$'" in out


@pytest.mark.unit
def test_duplicate_paths_are_suggested_once():
    out = _kong_path_suggestions(_rejected("paths", ["/a", "/a", "/b"]))
    assert out.count("'/a' -> '~/a$'") == 1


@pytest.mark.unit
def test_the_suggestion_rides_on_the_fix_invalid_action():
    action = _next_action({
        "isReady": False,
        "invalid_fields": _rejected("paths", ["/api/v1/users"]),
        "missing_fields": {"required": ["paths"], "optional": []},
    })
    assert action["type"] == "fix_invalid"
    assert "'~/api/v1/users$'" in action["instruction"]


# ============================================================
# Tag choices: the Gateway tab's own order, v2 naming
# ============================================================
# A tag's real options are the SERVICE's existing route groups, which the
# chatbot never sees — so the field reaches the user as free text whose only
# pill is "Skip". These build what the tab preloads instead.
#
# v2 naming throughout: the PLAIN service name, matching `preloadTag` /
# `suggestTags` in GatewayContentV4.tsx and `default_route_group_key`.
# `kong_route_group_naming.default_group_key` enforces `-service` — that is the
# v1 / infrastructure-request rule and must not leak in here.


@pytest.mark.unit
def test_first_choice_is_the_plain_service_name():
    """That group carries the terragrunt `service { }` block."""
    assert _sp.gateway_tag_choices("goms")[0] == "goms"


@pytest.mark.unit
def test_first_choice_is_not_the_v1_service_suffixed_name():
    from app.domain.policies.kong_route_group_naming import default_group_key

    assert default_group_key("goms") == "goms-service"       # v1 rule, unchanged
    assert _sp.gateway_tag_choices("goms")[0] == "goms"      # v2 is what chat offers


@pytest.mark.unit
def test_with_no_existing_groups_the_rest_are_tagn_suggestions():
    assert _sp.gateway_tag_choices("goms") == ["goms", "goms-tag1", "goms-tag2"]


@pytest.mark.unit
def test_existing_groups_are_offered_before_fresh_names():
    assert _sp.gateway_tag_choices("goms", ["goms-open"]) == ["goms", "goms-open", "goms-tag1"]


@pytest.mark.unit
def test_once_the_plain_name_is_taken_the_default_becomes_tag1():
    choices = _sp.gateway_tag_choices("goms", ["goms"])
    assert choices[0] == "goms-tag1"
    assert choices[1] == "goms"          # still offered — adding to it is valid


@pytest.mark.unit
def test_taken_names_are_never_suggested_as_new():
    choices = _sp.gateway_tag_choices("goms", ["goms", "goms-tag1"])
    assert choices[0] == "goms-tag2"


@pytest.mark.unit
def test_case_insensitive_like_the_frontend():
    assert _sp.gateway_tag_choices("goms", ["GOMS"])[0] == "goms-tag1"


@pytest.mark.unit
def test_choices_are_capped_so_skip_still_fits_in_the_dialog():
    """AskUserQuestion shows four; the fourth has to be Skip."""
    many = ["a-1", "a-2", "a-3", "a-4", "a-5"]
    assert len(_sp.gateway_tag_choices("goms", many)) == 3


@pytest.mark.unit
def test_no_service_name_means_no_suggestions():
    assert _sp.gateway_tag_choices("") == []
    assert _sp.gateway_tag_choices("   ") == []


@pytest.mark.unit
def test_both_bases_walk_the_same_rule():
    """v1 and v2 differ ONLY in what they count from — one implementation."""
    from app.domain.policies.kong_route_group_naming import (
        preload_for, suggest_tags, tag_names,
    )

    assert tag_names("goms", count=2) == ["goms-tag1", "goms-tag2"]           # v2 base
    assert suggest_tags("goms", count=2) == ["goms-service-tag1",
                                             "goms-service-tag2"]            # v1 base
    assert preload_for("goms", ["goms"]) == "goms-tag1"
    assert tag_names("", count=2) == [] and preload_for("") == ""


# ============================================================
# Card-level questions, answered from the gateway read at the RIGHT moment
# ============================================================
# Tag and priority only have real options once the method, the auth and the PATH
# are known — so the gateway is read then, not carried from the first turn. Late
# is also correct: another user's save can land in the same gateway meanwhile.

from app.mcp_servers.devlift_mcp.tools import chat as _chat  # noqa: E402


def _live_card(tag, method="GET", secured=True, priority=0, paths=()):
    return {
        "http_method": method, "secured": secured, "route_group_key": tag,
        "plugins": [], "regex_priority": priority,
        "paths": [{"route_path": p, "deployed": True} for p in paths],
    }


def _new_card(**over):
    base = {
        "service_mst_code": "SVC", "service_name": "goms", "environment": "stage",
        "geo_loc_mst_code": "geo", "http_method": "GET", "secured": True,
        "route_group_key": None, "paths": ["~/api/v1/new$"],
    }
    base.update(over)
    return base


@pytest.mark.unit
def test_tag_offers_only_groups_this_card_could_join():
    """A tag is one group per (tag, method); auth is outside that identity, so
    reusing one under the other auth is refused at save. Never offer it."""
    cards = [_live_card("goms", secured=True), _live_card("goms-open", secured=False)]
    out = _chat._tag_action({"field_id": "tag"}, _new_card(secured=True), cards)
    # `goms-open` is No Auth — not offered. `goms` is Auth, so it IS joinable.
    # The preload steps past `goms` because it is taken, exactly as `preloadTag`
    # does; the joinable group follows it.
    assert [o["text"] for o in out["options"]] == ["goms-tag1", "goms", "goms-tag2", "Skip"]
    assert out["options"][1]["description"].startswith("existing group")


@pytest.mark.unit
def test_a_taken_name_under_the_other_auth_is_still_stepped_over():
    """`goms-tag1` belongs to a No Auth group — unjoinable here, but suggesting
    it as NEW would hand the user a name the save then refuses."""
    cards = [_live_card("goms", secured=True), _live_card("goms-tag1", secured=False)]
    out = _chat._tag_action({"field_id": "tag"}, _new_card(secured=True), cards)
    assert "goms-tag1" not in [o["text"] for o in out["options"]]


@pytest.mark.unit
def test_an_existing_group_shows_what_is_already_in_it():
    cards = [_live_card("goms", paths=["~/a$", "~/b$", "~/c$"])]
    out = _chat._tag_action({"field_id": "tag"}, _new_card(), cards)
    joinable = next(o for o in out["options"] if o["text"] == "goms")
    assert joinable["description"] == "existing group — ~/a$, ~/b$, ..."


@pytest.mark.unit
def test_another_methods_groups_are_irrelevant():
    cards = [_live_card("goms-post", method="POST")]
    out = _chat._tag_action({"field_id": "tag"}, _new_card(http_method="GET"), cards)
    assert [o["text"] for o in out["options"]] == ["goms", "goms-tag1", "goms-tag2", "Skip"]


# ── the FIRST turn of an edit session is enriched too ────────────────────────
# Live run, in a brand new chat: "I want to delete a path from gateway for
# jp-test-service" printed the whole gateway as a table, decided the tag on
# the user's behalf ("only one tag exists, so I'll fill that in"), and asked
# for the paths in prose. None of the Remove work ran. `edit_service_
# configuration` builds its own next_action and never called the enrichment —
# `chat` enriches every LATER turn, which is exactly why this looked like a
# reload problem instead of a missing call.

def _gateway_edit_action(route_action, live_cards, gateway_group=None, section=None,
                         missing=("remove_paths",)):
    """What `_start_gateway_edit` decides, without its I/O."""
    from app.mcp_servers.devlift_mcp.dispatcher import _section_action  # noqa: F401
    from app.mcp_servers.devlift_mcp.tools.chat import (
        _LIVE_GROUP_INTENTS, _card_group_is_live, _group_pick_action,
        _named_paths, _plugin_card_action,
    )

    gw = gateway_group or {}
    if route_action in _LIVE_GROUP_INTENTS and live_cards and not _card_group_is_live(gw, live_cards):
        want_tag = str(gw.get("route_group_key") or "")
        if route_action == "Plugins":
            return _plugin_card_action(live_cards, want_tag)
        return _group_pick_action(live_cards, want_tag, route_action,
                                  tuple(_named_paths(gw, route_action)),
                                  str(gw.get("http_method") or ""))
    return None


@pytest.mark.unit
@pytest.mark.parametrize("intent,expected", [
    ("Remove", "ask_route_group"), ("Rename", "ask_route_group"),
    ("Plugins", "ask_plugin_card"),
])
def test_an_edit_session_opens_on_the_group_question(intent, expected):
    """One method on this service, so the method step is skipped and the tag
    question comes first — the same question `chat` would have built."""
    out = _gateway_edit_action(intent, [
        _live_card("jp-test-service", paths=["~/api/v1/orders$"]),
        _live_card("jp-test-service-open", secured=False, paths=[]),
    ])
    assert out is not None and out["type"] == expected


@pytest.mark.unit
def test_an_add_session_is_left_to_the_form():
    """It may be creating the group, so the form's own dialog is right."""
    assert _gateway_edit_action("Add", [_live_card("goms", paths=["~/a$"])]) is None


@pytest.mark.unit
def test_an_unknown_intent_is_left_to_the_form():
    """'delete' and 'edit' do not map, so the action is asked first and the
    later turns go through `chat`, which enriches them."""
    assert _gateway_edit_action(None, [_live_card("goms", paths=["~/a$"])]) is None


@pytest.mark.unit
def test_a_service_with_no_groups_is_left_to_the_form():
    assert _gateway_edit_action("Remove", []) is None


@pytest.mark.unit
def test_a_session_that_already_names_a_live_group_goes_straight_on():
    """Nothing to pick — `chat` takes it to the paths on the next turn."""
    assert _gateway_edit_action("Remove", [_live_card("goms", method="GET", secured=True)],
                                gateway_group=_new_card(route_group_key="goms",
                                                        http_method="GET", secured=True)) is None


# ── the group comes before its paths ─────────────────────────────────────────
# Remove and Rename were asked the form's way round: an HTTP method from
# nothing, an auth mode from nothing, a tag — and only then a path, which the
# user had to reproduce exactly from memory. But a route group IS (tag,
# method) and carries its own auth, so one row of the live gateway answers all
# three, and the group's paths are then a list to pick from. 'Add' is left
# alone: it may be creating the group, so there is nothing to pick.

def _group_pick(cards, intent="Remove", want_tag="", named=(), have_method=""):
    return _chat._group_pick_action(cards, want_tag, intent, named, have_method)


@pytest.mark.unit
@pytest.mark.parametrize("intent,phrase", [
    ("Remove", "remove paths from"), ("Rename", "rename a path in"),
])
def test_the_tag_pick_carries_the_auth_so_it_is_never_asked(intent, phrase):
    out = _group_pick([_live_card("goms", paths=["~/a$", "~/b$"]),
                       _live_card("goms-open", secured=False, paths=["~/c$"])], intent)
    assert out["type"] == "ask_route_group"
    assert [g["text"] for g in out["groups"]] == ["goms · GET", "goms-open · GET"]
    assert out["groups"][0]["secured"] == "Auth - JWT required"
    text = out["instruction"]
    assert phrase in text
    assert "Do NOT ask for an auth mode" in text
    assert "answers={'tag': <tag>, 'method': <method>, 'secured': <secured>}" in text


# ── one axis per question ────────────────────────────────────────────────────
# `<tag> · <METHOD>` in one pick is one step fewer and does not survive a real
# service: tags times methods is a list nobody reads, and those two axes are
# exactly what makes it long. Split, each question is bounded — at most six
# methods, then the tags of ONE method.

def _many_groups(tags=("orders", "orders-admin", "users"),
                 methods=("GET", "POST", "PUT", "DELETE")):
    return [_live_card(t, method=m, paths=[f"~/{t}/{m.lower()}$"])
            for t in tags for m in methods]


@pytest.mark.unit
def test_the_method_is_asked_before_the_tag():
    out = _group_pick(_many_groups())
    assert out["type"] == "ask_route_method"
    assert [m["text"] for m in out["methods"]] == ["GET", "POST", "PUT", "DELETE"]
    assert out["methods"][0]["description"] == "3 route groups · 3 paths"
    text = out["instruction"]
    assert "Ask ONLY that" in text
    assert "answers={'method': <method>}" in text
    assert "the tag question follows" in text


# Live run: Method (GET / POST), then a "Route group" dialog with one row and
# a confirm. Each method on that service has exactly ONE group, so picking the
# method had already picked the group — the second dialog was a question whose
# answer the first one decided.

@pytest.mark.unit
def test_a_method_with_one_group_carries_it():
    out = _group_pick([_live_card("goms", method="GET", paths=["~/a$"]),
                       _live_card("goms", method="POST", secured=False, paths=["~/b$"])])
    assert out["type"] == "ask_route_method"
    assert [(m["text"], m["tag"], m["secured"]) for m in out["methods"]] == [
        ("GET", "goms", "Auth - JWT required"),
        ("POST", "goms", "No Auth - public")]
    text = out["instruction"]
    assert "choosing it chose the group" in text
    assert "answers={'method': <method>, 'tag': <tag>, 'secured': <secured>}" in text
    assert "do NOT then ask which group they meant" in text


@pytest.mark.unit
def test_a_method_with_several_groups_carries_none_of_them():
    out = _group_pick([_live_card("goms", method="GET", paths=["~/a$"]),
                       _live_card("goms-admin", method="GET", paths=["~/b$"]),
                       _live_card("goms", method="POST", paths=["~/c$"])])
    by_method = {m["text"]: m for m in out["methods"]}
    assert "tag" not in by_method["GET"], "two groups there, so it is a real question"
    assert by_method["POST"]["tag"] == "goms"
    assert "Otherwise send only answers={'method': <method>}" in out["instruction"]


@pytest.mark.unit
def test_one_group_left_is_stated_not_asked():
    out = _group_pick([_live_card("goms", paths=["~/a$"])])
    text = out["instruction"]
    assert "Do NOT ask about it, with a dialog or otherwise" in text
    assert "a question with one answer" in text
    assert "go straight on to the paths" in text


@pytest.mark.unit
def test_only_methods_that_have_groups_are_offered():
    """A method absent from the list has nothing to remove — offering all six
    sends the user to a group that does not exist."""
    out = _group_pick(_many_groups(methods=("GET", "POST")))
    assert [m["text"] for m in out["methods"]] == ["GET", "POST"]
    assert "the ONLY methods this service has groups on" in out["instruction"]


@pytest.mark.unit
def test_the_chosen_method_narrows_the_tag_question_to_it():
    out = _group_pick(_many_groups(), have_method="POST")
    assert out["type"] == "ask_route_group"
    assert out["method"] == "POST"
    assert [g["text"] for g in out["groups"]] == [
        "orders · POST", "orders-admin · POST", "users · POST"]
    assert "Which route group on POST" in out["instruction"]
    assert "If they want a different method, ask the method question" in out["instruction"]


# Live run: the confirm for the only group on the service came with a "No,
# different method" row. Picking it produced a second question that said, in
# its own words, "no other method currently has routes on this service" — a
# choice whose only outcome is the question again. The escape is only real
# when there IS somewhere else to go.

@pytest.mark.unit
def test_a_service_with_one_method_is_not_offered_a_method_change():
    out = _group_pick([_live_card("goms", paths=["~/a$"]),
                       _live_card("goms-open", secured=False, paths=["~/b$"])])
    text = out["instruction"]
    assert "GET is the ONLY method this service has routes on" in text
    assert "do not offer to change it" in text
    assert "the answer to that question is this same card" in text
    assert "If they want a different method" not in text


@pytest.mark.unit
def test_the_only_group_on_the_only_method_says_exactly_that():
    out = _group_pick([_live_card("goms", paths=["~/a$"])])
    text = out["instruction"]
    assert "It is the only route group this service has, on its only method" in text
    assert "offer no alternative card and no 'different method'" in text


@pytest.mark.unit
def test_a_lone_group_on_a_service_with_other_methods_keeps_the_escape():
    """There, 'did you mean another method?' has a real answer."""
    out = _group_pick([_live_card("goms", paths=["~/orders$"]),
                       _live_card("goms", method="POST", paths=["~/b$"])],
                      named=("~/orders$",))
    text = out["instruction"]
    assert "Exactly ONE route group is left" in text
    assert "only route group this service has" not in text
    assert "If they want a different method" in text


@pytest.mark.unit
def test_one_method_on_the_service_is_not_a_question():
    """A picker of one row is a question the user cannot answer wrongly and
    still has to read."""
    out = _group_pick(_many_groups(methods=("GET",)))
    assert out["type"] == "ask_route_group"
    assert out["method"] == "GET"


@pytest.mark.unit
def test_one_group_left_names_which_one():
    assert "Exactly ONE route group is left: goms · GET" in (
        _group_pick([_live_card("goms", paths=["~/a$"])])["instruction"])


@pytest.mark.unit
def test_the_group_rows_say_whether_there_is_anything_to_work_on():
    out = _group_pick([_live_card("goms", paths=["~/a$", "~/b$"]),
                       _live_card("goms-empty", paths=[])])
    assert out["groups"][0]["description"] == "Auth · 2 paths"
    assert out["groups"][1]["description"] == "Auth · 0 paths"


@pytest.mark.unit
def test_a_plugin_card_still_sees_plugins_not_path_counts():
    """The same rows, described for what the picker is FOR."""
    out = _chat._plugin_card_action([
        dict(_live_card("goms"), plugins=["JWT", "User ID Injection"])])
    assert out["groups"][0]["description"] == "Auth · User ID Injection"


@pytest.mark.unit
def test_the_paths_are_not_asked_in_the_group_pick():
    text = _group_pick([_live_card("goms", paths=["~/a$"])])["instruction"]
    assert "The paths come next, from THAT group, and are not asked here" in text


@pytest.mark.unit
def test_a_service_with_no_groups_leaves_remove_to_the_form():
    """Nothing to remove from — degrade rather than show an empty picker."""
    assert _group_pick([]) is None


@pytest.mark.unit
def test_many_tags_on_one_method_are_numbered_rather_than_dropped():
    """Even split by method, a service can have more tags than a picker holds
    — and past four AskUserQuestion drops the rest silently."""
    cards = [_live_card(f"goms-tag{i}", method="GET") for i in range(6)]
    out = _group_pick(cards)
    assert out["render"] == "text_list"
    assert "There are 6 on GET" in out["instruction"]
    assert "NUMBERED list" in out["instruction"]
    assert _group_pick(cards, want_tag="goms-tag3")["render"] == "pills"


# ── a path does not identify a route ─────────────────────────────────────────
# The SAME path can sit on several methods, and on several groups of one
# method — that is what a route group is FOR. So "remove ~/api/v1/orders$"
# names a string, not a route: the group pick ignored it and the path question
# said only the path, leaving the user unable to tell which of three identical
# rows they were about to delete.

@pytest.mark.unit
def test_a_named_path_rules_out_every_group_without_it():
    """It still spans two methods, so the method question comes first — but
    only over the methods that actually carry the path."""
    out = _group_pick([
        _live_card("goms", paths=["~/orders$", "~/a$"]),
        _live_card("goms-admin", paths=["~/b$"]),
        _live_card("goms", method="POST", paths=["~/orders$"]),
    ], named=("~/orders$",))
    assert out["type"] == "ask_route_method"
    assert [m["text"] for m in out["methods"]] == ["GET", "POST"]


@pytest.mark.unit
def test_one_holder_is_confirmed_rather_than_offered_as_a_choice():
    out = _group_pick([
        _live_card("goms", paths=["~/orders$"]),
        _live_card("goms-admin", paths=["~/b$"]),
    ], named=("~/orders$",))
    assert [g["text"] for g in out["groups"]] == ["goms · GET"]
    text = out["instruction"]
    assert "Exactly ONE route group is left" in text
    assert "method and auth included" in text
    assert "naming a path does NOT pick a group" in text


@pytest.mark.unit
def test_the_row_says_which_named_path_it_holds():
    out = _group_pick([_live_card("goms", paths=["~/orders$", "~/a$"])],
                      named=("~/orders$",))
    assert out["groups"][0]["description"] == "Auth · 2 paths · has ~/orders$"


@pytest.mark.unit
def test_a_named_path_nobody_has_leaves_every_group_showing():
    """A typo or a stale path must not empty the picker — the user still has
    to choose a group, and the save will refuse the path soon enough."""
    out = _group_pick([_live_card("goms", paths=["~/a$"]),
                       _live_card("goms-admin", paths=["~/b$"])],
                      named=("~/typo$",))
    assert len(out["groups"]) == 2
    assert "groups have it" not in out["instruction"]


@pytest.mark.unit
@pytest.mark.parametrize("intent,card,expected", [
    ("Remove", {"remove_paths": ["~/a$", " "]}, ["~/a$"]),
    ("Rename", {"edit_paths": {"~/old$": "~/new$"}}, ["~/old$"]),
    ("Rename", {"edit_paths": [{"name": "~/old$", "value": "~/new$"}]}, ["~/old$"]),
    ("Plugins", {"remove_paths": ["~/a$"]}, []),
])
def test_the_named_path_is_read_off_whichever_field_the_intent_uses(intent, card, expected):
    assert _chat._named_paths(_new_card(**card), intent) == expected


@pytest.mark.unit
@pytest.mark.parametrize("builder", ["_remove_action", "_rename_action"])
def test_the_path_question_names_the_group_it_acts_on(builder):
    """On screen, `~/api/v1/orders$` is three different routes. The question
    has to say which one."""
    group = _new_card(route_group_key="goms", http_method="GET", secured=True, paths=[])
    for cards in ([_live_card("goms", paths=["~/a$", "~/b$"])], [_with_paths(100)]):
        out = getattr(_chat, builder)({"field_id": "x"}, group, cards)
        assert "goms · GET · Auth" in out["instruction"]
        assert "only unique within" in out["instruction"]


@pytest.mark.unit
def test_the_group_label_says_the_auth_too():
    assert _chat._group_label(
        _new_card(route_group_key="goms", http_method="POST", secured=False)
    ) == "goms · POST · No Auth"


# ── removing a path is a pick, not a recital ─────────────────────────────────

def _remove(cards, **over):
    group = _new_card(route_group_key="goms", paths=[], **over)
    return _chat._remove_action({"field_id": "remove_paths", "options": [], "render": "pills"},
                                group, cards)


@pytest.mark.unit
def test_a_short_group_picks_the_paths_to_remove():
    out = _remove([_live_card("goms", paths=["~/api/v1/a$", "~/api/v1/b$"])])
    text = out["instruction"]
    assert out["render"] == "pills"
    assert "one question 'Paths to remove'" in text
    assert "the 2 entries of `paths`" in text
    assert "multiSelect TRUE" in text, "several removals are one card"


@pytest.mark.unit
def test_a_hundred_paths_are_searched_for_removal_too():
    out = _remove([_with_paths(100)])
    text = out["instruction"]
    assert out["render"] == "search"
    assert "goms · GET · Auth has 100 paths" in text
    assert "it is data, not a list to print" in text
    for branch in ("1 match", "2 to 4", "more", "none"):
        assert branch in text


@pytest.mark.unit
def test_a_removal_is_sent_as_a_list_of_stored_paths():
    for out in (_remove([_live_card("goms", paths=["~/a$"])]),
                _remove([_with_paths(100)]),
                _chat._remove_action({"field_id": "remove_paths"}, _new_card(), [],
                                     paths_known=False)):
        assert "answers={'remove_paths': ['<path>', ...]}" in out["instruction"]
        assert "exactly as it is stored" in out["instruction"]


@pytest.mark.unit
def test_only_this_groups_paths_can_be_removed():
    out = _remove([_live_card("goms", paths=["~/mine$"]),
                   _live_card("goms-admin", paths=["~/theirs$"])])
    assert out["paths"] == ["~/mine$"]


@pytest.mark.unit
def test_an_empty_group_is_not_asked_what_to_remove():
    out = _remove([_live_card("goms", paths=[])])
    assert "nothing to remove" in out["instruction"]
    assert "Do NOT ask them to type a path" in out["instruction"]


@pytest.mark.unit
def test_an_unreadable_gateway_does_not_recite_paths_from_memory():
    out = _chat._remove_action({"field_id": "remove_paths"}, _new_card(), [],
                               paths_known=False)
    assert "Do NOT list or guess paths from memory" in out["instruction"]


# Live run: the remove question came out as prose — "Which Kong path should
# be removed from jp-test-service (stage / Mumbai, GET, tag jp-test-service)?
# Use Other to give several at once" — with the model inventing a description
# under every row. The MCP had already read the group's paths; none of it
# reached the question. A field reaches `ask_user` only when the chatbot found
# something to SUGGEST for it: a dropdown, or the Skip pill that optional
# non-array fields get. `tag` and `regex_priority` have that pill. An array
# and a key_value have neither, so they arrive as `ask_user_text` — and the
# guard let only `ask_user` through, making every enrichment written for them
# unreachable.

@pytest.mark.unit
@pytest.mark.parametrize("field,marker", [
    ("remove_paths", "one question 'Paths to remove'"),
    ("edit_paths", "'Old path'"),
])
@pytest.mark.asyncio
async def test_a_field_asked_as_text_is_still_enriched(monkeypatch, field, marker):
    async def _cards(auth_ctx, group):
        return [_live_card("goms", paths=["~/api/v1/a$", "~/api/v1/b$"])]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    reply = {
        "status": "pending", "isReady": False, "invalid_fields": [], "suggestions": [],
        "missing_fields": {"required": [field], "optional": []},
        "gateway_group": _new_card(route_group_key="goms"),
        "collected_data": {"route_action": "Remove", "tag": "goms", "method": "GET"},
    }
    action = _chat._build_next_action(reply)
    assert action["type"] == "ask_user_text", "the shape that was being dropped"
    out = await _chat._enrich_card_options(action, reply, None)
    assert marker in out["instruction"]
    assert out["paths"] == ["~/api/v1/a$", "~/api/v1/b$"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_rejection_is_still_never_enriched(monkeypatch):
    """Widening the guard must not swallow a refusal the user has to see."""
    async def _cards(auth_ctx, group):
        return [_live_card("goms", paths=["~/a$"])]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    action = {"type": "fix_invalid", "field_id": "remove_paths",
              "invalid_fields": [{"field_id": "remove_paths", "error": "not on this group"}],
              "instruction": "Surface the chatbot's `message` verbatim ..."}
    out = await _chat._enrich_card_options(dict(action), {
        "gateway_group": _new_card(route_group_key="goms"),
        "collected_data": {"route_action": "Remove", "tag": "goms", "method": "GET"},
    }, None)
    assert out == action


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_group_pick_fires_for_remove_and_rename(monkeypatch):
    async def _cards(auth_ctx, group):
        return [_live_card("goms", paths=["~/a$"])]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    for intent in ("Remove", "Rename", "Plugins"):
        reply = {
            "status": "pending", "isReady": False, "invalid_fields": [], "suggestions": [],
            "missing_fields": {"required": ["tag"], "optional": []},
            "gateway_group": _new_card(),
            "collected_data": {"route_action": intent},
        }
        out = await _chat._enrich_card_options(
            {"type": "ask_section", "title": "Route", "fields": []}, reply, None)
        expected = "ask_plugin_card" if intent == "Plugins" else "ask_route_group"
        assert out["type"] == expected, intent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_add_card_is_left_to_its_own_dialog(monkeypatch):
    """It may be CREATING the group, so there is nothing live to pick."""
    async def _cards(auth_ctx, group):
        return [_live_card("goms", paths=["~/a$"])]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    action = {"type": "ask_section", "title": "Route", "fields": []}
    out = await _chat._enrich_card_options(dict(action), {
        "gateway_group": _new_card(), "collected_data": {"route_action": "Add"},
    }, None)
    assert out == action


# ── a rename is two steps, and the first one scales ──────────────────────────
# Attempt one put both halves in the card's dialog and every row came out as
# half an answer: `~/api/v1/orders$ -> ?`, with a line underneath saying the
# row is not really the answer. Attempt two listed the group's live paths,
# which reads fine for four and not at all for a hundred. A busy group has
# more paths than anyone will scroll, so the first step is a picker only while
# it fits, and a SEARCH once it does not — the whole list stays data the model
# matches against, never something printed.

def _rename(cards, **over):
    group = _new_card(route_group_key="goms", paths=[], **over)
    return _chat._rename_action({"field_id": "edit_paths", "options": [], "render": "pills"},
                                group, cards)


def _with_paths(n, prefix="~/api/v1/thing"):
    return _live_card("goms", paths=[f"{prefix}{i}$" for i in range(n)])


# ── one dialog, two tabs ─────────────────────────────────────────────────────
# 'Old path' and 'New path' side by side. The second cannot hold the new PATHS
# — they are made from the first tab's answer, and both tabs are rendered
# before either is answered — so it holds what to DO to the path. Two choices,
# because that is a picker's minimum and because those are the two cases.

@pytest.mark.unit
def test_the_rename_is_one_dialog_of_two_tabs():
    out = _rename([_live_card("goms", paths=["~/api/v1/users/1$", "~/api/v1/orders$"])])
    text = out["instruction"]
    assert out["render"] == "pills"
    assert out["paths"] == ["~/api/v1/users/1$", "~/api/v1/orders$"]
    assert "TWO questions, both in that one dialog" in text
    assert "'Old path'" in text and "'New path'" in text
    assert "the 2 entries of `paths`, verbatim" in text
    assert "STEP 1" not in text, "not two turns any more"


@pytest.mark.unit
def test_the_second_tab_offers_what_to_do_not_the_path():
    text = _rename([_live_card("goms", paths=["~/api/v1/users/1$"])])["instruction"]
    assert _chat._RENAME_PARAMETERISE in text
    assert _chat._RENAME_TYPE_IT in text
    assert "cannot be a choice" in text


@pytest.mark.unit
def test_parameterising_is_spelled_out_with_its_two_refusals():
    """A path with no hardcoded segment, and one that already has an `id`
    group — PCRE refuses two of one name, so the rewrite would be a path Kong
    will not accept."""
    text = _rename([_live_card("goms", paths=["~/api/v1/users/1$"])])["instruction"]
    assert "(?<id>[^/]+)" in text
    assert "no such segment" in text
    assert "already has an `id` group" in text
    assert "ask what it should become" in text


@pytest.mark.unit
def test_the_rename_answer_is_a_map_either_way():
    for out in (_rename([_live_card("goms", paths=["~/api/v1/a$"])]),
                _rename([_with_paths(100)])):
        assert "answers={'edit_paths': {'<old>': '<new>'}}" in out["instruction"]
        assert "exactly as it is stored" in out["instruction"]


@pytest.mark.unit
def test_a_crowded_group_still_takes_two_steps():
    """No picker holds the old path there, so it is searched first and the new
    one asked after."""
    text = _rename([_with_paths(100)])["instruction"]
    assert "STEP 1" in text
    assert "STEP 2 — only once step 1 has an answer" in text
    assert "in a separate turn" in text


@pytest.mark.unit
def test_only_this_groups_paths_are_offered():
    """Another group's path cannot be renamed from this card — `build_gateway_
    group_save` refuses it, naming the group's real paths."""
    out = _rename([
        _live_card("goms", paths=["~/mine$"]),
        _live_card("goms-admin", paths=["~/theirs$"]),
        _live_card("goms", method="POST", paths=["~/other-method$"]),
    ])
    assert out["paths"] == ["~/mine$"]


@pytest.mark.unit
def test_a_group_with_no_paths_is_not_asked_for_a_rename():
    out = _rename([_live_card("goms", paths=[])])
    assert "nothing to rename" in out["instruction"]
    assert "Do NOT ask them to type a rename" in out["instruction"]


@pytest.mark.unit
def test_an_unreadable_gateway_asks_without_a_list():
    """A remembered list is how a user is sent to rename a path that is not
    there — but the two steps do not depend on the read."""
    out = _chat._rename_action({"field_id": "edit_paths"}, _new_card(), [], paths_known=False)
    text = out["instruction"]
    assert "Do NOT list or guess paths from memory" in text
    assert "STEP 1 — ask as PLAIN TEXT" in text
    assert "STEP 2" in text
    assert "render" not in out, "nothing to render either way"


@pytest.mark.unit
def test_the_rename_answer_is_always_a_map():
    """Two loose paths in a sentence would be stored as one path with the
    sentence in it — a route that matches nothing and looks almost right."""
    for out in (_rename([_live_card("goms", paths=["~/api/v1/a$"])]),
                _rename([_with_paths(100)]),
                _chat._rename_action({"field_id": "edit_paths"}, _new_card(), [],
                                     paths_known=False)):
        assert "answers={'edit_paths': {'<old>': '<new>'}}" in out["instruction"]
        assert "the old path exactly as it is stored" in out["instruction"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_rename_is_enriched_like_the_other_card_fields(monkeypatch):
    async def _cards(auth_ctx, group):
        return [_live_card("goms", paths=["~/api/v1/a$"])]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    out = await _chat._enrich_card_options(
        {"type": "ask_user", "field_id": "edit_paths", "instruction": "old"},
        {"gateway_group": _new_card(route_group_key="goms")}, None)
    assert out["instruction"] != "old"


@pytest.mark.unit
def test_the_skip_pill_never_reaches_the_priority_question():
    """A priority is a number the user types, not one of three buttons. The
    chatbot's pill is dropped in BOTH branches — the uncontested one asks
    nothing at all, and the clash asks for a number outside a named set."""
    out = _chat._priority_action({"field_id": "regex_priority", "options": [], "render": "pills"},
                                 _new_card(), [])
    assert "options" not in out and "render" not in out
    out = _chat._priority_action(
        {"field_id": "regex_priority", "options": [], "render": "pills"},
        _new_card(paths=["~/health$"]),
        [_live_card("goms-health", priority=200, paths=["~/health$"])],
    )
    assert "options" not in out and "render" not in out
    assert "no option buttons" in out["instruction"]


# ── an uncontested priority is not a question ────────────────────────────────
# It used to be asked anyway, with the instruction itself admitting "nothing
# currently clashes with these paths, so Skip is the usual answer" — a whole
# turn spent being told to leave the default alone. 0 is now taken silently
# and merely stated. The rule only holds while it IS uncontested: a clash has
# a real decision in it, and the user raising priority themselves is a
# question they asked.

@pytest.mark.unit
def test_an_uncontested_priority_is_taken_as_zero_without_asking():
    out = _chat._priority_action({"field_id": "regex_priority"},
                                 _new_card(paths=["~/api/v1/new$"]),
                                 [_live_card("other", paths=["~/unrelated$"])])
    text = out["instruction"]
    assert "Do NOT put this question to the user" in text
    assert "skip=['regex_priority']" in text
    assert "the priority stays 0" in text, "the user is told, not asked"


@pytest.mark.unit
def test_a_clash_is_still_a_real_question():
    out = _chat._priority_action(
        {"field_id": "regex_priority"},
        _new_card(paths=["~/health$"]),
        [_live_card("goms-health", priority=200, paths=["~/health$"])],
    )
    assert "Do NOT put this question to the user" not in out["instruction"]
    assert "ask them to TYPE a priority" in out["instruction"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_gateway_that_could_not_be_read_still_closes_the_priority_out(monkeypatch):
    """`_gateway_cards_now` returning None used to leave the bare question
    standing. No read means no clash to report, and a clash that IS real is
    refused at save by KongPathOverlapValidator with the blocking group named
    — so the turn is still not worth spending."""
    async def _no_read(auth_ctx, group):
        return None

    monkeypatch.setattr(_chat, "_gateway_cards_now", _no_read)
    out = await _chat._enrich_card_options(
        {"type": "ask_user", "field_id": "regex_priority", "options": [{"text": "Skip"}]},
        {"gateway_group": _new_card()}, None)
    assert "Do NOT put this question to the user" in out["instruction"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_tag_that_could_not_be_read_is_left_as_it_was(monkeypatch):
    """The tag has no save-time backstop to degrade into, so it falls back to
    the question the model would have asked anyway."""
    async def _no_read(auth_ctx, group):
        return None

    monkeypatch.setattr(_chat, "_gateway_cards_now", _no_read)
    action = {"type": "ask_user", "field_id": "tag", "options": [{"text": "Skip"}]}
    out = await _chat._enrich_card_options(dict(action), {"gateway_group": _new_card()}, None)
    assert out == action


@pytest.mark.unit
def test_a_clash_names_the_other_group_and_its_priority():
    """Without that number the user is picking blind."""
    out = _chat._priority_action(
        {"field_id": "regex_priority"},
        _new_card(paths=["~/health$"]),
        [_live_card("goms-health", secured=False, priority=200, paths=["~/health$"])],
    )
    assert "'~/health$' is already in group 'goms-health' at priority 200" in out["instruction"]
    assert "NOT 200" in out["instruction"]


@pytest.mark.unit
def test_a_clash_is_found_across_the_other_auth():
    """Overlap is per (method, path) — auth does not separate route groups."""
    out = _chat._priority_action(
        {"field_id": "regex_priority"},
        _new_card(secured=True, paths=["~/health$"]),
        [_live_card("goms-open", secured=False, priority=5, paths=["~/health$"])],
    )
    assert "goms-open" in out["instruction"]


@pytest.mark.unit
def test_the_group_you_are_adding_to_is_not_a_clash_with_itself():
    out = _chat._priority_action(
        {"field_id": "regex_priority"},
        _new_card(route_group_key="goms", paths=["~/health$"]),
        [_live_card("goms", priority=0, paths=["~/health$"])],
    )
    assert "Do NOT put this question to the user" in out["instruction"]


@pytest.mark.unit
def test_unanchored_stored_path_still_counts_as_a_clash():
    """Terragrunt holds a few routes written without the trailing `$`."""
    out = _chat._priority_action(
        {"field_id": "regex_priority"},
        _new_card(paths=["~/health$"]),
        [_live_card("legacy", priority=3, paths=["~/health"])],
    )
    assert "'legacy'" in out["instruction"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_other_fields_are_left_alone():
    action = {"type": "ask_user", "field_id": "plugins", "options": [], "instruction": "old"}
    assert await _chat._enrich_card_options(action, {"gateway_group": _new_card()}, None) == action


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_gateway_read_costs_the_suggestions_not_the_turn(monkeypatch):
    async def _boom(auth_ctx, group):
        return None

    monkeypatch.setattr(_chat, "_gateway_cards_now", _boom)
    action = {"type": "ask_user", "field_id": "tag", "options": [], "instruction": "old"}
    assert await _chat._enrich_card_options(action, {"gateway_group": _new_card()}, None) == action


# ============================================================
# The gateway form must be OPENED, not described
# ============================================================
# Seen live: the model answered "Method — GET/POST, Auth — JWT or public,
# Path(s) — e.g. ~/api/v1/users$. Say the word and I'll open the gateway form."
# The options only exist on the tool's next_action, so describing the fields
# instead of calling it costs a round trip AND gets the form wrong — that prose
# had no "what to do" question and no real route groups in it.


@pytest.mark.unit
def test_the_tool_is_told_to_open_the_form_in_the_same_turn():
    from app.mcp_servers.devlift_mcp import server

    src = inspect.getsource(server)
    assert "CALL IT IN THE SAME TURN" in src
    assert "Do not " in src and "announce it and wait" in src


@pytest.mark.unit
def test_the_server_instructions_forbid_pre_asking_in_prose():
    from app.mcp_servers.devlift_mcp.instructions import DEVLIFT_INSTRUCTIONS

    kong = DEVLIFT_INSTRUCTIONS[DEVLIFT_INSTRUCTIONS.index("KONG GATEWAY ROUTES"):]
    kong = kong[: kong.index("VARIABLES & SECRETS")]
    assert "IN THAT SAME TURN" in kong
    assert "Do not announce it and wait" in kong
    assert "do not pre-ask method / auth /" in kong
    assert "This tool ASKS THE QUESTIONS" in kong


@pytest.mark.unit
def test_path_suggestion_survives_when_another_field_is_being_asked():
    """The multi-path case, seen live.

    The first path is rejected while `paths` is the field being asked, so there
    is no dropdown suggestion and the correction shows. Once ONE valid path is
    accepted the array is non-empty, so `currently_asking` moves on to `tag` —
    which carries a Skip pill. On the NEXT message the rejected path then
    arrives alongside a `tag` suggestion, and the options branch rebuilt the
    instruction from scratch, silently dropping the correction. The user saw
    `api/v1/:filter` accepted with no complaint.
    """
    action = _next_action({
        "isReady": False,
        "invalid_fields": _rejected("paths", ["api/v1/:filter"]),
        # `paths` already holds a valid entry, so the form is on `tag` now.
        "suggestions": [{"field_id": "tag", "label": "Tag",
                         "options": [{"text": "Skip", "value": "skip", "skip": True}]}],
        "missing_fields": {"required": [], "optional": ["tag", "regex_priority", "plugins"]},
    })
    assert action["type"] == "fix_invalid"
    assert "~/api/v1/(?<filter>[^/]+)$" in action["instruction"], (
        "the correction must survive the dropdown-options branch"
    )


@pytest.mark.unit
def test_the_correction_outranks_the_field_the_form_moved_on_to():
    """The action's field_id is `tag` here — the form's next question — but the
    thing to fix is the path. Without this the model asks for a tag while the
    rejected path sits unmentioned."""
    action = _next_action({
        "isReady": False,
        "invalid_fields": _rejected("paths", ["api/v1/:filter"]),
        "suggestions": [{"field_id": "tag", "label": "Tag",
                         "options": [{"text": "Skip", "value": "skip", "skip": True}]}],
        "missing_fields": {"required": [], "optional": ["tag"]},
    })
    assert action["field_id"] == "tag"
    assert "OVERRIDES ANY FIELD NAMED ABOVE" in action["instruction"]


@pytest.mark.unit
def test_the_user_is_told_which_paths_survived():
    """Partial acceptance is the confusing part: one path landed, one did not."""
    out = _kong_path_suggestions(_rejected("paths", ["api/v1/:filter"]))
    assert "may well have been ACCEPTED" in out
    assert "re-sending an accepted one is refused as a duplicate" in out


@pytest.mark.unit
def test_several_bad_paths_are_all_corrected_in_one_go():
    out = _kong_path_suggestions(_rejected("paths", ["api/v1/:filter", "/api/v1/health"]))
    assert "2 PATHS WERE" in out
    assert "~/api/v1/(?<filter>[^/]+)$" in out
    assert "~/api/v1/health$" in out


def _form_accepts_path(path, field_id="paths"):
    """Run a path through the real chat form's validation, as the user's input
    would be. The form lives in the chatbot repo, so it is loaded by path."""
    import json as _json, os as _os, sys as _sys

    root = _os.path.join(
        _os.path.dirname(__file__), "..", "..", "..", "chat-bot-POC", "backend"
    )
    root = _os.path.abspath(root)
    if root not in _sys.path:
        _sys.path.insert(0, root)
    saved = {k: v for k, v in _sys.modules.items() if k == "app" or k.startswith("app.")}
    for k in saved:
        del _sys.modules[k]
    try:
        from app.validation import validate_field  # the chatbot's, not obs_tool's
        form = _json.load(open(_os.path.join(
            root, "data", "metadata", "aspora", "kong_route_form.json")))
        ok, _ = validate_field(form, field_id, [path], {
            "product": "W", "environment": "Stage", "geo_location": "Mumbai",
            "service_name": "s", "route_action": "Add",
        })
        return ok
    finally:
        _sys.path.remove(root)
        for k in [k for k in _sys.modules if k == "app" or k.startswith("app.")]:
            del _sys.modules[k]
        _sys.modules.update(saved)

# ============================================================
# A wrapped plain path is refused — the failure seen live
# ============================================================
# The model answered "DevLift converted them to Kong form itself" and sent
# `~/api/v1/:filters$`. Nothing had converted anything: it wrapped the plain
# path in `~` and `$` by hand. The result passed the structural check because it
# IS a valid regex — one that matches the literal text "/api/v1/:filters" and no
# real request. The Gateway tab stays permissive on purpose (a person there is
# writing regex deliberately); chat has an LLM in the middle, so it does not.


@pytest.mark.unit
@pytest.mark.parametrize("path", [
    "~/api/v1/users$",
    "~/api/v1/users/(?<id>[^/]+)$",       # a parameter, done properly
    "~/minis/(?<proxy>.*)$",
    "~/api/v1/.*$",                        # legitimate catch-all
    "~/files/(?<id>[^/]{1,10})$",          # {1,10} is a quantifier, not a placeholder
    "~/a/(?:x|y)$",                        # non-capturing group legitimately uses ':'
    "~/report\\.json$",
])
def test_real_regex_paths_still_pass(path):
    assert _form_accepts_path(path), f"{path} must stay valid"


@pytest.mark.unit
@pytest.mark.parametrize("path", [
    "~/api/v1/:filters$",                  # the exact value that got through
    "~/files/:id$",
    "~/api/v1/{id}$",
    "~/api/v1/*$",
])
def test_a_wrapped_plain_path_is_refused(path):
    assert not _form_accepts_path(path), f"{path} must be refused"


@pytest.mark.unit
def test_the_correction_unwraps_before_compiling():
    """`compile_route_path` treats anything starting with '~' as done, so the
    bolted-on wrapper has to come off or there is no correction to offer."""
    out = _kong_path_suggestions(_rejected("paths", ["~/api/v1/:filters$"]))
    assert "'~/api/v1/:filters$' -> '~/api/v1/(?<filters>[^/]+)$'" in out


@pytest.mark.unit
def test_a_genuine_regex_is_never_unwrapped_and_recompiled():
    """That round trip is lossy — `(?<proxy>.*)` comes back as `(?<rest>.+)`."""
    from app.mcp_servers.devlift_mcp.tools.chat import _plain_source

    assert _plain_source("~/minis/(?<proxy>.*)$") == "~/minis/(?<proxy>.*)$"
    assert _plain_source("~/api/v1/:id$") == "/api/v1/:id"


@pytest.mark.unit
def test_the_gateway_session_asks_for_the_optional_card_fields():
    """`/select-service` with a prefill normally marks every unfilled OPTIONAL
    field as skipped, so an EDIT session opens silently — right for 38 settings
    fields, wrong here. The kong form prefills only the placement; tag, priority
    and plugins are the card, not settings being carried over. Without
    `ask_optional` they were closed out unseen and the form reported ready the
    moment the path landed, so the save prompt replaced all three questions."""
    import inspect

    from app.mcp_servers.devlift_mcp import dispatcher

    src = inspect.getsource(dispatcher._start_gateway_edit)
    assert "ask_optional=True" in src


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ask_optional_is_only_sent_when_asked_for():
    """The settings edit must keep its silent open — it is the other caller of
    this client and 19 optional fields would otherwise be put to the user."""
    from app.mcp_servers.devlift_mcp import chatbot_client

    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json): sent.update(json); return _Resp()

    import httpx
    real = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: _Client()
    try:
        await chatbot_client.post_select_form(
            form_id="eks_service_form", ticket_code="t", tenant_code="a",
            user_mst_code="u", jwt_token="j", prefill={"service_name": "x"},
        )
        assert "ask_optional" not in sent, "the settings edit must stay silent"
        sent.clear()
        await chatbot_client.post_select_form(
            form_id="kong_route_form", ticket_code="t", tenant_code="a",
            user_mst_code="u", jwt_token="j", prefill={"service_name": "x"},
            ask_optional=True,
        )
        assert sent["ask_optional"] is True
    finally:
        httpx.AsyncClient = real


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_rejection_is_never_replaced_by_a_card_question(monkeypatch):
    """Seen live. A duplicate path was rejected while the form sat on
    `regex_priority`, so the fix_invalid action carried field_id
    'regex_priority' — matched the enricher, and its instruction was swapped for
    "type a priority". The user was never told their path had been refused."""
    async def _cards(auth_ctx, group):
        return [_live_card("jp-test-service")]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    action = {
        "type": "fix_invalid",
        "field_id": "regex_priority",
        "label": "Regex Priority",
        "invalid_fields": [{"field_id": "paths", "value": "~/a$", "error": "already in your list"}],
        "instruction": "Surface the chatbot's `message` verbatim ...",
    }
    out = await _chat._enrich_card_options(action, {"gateway_group": _new_card()}, None)
    assert out == action, "a rejection must pass through untouched"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_plain_card_question_is_still_enriched(monkeypatch):
    async def _cards(auth_ctx, group):
        return [_live_card("jp-test-service")]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    action = {"type": "ask_user", "field_id": "regex_priority", "instruction": "old"}
    out = await _chat._enrich_card_options(action, {"gateway_group": _new_card()}, None)
    assert out["instruction"] != "old"


# ============================================================
# "Edit the gateway path" must reach Rename, not ask for another add
# ============================================================
# Seen live: the card was mid-flight as an ADD when the user said "I need to
# edit gateway path". The change prompt listed tag / priority / plugins / method
# / auth / paths — everything EXCEPT what the card DOES — so the model asked for
# a different path to add. It also told the user plain paths were fine and that
# "DevLift rewrote the last ones for you", neither of which is true.


def _chatbot_resolve(collected):
    """Run the chatbot's own engine over a collected_data dict."""
    import os as _os, sys as _sys

    root = _os.path.abspath(_os.path.join(
        _os.path.dirname(__file__), "..", "..", "..", "chat-bot-POC", "backend"))
    if root not in _sys.path:
        _sys.path.insert(0, root)
    saved = {k: v for k, v in _sys.modules.items() if k == "app" or k.startswith("app.")}
    for k in saved:
        del _sys.modules[k]
    try:
        import json as _json
        from app.engine import resolve_and_validate
        from app.validation import get_currently_asking
        form = _json.load(open(_os.path.join(
            root, "data", "metadata", "aspora", "kong_route_form.json")))
        resolved, _, _, removed = resolve_and_validate(form, collected)
        asking, _fd = get_currently_asking(form, resolved, [])
        return resolved, removed, asking
    finally:
        _sys.path.remove(root)
        for k in [k for k in _sys.modules if k == "app" or k.startswith("app.")]:
            del _sys.modules[k]
        _sys.modules.update(saved)


# ── plugins is one toggle, so it is not a dialog ─────────────────────────────
# 'User ID Injection' is the only selectable entry — JWT follows the Auth
# answer on its own — so the question was a whole dialog to be told "none".
# Unlike the priority it has NO save-time backstop, so it is not simply
# dropped: it comes back as a row on the save confirmation, where the user is
# already answering something.

def _asking_plugins(**over):
    reply = {
        "status": "pending", "isReady": False, "collected_data": {},
        "invalid_fields": [],
        "missing_fields": {"required": [], "optional": ["plugins"]},
        "gateway_group": _new_card(),
        "suggestions": [{"field_id": "plugins", "label": "Plugins", "options": [
            {"text": "User ID Injection", "value": "change plugins to User ID Injection"}]}],
    }
    reply.update(over)
    return _chat._build_next_action(reply)


@pytest.mark.unit
def test_plugins_closes_itself_out_instead_of_asking():
    action = _asking_plugins()
    assert action["type"] == "skip_field"
    assert "skip=['plugins']" in action["instruction"]
    assert "do not mention it yet" in action["instruction"]


@pytest.mark.unit
def test_plugins_outside_a_gateway_card_is_untouched():
    """The branch is Kong's. Nothing else may lose a question to it."""
    action = _asking_plugins(gateway_group=None)
    assert action["type"] != "skip_field"


@pytest.mark.unit
def test_one_skip_closes_both_silent_fields():
    """The chatbot asks the priority first and plugins is still open then, so
    the priority's own skip carries it — two non-events, one round trip."""
    reply = {"missing_fields": {"required": [], "optional": ["regex_priority", "plugins"]}}
    out = _chat._priority_action(
        {"field_id": "regex_priority"}, _new_card(paths=["~/api/v1/new$"]),
        [_live_card("other", paths=["~/unrelated$"])],
        tuple(_chat._silent_optionals(reply)),
    )
    assert "skip=['regex_priority', 'plugins']" in out["instruction"]


@pytest.mark.unit
def test_a_clash_closes_nothing_out():
    """A real question about the priority must not quietly take the plugins
    decision with it."""
    reply = {"missing_fields": {"required": [], "optional": ["regex_priority", "plugins"]}}
    out = _chat._priority_action(
        {"field_id": "regex_priority"}, _new_card(paths=["~/health$"]),
        [_live_card("goms-health", priority=200, paths=["~/health$"])],
        tuple(_chat._silent_optionals(reply)),
    )
    assert "skip=" not in out["instruction"]


@pytest.mark.unit
def test_a_field_already_settled_is_not_skipped_again():
    """`_silent_optionals` reads what the form still WANTS — a priority the
    user set themselves is answered, so only plugins is left to close."""
    reply = {"missing_fields": {"required": [], "optional": ["plugins"]}}
    assert _chat._silent_optionals(reply) == ["plugins"]


# ── a card that changes only the plugins ─────────────────────────────────────
# "add user id injection to the orders group" touches no path at all. The form
# had no intent for it (Add / Remove / Rename), and `gateway_group_block`
# refused the resulting card with a message about paths — even though
# `build_gateway_group_save` has always emitted a save when the plugins differ.
# The dialog is its own shape too: a route group IS (tag, method) and carries
# its auth, so ONE pick settles all three and nothing else is asked.

def _plugin_reply(collected=None, **over):
    reply = {
        "status": "pending", "isReady": False, "invalid_fields": [], "suggestions": [],
        "missing_fields": {"required": ["method"], "optional": []},
        "gateway_group": _new_card(),
        "collected_data": {"route_action": "Plugins", **(collected or {})},
    }
    reply.update(over)
    return reply


@pytest.mark.unit
def test_the_mcp_plugin_list_matches_the_form():
    """The catalogue lives in the form; the MCP holds a copy because it asks a
    turn before the chatbot would send it. Drift makes the dialog offer a
    plugin the form then refuses."""
    import json, os

    form = json.load(open(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "chat-bot-POC", "backend",
        "data", "metadata", "aspora", "kong_route_form.json")))
    field = next(f for f in form["fields"] if f["field_id"] == "plugins")
    assert [o["value"] for o in field["dropdown_options"]] == list(_chat._SELECTABLE_PLUGINS)


@pytest.mark.unit
def test_one_pick_settles_the_group_its_method_and_its_auth():
    out = _chat._plugin_card_action([
        _live_card("goms", method="GET", secured=True),
        _live_card("goms-open", method="GET", secured=False),
    ])
    assert [g["text"] for g in out["groups"]] == ["goms · GET", "goms-open · GET"]
    assert out["groups"][0]["method"] == "GET"
    assert out["groups"][0]["secured"] == "Auth - JWT required"
    assert out["groups"][1]["secured"] == "No Auth - public"
    text = out["instruction"]
    assert "Do NOT ask for paths, a method, an auth mode or a priority" in text
    assert "'secured': <secured>" in text
    assert "Tag (route group)" in text, "one name for the field, everywhere"


@pytest.mark.unit
def test_a_group_shows_the_plugins_it_already_has():
    """Otherwise the user cannot see the change is unnecessary."""
    plain, held = _chat._plugin_card_action([
        _live_card("goms", secured=True),
        dict(_live_card("goms-admin", secured=True), plugins=["JWT", "User ID Injection"]),
    ])["groups"]
    assert plain["description"] == "Auth · no extra plugins"
    assert held["description"] == "Auth · User ID Injection", "JWT is auth, not an extra"


@pytest.mark.unit
def test_the_catalogue_of_one_says_so():
    """One plugin row plus a way out reads like a picker with something
    missing, and the user goes hunting for the rest. There is no rest."""
    # the two places a plugin is chosen or deliberately not chosen
    for text in (
        _chat._plugin_card_action([_live_card("goms")])["instruction"],
        _asking_plugins()["instruction"],
    ):
        assert "ONLY plugin" in text
        assert "JWT" in text, "and why JWT is not among the rows"
    # and the summary, which says it without offering anything
    assert "the only one there is" in _gateway_ready_instruction()


@pytest.mark.unit
def test_the_claim_is_read_off_the_catalogue_not_typed_out():
    """It has to stop being said by itself the day a second plugin lands."""
    assert len(_chat._SELECTABLE_PLUGINS) == 1
    text = _chat._plugin_card_action([_live_card("goms")])["instruction"]
    assert f"{_chat._SELECTABLE_PLUGINS[0]} is the ONLY plugin" in text


@pytest.mark.unit
def test_the_plugin_question_offers_a_way_out():
    out = _chat._plugin_card_action([_live_card("goms")])
    assert "Leave unchanged" in out["instruction"]
    assert "say the card was left alone and stop" in out["instruction"]


@pytest.mark.unit
def test_the_user_is_told_a_plugin_hits_the_whole_group():
    """It applies to routes already live in the group — the part someone
    adding one to an existing group does not expect."""
    out = _chat._plugin_card_action([_live_card("goms")])
    assert "EVERY route in the group, including the ones already live" in out["instruction"]


# ── a busy service's groups are not a picker ─────────────────────────────────
# `<tag> · <METHOD>` six methods deep, times every tag, is a wall of rows that
# all look alike — and past four AskUserQuestion silently drops the rest. The
# tag the user already named narrows it to the one group they meant; what is
# still too long is printed as a numbered list instead.

def _busy(tags=("goms", "goms-admin"), methods=("GET", "POST", "PUT", "DELETE")):
    return [_live_card(t, method=m) for t in tags for m in methods]


@pytest.mark.unit
def test_a_named_tag_narrows_the_list_to_that_group():
    out = _chat._plugin_card_action(_busy(), "goms-admin")
    assert [g["text"] for g in out["groups"]] == [
        "goms-admin · GET", "goms-admin · POST", "goms-admin · PUT", "goms-admin · DELETE"]
    assert out["render"] == "pills", "four methods still fit the picker"


@pytest.mark.unit
def test_a_tag_that_matches_nothing_does_not_empty_the_list():
    """A stale or misspelt tag must leave the user with the full set, not a
    dialog with no rows in it."""
    out = _chat._plugin_card_action(_busy(), "typo-service")
    assert len(out["groups"]) == 8


@pytest.mark.unit
def test_more_groups_than_the_picker_holds_become_a_numbered_list():
    out = _chat._plugin_card_action(_busy())
    assert out["render"] == "text_list"
    text = out["instruction"]
    assert "There are 8 route groups" in text
    assert "it would silently drop the rest" in text
    assert "NUMBERED list" in text
    assert "Tag (route group)" in text


@pytest.mark.unit
def test_the_long_list_still_takes_the_plugin_in_one_go():
    """Answering '3, user id injection' must not cost a second question."""
    text = _chat._plugin_card_action(_busy())["instruction"]
    assert "naming the plugin in the same line if they already know it" in text
    assert "If they named the plugin too, send it all at once" in text


@pytest.mark.unit
def test_a_service_with_no_groups_falls_back():
    """Nothing to add a plugin TO."""
    assert _chat._plugin_card_action([]) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_plugin_card_replaces_whatever_the_form_was_asking(monkeypatch):
    async def _cards(auth_ctx, group):
        return [_live_card("goms")]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    for action in ({"type": "ask_section", "title": "Route", "fields": []},
                   {"type": "ask_user_text", "field_id": "method"}):
        out = await _chat._enrich_card_options(action, _plugin_reply(), None)
        assert out["type"] == "ask_plugin_card"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_rejection_still_reaches_the_user_on_a_plugin_card(monkeypatch):
    async def _cards(auth_ctx, group):
        return [_live_card("goms")]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    action = {"type": "fix_invalid", "field_id": "tag", "instruction": "surface it"}
    out = await _chat._enrich_card_options(dict(action), _plugin_reply(), None)
    assert out == action


# Live run: the user added a path to `jp-test-service · GET`, then said "I
# want to delete a kong gateway path". The card kept GET, kept the tag and
# kept its public auth, so the group looked settled — and they were asked
# which path to remove from a group they had never chosen, having never once
# been asked a method. Worse, the card was public and the group is JWT, so the
# model offered to flip the auth to make them fit: a decision the user never
# saw, on a card the save would otherwise have refused.
#
# "Settled" is now "points at a group that EXISTS", matched against the live
# gateway. A leftover triple matches nothing, so it asks.

@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_group_carried_over_from_the_previous_card_is_re_asked(monkeypatch):
    async def _cards(auth_ctx, group):
        return [_live_card("jp-test-service", method="GET", secured=True)]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    for intent, expected in (("Plugins", "ask_plugin_card"),
                             ("Remove", "ask_route_group"),
                             ("Rename", "ask_route_group")):
        reply = {
            "status": "pending", "isReady": False, "invalid_fields": [], "suggestions": [],
            "missing_fields": {"required": [], "optional": []},
            # the Add card's answers, still sitting there: public, and the
            # live group is JWT
            "gateway_group": _new_card(route_group_key="jp-test-service",
                                       http_method="GET", secured=False),
            "collected_data": {"route_action": intent, "tag": "jp-test-service",
                               "method": "GET"},
        }
        out = await _chat._enrich_card_options(
            {"type": "ask_user_text", "field_id": "remove_paths"}, reply, None)
        assert out["type"] == expected, intent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_group_the_card_really_points_at_is_not_re_asked(monkeypatch):
    """Tag, method AND auth match a live group, so it is a group the card can
    act on — carrying on to its paths is right."""
    async def _cards(auth_ctx, group):
        return [_live_card("goms", method="GET", secured=True, paths=["~/a$"])]

    monkeypatch.setattr(_chat, "_gateway_cards_now", _cards)
    out = await _chat._enrich_card_options(
        {"type": "ask_user_text", "field_id": "remove_paths"},
        {"gateway_group": _new_card(route_group_key="goms", http_method="GET", secured=True),
         "collected_data": {"route_action": "Remove", "tag": "goms", "method": "GET"}},
        None)
    assert out["type"] == "ask_user_text"
    assert out["paths"] == ["~/a$"]


@pytest.mark.unit
@pytest.mark.parametrize("over,live", [
    ({"secured": False}, True),                 # public card, JWT group
    ({"http_method": "POST"}, True),            # right tag, wrong method
    ({"route_group_key": "goms-open"}, True),   # tag that is not there
    ({}, True),                                 # everything matches
])
def test_the_group_is_live_only_when_all_three_match(over, live):
    card = _new_card(route_group_key="goms", http_method="GET", secured=True)
    card.update(over)
    cards = [_live_card("goms", method="GET", secured=True)]
    assert _chat._card_group_is_live(card, cards) is (over == {})


@pytest.mark.unit
def test_a_card_with_no_tag_yet_is_never_live():
    assert _chat._card_group_is_live(_new_card(route_group_key=None), [_live_card("goms")]) is False


@pytest.mark.unit
def test_plugins_are_never_closed_out_on_a_plugin_card():
    """The silent skip that saves a dialog on an ordinary card would, here,
    save a queue row that changes nothing and report it as done."""
    reply = _plugin_reply(missing_fields={"required": [], "optional": ["regex_priority", "plugins"]})
    assert _chat._silent_optionals(reply) == ["regex_priority"]

    reply["suggestions"] = [{"field_id": "plugins", "label": "Plugins", "options": [
        {"text": "User ID Injection", "value": "change plugins to User ID Injection"}]}]
    reply["collected_data"]["tag"] = "goms"
    reply["collected_data"]["method"] = "GET"
    action = _chat._build_next_action(reply)
    assert action is None or action["type"] != "skip_field"


@pytest.mark.unit
def test_a_named_change_reopens_only_what_was_named():
    text = _gateway_ready_instruction()
    assert "names one thing changes one thing" in text
    assert "the tag, priority, method, auth and paths stay as they are" in text
    assert "ONE message carrying every change at once" in text


def _gateway_ready_instruction():
    return _next_action({"isReady": True, "gateway_group": {"http_method": "GET"}})["instruction"]


# ── a gateway card saves itself, like a configuration ────────────────────────
# It used to end with "Save as draft / Change something". A draft writes
# nothing live, deploys nothing and stays editable until it is submitted,
# approved and deployed — so the question guarded nothing and cost a turn. The
# configuration half of the SAME draft has always saved without asking.

@pytest.mark.unit
def test_a_ready_card_is_saved_without_asking():
    text = _gateway_ready_instruction()
    assert "SAVE IT NOW" in text
    assert "Do NOT ask permission first" in text
    assert "a confirmation here guards nothing and costs a turn" in text
    assert "Save as draft" not in text
    assert "Change something" not in text


@pytest.mark.unit
def test_saving_a_card_never_deploys_it():
    text = _gateway_ready_instruction()
    assert "deploys nothing" in text
    assert "Do NOT call trigger_resource_deployment" in text


@pytest.mark.unit
def test_the_summary_comes_after_the_save_and_names_the_group_wide_three():
    """They are group-wide rather than about this path, and the user has been
    shown them nowhere else — the save being automatic does not make them
    less worth saying."""
    text = _gateway_ready_instruction()
    assert "THEN show what was saved" in text
    for fact in ("TAG —", "PLUGINS,", "REGEX PRIORITY —"):
        assert fact in text
    assert "This card adds none" in text
    assert "do not offer to add it" in text


@pytest.mark.unit
def test_switching_an_add_to_a_rename_is_still_spelled_out():
    """The easiest edit to get wrong: on an ADD card, 'rename /old to /new' is
    a change of what the card DOES, not another path to add."""
    text = _gateway_ready_instruction()
    assert "means switching it to Rename" in text
    assert "not supplying a different path to add" in text


@pytest.mark.unit
def test_the_change_prompt_forbids_the_plain_path_claim():
    """The model twice told the user devlift had converted their paths."""
    text = _gateway_ready_instruction()
    assert "never say a plain path is fine or that devlift rewrote one" in text
    assert "a plain path is REFUSED" in text.replace("\n", " ")


@pytest.mark.unit
def test_switching_an_add_card_to_rename_drops_the_pending_add():
    """The capability the instruction promises: the engine must actually move."""
    resolved, removed, asking = _chatbot_resolve({
        "product": "W", "environment": "Stage", "geo_location": "Mumbai",
        "service_name": "jp-test-service", "route_action": "Rename",
        "method": "GET", "secured": "Auth - JWT required",
        "paths": ["~/api/v1/users$"],          # what the Add card was holding
    })
    assert "paths" not in resolved and "paths" in removed
    assert asking == "edit_paths"


# ============================================================
# A clear intent skips the "what to do" question
# ============================================================
# "add a path" should not be answered with a dialog asking whether this is an
# add. The caller can tell from the user's own words, so it answers the field up
# front and the dialog drops to method + auth. Ambiguous wording ("update a
# path") is left alone, because guessing sends the user to the wrong path field.


# Live run: "I want to edit a service kong path" was answered with the "what
# do you want to do?" dialog. The words had already said it — and the SAME
# codebase says so, in the mid-flight rule: "'edit the gateway path' means
# switching it to Rename". Only the entry point disagreed, telling the model
# to treat every "edit" as ambiguous. A verb aimed at a PATH is a Rename; a
# verb aimed at the GATEWAY is the ambiguous one.

def _model_facing_docs():
    """Everything the model reads about `route_action`: the tool parameter and
    the server instructions."""
    import inspect

    from app.mcp_servers.devlift_mcp import instructions, server

    return inspect.getsource(server) + instructions.DEVLIFT_INSTRUCTIONS


@pytest.mark.unit
@pytest.mark.parametrize("phrase", ["edit a path", "change a path", "update a path"])
def test_a_verb_aimed_at_a_path_is_documented_as_a_rename(phrase):
    docs = _model_facing_docs()
    assert phrase in docs, f"{phrase!r} has to be named, or the model asks again"
    assert "aimed at a PATH is a Rename" in docs


@pytest.mark.unit
def test_a_verb_aimed_at_the_gateway_is_still_left_to_the_form():
    """'edit the gateway' really could be any of the four."""
    docs = _model_facing_docs()
    assert "edit the gateway" in docs
    assert "LEAVE IT OUT only when NO path is named" in docs


@pytest.mark.unit
def test_the_entry_point_and_the_mid_flight_rule_agree():
    """The mid-flight rule always said an edited PATH is a Rename; only the
    entry point disagreed, which is how one sentence got two answers."""
    from app.mcp_servers.devlift_mcp.tools import chat as _c

    ready = _next_action({"isReady": True, "gateway_group": {"http_method": "GET"}})
    assert "means switching it to Rename" in ready["instruction"]
    assert "aimed at a PATH is a Rename" in _model_facing_docs()


@pytest.mark.unit
def test_every_action_the_form_offers_is_one_the_tool_accepts():
    """`_clean_route_action` drops anything outside `_ROUTE_ACTIONS` — silently,
    which is the worst way to lose a hint. 'Plugins' was added to the form and
    not to the tuple, so `route_action='Plugins'` vanished and the user was
    asked the question their own words had just answered."""
    import json, os

    from app.mcp_servers.devlift_mcp.dispatcher import _ROUTE_ACTIONS

    form = json.load(open(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "chat-bot-POC", "backend",
        "data", "metadata", "aspora", "kong_route_form.json")))
    field = next(f for f in form["fields"] if f["field_id"] == "route_action")
    assert [o["value"] for o in field["dropdown_options"]] == list(_ROUTE_ACTIONS)


@pytest.mark.unit
@pytest.mark.parametrize("given,expected", [
    ("Add", "Add"), ("add", "Add"), ("  REMOVE ", "Remove"), ("rename", "Rename"),
    ("plugins", "Plugins"), ("Plugins", "Plugins"),
])
def test_a_recognised_intent_is_normalised(given, expected):
    from app.mcp_servers.devlift_mcp.dispatcher import _clean_route_action

    assert _clean_route_action(given) == expected


@pytest.mark.unit
@pytest.mark.parametrize("given", [None, "", "  ", "update", "edit", "delete", "change it"])
def test_anything_unrecognised_falls_back_to_asking(given):
    """Including 'delete' and 'edit': close to the real words, but not them.
    Mapping a near-miss is how a user ends up on the wrong path question."""
    from app.mcp_servers.devlift_mcp.dispatcher import _clean_route_action

    assert _clean_route_action(given) is None


@pytest.mark.unit
def test_a_known_intent_is_prefilled_so_the_form_stops_asking():
    pre = _sp.build_gateway_prefill(
        service_name="goms", product_name="core", environment="stage",
        geo_name="Mumbai", route_action="Add",
    )
    assert pre["route_action"] == "Add"


@pytest.mark.unit
def test_no_intent_leaves_the_field_unanswered():
    pre = _sp.build_gateway_prefill(
        service_name="goms", product_name="core", environment="stage", geo_name="Mumbai",
    )
    assert "route_action" not in pre, "an absent hint must not answer the field"


@pytest.mark.unit
def test_the_prefilled_intent_drops_it_from_the_opening_dialog():
    """The point of the whole thing: one fewer question when it is obvious.

    With the intent known, the card's remaining fields come as ONE dialog,
    shaped to that intent: an Add asks method, auth, paths and tag, while a
    Remove asks neither method nor auth — those belong to the route group it
    points at, which is picked before its paths are.

    Without the intent there is no dialog at all: `route_action` is asked on
    its own. It used to open one that ran Action | Method | Auth, and picking
    'Plugins' there left the user staring at two questions a plugin card never
    asks. Every field that depends on the action now says so, and a dependency
    being answered in the same dialog ends the section.
    """
    import asyncio

    for pre_action, expected in (
        (None, None),
        ("Add", ["method", "secured", "paths", "tag"]),
        ("Remove", ["remove_paths", "tag"]),
        # Rename edits old -> new PAIRS, which no picker can hold, so
        # edit_paths is asked on its own and no section is emitted.
        ("Rename", None),
        ("Plugins", None),
    ):
        data = {"product": "core", "environment": "Stage", "geo_location": "Mumbai",
                "service_name": "goms - core"}
        if pre_action:
            data["route_action"] = pre_action
        section = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _kong_section(data))
        got = [f["field_id"] for f in section["fields"]] if section else None
        assert got == expected


@pytest.mark.unit
def test_the_path_examples_reach_the_dialog_as_rows_not_as_prose():
    """Live run: the Paths question printed '~/api/v1/users$' in its text AND
    again as a choice, with 'Example shape — pick Other...' under each. The
    shapes now travel as `examples` (the picker's rows) and the description
    only says what the field is for, so each is on screen once. The pair is
    also deliberate: a plain path first, the same path parameterised second —
    a capture group is the part people get wrong."""
    import asyncio

    section = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _kong_section({"product": "core", "environment": "Stage",
                       "geo_location": "Mumbai", "service_name": "goms - core",
                       "route_action": "Add"}))
    paths = next(f for f in section["fields"] if f["field_id"] == "paths")

    assert paths["options"] == [], "a typed field offers no allowed set"
    assert paths["examples"] == ["~/api/v1/users$", "~/api/v1/users/(?<id>[^/]+)$"]
    assert "(?<" in paths["examples"][1] and "(?<" not in paths["examples"][0]
    for text in (paths["description"], paths["hint"]):
        assert "e.g." not in text
        assert "~/api/v1/users" not in text, "the example is in the rows now"


# ── the tag rides in the card's own dialog ───────────────────────────────────
# It used to be a dialog of its own, asked straight after the one that took the
# method, the auth and the paths — a second round trip for the last field of
# the same card. It now rides along, and the price is stated in
# `_seed_section_tag_options`: the rows cannot be the LIVE groups, because they
# are filtered by the method and the auth this very dialog is collecting.

@pytest.mark.unit
def test_the_tag_is_open_in_the_form_so_the_client_fills_its_rows():
    """The form has no idea what a service's route groups are called, so it
    says only that the field is open — without `dialog_open` the section
    stopped one field short and the tag went back to being its own dialog."""
    import asyncio, json, os

    form = json.load(open(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "chat-bot-POC", "backend",
        "data", "metadata", "aspora", "kong_route_form.json")))
    tag = next(f for f in form["fields"] if f["field_id"] == "tag")
    assert tag.get("dialog_open") is True

    section = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _kong_section({"product": "core", "environment": "Stage",
                       "geo_location": "Mumbai", "service_name": "goms - core",
                       "route_action": "Add"}))
    entry = next(f for f in section["fields"] if f["field_id"] == "tag")
    assert entry["options"] == []


def _seeded_tag_rows(service="jp-test-service", **overrides):
    from app.mcp_servers.devlift_mcp.tools import chat as _chat

    response = {
        "gateway_group": {"service_name": service},
        "section": {"title": "Route", "fields": [
            {"field_id": "method", "label": "Method", "type": "dropdown",
             "options": [{"text": "GET", "value": "GET"}]},
            {"field_id": "tag", "label": "Tag", "type": "text", "options": []},
        ]},
    }
    response.update(overrides)
    _chat._seed_section_tag_options(response)
    return next(f for f in response["section"]["fields"]
                if f["field_id"] == "tag")["options"]


@pytest.mark.unit
def test_the_tag_rows_are_the_default_a_fresh_group_and_skip():
    """Three rows and Other. The default FIRST — it is what Skip takes and
    what most cards want — then one fresh name for a separate group."""
    rows = _seeded_tag_rows()
    assert [r["text"] for r in rows] == [
        "jp-test-service", "jp-test-service-tag1", "Skip"]
    assert rows[0]["description"] == "the default"
    assert "takes the default" in rows[2]["description"]
    assert "jp-test-service-open" in rows[2]["description"], (
        "a public card defaults to '-open', and Skip is where that is said")


@pytest.mark.unit
def test_skipping_the_tag_reads_as_a_skip_and_not_as_a_name():
    """The dialog answers every field at once, so Skip travels inside
    `answers` — routes.py turns '__skip__' back into a skip. Any other
    sentinel would be stored as the group's NAME."""
    rows = _seeded_tag_rows()
    assert rows[2]["value"] == "__skip__"


@pytest.mark.unit
def test_a_service_with_no_name_leaves_the_rows_alone():
    """Degrade to the question the model would have asked anyway; never invent
    a group name out of nothing."""
    assert _seeded_tag_rows(service="   ") == []


@pytest.mark.unit
def test_rows_already_filled_are_not_overwritten():
    """`_tag_action` fills the LIVE groups when the tag is asked on its own.
    Seeding must never step on them."""
    from app.mcp_servers.devlift_mcp.tools import chat as _chat

    live = [{"text": "goms", "value": "goms", "description": "existing group — ~/a$"}]
    response = {
        "gateway_group": {"service_name": "goms"},
        "section": {"title": "Route", "fields": [
            {"field_id": "tag", "label": "Tag", "type": "text", "options": live},
        ]},
    }
    _chat._seed_section_tag_options(response)
    assert response["section"]["fields"][0]["options"] == live


def _kong_section(collected):
    """The opening dialog the chatbot would emit for this collected_data."""
    import os as _os, sys as _sys

    root = _os.path.abspath(_os.path.join(
        _os.path.dirname(__file__), "..", "..", "..", "chat-bot-POC", "backend"))
    if root not in _sys.path:
        _sys.path.insert(0, root)
    saved = {k: v for k, v in _sys.modules.items() if k == "app" or k.startswith("app.")}
    for k in saved:
        del _sys.modules[k]

    async def run():
        try:
            import json as _json
            from app.sections import get_next_section
            form = _json.load(open(_os.path.join(
                root, "data", "metadata", "aspora", "kong_route_form.json")))
            section, _ = await get_next_section(form, collected, [], {}, "", {})
            return section
        finally:
            _sys.path.remove(root)
            for k in [k for k in _sys.modules if k == "app" or k.startswith("app.")]:
                del _sys.modules[k]
            _sys.modules.update(saved)

    return run()
