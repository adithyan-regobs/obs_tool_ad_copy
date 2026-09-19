"""The per-language configuration template: what it loads, when it is offered,
and what the closing summary says about it.

Sections 1-4 of the EKS form (placement, service, repository, language) are the
ten things only the user can answer. Sections 5-11 are properties of the
language, and the template is what 107 live deployments run with. It is keyed
by language, so it cannot be chosen until section 4 is done — which is exactly
where the offer lands.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_servers.devlift_mcp import config_templates as ct
from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp.tools import chat as chat_tool


# ── the template file itself ────────────────────────────────────────────────

def test_every_language_the_form_offers_has_a_template():
    assert set(ct.language_names()) == {"Go", "Java Gradle", "Java Maven", "Node.js", "Python"}


def test_language_lookup_is_case_insensitive():
    """The label arrives as the user's dropdown choice, not a slug."""
    assert ct.template_for_language("go") is ct.template_for_language("Go")
    assert ct.template_for_language("java gradle") is not None
    assert ct.template_for_language("Rust") is None
    assert ct.template_answers("Rust") == {}


def test_answers_are_keyed_by_form_field_id():
    """The template names fields the way a deployment does; only the
    autoscaling block differs from what the form asks for."""
    a = ct.template_answers("Go")
    assert "hpa_enabled" in a and "hpa.enabled" not in a
    py = ct.template_answers("Python")          # Python autoscales, so it has them
    assert "min_replicas" in py and "hpa.min_replicas" not in py
    assert "max_replicas" in py and "hpa.max_replicas" not in py


def test_only_the_active_side_of_the_replica_split_is_sent():
    """The form activates min/max when autoscaling is on and replica_count
    when it is off, and rejects an answer for the inactive side — "Min
    Replicas — 1 is not applicable for your current selections". Carrying both
    is a contradiction, so the template data holds one or the other."""
    go = ct.template_answers("Go")              # autoscaling off
    assert go["hpa_enabled"] == "false"
    assert go["replica_count"] == "1"
    assert "min_replicas" not in go and "max_replicas" not in go

    for lang in ("Python", "Java Gradle", "Node.js"):
        a = ct.template_answers(lang)           # autoscaling on
        assert a["hpa_enabled"] == "true"
        assert "replica_count" not in a


def test_option_values_are_sent_as_stored_not_translated_here():
    """Mapping an option's value to its label is the chatbot's job — it owns
    the form, and a copy of those labels over here would be a second source of
    truth for `eks_service_form.json`, free to drift the moment a label
    changes. See canonicalise_option_values in the chatbot's hierarchy.py."""
    a = ct.template_answers("Go")
    assert a["create_ecr"] == "true"            # not "Yes"
    assert a["auth_mode"] == "pod_identity"     # not "Pod Identity"
    assert a["compute"] == "on-demand"          # not "On-Demand"


def test_booleans_become_the_strings_the_form_takes():
    a = ct.template_answers("Go")
    assert a["generate_dockerfile"] == "false"
    assert a["create_ecr"] == "true"
    assert a["hpa_enabled"] == "false"          # Go runs a fixed replica count


def test_fields_that_are_not_on_the_form_are_dropped():
    """namespace / ebs / secrets_enabled live in the manifests this was derived
    from, but nobody is ever asked for them — sending them would be answering
    fields that do not exist."""
    a = ct.template_answers("Go")
    for ghost in ("namespace", "ebs_enabled", "ebs.volume", "secrets_enabled",
                  "secret_keys", "ci_provider"):
        assert ghost not in a


def test_nulls_are_left_for_the_user_to_answer():
    """A null means 'no reliable default'. Leaving it out is what makes the
    form ask."""
    assert set(ct.unset_fields("Go")) == {"service_path", "health"}   # no name yet
    # Every language now answers in full once the service has a name. Maven
    # was the last with gaps: its sample was too small for the gate to derive
    # memory or a replica range, so both were set by hand.
    for lang in ct.language_names():
        assert ct.unset_fields(lang, "x") == []
    maven = ct.template_answers("Java Maven", "x")
    assert maven["memory_requested"] == maven["memory_limit"] == "2.0"
    assert maven["min_replicas"] == "1" and maven["max_replicas"] == "2"


def test_the_service_name_decides_the_path_however_it_was_typed():
    """helloworld, helloworld-service and helloworld_service are the same
    service, and the manifests deploy all three as helloworld-service."""
    for typed in ("helloworld", "helloworld-service", "helloworld_service", "Hello World"):
        expected = "/hello-world-service" if " " in typed else "/helloworld-service"
        assert ct.template_answers("Go", typed)["service_path"] == expected


def test_java_probes_actuator_and_everything_else_probes_health():
    for lang in ("Java Gradle", "Java Maven"):
        a = ct.template_answers(lang, "helloworld")
        assert a["health"] == "/helloworld-service/actuator/health"
    for lang in ("Go", "Node.js", "Python"):
        a = ct.template_answers(lang, "helloworld")
        assert a["health"] == "/helloworld-service/health"


def test_path_and_health_are_asked_only_while_the_service_is_unnamed():
    """They are derived, not stored — with a name there is nothing to ask."""
    assert ct.unset_fields("Go", "helloworld") == []
    assert set(ct.unset_fields("Go")) == {"service_path", "health"}
    assert "service_path" not in ct.template_answers("Go")



def test_every_language_runs_on_8080():
    for lang in ct.language_names():
        assert ct.template_answers(lang, "x")["port"] == "8080"


def test_the_go_secret_flag_is_gone():
    for lang in ct.language_names():
        assert "go_use_aws_secrets" not in ct.template_answers(lang, "x")
        assert "go_use_aws_secrets" not in ct.template_skips(lang)


def test_xms_and_xmx_are_skipped_not_dropped():
    """They are "" by decision — leave the heap to the JVM. Dropping them left
    the fields unanswered, so the form asked for them right after the template
    had settled them; "leave unset" has to travel as a skip."""
    a = ct.template_answers("Java Gradle")
    assert "xms" not in a and "xmx" not in a
    assert "xms" in ct.template_skips("Java Gradle")
    assert "xmx" in ct.template_skips("Java Gradle")
    # and they are not "ask the user" — that is what null means
    assert "xms" not in ct.unset_fields("Java Gradle")


def test_limits_match_requests_and_alb_is_internal():
    for lang in ct.language_names():
        a = ct.template_answers(lang)
        if "cpu_requested" in a:
            assert a["cpu_limit"] == a["cpu_requested"]
        if "memory_requested" in a:
            assert a["memory_limit"] == a["memory_requested"]
        assert a["alb_schema"] == "internal"


# ── when it is offered ──────────────────────────────────────────────────────

AUTH = type("A", (), {"user_code": "u1", "tenant_code": "aspora"})()


def _chat_patches(reply, *, already_applied=None, remembered=None):
    return [
        patch.object(chat_tool, "get_auth_context", AsyncMock(return_value=AUTH)),
        patch.object(chat_tool, "mint_internal_jwt", return_value="jwt"),
        patch.object(chat_tool, "post_chat", AsyncMock(return_value=dict(reply))),
        patch.object(chat_tool, "get_template_applied", AsyncMock(return_value=already_applied)),
        patch.object(chat_tool, "cache_template_applied", remembered or AsyncMock(return_value=True)),
        patch.object(chat_tool, "cache_chatbot_result", AsyncMock(return_value=True)),
        patch.object(chat_tool, "_existing_draft_for_ticket", AsyncMock(return_value=None)),
    ]


async def _run_chat(reply, **kw):
    from contextlib import ExitStack
    remembered = AsyncMock(return_value=True)
    with ExitStack() as stack:
        for p in _chat_patches(reply, remembered=remembered, **kw):
            stack.enter_context(p)
        result = await chat_tool.chat_impl(message="Go", ticket_code="mcp-u1-t1")
    return result, remembered


# A session with the basics done — the only point at which a template means
# anything. The ten fields below are what it can never supply.
_BASICS = {
    "product": "core", "resource_group": "Wealth", "service_name": "api",
    "service_type": "API", "environment": "Stage", "geo_location": "Mumbai",
    "repository": "Regobs/api", "branches": ["main"],
}
PENDING_WITH_LANGUAGE = {
    "status": "pending", "message": "next", "isReady": False,
    "collected_data": {**_BASICS, "language": "Go", "version": "1.24"},
    "missing_fields": {"required": ["cpu_requested"], "optional": []},
    "suggestions": [], "invalid_fields": [],
}


@pytest.mark.asyncio
async def test_template_is_offered_once_the_language_is_known():
    result, remembered = await _run_chat(PENDING_WITH_LANGUAGE)
    action = result["next_action"]
    assert action["type"] == "review_template"
    assert action["language"] == "Go"
    assert action["template"]["cpu_requested"] == "0.25"
    # nothing left to ask: the service is named, so path and health follow
    assert action["still_asked"] == []
    assert action["template"]["service_path"] == "/api-service"
    assert action["template"]["health"] == "/api-service/health"
    # recorded as offered-but-not-applied, so it is not offered again
    remembered.assert_awaited_once()
    assert remembered.await_args.args[2]["applied"] is False


@pytest.mark.asyncio
async def test_the_offer_shows_everything_and_asks_once():
    """Twenty-odd separate questions whose answer is 'whatever everyone else
    uses' is the thing this replaces — so the directive must forbid that."""
    result, _ = await _run_chat(PENDING_WITH_LANGUAGE)
    text = result["next_action"]["instruction"]
    assert "Show them ALL" in text
    assert "do not present them as a question per value" in text
    assert "Never re-send `template`" in text


@pytest.mark.asyncio
async def test_template_is_not_offered_before_the_language_is_answered():
    reply = {**PENDING_WITH_LANGUAGE, "collected_data": dict(_BASICS)}
    result, remembered = await _run_chat(reply)
    assert result.get("next_action", {}).get("type") != "review_template"
    remembered.assert_not_awaited()


@pytest.mark.asyncio
async def test_template_is_offered_only_once_per_ticket():
    result, _ = await _run_chat(
        PENDING_WITH_LANGUAGE,
        already_applied={"language": "Go", "answers": {}, "applied": False},
    )
    assert result.get("next_action", {}).get("type") != "review_template"


@pytest.mark.asyncio
async def test_a_language_without_a_template_is_left_alone():
    reply = {**PENDING_WITH_LANGUAGE,
             "collected_data": {**_BASICS, "language": "Rust", "version": "1.80"}}
    result, remembered = await _run_chat(reply)
    assert result.get("next_action", {}).get("type") != "review_template"
    remembered.assert_not_awaited()


# ── applying it ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_sends_the_whole_template_and_the_users_override_wins():
    from contextlib import ExitStack
    post = AsyncMock(return_value=dict(PENDING_WITH_LANGUAGE))
    with ExitStack() as stack:
        for p in _chat_patches(
            PENDING_WITH_LANGUAGE,
            already_applied={"language": "Go", "answers": {}, "applied": False},
        ):
            stack.enter_context(p)
        stack.enter_context(patch.object(chat_tool, "post_chat", post))
        await chat_tool.chat_impl(
            message="apply the Go template", ticket_code="mcp-u1-t1",
            answers={"port": "9000"}, apply_template=True,
        )

    sent = post.await_args.kwargs["answers"]
    assert sent["cpu_requested"] == "0.25"        # from the template
    assert sent["dockerfile_path"] == "Dockerfile"
    assert sent["port"] == "9000"                 # the user's value, not 8080
    assert "service_path" not in sent             # still theirs to answer


# ── the closing summary ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_separates_what_was_kept_from_what_was_changed():
    cached = {"collected_data": {
        "cpu_requested": "0.25", "dockerfile_path": "Dockerfile", "port": "9000",
    }}
    applied = {"language": "Go", "applied": True, "answers": {
        "cpu_requested": "0.25", "dockerfile_path": "Dockerfile", "port": "8080",
    }}
    with patch("app.mcp_servers.devlift_mcp.chatbot_client.get_template_applied",
               AsyncMock(return_value=applied)):
        out = await dispatcher._template_summary("u1", "mcp-u1-t1", cached)

    assert out["language"] == "Go"
    assert out["kept_count"] == 2
    assert out["changed"] == {"port": {"template": "8080", "chosen": "9000"}}


@pytest.mark.asyncio
async def test_summary_does_not_call_a_value_changed_because_its_type_differs():
    """The template stores what the form takes; the chatbot gives back what it
    stored. 0.25 and "0.25" are the same answer, not a change the user made."""
    cached = {"collected_data": {"cpu_requested": 0.25, "other_paths": []}}
    applied = {"language": "Go", "applied": True,
               "answers": {"cpu_requested": "0.25", "other_paths": []}}
    with patch("app.mcp_servers.devlift_mcp.chatbot_client.get_template_applied",
               AsyncMock(return_value=applied)):
        out = await dispatcher._template_summary("u1", "mcp-u1-t1", cached)
    assert out["changed"] == {}
    assert out["kept_count"] == 2


@pytest.mark.asyncio
async def test_no_summary_when_the_template_was_only_offered():
    """Offered-but-declined must not be reported as if it had been applied."""
    with patch("app.mcp_servers.devlift_mcp.chatbot_client.get_template_applied",
               AsyncMock(return_value={"language": "Go", "answers": {}, "applied": False})):
        assert await dispatcher._template_summary("u1", "t", {"collected_data": {}}) is None


@pytest.mark.asyncio
async def test_no_summary_for_an_edit_session():
    with patch("app.mcp_servers.devlift_mcp.chatbot_client.get_template_applied",
               AsyncMock(return_value=None)):
        assert await dispatcher._template_summary("u1", "t", {"collected_data": {}}) is None


# ── the template belongs to creating, not to editing ────────────────────────

@pytest.mark.asyncio
async def test_an_edit_session_is_never_offered_the_template():
    """An edit opens with the live service's values already in place, so the
    language is known on turn one. Without the claim the offer would fire and
    propose replacing a RUNNING configuration with defaults — the user asked
    to change a port, not to reset twenty settings."""
    prefilled = {"language": None, "answers": {}, "applied": False, "source": "edit"}
    result, remembered = await _run_chat(PENDING_WITH_LANGUAGE, already_applied=prefilled)
    assert result.get("next_action", {}).get("type") != "review_template"
    remembered.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_clone_session_is_never_offered_the_template():
    """A clone exists to carry the source's settings over; offering defaults
    would offer to discard the thing being copied."""
    prefilled = {"language": None, "answers": {}, "applied": False, "source": "clone"}
    result, _ = await _run_chat(PENDING_WITH_LANGUAGE, already_applied=prefilled)
    assert result.get("next_action", {}).get("type") != "review_template"


@pytest.mark.asyncio
async def test_an_edit_reports_no_template_in_the_summary():
    """The claim record must not read as 'a template was applied' at the end."""
    prefilled = {"language": None, "answers": {}, "applied": True, "source": "edit"}
    with patch("app.mcp_servers.devlift_mcp.chatbot_client.get_template_applied",
               AsyncMock(return_value=prefilled)):
        out = await dispatcher._template_summary("u1", "t", {"collected_data": {"port": "9000"}})
    assert out is None


# ── "none of these" is a skip, not an empty list ────────────────────────────

def test_empty_lists_become_skips_not_answers():
    """`[]` is not an answer the form can take — structured answers drop it on
    the way in, so the field stays unanswered and is asked again. Custom IAM
    Policies and Additional Trigger Paths were both asked right after the
    template had settled them."""
    a = ct.template_answers("Go")
    for empty in ("custom_iam_policies", "other_paths", "build_args"):
        assert empty not in a
    assert set(ct.template_skips("Go")) == {
        "other_paths", "build_args", "custom_iam_policies"}
    # a Java language adds the two heap fields to the same list
    assert set(ct.template_skips("Java Gradle")) == {
        "other_paths", "build_args", "custom_iam_policies", "xms", "xmx"}


@pytest.mark.asyncio
async def test_applying_skips_the_fields_the_template_says_none_to():
    from contextlib import ExitStack
    post = AsyncMock(return_value=dict(PENDING_WITH_LANGUAGE))
    with ExitStack() as stack:
        for p in _chat_patches(
            PENDING_WITH_LANGUAGE,
            already_applied={"language": "Go", "answers": {}, "applied": False},
        ):
            stack.enter_context(p)
        stack.enter_context(patch.object(chat_tool, "post_chat", post))
        await chat_tool.chat_impl(
            message="apply the Go template", ticket_code="mcp-u1-t1", apply_template=True,
        )
    assert set(post.await_args.kwargs["skip"]) == {
        "custom_iam_policies", "other_paths", "build_args"}


@pytest.mark.asyncio
async def test_a_field_the_user_filled_in_is_never_skipped_for_them():
    """Asking for S3 access must not be overridden by the template's 'none'."""
    from contextlib import ExitStack
    post = AsyncMock(return_value=dict(PENDING_WITH_LANGUAGE))
    with ExitStack() as stack:
        for p in _chat_patches(
            PENDING_WITH_LANGUAGE,
            already_applied={"language": "Go", "answers": {}, "applied": False},
        ):
            stack.enter_context(p)
        stack.enter_context(patch.object(chat_tool, "post_chat", post))
        await chat_tool.chat_impl(
            message="apply it", ticket_code="mcp-u1-t1", apply_template=True,
            answers={"custom_iam_policies": ["s3"]},
        )
    assert set(post.await_args.kwargs["skip"]) == {"other_paths", "build_args"}
    assert post.await_args.kwargs["answers"]["custom_iam_policies"] == ["s3"]


# ── a failed call must not be narrated as if it had succeeded ───────────────

@pytest.mark.asyncio
async def test_an_unreachable_server_forbids_showing_the_ticket_or_a_summary():
    """With no directive the model filled the silence: it printed the ticket
    code as proof the session survived, rebuilt a 'What's captured so far'
    table from its own memory, and named a service it had invented. None of
    that is knowable once the call has failed."""
    import httpx
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in _chat_patches(PENDING_WITH_LANGUAGE):
            stack.enter_context(p)
        stack.enter_context(patch.object(
            chat_tool, "post_chat", AsyncMock(side_effect=httpx.ConnectError("refused"))))
        result = await chat_tool.chat_impl(message="hi", ticket_code="mcp-u1-t1")

    assert result["status"] == "error"
    assert "Nothing was saved" in result["message"]
    text = result["next_action"]["instruction"]
    assert result["next_action"]["type"] == "chat_unreachable"
    assert "Do NOT print the ticket code" in text
    assert "what was captured" in text
    assert "do not invent" in text.lower()
    # the raw exception is data for the model, never the headline
    assert "refused" not in result["message"]


@pytest.mark.asyncio
async def test_canonicalised_labels_are_not_reported_as_user_changes():
    """The chatbot stores a dropdown answer as its option LABEL, so the "true"
    obs_tool sent for create_ecr comes back as "Yes". Diffing sent-against-final
    listed all five such fields as changes the user had made — create_ecr:
    true → Yes — when the user had touched none of them."""
    applied = {
        "language": "Python", "applied": True, "overrides": [],
        "answers": {"create_ecr": "true", "auth_mode": "pod_identity", "port": "8000"},
        "baseline": {"create_ecr": "Yes", "auth_mode": "Pod Identity", "port": "8000"},
    }
    cached = {"collected_data": {
        "create_ecr": "Yes", "auth_mode": "Pod Identity", "port": "8000",
    }}
    with patch("app.mcp_servers.devlift_mcp.chatbot_client.get_template_applied",
               AsyncMock(return_value=applied)):
        out = await dispatcher._template_summary("u1", "t", cached)

    assert out["changed"] == {}
    assert out["kept_count"] == 3


@pytest.mark.asyncio
async def test_a_value_the_user_asked_to_differ_is_still_reported():
    """The baseline holds the user's value too, so without remembering WHICH
    fields they overrode, 'port 8080 → 9000' would vanish from the summary."""
    applied = {
        "language": "Go", "applied": True, "overrides": ["port"],
        "answers": {"port": "8080", "create_ecr": "true"},
        "baseline": {"port": "9000", "create_ecr": "Yes"},
    }
    cached = {"collected_data": {"port": "9000", "create_ecr": "Yes"}}
    with patch("app.mcp_servers.devlift_mcp.chatbot_client.get_template_applied",
               AsyncMock(return_value=applied)):
        out = await dispatcher._template_summary("u1", "t", cached)

    assert out["changed"] == {"port": {"template": "8080", "chosen": "9000"}}
    assert out["kept"] == ["create_ecr"]


@pytest.mark.asyncio
async def test_a_value_changed_after_the_template_landed_is_reported():
    """'actually make the cpu 1' later in the conversation is a real change."""
    applied = {
        "language": "Go", "applied": True, "overrides": [],
        "answers": {"cpu_limit": "0.25"}, "baseline": {"cpu_limit": "0.25"},
    }
    cached = {"collected_data": {"cpu_limit": "1"}}
    with patch("app.mcp_servers.devlift_mcp.chatbot_client.get_template_applied",
               AsyncMock(return_value=applied)):
        out = await dispatcher._template_summary("u1", "t", cached)
    assert out["changed"] == {"cpu_limit": {"template": "0.25", "chosen": "1"}}


@pytest.mark.asyncio
async def test_the_offer_accounts_for_every_remaining_field():
    """The template makes two kinds of decision: values it sets, and fields it
    leaves empty. Only the first was ever shown, so accepting the template
    also accepted four invisible ones — no IAM policies, no build args, no JVM
    heap — that are then never asked about again."""
    result, _ = await _run_chat(PENDING_WITH_LANGUAGE)
    action = result["next_action"]
    assert set(action["left_unset"]) == {
        "other_paths", "build_args", "custom_iam_policies"}
    assert action["still_asked"] == []

    text = action["instruction"]
    # and they belong IN the list, as rows — not summarised as prose beneath it,
    # which reads as a footnote next to a table the user is scanning
    assert "ROWS IN THE SAME GROUPS" in text
    assert "Custom IAM policies: none" in text
    assert "Do NOT collect them into" in text
    assert "every remaining setting is accounted for" in text


# ── accepting the template settles every optional field ─────────────────────

ACCEPTED = {
    "language": "Java Gradle", "answers": {}, "applied": False,
    "optional_open": ["build_path", "build_args", "other_paths",
                      "custom_iam_policies", "xms", "xmx"],
}
ONLY_OPTIONAL_LEFT = {
    "status": "pending", "message": "x", "isReady": False,
    "collected_data": {"language": "Java Gradle"},
    "missing_fields": {"required": [],
                       "optional": ["build_path", "build_args", "other_paths"]},
    "suggestions": [], "invalid_fields": [],
}


@pytest.mark.asyncio
async def test_accepting_the_template_skips_every_optional_field():
    """The review is where the user accepts the non-required configuration as
    a whole — including fields the template says nothing about, like
    build_path on a Java service. Being asked for a build path straight after
    accepting is the review not having meant anything."""
    from contextlib import ExitStack
    post = AsyncMock(return_value=dict(ONLY_OPTIONAL_LEFT))
    with ExitStack() as stack:
        for p in _chat_patches(ONLY_OPTIONAL_LEFT, already_applied=ACCEPTED):
            stack.enter_context(p)
        stack.enter_context(patch.object(chat_tool, "post_chat", post))
        await chat_tool.chat_impl(message="apply", ticket_code="t1", apply_template=True)

    assert set(post.await_args.kwargs["skip"]) == {
        "build_path", "build_args", "other_paths", "custom_iam_policies", "xms", "xmx"}


@pytest.mark.asyncio
async def test_a_field_the_user_asked_for_is_answered_not_skipped():
    from contextlib import ExitStack
    post = AsyncMock(return_value=dict(ONLY_OPTIONAL_LEFT))
    with ExitStack() as stack:
        for p in _chat_patches(ONLY_OPTIONAL_LEFT, already_applied=ACCEPTED):
            stack.enter_context(p)
        stack.enter_context(patch.object(chat_tool, "post_chat", post))
        await chat_tool.chat_impl(message="apply", ticket_code="t1", apply_template=True,
                                  answers={"build_path": "api-server"})
    kw = post.await_args.kwargs
    assert kw["answers"]["build_path"] == "api-server"
    assert "build_path" not in kw["skip"]


def test_the_optional_round_up_is_silent_once_the_template_is_accepted():
    """`_last_optional_fields_clause` exists so an optional field is never
    decided unseen. The template review is where it IS seen, so after that the
    clause asks a question the user has already answered."""
    assert chat_tool._last_optional_fields_clause(ONLY_OPTIONAL_LEFT, True) == ""
    # and without a template it still does its original job
    text = chat_tool._last_optional_fields_clause(ONLY_OPTIONAL_LEFT, False)
    assert "ASK THE USER about them" in text


def test_required_fields_are_still_asked_after_the_template():
    """What survives is exactly what is required and missing — health and
    service path always, plus port on Python and memory / replicas on Maven."""
    reply = {**ONLY_OPTIONAL_LEFT,
             "missing_fields": {"required": ["health", "service_path"], "optional": ["build_path"]}}
    assert chat_tool._last_optional_fields_clause(reply, True) == ""
    action = chat_tool._build_next_action(reply, True)
    assert action is not None          # the required ones still drive a question


# ── the offer follows the LANGUAGE, not the ticket ──────────────────────────

@pytest.mark.asyncio
async def test_correcting_the_language_offers_the_new_template():
    """"Actually it is Java Maven", said after the Go template has gone by,
    used to leave the service holding Go's values — ./cmd, configs/config.json,
    go_use_aws_secrets — with nothing offering Maven's, because the ticket had
    already been served once."""
    reply = {**PENDING_WITH_LANGUAGE,
             "collected_data": {**_BASICS, "language": "Java Maven", "version": "21"}}
    served_go = {"language": "Go", "answers": {}, "applied": True}
    result, remembered = await _run_chat(reply, already_applied=served_go)

    action = result["next_action"]
    assert action["type"] == "review_template"
    assert action["language"] == "Java Maven"
    assert "xms" in action["left_unset"]          # Java's fields, not Go's
    assert "go_config_path" not in action["template"]
    remembered.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_same_language_is_not_offered_twice():
    served_go = {"language": "Go", "answers": {}, "applied": True}
    result, remembered = await _run_chat(PENDING_WITH_LANGUAGE, already_applied=served_go)
    assert result.get("next_action", {}).get("type") != "review_template"
    remembered.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_edit_is_still_never_offered_one_even_if_the_language_changes():
    """Changing a live service's language is an edit, not a bootstrap: the
    template must not offer to reset twenty settings alongside it."""
    reply = {**PENDING_WITH_LANGUAGE,
             "collected_data": {**_BASICS, "language": "Java Maven", "version": "21"}}
    prefilled = {"language": None, "answers": {}, "applied": False, "source": "edit"}
    result, remembered = await _run_chat(reply, already_applied=prefilled)
    assert result.get("next_action", {}).get("type") != "review_template"
    remembered.assert_not_awaited()


# ── every question is a dialog ──────────────────────────────────────────────

def test_a_field_without_choices_is_still_asked_as_a_dialog():
    """"No dropdown options" used to mean "ask in prose", and the service path
    came back as a paragraph the user had to read and answer in their own
    words. A dialog is one click; the picker's Other carries free input."""
    reply = {
        "status": "pending", "message": "x", "isReady": False,
        "collected_data": {}, "missing_fields": {"required": ["service_path"], "optional": []},
        "suggestions": [], "invalid_fields": [],
    }
    action = chat_tool._build_next_action(reply)
    assert action["type"] == "ask_user_text"
    text = action["instruction"]
    assert "ONE AskUserQuestion" in text
    assert "does NOT make" in text and "plain-text question" in text
    assert "Other" in text
    # and it must not pad the picker to look fuller
    assert "do not invent a third" in text


# ── nothing is offered until the service exists on paper ────────────────────

FULL = {"product": "core", "resource_group": "Wealth", "service_name": "chat-bot-poc",
        "service_type": "API", "environment": "Stage", "geo_location": "Mumbai",
        "repository": "Vance-Club/x", "branches": ["main"],
        "language": "Python", "version": "3.11"}


def test_the_ten_fields_a_template_can_never_supply():
    """They resolve to applications_mst_code, resource_group_code, the
    service's name and type, environment, geo_loc_mst_code, language_ref_code
    and the repository the build reads. No template has an opinion on any."""
    assert ct.prerequisites_met(FULL)
    for field in FULL:
        partial = {k: v for k, v in FULL.items() if k != field}
        assert not ct.prerequisites_met(partial), f"{field} should block the offer"
        assert ct.missing_prerequisites(partial) == [field]


def test_an_empty_value_counts_as_unanswered():
    for empty in (None, "", [], {}):
        assert not ct.prerequisites_met({**FULL, "branches": empty})


@pytest.mark.asyncio
async def test_the_template_waits_for_the_basics():
    """Knowing the language is not enough. The project-context preface offers
    repository and language early, so language lands while the resource group
    and even the service name are still open — and the review then covered
    twenty settings for a service that was not placed anywhere yet."""
    reply = {
        "status": "pending", "message": "x", "isReady": False,
        "collected_data": {"repository": "Vance-Club/devlift-secret-config-manager",
                           "language": "Python", "version": "3.11.6"},
        "missing_fields": {"required": ["resource_group"], "optional": []},
        "suggestions": [], "invalid_fields": [],
    }
    result, remembered = await _run_chat(reply)
    assert result.get("next_action", {}).get("type") != "review_template"
    remembered.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_template_arrives_once_they_are_all_in():
    reply = {
        "status": "pending", "message": "x", "isReady": False,
        "collected_data": dict(FULL),
        "missing_fields": {"required": ["cpu_requested"], "optional": []},
        "suggestions": [], "invalid_fields": [],
    }
    result, _ = await _run_chat(reply)
    action = result["next_action"]
    assert action["type"] == "review_template"
    assert action["language"] == "Python"
    # and with the name in hand, the path follows rather than being asked
    assert action["template"]["service_path"] == "/chat-bot-poc-service"
    assert action["template"]["health"] == "/chat-bot-poc-service/health"
    assert action["still_asked"] == []


# ── one question per turn, in one shape ─────────────────────────────────────

def _repo_language_pending(options):
    """A turn where the project-context offer is live AND a field is pending."""
    return {
        "status": "pending", "message": "x", "isReady": False,
        "collected_data": {},                     # no repository, no language yet
        "missing_fields": {"required": ["repository", "language", "resource_group"],
                           "optional": []},
        "suggestions": [{"field_id": "resource_group", "label": "Resource Group",
                         "options": options}],
        "invalid_fields": [],
    }


def test_a_long_list_does_not_also_open_the_project_context_dialog():
    """Nine resource groups printed as a numbered list AND a popup asking for
    repository and language — two questions, two shapes, one turn. The preface
    says to ask the field "in the same single dialog", which a list too long
    for the picker cannot join."""
    nine = [{"text": f"Group {i}", "value": f"g{i}"} for i in range(9)]
    action = chat_tool._build_next_action(_repo_language_pending(nine))
    assert action["render"] == "text_list"
    assert "the user's own project" not in action["instruction"]
    assert "same single dialog" not in action["instruction"]


def test_a_short_list_still_rides_along_with_the_project_context_dialog():
    """Four or fewer can share the dialog, which is the whole point of it."""
    four = [{"text": f"Group {i}", "value": f"g{i}"} for i in range(4)]
    action = chat_tool._build_next_action(_repo_language_pending(four))
    assert action["render"] == "pills"
    assert "the user's own project" in action["instruction"]
    assert "same single dialog" in action["instruction"]
