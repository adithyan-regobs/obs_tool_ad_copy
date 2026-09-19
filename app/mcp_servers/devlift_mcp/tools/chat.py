"""chat tool — single conversational entry point that proxies user messages
to chatbot-POC's /chat endpoint.

Flow:
    1. First call from the LLM: ticket_code is omitted. MCP generates a fresh
       ticket_code, mints an internal JWT, calls chatbot, and returns the
       response with the new ticket_code embedded.
    2. Subsequent calls: LLM passes ticket_code back. MCP forwards.
    3. When chatbot's response carries `isReady: true`, `next_action.type`
       tells the LLM which tool follows. A resource form (S3, SQS, database)
       gets `confirm_deployment`: the LLM shows the collected values, asks
       the user to confirm, and only then calls
       `trigger_resource_deployment(ticket_code=...)`. A service form (EKS)
       gets `create_service_and_save_draft`, called right away: it saves a
       draft and deploys nothing.
"""

import logging
import re
import secrets
from typing import Optional

import httpx

from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt
from app.mcp_servers.devlift_mcp.auth import get_auth_context
from app.mcp_servers.devlift_mcp import config_templates
from app.mcp_servers.devlift_mcp.chatbot_client import (
    cache_chatbot_result,
    cache_template_applied,
    get_template_applied,
    post_chat,
)
from app.mcp_servers.devlift_mcp.dispatcher import _find_redis_service_entry, _section_action

logger = logging.getLogger(__name__)


def _new_ticket_code(user_code: str) -> str:
    return f"mcp-{user_code}-{secrets.token_hex(4)}"


# Claude Code's AskUserQuestion tool schema caps option count at 4. Lists
# longer than this must be rendered as plain text or the LLM silently
# truncates to 4 and the user loses access to the remaining choices.
_ASK_USER_QUESTION_MAX_OPTIONS = 4


def _is_service_form_result(response: dict) -> bool:
    """True when the chatbot's result is a service deployment (EKS/ECS form).

    Resource forms resolve to `attribute_parameters` + `placement_parameters`;
    service forms resolve to `create_service` + `service_config` instead. The
    two shapes are consumed by different tools, so the caller needs to know
    which one it is holding.
    """
    return bool(response.get("service_config")) and bool(response.get("create_service"))


# Supplying a value you already hold is NOT the same act as asking the user an
# extra question, and every directive below used to forbid both in one breath
# ("do not preempt other fields"). That wording was written to stop the model
# inventing questions; it also stopped the one flow whose whole purpose is to
# supply known values — building a service from another's configuration, which
# then asked the user for every field it had already read from the template.
# The two rules are separated here: what you ASK is limited to the field named;
# what you SEND is limited only by what you actually know.
_KNOWN_VALUES_CLAUSE = (
    " Separately from what you ask: if you ALREADY HOLD values for other fields "
    "— copying another service's configuration, or the user named several at "
    "once — put them in this SAME chat(ticket_code=..., answers={field_id: "
    "value, ...}) call rather than waiting to be asked for each one. That is "
    "supplying what you know, not preempting, and it is always allowed. "
    "`missing_fields` says what is still open. Send only values you actually "
    "have — never a guess — and keep a field whose options depend on another "
    "(branches on repository, version on language) for the NEXT call."
)


def _last_optional_fields_clause(response: dict, template_accepted: bool = False) -> str:
    """Fires when NOTHING required is left and only optional fields remain.

    Silent once a language template has been accepted. The template review is
    where the user is shown the whole non-required configuration and agrees to
    it, so asking about those fields again afterwards asks a question they
    already answered — and the answer they gave was "all of it". The concern
    below is that a field is never decided unseen; the review is where they
    are seen.

    The form will not report isReady until each optional field is answered or
    skipped, and the quickest way to that is to skip them all — which is what
    the copy flow's instructions used to say outright. So a service was saved
    with `other_paths` and `build_args` closed out on the user's behalf, having
    never been shown either.

    "Optional" means the form does not demand it, not that it does not matter.
    `build_args` is where CONFIG_ENV lives, and a Go service with
    `go_use_aws_secrets` on whose build args were skipped fails its image build
    three steps later; `other_paths` decides what starts a build at all. The
    user has to see them.
    """
    if template_accepted:
        return ""
    missing = response.get("missing_fields") or {}
    required = [f for f in (missing.get("required") or [])]
    optional = [f for f in (missing.get("optional") or [])]
    if required or not optional:
        return ""
    names = ", ".join(optional)
    return (
        f" These are the LAST fields ({names}) and every one is OPTIONAL, so "
        f"the form will not finish until each is answered or skipped. ASK THE "
        f"USER about them — together, in ONE AskUserQuestion, one question per "
        f"field, each offering its Skip choice — then send what they filled in "
        f"`answers` and ONLY what they chose to skip in `skip`. Do NOT skip any "
        f"of them yourself to reach isReady: optional means the form does not "
        f"demand it, not that it does not matter (build_args is where "
        f"CONFIG_ENV lives, other_paths decides what triggers a build), and a "
        f"field the user never saw is a decision they never made."
    )


def _project_context_preface(response: dict) -> str:
    """Offer the working directory's repository and language, once, up front.

    This has to travel on `next_action` rather than as prose in the server
    instructions: every asking directive below tells the LLM to ask for ONE
    field and not to preempt others, and that wins against a section it read
    at session start. So the directive that forbids extra questions is also
    the one that has to grant this exception.

    The gate is what the form still WANTS, not which form it is: an ordinary
    chat turn carries no `form_id`, and "this form is asking for a repository
    and a language, and the user is sitting in a project that has both" is the
    condition that actually matters. Only the service forms have those fields,
    so resource forms never see this. It stops as soon as either is answered.
    """
    collected = response.get("collected_data") or {}
    if collected.get("repository") or collected.get("language"):
        return ""
    missing = response.get("missing_fields") or {}
    pending = set(missing.get("required") or []) | set(missing.get("optional") or [])
    if not {"repository", "language"} & pending:
        return ""
    return (
        "FIRST, the user's own project — they are almost certainly sitting in "
        "the repository this service is for, and only YOU can see it. Read "
        "`git remote get-url origin` (reduce to `owner/repo`, drop `.git`) and "
        "look for go.mod / pom.xml / build.gradle / package.json / "
        "pyproject.toml / requirements.txt in the project root AND one level "
        "down. Then ask the field(s) below TOGETHER WITH Repository and "
        "Language in ONE AskUserQuestion — each offering what you detected as "
        "the first option and 'choose from the list instead' as the second, "
        "and saying where the fact came from ('go.mod says Go 1.24'). If they "
        "want the list, LEAVE `repository` OUT of the answers call and say the "
        "form will offer it shortly: the connectable repositories are fetched "
        "by the chatbot when it reaches that field, and until then no list "
        "exists here. NEVER enumerate repositories yourself — not `gh repo "
        "list`, not `git`, not anything on GitHub. DevLift can only use "
        "repositories connected to this tenant, which is a SMALLER set than "
        "the org: naming one from any other source offers the user something "
        "that will be refused. Send it all in one "
        "chat(ticket_code=<same>, answers={...}) call, in which EVERY entry is "
        "that field's exact option `value` ('Stage') — never a sentence and "
        "never 'change environment to Stage', which is refused. ONE question "
        "to the "
        "user names the language and its version together ('Python 3.12'), "
        "but they are TWO fields and `language` takes the bare name ONLY — "
        "Go, Java Gradle, Java Maven, Node.js or Python. So send "
        "answers={'repository': 'owner/repo', 'language': 'Python'} and hold "
        "the version back: `version` goes in the NEXT call, with `branches`, "
        "because its options only exist once the language is chosen. Sending "
        "'Python 3.12' as `language` is refused. Detected nothing, or "
        "the signals disagree? Leave that question out and just say so. NEVER "
        "default the branch: it is asked later, once the repository is known. "
        "Offer this ONCE — if you already did in this conversation, skip it "
        "and ask only the field(s) below. Full rules: THE USER'S CURRENT "
        "PROJECT in the server instructions.\n\nTHEN: "
    )


#: Fields of `kong_route_form` that hold a Kong route path.
_KONG_PATH_FIELDS = ("paths", "remove_paths", "edit_paths")

#: Plain-path placeholder syntax: `/:id`, `{id}`, `/*`. Legal in a URL template,
#: literal text in a regex. `{1,10}` is a quantifier, so a brace run starting
#: with a digit is left alone.
_PLAIN_PLACEHOLDER = re.compile(r"/:[A-Za-z_]|\{[A-Za-z_][A-Za-z0-9_]*\}|/\*")


def _plain_source(text: str) -> str:
    """The plain path hiding inside an anchored one that was never compiled.

    `compile_route_path` is idempotent and treats anything starting with `~` as
    already done — which is right for `~/api/v1/users$` and wrong for
    `~/api/v1/:id$`, where the `~` and `$` were bolted on by a caller that did
    not compile the middle. Those are the paths worth correcting, so the wrapper
    is peeled off first and the real compile runs on what is left.

    Only peels when a placeholder is actually present: stripping and recompiling
    a genuine regex is the lossy round trip the Gateway tab removed.
    """
    if not _PLAIN_PLACEHOLDER.search(text):
        return text
    inner = text[1:] if text.startswith("~") else text
    inner = inner[:-1] if inner.endswith("$") else inner
    return inner or text


def _kong_path_candidates(value) -> list[str]:
    """The individual path strings inside one rejected `invalid_fields` value.

    An array field carries a list, a key_value field a dict or [{name, value}],
    and a single item arrives bare — the rejection says which field, not which
    shape, so all four are unwrapped here.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(v) for pair in value.items() for v in pair]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, dict):
                out += [str(item.get(k)) for k in ("name", "value") if item.get(k)]
            elif item:
                out.append(str(item))
        return out
    return []


def _kong_path_suggestions(invalid: list) -> str:
    """Offer Kong's spelling of a path the user typed plainly — never apply it.

    The Gateway tab takes the regex EXACTLY as it will be stored
    (`pathFormatError` in GatewayContentV4.tsx): it stopped accepting plain
    paths because the compiled result was not what the user typed and the
    decompile/recompile round trip is lossy. Chat has to hold the same
    contract, or one route reads `~/minis/(?<proxy>.*)$` in the UI and
    `~/minis/(?<rest>.+)$` here.

    So the compiler is used only to SHOW the user what their plain path would
    become. They confirm it, and the string they confirm is the string that is
    stored — the same deal the UI gives them. Nothing is rewritten on their
    behalf, and the model is told not to invent the conversion itself: it gets
    `/files/:id` -> `~/files/(?<id>[^/]+)$` wrong, which is precisely the kind
    of silent mangling this whole rule exists to prevent.
    """
    from app.utils.kong_path_compiler import compile_route_path

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in invalid or []:
        if not isinstance(entry, dict) or entry.get("field_id") not in _KONG_PATH_FIELDS:
            continue
        for raw in _kong_path_candidates(entry.get("value")):
            text = (raw or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            try:
                compiled = compile_route_path(_plain_source(text))
            except ValueError:
                continue
            if compiled != text:
                pairs.append((text, compiled))

    if not pairs:
        return ""
    shown = "; ".join(f"'{raw}' -> '{fixed}'" for raw, fixed in pairs)
    plural = "PATHS WERE" if len(pairs) > 1 else "PATH WAS"
    return (
        f" THIS OVERRIDES ANY FIELD NAMED ABOVE: {len(pairs)} {plural} REJECTED "
        "FOR FORMAT, and that is what to ask about now — not the next field the "
        "form happens to be on. A message may carry several paths and the form "
        "takes them one by one, so the others may well have been ACCEPTED; the "
        "chatbot's `message` says which, so surface it rather than implying the "
        "whole message failed or that all of it landed. devlift has worked out "
        f"what the user most likely meant: {shown}. Show that mapping, say Kong "
        "needs the regex form (starts '~/', ends '$') and that this is the same "
        "input the Gateway tab takes, and ask them to confirm or correct it. "
        "Send back ONLY the exact string they confirm, and ONLY the paths still "
        "missing — re-sending an accepted one is refused as a duplicate. Do NOT "
        "convert a path yourself and do NOT assume the suggestion is right, "
        "because what they confirm is stored verbatim and is what Kong will "
        "match on."
    )


#: AskUserQuestion shows four choices, and one of them has to be Skip.
_TAG_CHOICE_LIMIT = 3

#: The card-level fields — the ones whose real options only exist once the
#: method, the auth and the PATH are known.
_CARD_OPTION_FIELDS = ("tag", "regex_priority", "edit_paths", "remove_paths")

#: Card fields that are closed out WITHOUT a question, in the order the form
#: reaches them. Both are optional, both have a right answer while nothing
#: contests them, and each was costing a dialog to be told to leave it alone:
#: a priority of 0 (a real clash is caught at save, with the blocking group
#: named), and plugins, whose only selectable entry is User ID Injection —
#: JWT follows the Auth answer by itself. Plugins is the one with no backstop,
#: so it is not dropped: it comes back as a row on the save confirmation,
#: where the user is answering a question anyway.
_CARD_SILENT_OPTIONALS = ("regex_priority", "plugins")


def _silent_optionals(response: dict) -> list[str]:
    """Which of them are still open, so ONE `skip` closes the lot.

    Skipping them one question at a time is two chatbot round trips for two
    non-events. The chatbot asks `regex_priority` first, and plugins is still
    unanswered then — so the priority's own skip carries it. A CLASH never
    reaches this: that branch asks for a number and closes nothing out.
    """
    optional = (response.get("missing_fields") or {}).get("optional") or []
    silent = [f for f in _CARD_SILENT_OPTIONALS if f in optional]
    if _is_plugin_card(response):
        # The plugins ARE the card. Closing them out silently would save a
        # queue row that changes nothing and report it as done.
        silent = [f for f in silent if f != "plugins"]
    return silent


async def _gateway_cards_now(auth_ctx, group: dict):
    """The service's live route cards, read at the moment they are needed.

    Deliberately read HERE rather than carried from the start of the session.
    The card list is only useful once the method, the auth and the path are
    settled — which is exactly the turn these questions are asked — and by then
    a list captured three turns ago may already be wrong: another user's save
    lands in the same gateway. Reading late is both simpler (no cache to seed,
    expire or invalidate) and correct.

    Returns None when anything is missing or fails; every caller degrades to the
    question it would have asked anyway, because a suggestion is a convenience
    and must never cost the user their turn.
    """
    from app.core.enum import EnvironmentEnum
    from app.db.session import AsyncSessionLocal
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp import service_payloads as sp
    from app.repository.service_config_repository import ServiceConfigRepository

    service_mst_code = (group.get("service_mst_code") or "").strip()
    geo = (group.get("geo_loc_mst_code") or "").strip()
    raw_env = (group.get("environment") or "").strip()
    if not (service_mst_code and geo and raw_env):
        return None
    try:
        environment = EnvironmentEnum(raw_env)
    except ValueError:
        return None

    try:
        async with AsyncSessionLocal() as db:
            config = await ServiceConfigRepository(db).get_by_tenant_service_env_geo_loc(
                auth_ctx.tenant_code, service_mst_code, environment.value, geo
            )
        if config is None:
            return None
        jwt_token = mint_internal_jwt(auth_ctx)
        if not jwt_token:
            return None
        state = await obs_tool_client.get_gateway_state(
            jwt_token=jwt_token, service_config_code=config.code
        )
        return sp.summarize_gateway_state(state)
    except Exception:
        logger.exception("devlift_mcp chat: gateway card lookup failed (non-fatal)")
        return None


def _tag_action(action: dict, group: dict, cards: list) -> dict:
    """The tag question, offering the groups this card could actually join.

    A route group is identified by (tag, http_method) — auth is NOT part of that
    identity, so a tag already held by a group with the OTHER auth is refused at
    save (`GatewayConflict`). Filtering the offer by method AND auth means the
    user is never shown a choice that cannot work; the same-method tags still
    count as TAKEN, so a fresh `<service>-tagN` steps over them.
    """
    from app.mcp_servers.devlift_mcp import service_payloads as sp

    service_name = (group.get("service_name") or group.get("service_mst_code") or "").strip()
    method = (group.get("http_method") or "").strip().upper()
    secured = bool(group.get("secured"))
    if not service_name or not method:
        return action

    same_method = [c for c in cards if (c.get("http_method") or "").upper() == method]
    taken = [c["route_group_key"] for c in same_method if c.get("route_group_key")]
    joinable = [
        c["route_group_key"] for c in same_method
        if c.get("route_group_key") and bool(c.get("secured")) == secured
    ]

    choices = sp.gateway_tag_choices(
        service_name, joinable, limit=_TAG_CHOICE_LIMIT, taken_tags=taken
    )
    if not choices:
        return action

    joinable_lower = {t.lower() for t in joinable}
    paths_of = {
        (c.get("route_group_key") or "").lower(): [
            p.get("route_path") for p in (c.get("paths") or []) if p.get("route_path")
        ]
        for c in same_method
    }
    options = []
    for tag in choices:
        if tag.lower() in joinable_lower:
            live = paths_of.get(tag.lower()) or []
            shown = ", ".join(live[:2]) + (", ..." if len(live) > 2 else "")
            description = f"existing group — {shown}" if shown else "existing group"
        else:
            description = "creates a new group"
        options.append({"text": tag, "value": tag, "description": description})
    options.append({"text": "Skip", "value": SKIP_TAG_VALUE, "skip": True})

    action = {**action, "options": options, "render": "pills"}
    action["instruction"] = (
        "Ask which 'Tag (route group)' these routes belong to — that spelling, "
        "the one the Gateway tab and the rest of this form use — offering the "
        "options below via AskUserQuestion and using each option's "
        "`description` as its description — the first is the default the "
        "Gateway tab would preload, and a tag not already in use CREATES a new "
        "group. Only groups with this method AND this auth are offered, because "
        "a tag is one group per method and reusing one under the other auth is "
        "refused at save. Treat Other as real: any name the user types is "
        "valid. Send it with chat(ticket_code=<same>, answers={'tag': '<tag>'}), "
        "or chat(ticket_code=<same>, skip=['tag']) on Skip — skipping lets "
        "devlift pick, which for a first card is the service name itself. Ask "
        "nothing else this turn."
    )
    return action


#: The plugins a user can actually pick, mirroring `plugins.dropdown_options`
#: in kong_route_form.json. JWT is NOT here: it follows the card's auth and is
#: added by the translator. Kept as a constant because the chatbot sends the
#: form's catalogue only on the turn it happens to be asking for plugins, and
#: this dialog asks for them a turn earlier. A test holds the two in step.
_SELECTABLE_PLUGINS = ("User ID Injection",)

#: Its own row on the plugin question: a user who opened the flow by mistake
#: needs a way out that is not Esc, and "no change" is a real answer.
_NO_PLUGIN_CHANGE = "Leave unchanged"


#: The card intents that can only ever touch a route group that ALREADY
#: exists. Every one of them needs the group settled BEFORE its own question
#: means anything: you cannot pick a path to remove or rename until you know
#: which group's paths you are looking at, and a plugin goes onto a group.
#:
#: The form asks for them the long way round — method, then auth, then tag,
#: each from nothing — and only then a path, typed from memory. But a group IS
#: (tag, http_method) and carries its own auth, so ONE row of the live gateway
#: answers all three, and the paths follow from it. 'Add' is deliberately not
#: here: it may be creating the group, so there is nothing to pick from.
_LIVE_GROUP_INTENTS = ("Remove", "Rename", "Plugins")


def _card_intent(response: dict) -> str:
    """The gateway card's `route_action`, normalised, or ''."""
    if not _is_gateway_form_result(response):
        return ""
    raw = str((response.get("collected_data") or {}).get("route_action") or "").strip().lower()
    return next((a for a in _LIVE_GROUP_INTENTS + ("Add",) if a.lower() == raw), "")


def _is_plugin_card(response: dict) -> bool:
    """A gateway card whose whole purpose is the group's plugins.

    Set by `route_action: Plugins` — the intent for "add user id injection to
    the orders group", a change that touches no path at all. It matters in
    three places: this card's dialog, and the two skips that close plugins out
    on an ordinary card, which must never fire on the card that exists FOR
    them.
    """
    return _card_intent(response) == "Plugins"


def _card_group_is_live(group: dict, cards: list) -> bool:
    """Does this card point at a route group that ACTUALLY EXISTS?

    Not "are tag and method filled in". They are filled in the moment a card
    is started, and they SURVIVE a change of intent: a user who added a path
    to `jp-test-service · GET` and then said "delete a kong gateway path" got
    a remove question about GET, on a group they never chose for it, without
    ever being asked a method. The values were a previous card's answers, and
    nothing about them said so.

    Matching against the live gateway says it without any history to keep. A
    Remove, a Rename and a Plugins card can only act on a group that is
    already there, so a (tag, method, auth) triple that matches nothing is not
    a settled group — it is a leftover, and the user is asked to pick.

    Auth counts. A group is (tag, method) and its auth is a property of it, so
    a public card aimed at a JWT group matches no group at all: the save
    refuses it with `GatewayConflict`, and the LLM's instinct — quietly flip
    the card's auth to fit — decides for the user something they never saw.
    """
    tag = (group.get("route_group_key") or "").strip().lower()
    method = (group.get("http_method") or "").strip().upper()
    if not (tag and method):
        return False
    secured = bool(group.get("secured"))
    return any(
        (c.get("route_group_key") or "").strip().lower() == tag
        and (c.get("http_method") or "").strip().upper() == method
        and bool(c.get("secured")) == secured
        for c in cards
    )


def _live_group_rows(cards: list, want_tag: str = "") -> list[dict]:
    """The service's route groups as rows that answer tag, method and auth.

    A busy service has more groups than a picker can hold and every one of
    them looks alike — `<tag> · <METHOD>`, six methods deep. A tag the user
    already named narrows them to that tag's methods, which is the usual case
    ("remove it from the orders group"); a tag matching nothing is ignored
    rather than emptying the list.
    """
    from app.mcp_servers.devlift_mcp.service_payloads import JWT_PLUGIN

    groups = []
    for card in cards:
        tag = (card.get("route_group_key") or "").strip()
        method = (card.get("http_method") or "").strip().upper()
        if not tag or not method:
            continue
        secured = bool(card.get("secured"))
        extras = [p for p in (card.get("plugins") or []) if p and p != JWT_PLUGIN]
        paths = [p.get("route_path") for p in (card.get("paths") or []) if p.get("route_path")]
        # The description says what the picker is FOR: a plugin change wants
        # to see the plugins the group already has (that is how the user sees
        # the change is unnecessary), a remove or a rename wants to see it has
        # paths at all. Both would be a row of facts nobody asked for.
        groups.append({
            "text": f"{tag} · {method}",
            "tag": tag,
            "method": method,
            "secured": "Auth - JWT required" if secured else "No Auth - public",
            "paths": paths,
            "auth_word": "Auth" if secured else "No Auth",
            "plugins": extras,
        })
    wanted = (want_tag or "").strip().lower()
    if wanted:
        groups = [g for g in groups if g["tag"].lower() == wanted] or groups
    return groups


def _named_paths(group: dict, intent: str) -> list[str]:
    """Paths the user has already named on this card, before any group is set.

    "remove ~/api/v1/orders$ from goms" fills the path and leaves the group
    open, which is the wrong way round — but it is the way people say it, and
    the path is the strongest hint available about which group they mean.
    """
    if intent == "Remove":
        return [str(p).strip() for p in (group.get("remove_paths") or []) if str(p).strip()]
    if intent == "Rename":
        pairs = group.get("edit_paths") or {}
        if isinstance(pairs, dict):
            return [str(k).strip() for k in pairs if str(k).strip()]
        return [str(e.get("name") or "").strip() for e in pairs
                if isinstance(e, dict) and str(e.get("name") or "").strip()]
    return []


def _group_pick_action(cards: list, want_tag: str, intent: str,
                      named_paths: tuple = (), have_method: str = "") -> dict | None:
    """Settle WHICH route group a Remove or Rename is about, before its paths.

    One question per axis, narrowest first: METHOD, then TAG, then the paths.
    A combined `<tag> · <METHOD>` pick was one step fewer and does not survive
    a real service — tags times methods is a list nobody reads, and the two
    axes are exactly what makes it long. Split, each question is bounded:
    at most six methods, and the tags of ONE method.

    Degenerate questions are skipped rather than shown. One method on the
    service, or one tag on the chosen method, is not a choice — a picker with
    a single row is a question the user cannot answer wrongly and still has to
    read.

    The narrowing happens before any of it: a tag the user named, and then the
    paths they named, which rule out every group that does not carry them.
    """
    groups = _live_group_rows(cards, want_tag)
    if not groups:
        return None

    # A path the user named narrows this far better than anything else. The
    # SAME path can sit on several methods, and on several groups of one
    # method — that is what a route group is FOR — so naming one does not pick
    # a group, but it rules out every group without it, and often leaves one.
    wanted = {p for p in named_paths if p}
    holders = [g for g in groups if wanted & set(g["paths"])] if wanted else []
    if holders:
        groups = holders

    for g in groups:
        count = len(g["paths"])
        base = f"{g['auth_word']} · {count} path" + ("s" if count != 1 else "")
        hit = sorted(wanted & set(g["paths"]))
        g["description"] = base + (" · has " + ", ".join(hit) if hit else "")

    what = "remove paths from" if intent == "Remove" else "rename a path in"
    methods: list[str] = list(dict.fromkeys(g["method"] for g in groups))
    chosen = (have_method or "").strip().upper()
    # Every method the SERVICE has groups on, not just the ones left after
    # narrowing. It decides whether "did you mean another method?" is a real
    # question or a dead end — and offered on a service with one method it is
    # a dead end that loops: the user picks it, and the only answer available
    # is the method they were already on.
    service_methods = {
        (c.get("http_method") or "").strip().upper()
        for c in cards if (c.get("http_method") or "").strip()
    }

    # ── step 1: the method ───────────────────────────────────────────────────
    if len(methods) > 1 and chosen not in methods:
        rows = []
        for m in methods:
            on_method = [g for g in groups if g["method"] == m]
            tags = len(on_method)
            paths = sum(len(g["paths"]) for g in on_method)
            row = {
                "text": m,
                "method": m,
                "description": f"{tags} route group" + ("s" if tags != 1 else "")
                               + f" · {paths} path" + ("s" if paths != 1 else ""),
            }
            # One group on this method means picking the method HAS picked the
            # group, so the row carries it and the tag question never happens.
            # Asking it anyway is a dialog of one row: a question whose answer
            # was decided by the previous one.
            if tags == 1:
                row["tag"] = on_method[0]["tag"]
                row["secured"] = on_method[0]["secured"]
            rows.append(row)
        return {
            "type": "ask_route_method",
            "methods": rows,
            "render": "pills",
            "instruction": (
                f"Which HTTP METHOD does this card {what}? Ask ONLY that — not "
                f"the tag, not the auth, not a path. A route group is (tag, "
                f"method), so the method halves the list before the tag is "
                f"even shown, and asking them together is how a service with "
                f"six methods and four tags becomes a picker of "
                f"{len(groups)} rows.\n\n"
                f"ONE AskUserQuestion, one question 'Method', the entries of "
                f"`methods` as its choices — each `text` as the label and its "
                f"`description` (what is actually on that method) as the "
                f"description. These are the ONLY methods this service has "
                f"groups on; a method absent from the list has nothing to "
                f"{intent.lower()}.\n\n"
                f"Then ONE call. When the entry they picked carries a `tag` "
                f"— that method has exactly one route group, so choosing it "
                f"chose the group — send all three at once: "
                f"chat(ticket_code=<same>, answers={{'method': <method>, "
                f"'tag': <tag>, 'secured': <secured>}}), and do NOT then ask "
                f"which group they meant. Otherwise send only "
                f"answers={{'method': <method>}} and the tag question "
                f"follows. The paths come after either way."
            ),
        }

    if chosen in methods:
        groups = [g for g in groups if g["method"] == chosen]

    # ── step 2: the tag, among that method's groups only ─────────────────────
    only = groups[0] if len(groups) == 1 else None
    method_word = only["method"] if only else (chosen or methods[0])
    long_list = len(groups) > _ASK_USER_QUESTION_MAX_OPTIONS
    sole = len(service_methods) <= 1
    if only:
        how = (
            f"Exactly ONE route group is left: {only['text']} — "
            f"{only['description']}. Do NOT ask about it, with a dialog or "
            f"otherwise: a question with one answer is a turn spent reading a "
            f"decision that was already made. Say in ONE line which group you "
            f"are working in, method and auth included, send it in this same "
            f"turn, and go straight on to the paths."
            + (
                f" It is the only route group this service has, on its only "
                f"method: say THAT, and offer no alternative card and no "
                f"'different method' — there is no other, and a choice whose "
                f"only outcome is the question again is worse than no choice."
                if sole else ""
            )
        )
    elif long_list:
        how = (
            f"There are {len(groups)} on {method_word} — more than "
            f"AskUserQuestion's cap of {_ASK_USER_QUESTION_MAX_OPTIONS}, which "
            f"would silently drop the rest. List every entry of `groups` as a "
            f"NUMBERED list instead, its `text` then its `description`, under "
            f"the heading 'Tag (route group)', and ask them to reply with the "
            f"number."
        )
    else:
        how = (
            f"ONE AskUserQuestion, one question 'Tag (route group)', the "
            f"{len(groups)} entries of `groups` as its choices — each one's "
            f"`text` as the label and its `description` as the description."
        )

    named_clause = ""
    if holders:
        listed = ", ".join(sorted(wanted))
        named_clause = (
            f"\n\nThe user named {listed}. Every group without that path has "
            f"been ruled out already — naming a path does NOT pick a group, "
            f"because the same path can sit on several of them."
        )

    return {
        "type": "ask_route_group",
        "groups": groups,
        "method": method_word,
        "render": "text_list" if long_list else "pills",
        "instruction": (
            f"Which route group on {method_word} does this card {what}? Ask "
            f"ONLY that. Do NOT ask for an auth mode: a group carries its own, "
            f"so the entry they pick answers it. Say {method_word} in the "
            f"question — the method is settled and they should see which one "
            f"they are inside."
            + (
                " If they want a different method, ask the method question "
                "instead of guessing."
                if not sole else
                f" {method_word} is the ONLY method this service has routes "
                f"on, so do not offer to change it and do not ask which method "
                f"they meant: there is nowhere else to go, and the answer to "
                f"that question is this same card."
            )
            + f"{named_clause}\n\n"
            f"{how}\n\n"
            f"Then ONE call, taking the values off that entry: "
            f"chat(ticket_code=<same>, answers={{'tag': <tag>, 'method': "
            f"<method>, 'secured': <secured>}}) — `secured` exactly as the "
            f"entry spells it. The paths come next, from THAT group, and are "
            f"not asked here."
        ),
    }


def _plugin_card_action(cards: list, want_tag: str = "") -> dict | None:
    """The whole plugin change in one dialog: which group, and which plugin.

    A route group IS (tag, http_method), and its auth is a property of the
    group rather than a question — so ONE pick settles tag, method and secured
    together, and the card needs nothing else. Paths are not asked (there are
    none), and the priority is not asked (nothing moved to clash with).

    The rows are the service's LIVE groups, which is both possible and
    necessary here: nothing in this dialog is being filtered by an answer
    inside it, and a plugin can only be added to a group that exists.

    A busy service has more groups than a picker can hold, and every one of
    them looks alike — `<tag> · <METHOD>`, six methods deep. Two things keep
    that readable. A tag the user already named NARROWS the rows to that tag's
    methods, which is the usual case ("add it to the orders group"); and a
    list still too long for the picker is printed NUMBERED in the reply
    instead, the same fallback the long dropdowns take, because a picker that
    silently drops rows is worse than a list.

    None when the service has no groups at all — there is nothing to add a
    plugin TO, and the caller degrades to the ordinary question.
    """
    groups = _live_group_rows(cards, want_tag)
    if not groups:
        return None
    for g in groups:
        g["description"] = f"{g['auth_word']} · " + (", ".join(g["plugins"]) or "no extra plugins")

    plugin_rows = [*_SELECTABLE_PLUGINS, _NO_PLUGIN_CHANGE]
    # One row that is a plugin and one that is a way out reads like a picker
    # with something missing, and the user goes hunting for the rest. There is
    # no rest. Saying so costs a clause and stops the hunt — and it stops
    # being said by itself if the catalogue ever grows.
    only_one = (
        f" Say too that {_SELECTABLE_PLUGINS[0]} is the ONLY plugin DevLift "
        f"offers on a route group — JWT is not a choice, it follows the "
        f"card's auth — so nobody goes looking for one that is not there."
        if len(_SELECTABLE_PLUGINS) == 1 else ""
    )
    plugin_question = (
        f"  'Plugin' — exactly these rows: {', '.join(plugin_rows)}. Say in "
        f"the question that a plugin applies to EVERY route in the group, "
        f"including the ones already live in it — that is the part a user "
        f"adding one to an existing group does not expect.{only_one}"
    )
    send = (
        f"taking the last three values off the row they chose: "
        f"chat(ticket_code=<same>, answers={{'tag': <tag>, 'method': "
        f"<method>, 'secured': <secured>, 'plugins': [<the plugin>]}}) — "
        f"`secured` exactly as the entry spells it. On "
        f"'{_NO_PLUGIN_CHANGE}' send nothing: say the card was left alone "
        f"and stop."
    )
    preamble = (
        "This card changes a route group's PLUGINS and nothing else. Do NOT "
        "ask for paths, a method, an auth mode or a priority: a route group "
        "IS a (tag, method) pair and its auth belongs to the group, so the "
        "entry the user picks carries all three. Call it 'Tag (route group)' "
        "— the Gateway tab and the rest of this form say Tag, and a second "
        "name for one thing is how a user ends up looking for a field that "
        "is not there.\n\n"
    )

    if len(groups) <= _ASK_USER_QUESTION_MAX_OPTIONS:
        instruction = (
            f"{preamble}"
            f"ONE AskUserQuestion, TWO questions, nothing else this turn:\n"
            f"  'Tag (route group)' — the entries of `groups`, each one's "
            f"`text` as the label and its `description` as the description. "
            f"The description names the auth and the plugins the group "
            f"ALREADY has, which is how the user sees whether the change is "
            f"even needed.\n"
            f"{plugin_question}\n\n"
            f"Then ONE call, {send}"
        )
    else:
        instruction = (
            f"{preamble}"
            f"There are {len(groups)} route groups — more than "
            f"AskUserQuestion's cap of {_ASK_USER_QUESTION_MAX_OPTIONS}, and "
            f"it would silently drop the rest. Do NOT use it for them. List "
            f"every entry of `groups` in your reply as a NUMBERED list — 1, "
            f"2, 3 … in the order given, one per line, the `text` then its "
            f"`description` — under the heading 'Tag (route group)', and ask "
            f"the user to reply with the number, naming the plugin in the "
            f"same line if they already know it "
            f"({', '.join(_SELECTABLE_PLUGINS)} is all there is). Add no "
            f"entry that is not in `groups`.\n\n"
            f"When they answer with a number, take the entry at that 1-based "
            f"position. If they named the plugin too, send it all at once, "
            f"{send} If they did not, put ONLY the plugin question to them "
            f"via AskUserQuestion:\n"
            f"{plugin_question}"
        )

    return {
        "type": "ask_plugin_card",
        "groups": groups,
        "plugins": list(_SELECTABLE_PLUGINS),
        "render": "pills" if len(groups) <= _ASK_USER_QUESTION_MAX_OPTIONS else "text_list",
        "instruction": instruction,
    }


#: The two choices of the rename's second question. The new path cannot BE a
#: choice — it is made from the answer to the first question, and both are
#: rendered before either is answered — so the choices are what to do to it.
#: Two, because that is a picker's minimum and because those are the two cases.
_RENAME_PARAMETERISE = "Turn its id into a parameter"
_RENAME_TYPE_IT = "Tell me — I'll type it"


def _group_label(group: dict) -> str:
    """`goms · GET · Auth` — which group a path question is about.

    A path is only unique WITHIN a group: the same path can sit on several
    methods, and on several groups of one method. So a question that names
    only the path is ambiguous on screen even when the code behind it is not,
    and a user who has two groups in mind cannot tell which one they are
    about to change.
    """
    tag = (group.get("route_group_key") or "").strip() or "?"
    method = (group.get("http_method") or "").strip().upper() or "?"
    return f"{tag} · {method} · " + ("Auth" if group.get("secured") else "No Auth")


def _group_live_paths(group: dict, cards: list) -> list[str]:
    """The paths of the ONE group this card is about. Another group's paths
    cannot be removed or renamed from here — the save refuses them, naming the
    group's real ones — so they are never offered."""
    method = (group.get("http_method") or "").strip().upper()
    tag = (group.get("route_group_key") or "").strip().lower()
    for card in cards:
        if (card.get("http_method") or "").strip().upper() != method:
            continue
        if (card.get("route_group_key") or "").strip().lower() != tag:
            continue
        return [p.get("route_path") for p in (card.get("paths") or []) if p.get("route_path")]
    return []


def _remove_action(action: dict, group: dict, cards: list, paths_known: bool = True) -> dict:
    """Which of the group's paths to delete — picked, never typed.

    By the time this is asked the group is settled, so the exact set is known
    and the user chooses from it instead of reproducing a regex from memory.
    It scales the same way the rename does: a picker while the paths fit one,
    a search once they do not, and the full list stays data to match against
    rather than a wall of near-identical regexes on screen.
    """
    live = _group_live_paths(group, cards)
    label = _group_label(group)
    action = {k: v for k, v in action.items() if k not in ("options", "render")}
    action["paths"] = live

    send = (
        "Send them as chat(ticket_code=<same>, answers={'remove_paths': "
        "['<path>', ...]}) — a LIST, each path exactly as it is stored. A "
        "path the group does not have is refused at save, so send only ones "
        "that came from `paths`."
    )
    if not paths_known:
        action["instruction"] = (
            "This session could not read the group's live paths. Ask as PLAIN "
            "TEXT, one line: which path(s) should be removed? Do NOT list or "
            "guess paths from memory — a remembered list is how a user is "
            "sent to delete something that is not there. " + send
        )
        return action
    if not live:
        action["instruction"] = (
            "This route group has no paths, so there is nothing to remove. "
            "Say exactly that and ask what they meant to do instead. Do NOT "
            "ask them to type a path."
        )
        return action

    action["render"] = "pills" if len(live) <= _ASK_USER_QUESTION_MAX_OPTIONS else "search"
    if len(live) <= _ASK_USER_QUESTION_MAX_OPTIONS:
        action["instruction"] = (
            f"ONE AskUserQuestion, one question 'Paths to remove': which of "
            f"{label}'s paths should go? NAME THAT GROUP in the question — a "
            f"path is only unique within one, so 'remove ~/api/v1/orders$' "
            f"does not say which route it removes. Its choices are the {len(live)} "
            f"entries of `paths`, verbatim, as bare labels, and multiSelect "
            f"TRUE — removing several at once is one card, not several. "
            f"Nothing else is asked. " + send
        )
    else:
        action["instruction"] = (
            f"{label} has {len(live)} paths. NAME THAT GROUP in the question: "
            f"a path is only unique within one. Do NOT list the paths and do NOT "
            f"put them in a picker: past {_ASK_USER_QUESTION_MAX_OPTIONS} "
            f"AskUserQuestion drops the rest silently, and a wall of "
            f"near-identical regexes is not something anyone reads. `paths` "
            f"carries all of them for YOU to search — it is data, not a list "
            f"to print.\n"
            f"Ask as PLAIN TEXT, one line: which path(s) do they want to "
            f"remove, naming any part. Match what they type against `paths` "
            f"case-insensitively, as a substring:\n"
            f"  1 match  — say which one you found and ask them to confirm.\n"
            f"  2 to {_ASK_USER_QUESTION_MAX_OPTIONS} — ONE AskUserQuestion, "
            f"those as the choices, verbatim, multiSelect TRUE.\n"
            f"  more     — say how many matched and ask for more of the path. "
            f"Print none of them.\n"
            f"  none     — say nothing matched and ask again. Never offer a "
            f"path that is not in `paths`.\n"
            + send
        )
    return action


def _rename_action(action: dict, group: dict, cards: list, paths_known: bool = True) -> dict:
    """A rename in two steps: WHICH path, then what it becomes.

    Both halves are the user's to give, so neither is one picker. The first
    attempt put them in one dialog and every row came out as half an answer —
    `~/api/v1/orders$ -> ?` with a line underneath saying the row is not
    really the answer. The second listed the group's live paths, which reads
    fine for four and not at all for a hundred: a busy group has more paths
    than anyone will scroll, and dumping them is the same failure in a longer
    form.

    So the first step SCALES. Few paths are a picker. Many are a search: the
    model holds the whole list as data, the user types part of a path, and
    only the matches are ever on screen. The second step is one line of text,
    because a path that does not exist yet cannot be offered as a choice.
    """
    live = _group_live_paths(group, cards)
    label = _group_label(group)
    action = {k: v for k, v in action.items() if k not in ("options", "render")}
    action["paths"] = live

    # Step two never changes: the new path does not exist yet, so there is
    # nothing to offer and nothing to look up. One line, naming the path they
    # picked so they can see what they are changing.
    step_two = (
        "STEP 2 — only once step 1 has an answer, and in a separate turn. Ask "
        "as PLAIN TEXT, one line: what should <the chosen path> become? Name "
        "the path they picked. Say nothing else — no regex rules, no '~/ … $' "
        "reminder. Then send chat(ticket_code=<same>, answers={'edit_paths': "
        "{'<old>': '<new>'}}) — a MAP, the old path exactly as it is stored."
    )

    if not paths_known:
        action["instruction"] = (
            "This session could not read the group's live paths, so there is "
            "nothing to pick from and nothing to match against.\n\n"
            "STEP 1 — ask as PLAIN TEXT, one line: which path do they want to "
            "rename? Do NOT list or guess paths from memory; a remembered list "
            "is how a user is sent to rename a path that is not there.\n\n"
            + step_two
        )
        return action

    if not live:
        action["instruction"] = (
            "This route group has no paths, so there is nothing to rename. "
            "Say exactly that, ask whether they meant to ADD one instead, and "
            "send their answer as the next `chat` message. Do NOT ask them to "
            "type a rename."
        )
        return action

    # ── the group's paths fit a picker: ONE dialog, TWO tabs ────────────────
    if live and len(live) <= _ASK_USER_QUESTION_MAX_OPTIONS:
        action["render"] = "pills"
        action["instruction"] = (
            f"ONE AskUserQuestion, TWO questions, both in that one dialog:\n"
            f"  'Old path' — which path of {label} is being renamed? Choices "
            f"are the {len(live)} entries of `paths`, verbatim. NAME THAT "
            f"GROUP in the question: a path is only unique within one.\n"
            f"  'New path' — what it becomes. Two choices: "
            f"'{_RENAME_PARAMETERISE}' and '{_RENAME_TYPE_IT}'. The new path "
            f"itself cannot be a choice: it is made from the answer to the "
            f"other question, and both are rendered before either is "
            f"answered.\n\n"
            f"On '{_RENAME_PARAMETERISE}', rewrite the chosen path with its "
            f"hardcoded id segment as '(?<id>[^/]+)' — a route pinned to one "
            f"record's id matches that record and nothing else, which is why "
            f"renames exist. If the path has no such segment, or already has "
            f"an `id` group (PCRE refuses two of one name), say so in one "
            f"line and ask what it should become. Same on '{_RENAME_TYPE_IT}' "
            f"and on Other.\n"
            f"Send it as chat(ticket_code=<same>, answers={{'edit_paths': "
            f"{{'<old>': '<new>'}}}}) — a MAP, the old path exactly as it is "
            f"stored."
        )
        return action

    # Nothing to work out from any of them, or too many to show: the two
    # steps stay two, because the new path still cannot be offered until the
    # old one is known.
    action["render"] = "pills" if len(live) <= _ASK_USER_QUESTION_MAX_OPTIONS else "search"
    if len(live) <= _ASK_USER_QUESTION_MAX_OPTIONS:
        step_one = (
            f"STEP 1 — ONE AskUserQuestion, one question 'Old path': which "
            f"path of {label} is being renamed? NAME THAT GROUP in the "
            f"question — a path is only unique within one. Its choices are "
            f"the {len(live)} entries of `paths`, verbatim, as bare labels. "
            f"Ask nothing else in it — not the new path, which step 2 takes."
        )
    else:
        step_one = (
            f"STEP 1 — {label} has {len(live)} paths; name that group in the "
            f"question, because a path is only unique within one. Do NOT list them and "
            f"do NOT put them in a picker: past "
            f"{_ASK_USER_QUESTION_MAX_OPTIONS} AskUserQuestion drops the rest "
            f"silently, and a wall of near-identical regexes is not something "
            f"anyone reads. `paths` carries all of them for YOU to search — it "
            f"is data, not a list to print.\n"
            f"Ask as PLAIN TEXT, one line: which path do they want to rename, "
            f"naming any part of it. Then match what they type against `paths` "
            f"case-insensitively, as a substring:\n"
            f"  1 match  — say which one you found and ask them to confirm.\n"
            f"  2 to {_ASK_USER_QUESTION_MAX_OPTIONS} — ONE AskUserQuestion, "
            f"those as the choices, verbatim.\n"
            f"  more     — say how many matched and ask for more of the path. "
            f"Print none of them.\n"
            f"  none     — say nothing matched and ask again. Never offer a "
            f"path that is not in `paths`; it is the only set that can be "
            f"renamed, and the save refuses anything else."
        )

    action["instruction"] = f"{step_one}\n\n{step_two}"
    return action


def _priority_action(action: dict, group: dict, cards: list, silent: tuple = ()) -> dict:
    """Priority as a TYPED number, plus the clash it has to settle.

    Two groups of one method may both claim a path — Kong serves the higher
    `regex_priority` and shadows the other, which is how a public `/health`
    group overrides the service's own. What is NOT legal is both at the SAME
    priority: there is no tie-break and which route Kong serves is arbitrary.

    So the question is only ever interesting against the paths just entered.
    When they clash, the other group and ITS priority are named — without that
    number the user is picking blind, and "pick something else" is not a number.
    """
    from app.domain.validators.kong_path_overlap_validator import KongPathOverlapValidator

    action = {k: v for k, v in action.items() if k not in ("options", "render")}
    method = (group.get("http_method") or "").strip().upper()
    tag = (group.get("route_group_key") or "").strip().lower()
    incoming = {
        key
        for path in (group.get("paths") or [])
        if path
        for key in KongPathOverlapValidator.path_keys(str(path))
    }

    clashes = []
    for card in cards:
        if (card.get("http_method") or "").upper() != method:
            continue
        if (card.get("route_group_key") or "").strip().lower() == tag:
            continue  # the group we are adding to — its own paths are not a clash
        for p in card.get("paths") or []:
            route = p.get("route_path")
            if route and incoming & set(KongPathOverlapValidator.path_keys(route)):
                clashes.append((card.get("route_group_key"), int(card.get("regex_priority") or 0), route))
                break

    if clashes:
        detail = "; ".join(
            f"'{route}' is already in group '{other}' at priority {pri}"
            for other, pri, route in clashes
        )
        blocked = sorted({pri for _, pri, _ in clashes})
        action["instruction"] = (
            f"THESE PATHS CLASH: {detail}. Kong serves the HIGHER priority and "
            f"shadows the other; the same priority on both is refused outright, "
            f"because there is no tie-break. Tell the user exactly that, naming "
            f"the other group and its number, then ask them to TYPE a priority "
            f"— above to take over, below to sit behind it — and NOT "
            f"{', '.join(str(p) for p in blocked)}. Free text, no option "
            f"buttons, any whole number. Send it with chat(ticket_code=<same>, "
            f"answers={{'regex_priority': <number>}}). Ask nothing else."
        )
    else:
        action["instruction"] = (
            f"Do NOT put this question to the user. Nothing claims these "
            f"paths, so the priority has nothing to settle and 0 — the "
            f"default — is the answer; asking costs a turn to be told 'leave "
            f"it'. Send chat(ticket_code=<same>, skip={list(silent) or ['regex_priority']}) "
            f"NOW, in this same turn, and when you next speak to the user say "
            f"in one clause that the priority stays 0. These are the only "
            f"fields you close out yourself, and the priority only while it "
            f"is uncontested: if the USER raises it, or a later path clashes, "
            f"it becomes a real question and is asked properly."
        )
    return action


#: What the chatbot treats as "the user pressed Skip" inside an `answers` map
#: (routes.py turns it back into a skip). Used for the tag row seeded into a
#: section, where the client answers every field of the dialog at once.
SECTION_SKIP_VALUE = "__skip__"


def _seed_section_tag_options(response: dict) -> None:
    """Give the `tag` entry of a Route dialog its rows, in place.

    `tag` rides in the same dialog as the method, the auth and the paths it
    belongs to — one dialog for the whole card instead of a follow-up for its
    name. The price is that the rows cannot be the LIVE ones `_tag_action`
    builds: those are the service's route groups filtered by method AND auth,
    and both are being answered in this very dialog. So the rows here are the
    two names that need nothing looked up — the service's own group and a
    fresh `-tag1` — and a tag that turns out to belong to the other auth is
    refused at save rather than never offered. That is the accepted trade.

    `_tag_action` is NOT dead: when `tag` is the only field left unanswered
    the section is not emitted (it needs two), the question falls through to
    the single-field path, and the live groups are read as before.

    Silent when the service name is missing: the model then asks with what it
    holds, which is the behaviour a field with no rows already has.
    """
    section = response.get("section") or {}
    service = ((response.get("gateway_group") or {}).get("service_name") or "").strip()
    if not service:
        return
    for field in section.get("fields") or []:
        if field.get("field_id") != "tag" or field.get("options"):
            continue
        field["options"] = [
            {"text": service, "value": service, "description": "the default"},
            {"text": f"{service}-tag1", "value": f"{service}-tag1",
             "description": "a separate group"},
            {"text": "Skip", "value": SECTION_SKIP_VALUE,
             "description": f"takes the default, or '{service}-open' if public"},
        ]


async def _enrich_card_options(action: dict, response: dict, auth_ctx) -> dict:
    """Give the card-level questions what only the live gateway can tell them.

    The chatbot cannot: a tag's real choices are the SERVICE's route groups and
    a priority's only real constraint is what already claims these paths —
    neither is in the form. One read serves both, taken once the path is known.

    ONLY a plain question is enriched. A `fix_invalid` carries a rejection that
    has to reach the user, and it names whatever field the form moved on to —
    so a rejected PATH arriving while the form sits on `regex_priority` matched
    here and had its message replaced by "type a priority", leaving the user
    with no idea their path had been refused.
    """
    group = response.get("gateway_group") or {}
    if not group:
        return action
    # A Plugins card replaces the question outright rather than enriching one:
    # whatever the form is asking for next (method, auth, tag — it walks them
    # in order), the answer to all of it is one row of the live gateway. Not
    # on a `fix_invalid`, for the reason below.
    intent = _card_intent(response)
    cards: list | None = None
    read_done = False
    if (
        action.get("type") in ("ask_section", "ask_user", "ask_user_text")
        and intent in _LIVE_GROUP_INTENTS
    ):
        cards = await _gateway_cards_now(auth_ctx, group)
        read_done = True
        if cards is not None and not _card_group_is_live(group, cards):
            want_tag = str((response.get("collected_data") or {}).get("tag") or "")
            if intent == "Plugins":
                return _plugin_card_action(cards, want_tag) or action
            return _group_pick_action(
                cards, want_tag, intent, tuple(_named_paths(group, intent)),
                str(group.get("http_method") or ""),
            ) or action
    # `ask_user` AND `ask_user_text`. A field reaches the first only when the
    # chatbot found something to suggest for it — a dropdown, or the Skip pill
    # that optional non-array fields get. `tag` and `regex_priority` have that
    # pill; `remove_paths` and `edit_paths` are arrays and key_values, so they
    # have neither and arrive as plain text. Guarding on `ask_user` alone made
    # every enrichment written for them unreachable: the user was asked for a
    # path in prose, and the live paths the MCP had just read never appeared.
    # `fix_invalid` stays out, for the reason in the docstring.
    if action.get("type") not in ("ask_user", "ask_user_text"):
        return action
    if action.get("field_id") not in _CARD_OPTION_FIELDS:
        return action
    if not read_done:
        cards = await _gateway_cards_now(auth_ctx, group)
    if cards is None:
        # The read is what finds a clash; without it there is none to report.
        # The priority question is not worth a turn on its own either way —
        # and a clash that IS real is refused at save by
        # KongPathOverlapValidator, with the blocking group named. So it still
        # closes itself out. The tag has no such backstop and degrades to the
        # question the model would have asked anyway.
        if action["field_id"] == "regex_priority":
            return _priority_action(action, group, [], tuple(_silent_optionals(response)))
        if action["field_id"] == "edit_paths":
            return _rename_action(action, group, [], paths_known=False)
        if action["field_id"] == "remove_paths":
            return _remove_action(action, group, [], paths_known=False)
        return action
    if action["field_id"] == "tag":
        return _tag_action(action, group, cards)
    if action["field_id"] == "edit_paths":
        return _rename_action(action, group, cards)
    if action["field_id"] == "remove_paths":
        return _remove_action(action, group, cards)
    return _priority_action(action, group, cards, tuple(_silent_optionals(response)))


#: What the chatbot's own Skip pill sends; reused so the two agree.
SKIP_TAG_VALUE = "skip"


def _is_gateway_form_result(response: dict) -> bool:
    """True when the chatbot's result is a Kong gateway card (`kong_route_form`):
    routes for an EXISTING service, saved by create_service_and_save_draft as
    the gateway half of that service's draft."""
    return bool(response.get("gateway_group"))


def _option_render_directive(options: list) -> tuple[str, str]:
    """Pick the right rendering mode for a list of dropdown options.

    Returns (render, instruction_suffix). `render` is a hint the LLM can
    branch on directly; the suffix is appended to whatever the outer
    instruction is so it reads naturally either way.
    """
    count = len(options)
    if count <= _ASK_USER_QUESTION_MAX_OPTIONS:
        return (
            "pills",
            (
                f"Render the {count} option(s) via AskUserQuestion — one "
                f"question, the listed options as its choices, each option's "
                f"`text` as the choice label. Give a `description` ONLY when it "
                f"tells the user something the label does not — 'Yes' → "
                f"'Create the Argo CD application' earns its place. OMIT it "
                f"when it would restate the label: 'config.yaml' under "
                f"'config.yaml' is the same string printed twice. Never pass "
                f"the option's `value` as the description; for most options it "
                f"IS the label. Invent no claim the data does not support. "
                f"Do not invent values, descriptions or claims not in this list."
            ),
        )
    return (
        "text_list",
        (
            f"There are {count} options — more than AskUserQuestion's cap of "
            f"{_ASK_USER_QUESTION_MAX_OPTIONS}. Do NOT use AskUserQuestion; "
            f"it will silently drop options. Instead, list every option "
            f"from `options` in your reply as a NUMBERED list — 1, 2, 3 … in "
            f"the order given, one per line, the option's `text` as the entry "
            f"— and tell the user they can reply with the number or the name. "
            f"When the user replies with a number, take the option at that "
            f"1-based position; when they reply with a name, match it. Either "
            f"way send that option's `value` back to `chat`, never the number "
            f"and never the label. Do not invent values not in this list, "
            f"and do not go looking for more of them: this list came from "
            f"DevLift and IS the set it can use. For repositories that set is "
            f"the tenant's connected ones, which is smaller than the GitHub "
            f"org — `gh repo list` or `git` would name repositories DevLift "
            f"will refuse. If the user wants something absent from it, say it "
            f"is not connected rather than sourcing it elsewhere."
        ),
    )


def _build_next_action(response: dict, template_accepted: bool = False) -> dict | None:
    """Translate a chatbot response into an explicit directive for the LLM.

    LLMs follow a literal `next_action` more reliably than they infer behavior
    from `isReady` / `suggestions` / `invalid_fields`. The MCP server is the
    only place that can synthesize this view because the raw chatbot response
    is shaped for the web frontend.
    """
    if response.get("status") == "error":
        return None

    if response.get("isReady"):
        if _is_service_form_result(response):
            return {
                "type": "create_service_and_save_draft",
                "instruction": (
                    "This is a service configuration. Call "
                    "create_service_and_save_draft(ticket_code=..., "
                    "project_id=...) in the same turn. Do NOT call "
                    "trigger_resource_deployment for a service."
                ),
            }
        if _is_gateway_form_result(response):
            return {
                "type": "create_service_and_save_draft",
                "instruction": (
                    f"Kong gateway routes for an existing service. SAVE IT "
                    f"NOW: call create_service_and_save_draft(ticket_code=..., "
                    f"project_id=...) in this same turn, exactly as a "
                    f"configuration is saved. Do NOT ask permission first. A "
                    f"draft writes nothing live, deploys nothing and stays "
                    f"editable until it is submitted, approved and deployed — "
                    f"so a confirmation here guards nothing and costs a turn. "
                    f"Do NOT call trigger_resource_deployment.\n\n"
                    f"THEN show what was saved: `collected_data` as a short "
                    f"summary, one line per field, human labels, no internal "
                    f"codes. Name these three, which are GROUP-WIDE rather "
                    f"than about this path, and which the user has been shown "
                    f"nowhere else:\n"
                    f"  • TAG — a tag that did not already exist created a NEW "
                    f"route group. Groups matching one path are ranked by "
                    f"regex_priority, so a tag is a routing decision, not a "
                    f"label.\n"
                    f"  • PLUGINS, incl. Auth/JWT from Secured — they apply to "
                    f"every route in the group. This card adds none; "
                    f"{_SELECTABLE_PLUGINS[0]} is the only one there is. Say "
                    f"so and leave it there: do not offer to add it, as that "
                    f"is a card of its own.\n"
                    f"  • REGEX PRIORITY — 0 unless a clash made you set it.\n\n"
                    f"The tool's own next_action then says what to offer next; "
                    f"follow it. If the user wants the card different, they "
                    f"say so and it goes back through `chat` as ONE message "
                    f"carrying every change at once — never one field per "
                    f"turn, and never a question per field. A request that "
                    f"names one thing changes one thing: 'add the user id "
                    f"plugin' is the whole edit, and the tag, priority, "
                    f"method, auth and paths stay as they are.\n"
                    f"Two edits are easy to get wrong. 'Edit the gateway path' "
                    f"or 'rename /old to /new' on an ADD card means switching "
                    f"it to Rename, not supplying a different path to add; "
                    f"same for Remove. And PATHS stay Kong regex — never say a "
                    f"plain path is fine or that devlift rewrote one, because "
                    f"a plain path is REFUSED and the refusal comes back with "
                    f"the compiled form to confirm."
                ),
            }
        return {
            "type": "confirm_deployment",
            "instruction": (
                "Do NOT call trigger_resource_deployment yet. Show the user a "
                "short summary of `collected_data` (one line per field, human "
                "labels and values, no internal codes) and ask ONE question via "
                "AskUserQuestion with the options 'Deploy' and 'Change "
                "something'. Only after the user picks Deploy, call "
                "trigger_resource_deployment(ticket_code=...) in that same "
                "turn. If they want a change, send it as the next `chat` "
                "message with the same ticket_code. If they decline, do not "
                "trigger and say nothing was created."
            ),
        }

    invalid = response.get("invalid_fields") or []
    suggestions = response.get("suggestions") or []
    first_suggestion = suggestions[0] if suggestions else None

    preface = _project_context_preface(response)

    section = response.get("section") or {}
    if not invalid and len(section.get("fields") or []) >= 2:
        action = _section_action(section, preface)
        action["instruction"] += _last_optional_fields_clause(response, template_accepted)
        return action

    if invalid:
        action = {
            "type": "fix_invalid",
            "invalid_fields": invalid,
            "instruction": (
                "Surface the chatbot's `message` verbatim so the user sees "
                "what was wrong, then ask ONLY for the single field below. "
                "Wait for the user's reply before calling `chat` again."
            ),
        }
        if first_suggestion:
            options = first_suggestion.get("options") or []
            action["field_id"] = first_suggestion.get("field_id")
            action["label"] = first_suggestion.get("label")
            action["options"] = options
            if options:
                render, render_suffix = _option_render_directive(options)
                action["render"] = render
                action["instruction"] = (
                    "Surface the chatbot's `message` verbatim so the user "
                    "sees what was wrong, then ask ONLY for the single "
                    f"field below. {render_suffix} Wait for the user's "
                    "reply before calling `chat` again."
                )
        # Appended LAST, after every branch that rewrites `instruction`.
        # It used to go on before the options branch, which rebuilt the string
        # from scratch and dropped it — and that branch fires exactly when the
        # correction matters most. Once ONE valid path is accepted the array is
        # non-empty, so the form moves on to `tag`, whose Skip pill IS a
        # suggestion; a bad path sent in that same message then lost its
        # correction and the user saw it accepted in silence.
        action["instruction"] += _kong_path_suggestions(invalid)
        return action

    if (
        _is_gateway_form_result(response)
        and not _is_plugin_card(response)
        and first_suggestion
        and first_suggestion.get("field_id") == "plugins"
    ):
        # The priority's skip normally carries plugins with it, so this fires
        # only when the priority was a real question (a clash) and plugins is
        # what is left.
        return {
            "type": "skip_field",
            "field_id": "plugins",
            "instruction": (
                f"Do NOT put this question to the user. "
                f"'{_SELECTABLE_PLUGINS[0]}' is the ONLY plugin selectable "
                f"here — JWT follows the Auth answer by itself — so this is "
                f"one toggle, and a toggle does not earn a dialog of its own. "
                f"Send chat(ticket_code=<same>, "
                "skip=['plugins']) NOW, in this same turn. The choice is NOT "
                "dropped and you do not mention it yet: it comes back as its "
                "own row on the card's save confirmation, where the user is "
                "answering a question anyway."
            ),
        }

    if first_suggestion and (first_suggestion.get("options") or []):
        options = first_suggestion["options"]
        render, render_suffix = _option_render_directive(options)
        if render == "text_list":
            # The preface below offers repository and language in a dialog and
            # says to ask this field "in the same single dialog" — but a list
            # too long for the picker cannot go in one. Told to do both, the
            # model did: nine resource groups printed as a numbered list AND a
            # popup for repository and language, two questions in two shapes
            # in one turn. The field the chatbot is actually asking for wins;
            # the project-context offer fires again next turn, because it is
            # gated on repository and language still being unanswered.
            preface = ""
        return {
            "type": "ask_user",
            "field_id": first_suggestion.get("field_id"),
            "label": first_suggestion.get("label"),
            "options": options,
            "render": render,
            "instruction": (
                f"{preface}Ask the user for this field"
                + (
                    " (alongside the project-context questions above, in the "
                    "same single dialog)"
                    if preface
                    else " and ONLY this field — put no other question to the "
                    "user this turn, and stack no speculative follow-ups"
                )
                + f". {render_suffix} Send the user's chosen `value` back "
                + (
                    "with the same ticket_code in that one `answers` call, "
                    "exactly as the option gives it and never as a sentence "
                    "about it"
                    if preface
                    else "as the next `chat` message with the same ticket_code"
                )
                + ", then wait for the next response before asking anything "
                "else." + _KNOWN_VALUES_CLAUSE
                + _last_optional_fields_clause(response, template_accepted)
            ),
        }

    missing = (response.get("missing_fields") or {}).get("required") or []
    if missing:
        return {
            "type": "ask_user_text",
            "field_id": missing[0] if missing else None,
            "missing_fields": missing,
            "instruction": (
                f"{preface}Ask the user for the first required field shown, in "
                "ONE AskUserQuestion. Having no dropdown options does NOT make "
                "this a plain-text question: a prose paragraph ending in a "
                "question mark is something the user has to read, parse and "
                "answer in their own words, where the dialog is one click. Ask "
                "it as a dialog even for free input — offer the most likely "
                "value as the first choice and say where it came from ('the "
                "repo's go.mod'), any other value you genuinely hold as the "
                "second, and let 'Other' carry anything typed. Two choices are "
                "enough; do not invent a third to fill the picker. If you hold "
                "nothing worth offering, the question still goes in the dialog "
                "— one choice you believe in plus Other beats a paragraph.\n\n"
                "Put the explanation IN the question text, not around it: what "
                "the field is for, and any constraint it has. Do not batch-ASK "
                "the other required fields; the chatbot surfaces them one at a "
                "time. Wait for the reply before calling `chat` again."
                + _KNOWN_VALUES_CLAUSE
            ),
        }

    return None


def _draft_exists_action(entry: dict, ticket_code: str) -> dict:
    """The message reached the chatbot on a ticket whose draft is already
    saved, and it changed no value. The chatbot keeps the session open on
    purpose (a value change re-saves the draft), so this is not an error —
    but 'show the preview' / 'submit it' belong to the service tools, and the
    LLM has no other way to learn that a draft exists for this ticket."""
    service = entry.get("identifier") or "this service"
    return {
        "type": "draft_exists",
        "instruction": (
            f"This ticket already has a saved draft for {service} and the "
            "message changed no value, so it did not belong in `chat`. If the "
            "user asked to see the preview / changes, call "
            f"get_service_configuration(ticket_code='{ticket_code}'); for the "
            f"settings, view_service_settings(ticket_code='{ticket_code}'); to "
            f"submit, submit_service_request(ticket_code='{ticket_code}') — in "
            "the SAME turn. Only value changes ('change cpu to 1') go to `chat`. "
            "If it was none of those, surface `message` as is."
        ),
    }


async def _existing_draft_for_ticket(user_code: str, ticket_code: str) -> dict | None:
    try:
        return await _find_redis_service_entry(user_code, ticket_code=ticket_code)
    except Exception:
        logger.exception("devlift_mcp chat: draft lookup failed (non-fatal)")
        return None


def _slim_response_for_llm(response: dict) -> dict:
    """Strip duplicated and internal-only fields from the chat response.

    Once `next_action` carries the authoritative directive, the chatbot's
    raw `suggestions` array is a byte-for-byte duplicate from the LLM's
    perspective. `missing_fields` is NOT: it duplicates the directive only for
    the ONE field being asked and is the sole remaining map of everything else
    still open. Stripping it left a caller that already holds values — copying
    another service's configuration — unable to see what it could fill, so it
    answered one question at a time and asked the user for values it had
    already read from the template. It is two short lists of ids; it stays.
    The `attribute_parameters` /
    `placement_parameters` / `collected_data` bags are internal accumulators
    the chatbot uses to drive its own state machine — useless to the LLM
    while the form is still in progress, and the chatbot's `message` field
    already echoes accepted values back to the user. Once `isReady: true`,
    `collected_data` is worth surfacing so the LLM can build a final summary,
    but the parameter bags still aren't.
    """
    next_action = response.get("next_action")

    if next_action:
        response.pop("suggestions", None)
    # `section` is the payload of an ask_section directive and noise otherwise.
    if not (next_action and next_action.get("type") == "ask_section"):
        response.pop("section", None)

    # The trigger / create tools read these bags from the Redis cache; the LLM
    # never needs them. `create_service` / `service_config` are the service-form
    # equivalents of the resource-form parameter bags.
    for internal_key in (
        "attribute_parameters",
        "placement_parameters",
        "create_service",
        "service_config",
        "gateway_group",
    ):
        response.pop(internal_key, None)

    if not response.get("isReady"):
        response.pop("collected_data", None)

    return response


async def _remember_template(user_code: str, ticket_code: str, applied: dict) -> None:
    """Never let a Redis hiccup break the conversation — the worst it can cost
    is the template being offered twice or missing from the closing summary."""
    try:
        await cache_template_applied(user_code, ticket_code, applied)
    except Exception:
        logger.exception("devlift_mcp chat: failed to record the language template")


def _template_offer_action(language: str, answers: dict, asks: list, unset: list) -> dict:
    """The one decision point the language template introduces.

    Sections 1-4 of the form are the ten things only the user can answer; 5-11
    are properties of the language, and this is what 107 live deployments run
    with. Offer them as a block rather than asking twenty questions whose
    answer is "whatever everyone else uses" — but offer them, do not impose
    them: the whole set is shown and any of it can be overridden in one reply.
    """
    return {
        "type": "review_template",
        "language": language,
        "template": answers,
        "left_unset": unset,
        "still_asked": asks,
        "instruction": (
            f"The remaining configuration has a standard set of values for "
            f"{language}, in `template` — what live {language} services on this "
            f"platform actually run with. Show them ALL as one grouped list — "
            f"runtime, resources, scaling, build, AWS — every field a row, "
            f"then ask in one line whether anything should differ. Do not ask "
            f"about them field by field, and do not present them as a question "
            f"per value.\n\n"
            f"Then call chat(ticket_code=<same>, message='apply the "
            f"{language} template', apply_template=true) — and if the user named "
            f"changes, put ONLY those in `answers`, e.g. answers={{'port': "
            f"'9000'}}. Never re-send `template` itself; the server holds it. "
            f"The user is choosing whether to accept a set of values, so treat "
            f"'yes' / 'looks good' / 'go ahead' as apply-with-no-overrides.\n\n"
            + (
                f"`left_unset` — {', '.join(unset)} — are fields the template "
                f"decides to leave EMPTY. List them as ROWS IN THE SAME GROUPS, "
                f"beside the values above, each showing what it is set to: "
                f"'Custom IAM policies: none', 'Additional trigger paths: "
                f"none', 'Initial heap (Xms): unset'. Do NOT collect them into "
                f"a sentence underneath the list — a decision written as prose "
                f"below a table of values reads as a footnote, and the user "
                f"scans the rows. They are accepted along with everything else "
                f"and are never asked about again, so a row is the only place "
                f"the user will see them.\n\n"
                if unset else ""
            )
            + (
                f"These are NOT in the template and will still be asked, because "
                f"they belong to this service rather than to {language}: "
                f"{', '.join(asks)}. Do not invent values for them here.\n\n"
                if asks else ""
            )
            + (
                "Between `template`, `left_unset` and the fields still to be "
                "asked, every remaining setting is accounted for. Do not leave "
                "any of the three out — a value silently decided is the one "
                "thing this review exists to prevent."
            )
        ),
    }


def _unreachable_action() -> dict:
    """What to say when the call did not get through.

    These returns used to carry no directive at all, so the model filled the
    silence: it printed the ticket code as proof the session survived, rebuilt
    a "What's captured so far" table from its own memory of the conversation,
    and named a service it had invented earlier. None of that is knowable from
    here — the call failed, so the server's state is exactly what it was, and
    a reconstruction from chat history is a guess wearing the clothes of a
    record.
    """
    return {
        "type": "chat_unreachable",
        "instruction": (
            "Say in one or two lines that DevLift could not be reached and that "
            "NOTHING was saved, submitted or deployed, then offer to retry. "
            "The session is intact and you already hold what is needed to "
            "resume — the user re-answers nothing.\n\n"
            "Do NOT print the ticket code: it is an internal identifier, it is "
            "yours to keep, and showing it tells the user nothing they can act "
            "on. Do NOT rebuild the configuration as a table of 'what was "
            "captured' — you cannot see what the server holds, so that is a "
            "guess presented as a record. Do not restate values, do not invent "
            "a service name, and do not claim any value was stored."
        ),
    }


async def chat_impl(
    *,
    message: str,
    ticket_code: Optional[str] = None,
    answers: Optional[dict] = None,
    skip: Optional[list] = None,
    apply_template: bool = False,
) -> dict:
    auth_ctx = await get_auth_context()
    if auth_ctx is None:
        return {
            "status": "error",
            "message": (
                "Authentication required. Run the 'authenticate' tool first "
                "to log in via your browser."
            ),
        }

    if not ticket_code:
        ticket_code = _new_ticket_code(auth_ctx.user_code)

    jwt_token = mint_internal_jwt(auth_ctx)

    # Applying the template: the model sends only what the user wants
    # different, and the server supplies the rest. The overrides go in LAST so
    # a user's value always beats the template's.
    applied_template: Optional[dict] = None
    if apply_template:
        prior = await get_template_applied(auth_ctx.user_code, ticket_code)
        language = (prior or {}).get("language")
        template = config_templates.template_answers(language, (prior or {}).get("service_name"))
        if template:
            overrides = answers or {}
            answers = {**template, **overrides}
            # Accepting the template settles the WHOLE non-required
            # configuration, not just the fields it names. Two kinds of field
            # have to be skipped or the form keeps asking one at a time:
            #
            #   - the ones it sets empty ("no IAM policies"), because the
            #     chatbot drops `[]` and `""` on the way in, leaving the field
            #     unanswered;
            #   - every other optional field still open, including ones the
            #     template says nothing about, like build_path on a Java
            #     service. The user accepted the standard configuration; being
            #     asked for a build path immediately afterwards is the review
            #     not having meant anything.
            #
            # What remains asked is what is REQUIRED and still missing —
            # health and service path always, plus port on Python and memory
            # and replicas on Java Maven. Anything the user overrode is theirs
            # and is answered, never skipped.
            settled = set(config_templates.template_skips(language))
            settled |= set(prior.get("optional_open") or [])
            settled -= set(overrides)
            settled -= set(template)          # answered, so not skipped
            if settled:
                skip = sorted(set(list(skip or [])) | settled)
            applied_template = {
                "language": language,
                "answers": template,
                # What the user asked to differ, so the summary can say so
                # even though the value below already reflects it.
                "overrides": sorted(k for k in overrides if k in template),
                "applied": True,
            }

    try:
        response = await post_chat(
            message=message,
            ticket_code=ticket_code,
            tenant_code=auth_ctx.tenant_code,
            user_mst_code=auth_ctx.user_code,
            jwt_token=jwt_token,
            answers=answers or None,
            skip=skip or None,
        )
    except httpx.HTTPStatusError as e:
        logger.exception("devlift_mcp chat: chatbot HTTP error")
        return {
            "status": "error",
            "message": (
                f"DevLift returned {e.response.status_code} and could not take "
                f"that. Nothing was saved."
            ),
            "detail": e.response.text[:200],
            "ticket_code": ticket_code,
            "next_action": _unreachable_action(),
        }
    except Exception as e:
        logger.exception("devlift_mcp chat: chatbot call failed")
        return {
            "status": "error",
            "message": "DevLift could not be reached. Nothing was saved.",
            "detail": str(e)[:200],
            "ticket_code": ticket_code,
            "next_action": _unreachable_action(),
        }

    response["ticket_code"] = ticket_code

    # ── The language template ────────────────────────────────────────────
    # Section 4 of the form is Language, and the template is keyed by it, so
    # this is the first turn at which it can be chosen. Everything the form
    # asks after this point (sections 5-11) is what the template covers, which
    # is why the offer lands exactly here and not at the start of the session.
    #
    # Only for a session that is BUILDING a configuration. An edit or a clone
    # opens with every field already answered — from the live service, or from
    # the one being copied — so `language` is there on turn one and the offer
    # would fire immediately, proposing to replace a running service's real
    # values with defaults. Both of those sessions mark the ticket as already
    # served when they open, which the `get_template_applied` check below
    # reads; see `_mark_session_prefilled`.
    # Has the user already accepted the standard configuration on this ticket?
    # If so the optional fields are settled and must not be asked again.
    prior_record: Optional[dict] = None
    template_accepted = applied_template is not None
    if not template_accepted:
        try:
            prior_record = await get_template_applied(auth_ctx.user_code, ticket_code)
        except Exception:
            # Losing the record only means the optional round-up runs, which
            # asks rather than assumes. Never fail a turn over it.
            logger.exception("devlift_mcp chat: template lookup failed")
            prior_record = None
        template_accepted = bool(prior_record and prior_record.get("applied"))

    template_action: Optional[dict] = None
    if applied_template is not None:
        # The baseline for the closing summary is what the values BECAME, read
        # back from this reply, not what was sent. The chatbot canonicalises a
        # dropdown answer to its option label, so "true" is stored as "Yes" and
        # "pod_identity" as "Pod Identity" — comparing the sent form against
        # the final one reported every such field as a change the user had
        # made, when the user had touched none of them.
        settled = response.get("collected_data") or {}
        applied_template["baseline"] = {
            field_id: settled[field_id]
            for field_id in applied_template["answers"]
            if field_id in settled
        }
        await _remember_template(auth_ctx.user_code, ticket_code, applied_template)
    elif not response.get("isReady"):
        collected = response.get("collected_data") or {}
        language = collected.get("language")
        # Offered per LANGUAGE, not once per ticket. Correcting "actually it is
        # Java Maven" after the Go template has gone by leaves the service
        # holding Go's values — ./cmd, configs/config.json, 0.25 CPU,
        # go_use_aws_secrets — and nothing offers Maven's, because the ticket
        # had already been served. Still create-only: a session prefilled from
        # a real service stamps `source`, and is never offered one whatever
        # the language does.
        served_for = (prior_record or {}).get("language")
        prefilled_session = bool((prior_record or {}).get("source"))
        # Not until the service exists on paper. Knowing the language is not
        # enough — the project-context preface offers repository and language
        # early, so language can land while the resource group and even the
        # service name are still open, and the review then covers twenty
        # settings for a service that is not yet placed anywhere.
        basics_done = config_templates.prerequisites_met(collected)
        if not basics_done and language and not prefilled_session:
            logger.debug(
                "devlift_mcp chat: template held back, still open: %s",
                config_templates.missing_prerequisites(collected),
            )
        if (
            language
            and basics_done
            and not prefilled_session
            and str(language) != str(served_for or "")
        ):
            service_name = collected.get("service_name")
            template = config_templates.template_answers(language, service_name)
            if template:
                await _remember_template(
                    auth_ctx.user_code, ticket_code,
                    {
                        "language": language, "answers": template, "applied": False,
                        # service_path and health are worked out from this, so
                        # the apply turn needs the same name the offer used.
                        "service_name": service_name,
                        # Every optional field still open at this moment. The
                        # template review is where the user accepts the
                        # non-required configuration as a whole, so on apply
                        # these are settled and must never be asked one by one
                        # afterwards. Taken from the chatbot's own
                        # classification rather than a list of field names
                        # here, which would be a second copy of the form.
                        "optional_open": list(
                            (response.get("missing_fields") or {}).get("optional") or []
                        ),
                    },
                )
                template_action = _template_offer_action(
                    str(language), template,
                    config_templates.unset_fields(language, service_name),
                    config_templates.template_skips(language),
                )

    # BEFORE the action is built, not after: `_section_action` reads the rows
    # to write its instruction, and a tag entry still empty at that point is
    # described to the model as a field with nothing to offer.
    _seed_section_tag_options(response)
    next_action = template_action or _build_next_action(response, template_accepted)
    if next_action is None and response.get("status") != "error":
        # Nothing to ask and nothing ready: on a ticket whose draft is already
        # saved that means a read / lane request was relayed here by mistake.
        entry = await _existing_draft_for_ticket(auth_ctx.user_code, ticket_code)
        if entry:
            response["draft"] = {
                "service_name": entry.get("identifier"),
                "queue_code": entry.get("queue_code"),
                "status": entry.get("queue_status") or entry.get("status"),
            }
            next_action = _draft_exists_action(entry, ticket_code)
    if next_action is not None:
        # The one question whose options the chatbot cannot know: a Kong tag's
        # real choices are the service's existing route groups.
        next_action = await _enrich_card_options(next_action, response, auth_ctx)
    if next_action is not None:
        response["next_action"] = next_action

    # When the chatbot signals the form is fully collected, cache the resolved
    # result so the follow-up tool can pick it up without re-calling the chat
    # endpoint. Resource forms carry attribute_parameters + placement_parameters
    # (consumed by trigger_resource_deployment); service forms carry
    # create_service + service_config (consumed by create_service_and_save_draft).
    cache_payload: dict | None = None
    if response.get("isReady"):
        if _is_service_form_result(response):
            cache_payload = {
                "kind": "service",
                "form_id": response.get("form_id"),
                "create_service": response.get("create_service") or {},
                "service_config": response.get("service_config") or {},
                "collected_data": response.get("collected_data") or {},
            }
        elif _is_gateway_form_result(response):
            cache_payload = {
                "kind": "gateway",
                "form_id": response.get("form_id"),
                "gateway_group": response.get("gateway_group") or {},
                "collected_data": response.get("collected_data") or {},
            }
        elif response.get("attribute_parameters") or response.get("placement_parameters"):
            cache_payload = {
                "kind": "resource",
                "form_id": response.get("form_id"),
                "attribute_parameters": response.get("attribute_parameters") or {},
                "placement_parameters": response.get("placement_parameters") or {},
                "collected_data": response.get("collected_data") or {},
            }
    if cache_payload is not None:
        try:
            await cache_chatbot_result(auth_ctx.user_code, ticket_code, cache_payload)
        except Exception:
            logger.exception("devlift_mcp chat: failed to cache chatbot result")

    return _slim_response_for_llm(response)
