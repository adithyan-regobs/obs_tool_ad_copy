"""The `chat` tool routes an isReady result to the right follow-up tool and
caches the right shape for it."""

from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_servers.devlift_mcp.auth import AuthContext
from app.mcp_servers.devlift_mcp.tools import chat as chat_tool


SERVICE_RESULT = {
    "status": "deployed",
    "message": "Here's your EKS Service Deployment — please review",
    "isReady": True,
    "form_id": "eks_service_form",
    "create_service": {"service_name": "demo", "application_code": "app", "resource_group_code": "rg", "service_type": "API"},
    "service_config": {"environment": "stage", "geo_loc_mst_code": "geo", "config": {"port": "8080"}},
    "collected_data": {"service_name": "demo"},
    "missing_fields": {"required": [], "optional": []},
    "suggestions": [],
    "invalid_fields": [],
}

RESOURCE_RESULT = {
    "status": "deployed",
    "message": "review",
    "isReady": True,
    "form_id": "s3_bucket_creation_form",
    "attribute_parameters": {"identifier": "b"},
    "placement_parameters": {"case_ref_code": "create_bucket"},
    "collected_data": {"bucket_name": "b"},
}


def test_next_action_for_service_result_points_at_create_service_tool():
    action = chat_tool._build_next_action(dict(SERVICE_RESULT))
    assert action["type"] == "create_service_and_save_draft"
    assert "trigger_resource_deployment" in action["instruction"]  # told NOT to


def test_next_action_for_resource_result_asks_for_confirmation():
    action = chat_tool._build_next_action(dict(RESOURCE_RESULT))
    assert action["type"] == "confirm_deployment"
    text = action["instruction"].lower()
    assert "do not call trigger_resource_deployment yet" in text
    assert "collected_data" in text
    assert "deploy" in text and "change" in text


def test_slim_response_drops_service_bags_but_keeps_collected_data_when_ready():
    response = dict(SERVICE_RESULT)
    response["next_action"] = {"type": "create_service_and_save_draft"}
    slim = chat_tool._slim_response_for_llm(response)
    assert "create_service" not in slim and "service_config" not in slim
    assert "attribute_parameters" not in slim
    assert "suggestions" not in slim
    assert slim["collected_data"] == {"service_name": "demo"}


def test_slim_keeps_missing_fields_as_the_map_of_what_is_still_open():
    """`missing_fields` used to be stripped alongside `suggestions` as a
    duplicate of next_action. It only duplicates the ONE field being asked; for
    every other field it is the sole remaining map. Without it a caller holding
    a template — building a service from another's configuration — could not
    see what it was allowed to fill, so it answered one question at a time and
    asked the user for values it had already read from the template."""
    reply = {
        "status": "pending",
        "message": "Please provide the Container Port",
        "isReady": False,
        "collected_data": {"product": "core"},
        "missing_fields": {"required": ["port", "health"], "optional": ["build_args"]},
        "suggestions": [],
        "invalid_fields": [],
    }
    reply["next_action"] = {"type": "ask_user_text", "field_id": "port"}
    slim = chat_tool._slim_response_for_llm(dict(reply))
    assert slim["missing_fields"] == {
        "required": ["port", "health"], "optional": ["build_args"],
    }
    assert "suggestions" not in slim


def test_asking_directives_allow_sending_values_you_already_hold():
    """Asking and supplying are different acts. The directive limits what you
    ASK the user to the field named; it must not stop a caller sending values
    it genuinely has."""
    text = chat_tool._build_next_action(dict(ENV_QUESTION))["instruction"]
    assert "ALREADY HOLD values for other fields" in text
    assert "supplying what you know, not preempting" in text
    # and it must still say which fields cannot travel together
    assert "branches on repository" in text


@pytest.mark.asyncio
async def test_chat_impl_caches_service_result_with_kind_marker():
    auth = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x", clerk_user_id="")
    cache = AsyncMock(return_value=True)
    with patch.object(chat_tool, "get_auth_context", AsyncMock(return_value=auth)), \
         patch.object(chat_tool, "mint_internal_jwt", return_value="jwt"), \
         patch.object(chat_tool, "post_chat", AsyncMock(return_value=dict(SERVICE_RESULT))), \
         patch.object(chat_tool, "cache_chatbot_result", cache):
        result = await chat_tool.chat_impl(message="skip", ticket_code="mcp-u1-abcd")

    cache.assert_awaited_once()
    user_code, ticket_code, payload = cache.await_args.args
    assert (user_code, ticket_code) == ("u1", "mcp-u1-abcd")
    assert payload["kind"] == "service"
    assert payload["form_id"] == "eks_service_form"
    assert payload["create_service"]["service_name"] == "demo"
    assert payload["service_config"]["config"] == {"port": "8080"}
    assert result["next_action"]["type"] == "create_service_and_save_draft"
    assert "service_config" not in result


NOT_READY_QUERY_REPLY = {
    # What the chatbot answers to "show the preview" on a ticket whose form is
    # already complete: a summary, nothing to ask, nothing changed.
    "status": "deployed",
    "message": "Here's a summary of what you've entered so far…",
    "isReady": False,
    "form_id": "eks_service_form",
    "missing_fields": {"required": [], "optional": []},
    "suggestions": [],
    "invalid_fields": [],
}

DRAFT_ENTRY = {
    "identifier": "demo",
    "queue_code": "queue-1",
    "queue_status": "draft",
    "status": "pending",
    "ticket_code": "mcp-u1-abcd",
    "transaction_code": "sc-1",
}


def _chat_patches(reply: dict, entry: dict | None):
    auth = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x", clerk_user_id="")
    return [
        patch.object(chat_tool, "get_auth_context", AsyncMock(return_value=auth)),
        patch.object(chat_tool, "mint_internal_jwt", return_value="jwt"),
        patch.object(chat_tool, "post_chat", AsyncMock(return_value=dict(reply))),
        patch.object(chat_tool, "cache_chatbot_result", AsyncMock(return_value=True)),
        patch.object(chat_tool, "_find_redis_service_entry", AsyncMock(return_value=entry)),
    ]


@pytest.mark.asyncio
async def test_read_request_on_saved_draft_ticket_gets_draft_exists_hint():
    patches = _chat_patches(NOT_READY_QUERY_REPLY, DRAFT_ENTRY)
    with patches[0], patches[1], patches[2], patches[3], patches[4] as lookup:
        result = await chat_tool.chat_impl(message="show the preview", ticket_code="mcp-u1-abcd")

    lookup.assert_awaited_once_with("u1", ticket_code="mcp-u1-abcd")
    assert result["next_action"]["type"] == "draft_exists"
    assert "get_service_configuration(ticket_code='mcp-u1-abcd')" in result["next_action"]["instruction"]
    assert result["draft"] == {"service_name": "demo", "queue_code": "queue-1", "status": "draft"}
    assert result["message"] == NOT_READY_QUERY_REPLY["message"]  # still surfaced


@pytest.mark.asyncio
async def test_value_change_on_saved_draft_ticket_keeps_the_edit_loop():
    patches = _chat_patches(SERVICE_RESULT, DRAFT_ENTRY)
    with patches[0], patches[1], patches[2], patches[3], patches[4] as lookup:
        result = await chat_tool.chat_impl(message="change generate dockerfile to false", ticket_code="mcp-u1-abcd")

    lookup.assert_not_awaited()
    assert result["next_action"]["type"] == "create_service_and_save_draft"
    assert "draft" not in result


@pytest.mark.asyncio
async def test_not_ready_reply_without_a_draft_is_unchanged():
    patches = _chat_patches(NOT_READY_QUERY_REPLY, None)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await chat_tool.chat_impl(message="show the preview", ticket_code="mcp-u1-abcd")

    assert "next_action" not in result
    assert "draft" not in result


@pytest.mark.asyncio
async def test_chat_impl_caches_resource_result_with_kind_marker():
    auth = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x", clerk_user_id="")
    cache = AsyncMock(return_value=True)
    with patch.object(chat_tool, "get_auth_context", AsyncMock(return_value=auth)), \
         patch.object(chat_tool, "mint_internal_jwt", return_value="jwt"), \
         patch.object(chat_tool, "post_chat", AsyncMock(return_value=dict(RESOURCE_RESULT))), \
         patch.object(chat_tool, "cache_chatbot_result", cache):
        result = await chat_tool.chat_impl(message="yes", ticket_code="mcp-u1-abcd")

    payload = cache.await_args.args[2]
    assert payload["kind"] == "resource"
    assert payload["attribute_parameters"] == {"identifier": "b"}
    assert result["next_action"]["type"] == "confirm_deployment"


def test_error_response_has_no_directive():
    assert chat_tool._build_next_action({"status": "error"}) is None


def test_missing_required_field_asks_free_text():
    action = chat_tool._build_next_action({"missing_fields": {"required": ["bucket_name"]}})
    assert action["type"] == "ask_user_text"
    assert action["field_id"] == "bucket_name"


# ── sectioned asking: one dialog for up to 4 fields ──────────────────────────

SECTION_REPLY = {
    "status": "pending",
    "message": "Now the resources.",
    "isReady": False,
    "form_id": "eks_service_form",
    "missing_fields": {"required": ["cpu_requested", "cpu_limit"], "optional": []},
    "suggestions": [{"field_id": "cpu_requested", "label": "CPU Requested (cores)", "options": [{"text": "0.5", "value": "change cpu to 0.5"}]}],
    "invalid_fields": [],
    "section": {
        "title": "Resources",
        "fields": [
            {"field_id": "cpu_requested", "label": "CPU Requested (cores)", "type": "number", "required": True, "multi": False,
             "options": [{"text": "0.5", "value": "0.5"}, {"text": "1", "value": "1"}], "hint": "between 0.1 and 4", "default": None},
            {"field_id": "cpu_limit", "label": "CPU Limit (cores)", "type": "number", "required": True, "multi": False,
             "options": [{"text": "1", "value": "1"}, {"text": "2", "value": "2"}], "hint": None, "default": None},
        ],
    },
}


def test_section_with_two_or_more_fields_becomes_one_dialog():
    action = chat_tool._build_next_action(dict(SECTION_REPLY))
    assert action["type"] == "ask_section"
    assert action["title"] == "Resources"
    assert [f["field_id"] for f in action["fields"]] == ["cpu_requested", "cpu_limit"]
    assert "AskUserQuestion" in action["instruction"] and "answers=" in action["instruction"]


def test_single_field_section_keeps_the_single_question_flow():
    reply = dict(SECTION_REPLY)
    reply["section"] = {"title": "Resources", "fields": SECTION_REPLY["section"]["fields"][:1]}
    action = chat_tool._build_next_action(reply)
    assert action["type"] == "ask_user"


def test_invalid_value_wins_over_the_section():
    reply = dict(SECTION_REPLY)
    reply["invalid_fields"] = [{"field_id": "cpu_limit", "error": "must be at most 4"}]
    action = chat_tool._build_next_action(reply)
    assert action["type"] == "fix_invalid"


def test_slim_keeps_section_only_for_ask_section():
    reply = dict(SECTION_REPLY)
    reply["next_action"] = chat_tool._build_next_action(dict(SECTION_REPLY))
    assert "section" in chat_tool._slim_response_for_llm(dict(reply))
    other = dict(SECTION_REPLY)
    other["next_action"] = {"type": "ask_user"}
    assert "section" not in chat_tool._slim_response_for_llm(other)


@pytest.mark.asyncio
async def test_chat_forwards_dialog_answers_and_skips():
    auth = AuthContext(user_code="u1", tenant_code="aspora", user_email="u@x", clerk_user_id="")
    post = AsyncMock(return_value=dict(SECTION_REPLY))
    with patch.object(chat_tool, "get_auth_context", AsyncMock(return_value=auth)), \
         patch.object(chat_tool, "mint_internal_jwt", return_value="jwt"), \
         patch.object(chat_tool, "post_chat", post), \
         patch.object(chat_tool, "cache_chatbot_result", AsyncMock(return_value=True)):
        result = await chat_tool.chat_impl(
            message="cpu=0.5, memory=1", ticket_code="mcp-u1-abcd",
            answers={"cpu_requested": "0.5", "memory_requested": "1"}, skip=["build_args"],
        )
    kwargs = post.await_args.kwargs
    assert kwargs["answers"] == {"cpu_requested": "0.5", "memory_requested": "1"}
    assert kwargs["skip"] == ["build_args"]
    assert result["next_action"]["type"] == "ask_section"
    assert result["section"]["title"] == "Resources"
    assert "suggestions" not in result


# --- the user's current project (repository + language) -----------------
# The offer has to ride `next_action`: the asking directives tell the LLM to
# ask for ONE field and not to preempt others, which beats a prose section it
# read at session start. A live run proved it — the model went straight to the
# Environment question and never offered anything.

ENV_QUESTION = {
    "status": "pending",
    "message": "Please choose the Environment",
    "isReady": False,
    "collected_data": {"product": "core"},
    "missing_fields": {
        "required": ["environment", "repository", "language", "branches"],
        "optional": ["build_args"],
    },
    "suggestions": [
        {"field_id": "environment", "label": "Environment",
         "options": [{"text": "Prod", "value": "Prod"}, {"text": "Stage", "value": "Stage"}]},
    ],
    "invalid_fields": [],
}


def test_asking_a_field_also_offers_the_users_project():
    action = chat_tool._build_next_action(dict(ENV_QUESTION))
    assert action["type"] == "ask_user"
    text = action["instruction"]
    assert "git remote get-url origin" in text
    assert "go.mod" in text
    # and it must NOT still forbid the extra questions it just asked for
    assert "do not preempt other fields" not in text


def test_branch_is_never_offered_as_a_default():
    text = chat_tool._build_next_action(dict(ENV_QUESTION))["instruction"]
    assert "NEVER default the branch" in text


def test_offer_stops_once_the_repository_is_answered():
    reply = dict(ENV_QUESTION)
    reply["collected_data"] = {"product": "core", "repository": "Regobs/obs_tool"}
    text = chat_tool._build_next_action(reply)["instruction"]
    assert "git remote get-url origin" not in text
    assert "ONLY this field" in text  # the ordinary directive is back


def test_resource_forms_never_see_the_offer():
    """An S3 form has no repository or language to fill."""
    reply = dict(ENV_QUESTION)
    reply["missing_fields"] = {"required": ["bucket_name", "region"], "optional": []}
    assert "git remote" not in chat_tool._build_next_action(reply)["instruction"]


def test_the_offer_rides_a_section_dialog_too():
    reply = dict(SECTION_REPLY)
    reply["collected_data"] = {}
    reply["missing_fields"] = {"required": ["repository", "language"], "optional": []}
    action = chat_tool._build_next_action(reply)
    assert action["type"] == "ask_section"
    assert "git remote get-url origin" in action["instruction"]
    assert "Render ONE AskUserQuestion" in action["instruction"]


def test_language_is_sent_as_the_bare_name_not_name_plus_version():
    """A live run sent language='Python 3.12' and the chatbot refused it: the
    form asks one question but fills two fields, and `language` holds only the
    base name ('Python'), `version` the bare number ('3.12')."""
    text = chat_tool._build_next_action(dict(ENV_QUESTION))["instruction"]
    assert "'language': 'Python'" in text
    assert "bare name ONLY" in text
    # and the version must be held back for the following call
    assert "NEXT call" in text


def test_batched_answers_must_carry_exact_option_values():
    """A live run sent environment='change environment to Stage' and it was
    refused: `answers` entries are option values, not sentences."""
    text = chat_tool._build_next_action(dict(ENV_QUESTION))["instruction"]
    assert "never a sentence" in text
    # the single-field tail must not still say "send it as the next message"
    assert "as the next `chat` message" not in text
    assert "in that one `answers` call" in text


# ── the last optional fields must be OFFERED, never skipped for the user ────
# Reported from a live run: a service was saved with `build_args` and
# `other_paths` closed out on the user's behalf — "Only the two optional fields
# remain. Skipping both." The user never saw either. That is not harmless:
# build_args is where CONFIG_ENV lives, and a Go service with
# go_use_aws_secrets on whose build args were skipped fails its image build.

OPTIONAL_ONLY = {
    "status": "pending",
    "message": "Please provide the Additional Trigger Paths",
    "isReady": False,
    "collected_data": {"service_name": "go-testing-ser"},
    "missing_fields": {"required": [], "optional": ["build_args", "other_paths"]},
    "suggestions": [
        {"field_id": "other_paths", "label": "Additional Trigger Paths",
         "options": [{"text": "Skip", "value": "__skip__"}]},
    ],
    "invalid_fields": [],
}


def test_last_optional_fields_are_put_to_the_user():
    text = chat_tool._build_next_action(dict(OPTIONAL_ONLY))["instruction"]
    assert "ASK THE USER about them" in text
    assert "build_args, other_paths" in text          # named, so none is missed
    assert "Do NOT skip any of them yourself" in text


def test_the_clause_names_why_an_optional_field_can_matter():
    text = chat_tool._build_next_action(dict(OPTIONAL_ONLY))["instruction"]
    assert "CONFIG_ENV" in text


def test_it_does_not_fire_while_required_fields_remain():
    reply = dict(OPTIONAL_ONLY, missing_fields={"required": ["port"], "optional": ["build_args"]})
    assert "These are the LAST fields" not in chat_tool._build_next_action(reply)["instruction"]


def test_it_does_not_fire_when_nothing_is_optional_either():
    reply = dict(OPTIONAL_ONLY, missing_fields={"required": [], "optional": []})
    action = chat_tool._build_next_action(reply)
    assert action is None or "These are the LAST fields" not in action["instruction"]


# ── suggestions are not limits ───────────────────────────────────────────────
# Reported: asked for 0.25 cores, the model answered "minimum 0.5". No code
# enforces 0.5 — the form accepts 0.1, obs_tool's validator wants only > 0, and
# the dashboard has no floor. 0.5 was the lowest suggestion button, and
# "do not invent choices" made it read as the allowed set.

RESOURCES_SECTION = {
    "title": "Resources",
    "fields": [
        {"field_id": "cpu_requested", "label": "CPU Requested (cores)",
         "type": "number", "required": True, "hint": "between 0.1 and 4",
         "options": [{"text": "0.5", "value": "0.5"}, {"text": "1", "value": "1"}]},
        {"field_id": "compute", "label": "Compute", "type": "dropdown", "required": True,
         "options": [{"text": "On-Demand", "value": "on-demand"}]},
    ],
}


def _resources_instruction():
    return chat_tool._section_action(dict(RESOURCES_SECTION))["instruction"]


def test_a_numeric_fields_range_is_stated_not_its_buttons():
    text = _resources_instruction()
    assert "cpu_requested (between 0.1 and 4)" in text
    assert "NOT the permitted set" in text
    assert "Never claim a limit the hint does not give" in text


def test_a_dropdown_is_not_treated_as_free_input():
    """`compute` options ARE the allowed set — inventing one there is wrong."""
    text = _resources_instruction()
    assert "compute (" not in text
    assert "for a dropdown or array the options really are the allowed set" in text


def test_a_section_of_only_dropdowns_gets_no_free_input_clause():
    section = {"title": "Placement", "fields": [
        {"field_id": "product", "label": "Product", "type": "dropdown",
         "options": [{"text": "core", "value": "core"}]},
    ]}
    assert "take FREE input" not in chat_tool._section_action(section)["instruction"]


def test_a_numeric_field_with_no_options_is_not_named():
    """Nothing is on screen to mistake for a limit, so the clause is noise."""
    section = {"title": "Resources", "fields": [
        {"field_id": "port", "label": "Port", "type": "number", "hint": "1-65535"},
        {"field_id": "health", "label": "Health", "type": "text"},
    ]}
    assert "take FREE input" not in chat_tool._section_action(section)["instruction"]


# ── the repository list has exactly one source ───────────────────────────────
# Live run: the user declined the detected repo and asked to see the list. The
# project-context question is raised at the PLACEMENT stage, long before the
# chatbot fetches the repository options (preload: false), so there was no list
# to show — and the model ran `gh repo list Regobs` instead. That enumerates the
# GitHub ORG, not the repositories connected to the tenant, so every name it
# offered was one DevLift would refuse.

def test_the_project_context_offer_never_enumerates_repositories():
    text = chat_tool._build_next_action(dict(ENV_QUESTION))["instruction"]
    assert "NEVER enumerate repositories yourself" in text
    assert "gh repo list" in text            # named, so it is unmistakable
    assert "LEAVE `repository` OUT of the answers call" in text


def test_an_option_list_is_declared_complete():
    """A fetched list IS the permitted set; looking elsewhere widens it wrongly."""
    reply = {
        "status": "pending", "message": "Choose a repository", "isReady": False,
        "collected_data": {}, "missing_fields": {"required": ["repository"], "optional": []},
        "suggestions": [{"field_id": "repository", "label": "Repository", "options": [
            {"text": f"Regobs/repo-{i}", "value": f"Regobs/repo-{i}"} for i in range(6)
        ]}],
        "invalid_fields": [],
    }
    text = chat_tool._build_next_action(reply)["instruction"]
    assert "IS the set it can use" in text
    assert "smaller than the GitHub" in text
    assert "say it is not connected rather than sourcing it elsewhere" in text


def test_options_carry_no_description_so_choices_are_not_printed_twice():
    """Live: every choice rendered as 'config.yaml' above 'config.yaml'. The
    old wording banned a description "of your own", which read as permitting
    one taken from the data — and `value` equals `text` for these options."""
    section = {"title": "Config Path", "fields": [
        {"field_id": "go_config_path", "label": "Config File Path", "type": "text",
         "options": [{"text": "config.yaml", "value": "config.yaml"}]},
    ]}
    for text in (
        chat_tool._section_action(section)["instruction"],
        chat_tool._build_next_action({
            "status": "pending", "message": "x", "isReady": False, "collected_data": {},
            "missing_fields": {"required": ["compute"], "optional": []},
            "suggestions": [{"field_id": "compute", "label": "Compute",
                             "options": [{"text": "Spot", "value": "spot"}]}],
            "invalid_fields": [],
        })["instruction"],
    ):
        # a description that repeats the label is out ...
        assert "same string" in text
        assert "never pass the option's `value` as the description" in text.lower()
        # ... but a genuinely useful one is still wanted
        assert "Create the Argo CD application" in text
        assert "of your own" not in text  # the ambiguous phrasing is gone


def test_long_option_lists_are_numbered_so_the_user_can_answer_by_number():
    """Past AskUserQuestion's cap the options are printed as text, and a
    bulleted list gives the user nothing short to type back — branch names and
    repo slugs are long and easy to mistype. Numbering them makes '3' a valid
    answer. `value` is still what goes back to `chat`, never the number."""
    reply = {
        "status": "pending", "message": "Choose a branch", "isReady": False,
        "collected_data": {}, "missing_fields": {"required": ["branches"], "optional": []},
        "suggestions": [{"field_id": "branches", "label": "Branch", "options": [
            {"text": f"feature/some-long-branch-name-{i}", "value": f"feature/some-long-branch-name-{i}"}
            for i in range(9)
        ]}],
        "invalid_fields": [],
    }
    action = chat_tool._build_next_action(reply)
    assert action["render"] == "text_list"
    text = action["instruction"]
    assert "NUMBERED list" in text
    assert "number or the name" in text
    assert "1-based position" in text
    assert "never the number" in text
    assert "bulleted" not in text.lower()


def test_short_option_lists_still_use_askuserquestion_not_numbering():
    """Numbering is only for the text fallback — pills are already clickable."""
    reply = {
        "status": "pending", "message": "Compute?", "isReady": False,
        "collected_data": {}, "missing_fields": {"required": ["compute"], "optional": []},
        "suggestions": [{"field_id": "compute", "label": "Compute", "options": [
            {"text": "On-demand", "value": "on-demand"}, {"text": "Spot", "value": "spot"},
        ]}],
        "invalid_fields": [],
    }
    action = chat_tool._build_next_action(reply)
    assert action["render"] == "pills"
    assert "NUMBERED" not in action["instruction"]


def test_section_question_carries_description_and_hint_separately():
    """`hint` says what a field ACCEPTS, `description` what it is FOR. They
    used to share one slot in the chatbot payload, where any validation rule
    beat the description — so service_path could state its format but never
    its purpose. Both travel now, and the directive has to ask for both."""
    section = {"title": "Networking", "fields": [
        {"field_id": "service_path", "label": "Service Path", "type": "text",
         "hint": "a path starting with '/', e.g. '/api' (not '/' or '/*')",
         "description": "The URL path the load balancer routes to this service.",
         "options": [{"text": "/api", "value": "/api"}, {"text": "/v1", "value": "/v1"}]},
    ]}
    text = chat_tool._section_action(section)["instruction"]
    assert "`description` when present" in text
    assert "what the field is FOR" in text
    assert "what the field ACCEPTS" in text
    # and the two must not be presented as interchangeable
    assert "not interchangeable" in text


# ── an example belongs on screen once ────────────────────────────────────────
# Live run, Kong's Route section: `paths` reaches the dialog with `options: []`
# (sections.py: a free-text array is askable beside the method and the auth it
# belongs with). The picker needs rows, so the model lifted the two example
# paths out of the question text and wrote "Example shape — pick Other to type
# your own path(s), comma separated" under EACH of them. The user read the same
# two paths twice and the same sentence twice, for a field they were always
# going to type into. The examples now travel as `examples` and belong in the
# rows only; the question text keeps the explanation.

FREE_ARRAY_SECTION = {
    "title": "Route",
    "fields": [
        {"field_id": "method", "label": "Method", "type": "dropdown", "required": True,
         "options": [{"text": "GET", "value": "GET"}, {"text": "POST", "value": "POST"}]},
        {"field_id": "paths", "label": "Route Paths", "type": "array", "required": True,
         "multi": True, "options": [],
         "examples": ["~/api/v1/users$", "~/api/v1/users/(?<id>[^/]+)$"],
         "description": "Kong regex paths to ADD to this route group.",
         "hint": "a Kong regex path: starts with '~/', ends with '$', no spaces."},
    ],
}


def test_a_typed_field_is_named_and_its_examples_become_the_rows():
    text = chat_tool._section_action(FREE_ARRAY_SECTION)["instruction"]
    assert "no choices of their own and are typed: paths" in text
    assert "`examples`, those ARE its rows" in text
    assert "BARE labels with no description under them" in text
    assert "Invent no extra row" in text


def test_the_examples_are_not_printed_in_the_question_as_well():
    """The whole point: the same path twice is what made this hard to read."""
    text = chat_tool._section_action(FREE_ARRAY_SECTION)["instruction"]
    assert "leave them OUT of the question text" in text


def test_the_hint_is_dropped_where_the_rows_already_show_the_shape():
    """Two paths show the anchors and the capture group at a glance; the
    sentence spelling out the same rule is a third thing to read first. It is
    not lost — a value that breaks it is refused WITH the rule attached."""
    text = chat_tool._section_action(FREE_ARRAY_SECTION)["instruction"]
    assert "drop its `hint`" in text
    assert "state no format rule and warn about no spelling" in text
    assert "comes straight back refused" in text
    # and the general rule, for every other field, is untouched
    assert "which says what the field ACCEPTS" in text


def test_a_typed_field_without_examples_keeps_its_hint():
    """Nothing on screen shows the shape there, so the sentence is all there
    is — dropping it would leave the user with a bare label."""
    section = {"title": "Build", "fields": [
        {"field_id": "repository", "label": "Repository", "type": "dropdown",
         "options": [{"text": "Regobs/goms", "value": "Regobs/goms"}]},
        {"field_id": "other_paths", "label": "Trigger Paths", "type": "array",
         "multi": True, "options": [], "hint": "a repo-relative path"},
    ]}
    assert "its `hint` stays" in chat_tool._section_action(section)["instruction"]


def test_examples_are_not_presented_as_the_allowed_set():
    """An array's `options` ARE its allowed set; `examples` are the opposite,
    and the section payload keeps them apart so the model does not conflate
    them and refuse a path the user is entitled to type."""
    text = chat_tool._section_action(FREE_ARRAY_SECTION)["instruction"]
    assert "NOT the values the field is limited to" in text
    assert "arrives through Other" in text


def test_the_examples_reach_the_model_verbatim():
    fields = chat_tool._section_action(FREE_ARRAY_SECTION)["fields"]
    paths = next(f for f in fields if f["field_id"] == "paths")
    assert paths["examples"] == ["~/api/v1/users$", "~/api/v1/users/(?<id>[^/]+)$"]
    assert paths["options"] == []


def test_a_dropdown_beside_it_is_not_swept_into_the_clause():
    text = chat_tool._section_action(FREE_ARRAY_SECTION)["instruction"]
    assert "typed: paths." in text  # `method` is not named
    assert "Do not invent choices." in text  # the general rule still stands


def test_a_section_with_every_field_offering_something_gets_no_clause():
    assert "no choices of their own" not in chat_tool._section_action(RESOURCES_SECTION)["instruction"]


def test_a_missing_options_key_is_not_an_empty_one():
    """`options` absent means the payload never said; `[]` means it said NONE.
    Only the second is a typed field, and the clause must not fire on the first
    or it lands on every hand-built section in the suite."""
    section = {"title": "Resources", "fields": [
        {"field_id": "port", "label": "Port", "type": "number", "hint": "1-65535"},
        {"field_id": "health", "label": "Health", "type": "text"},
    ]}
    assert "no choices of their own" not in chat_tool._section_action(section)["instruction"]


def test_a_typed_field_with_nothing_to_show_still_gets_a_rule():
    """other_paths, cross_account_ids: free arrays with no examples yet. The
    clause must not go silent on them, or the model is back to improvising."""
    section = {"title": "Build", "fields": [
        {"field_id": "repository", "label": "Repository", "type": "dropdown",
         "options": [{"text": "Regobs/goms", "value": "Regobs/goms"}]},
        {"field_id": "other_paths", "label": "Trigger Paths", "type": "array",
         "multi": True, "options": []},
    ]}
    text = chat_tool._section_action(section)["instruction"]
    assert "typed: other_paths" in text
    assert "offer only values you genuinely hold" in text
