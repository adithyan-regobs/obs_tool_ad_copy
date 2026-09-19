"""FastMCP HTTP app factory for the DevLift MCP server.

Mirrors the pattern from `app/infra_chat_agent_with_tools/mcp_server/eks/eks_onboarding_sse.py`
but lives under `app/mcp_servers/devlift_mcp/` and exposes the DevLift tools.

Active tools (chatbot-driven flow):
    - list_supported_resources     (proxy to chatbot's /services)
    - chat                         (proxy to chatbot's /chat — collects + validates)
    - trigger_resource_deployment  (deploy by ticket_code once chat says isReady)
    - get_deployment_status        (poll pipeline_run_track for live status)
    - describe_data_schema         (read-only view catalog for query_data)
    - query_data                   (guarded SELECT over tenant-scoped mcp_ro views;
                                    fallback when no concrete tool answers a question)

Deprecated tools — implementations preserved but no longer registered:
    - describe_resource            (chatbot drives field collection now)
    - provision_resource           (folded into trigger_resource_deployment)
    - provision_service            (eks_service awaiting chatbot form)
    - trigger_service_deployment   (eks_service awaiting chatbot form)
    - clone_service                (copying a service is query_data + chat now:
                                    the settings are readable from the
                                    service_configs view, so no tool of its own)

CRITICAL: must NOT use module-level mutable state. All session state lives in
Redis (see `session_cache.py`). The existing EKS MCP server has a known multi-worker
bug from `stateless_http=True` + module-global `_state` dict — do not repeat that
pattern here. `stateless_http=True` is correct as long as no module-level state is added.
"""

from typing import Annotated, List
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP, Context  # noqa: F401  (Context kept for legacy tool signatures)
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from app.core.config import settings
from app.mcp_servers.devlift_mcp.instructions import DEVLIFT_INSTRUCTIONS
from app.mcp_servers.devlift_mcp.oauth_provider import DevLiftOAuthProvider
from app.mcp_servers.devlift_mcp.tools.chat import chat_impl
from app.mcp_servers.devlift_mcp.tools.create_service_and_save_draft import (
    create_service_and_save_draft_impl,
)
from app.mcp_servers.devlift_mcp.tools.describe_data_schema import describe_data_schema_impl
from app.mcp_servers.devlift_mcp.tools.get_deployment_status import get_deployment_status_impl
from app.mcp_servers.devlift_mcp.tools.get_application_status import get_application_status_impl
from app.mcp_servers.devlift_mcp.tools.list_supported_resources import (
    list_supported_resources_impl,
)
from app.mcp_servers.devlift_mcp.tools.service_details import get_service_configuration_impl
from app.mcp_servers.devlift_mcp.tools.service_approval import (
    approve_service_request_impl,
    list_pending_approvals_impl,
    reject_service_request_impl,
    review_service_request_impl,
    revoke_approval_impl,
)
from app.mcp_servers.devlift_mcp.tools.service_deploy import deploy_service_request_impl
from app.mcp_servers.devlift_mcp.tools.service_edit import edit_service_configuration_impl
from app.mcp_servers.devlift_mcp.tools.resource_edit import (
    apply_resource_edit_impl,
    start_resource_edit_impl,
)
from app.mcp_servers.devlift_mcp.tools.service_settings import view_service_settings_impl
from app.mcp_servers.devlift_mcp.tools.service_variables import open_variables_editor_impl
from app.mcp_servers.devlift_mcp.tools.service_request import (
    discard_service_request_impl,
    submit_service_request_impl,
    withdraw_service_request_impl,
)
from app.mcp_servers.devlift_mcp.tools.query_data import query_data_impl
from app.mcp_servers.devlift_mcp.tools.trigger_resource_deployment import trigger_resource_deployment_impl
# Deprecated imports — kept for revert; not registered below.
# from app.mcp_servers.devlift_mcp.tools.describe_resource import describe_resource_impl
# from app.mcp_servers.devlift_mcp.tools.provision_resource import provision_resource_impl
# from app.mcp_servers.devlift_mcp.tools.provision_service import provision_service_impl
# from app.mcp_servers.devlift_mcp.tools.trigger_service_deployment import trigger_service_deployment_impl
# from app.mcp_servers.devlift_mcp.tools.service_clone import clone_service_impl


# ============================================================
# Shared parameter descriptions
# ============================================================

_PROJECT_ID_DESCRIPTION = (
    "DevLift project ID from .devlift/project.json at the project root. "
    "If the file does not exist, omit this parameter — the server will "
    "issue a new project_id and instruct you to create the file, then "
    "retry this call with the returned project_id. Treat as opaque."
)

_QUEUE_CODE_DESCRIPTION = (
    "Change-request code (`queue-…`) returned by create_service_and_save_draft "
    "or by a previous review-lane tool. Prefer it when you have it. Treat as "
    "opaque; never show it to the user."
)

_SERVICE_NAME_DESCRIPTION = (
    "The service the user is talking about, as they call it (e.g. "
    "'sample-mcp-service'). Used to look the request up when no queue_code or "
    "ticket_code is at hand."
)

_TICKET_CODE_DESCRIPTION = (
    "Conversation ticket code returned by the `chat` tool. Omit on the "
    "very first call to `chat` — the server generates and returns one. "
    "Pass it back on every subsequent call (chat or trigger) for the "
    "same provisioning request. Treat as opaque; never fabricate."
)


def _allowed_hosts() -> List[str]:
    """Hosts the MCP endpoint will answer to.

    The SDK auto-enables DNS-rebinding protection whenever `host` is left at its
    default of 127.0.0.1, and then only accepts a localhost Host header — so a
    deployment behind a real hostname answers 421 "Invalid Host header" on
    /devlift-mcp/mcp while the OAuth routes beside it, which are not wrapped by
    that middleware, keep working. Login succeeds and the first real MCP call
    fails, which is a confusing way to find out.

    Rather than switch the protection off, name the hosts. They are already
    known per-environment: the issuer and resource-server URLs are this
    deployment's own public address. MCP_ALLOWED_HOSTS adds any extra (an ALB
    health check, a second domain) as a comma-separated list.
    """
    hosts: List[str] = []

    def add(value: str) -> None:
        netloc = urlparse(value).netloc if "//" in value else value
        netloc = netloc.strip().strip("/")
        if not netloc:
            return
        for candidate in (netloc, f"{netloc.split(':')[0]}:*"):
            if candidate not in hosts:
                hosts.append(candidate)

    add(settings.mcp_oauth_issuer_url)
    add(settings.mcp_oauth_resource_server_url)
    for extra in (settings.mcp_allowed_hosts or "").split(","):
        add(extra)
    for local in ("127.0.0.1:*", "localhost:*", "[::1]:*"):
        if local not in hosts:
            hosts.append(local)
    return hosts


def create_devlift_mcp_server() -> FastMCP:
    """Build the DevLift MCP server instance with the tools registered."""

    mcp = FastMCP(
        name="DevLift Resource Provisioner",
        instructions=DEVLIFT_INSTRUCTIONS,
        # Passed explicitly so the SDK does not fall back to its localhost-only
        # default. See _allowed_hosts above.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_allowed_hosts(),
            allowed_origins=[],
        ),
        json_response=True,
        stateless_http=True,
        auth_server_provider=DevLiftOAuthProvider(),
        auth=AuthSettings(
            issuer_url=settings.mcp_oauth_issuer_url,
            resource_server_url=settings.mcp_oauth_resource_server_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    # ========================================================
    # list_supported_resources — proxy to chatbot's /services
    # ========================================================
    async def list_supported_resources() -> dict:
        return await list_supported_resources_impl()

    mcp.add_tool(
        list_supported_resources,
        name="list_supported_resources",
        description=(
            "List the resource types available for provisioning (S3 buckets, "
            "DynamoDB tables, etc.). Call this first when discovering what's "
            "available for the user's tenant. Sourced from the chatbot's "
            "form catalog."
        ),
    )

    # ========================================================
    # chat — conversational entry point (proxies chatbot /chat)
    # ========================================================
    async def chat(
        message: Annotated[
            str,
            Field(
                description=(
                    "The user's message — free text describing what they want, "
                    "or a value the chatbot last asked for. The chatbot detects "
                    "the form, extracts values, validates them (regex + uniqueness "
                    "via upstream APIs), and asks for the next missing field."
                )
            ),
        ],
        ticket_code: Annotated[
            str | None,
            Field(default=None, description=_TICKET_CODE_DESCRIPTION),
        ] = None,
        answers: Annotated[
            dict | None,
            Field(
                default=None,
                description=(
                    "A whole dialog's answers, {field_id: value}, after an "
                    "`ask_section` directive: the chosen option's `value`, or the "
                    "text the user typed; a multi-select as a list. Filled "
                    "deterministically by the chatbot, no guessing. Leave out "
                    "fields the user did not answer."
                ),
            ),
        ] = None,
        skip: Annotated[
            list[str] | None,
            Field(
                default=None,
                description="Field ids the user chose 'Skip' for in the dialog (optional fields only).",
            ),
        ] = None,
        apply_template: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Accept the language's standard configuration, after a "
                    "`review_template` directive and only once the user has seen "
                    "it. The server holds the values — do NOT copy them into "
                    "`answers`. Put in `answers` only what the user wants "
                    "different, e.g. answers={'port': '9000'}; those win over "
                    "the template."
                ),
            ),
        ] = False,
    ) -> dict:
        return await chat_impl(
            message=message,
            ticket_code=ticket_code,
            answers=answers,
            skip=skip,
            apply_template=apply_template,
        )

    mcp.add_tool(
        chat,
        name="chat",
        description=(
            "Drive the conversational provisioning flow. The chatbot extracts "
            "and validates one or more fields per turn and asks for the next "
            "missing one.\n\n"
            "FIRST CALL: omit `ticket_code` — the server mints one and echoes "
            "it on every response. Pass that SAME ticket_code on every "
            "subsequent call in the conversation.\n\n"
            "CONVERSATION SHAPE: this is a back-and-forth chat. Each `chat` "
            "call returns ONE thing for the user: a dialog of up to 4 fields "
            "(`ask_section`), a single field to fill, a validation error, or "
            "'all set'. Mirror that on the user-facing side: ONE "
            "AskUserQuestion per turn, with only the fields and options the "
            "chatbot listed. Never stack speculative follow-up questions for "
            "fields the chatbot hasn't asked yet — they surface on the next "
            "response.\n\n"
            "RESPONSE CONTRACT — follow `next_action` literally:\n"
            "  • next_action.type == 'ask_section' → ONE AskUserQuestion with "
            "one question per entry of `fields` (see the instruction), then "
            "call `chat` again with `answers={field_id: value}` and "
            "`skip=[...]` — the chatbot fills them all at once.\n"
            "  • next_action.type == 'ask_user'   → branch on `render`:\n"
            "      - `render: 'pills'` → ONE AskUserQuestion using the listed "
            "options as its choices.\n"
            "      - `render: 'text_list'` → there are more than 4 options "
            "(AskUserQuestion's cap), so list them all in your reply as a "
            "NUMBERED list (1, 2, 3 … in the order given) and tell the user "
            "they can answer with the number or the name. Do NOT use "
            "AskUserQuestion in this case — it would silently drop options.\n"
            "    Either way: options are authoritative; do NOT invent values, "
            "do NOT pad with extras, do NOT free-text-ask when options exist. "
            "Send the user's chosen `value` back as the next `chat` message.\n"
            "  • next_action.type == 'ask_user_text' → no preset choices for "
            "the SINGLE field shown. Still ONE AskUserQuestion, not a prose "
            "question: offer the likeliest value as the first choice and let "
            "Other carry anything typed. Do not "
            "batch-ask the other listed fields — wait for the chatbot to "
            "surface them one at a time.\n"
            "  • next_action.type == 'fix_invalid' → surface the `message` "
            "verbatim so the user sees what was wrong, then re-ask using the "
            "options below if present. Still ONE question.\n"
            "  • next_action.type == 'confirm_deployment' → the form is "
            "complete. Do NOT trigger yet. Show a short summary of "
            "`collected_data` (human labels and values, no internal codes) "
            "and ask ONE AskUserQuestion with options 'Deploy' and 'Change "
            "something'. On Deploy, call "
            "`trigger_resource_deployment(ticket_code=...)` in that same "
            "turn. On a change, send it as the next `chat` message with the "
            "same ticket_code. If the user declines, do not trigger.\n"
            "  • next_action.type == 'create_service_and_save_draft' → the "
            "form was a SERVICE (EKS) or a Kong GATEWAY card for one. "
            "Immediately call "
            "`create_service_and_save_draft(ticket_code=..., project_id=...)` "
            "in the SAME turn; it only saves a draft, nothing deploys, so no "
            "confirmation is needed. Never call trigger_resource_deployment "
            "for a service or its routes.\n"
            "  • next_action.type == 'draft_exists' → this ticket already has "
            "a saved draft and the message changed no value, so it should NOT "
            "have gone to `chat`. Call the tool the instruction names "
            "(get_service_configuration / view_service_settings / "
            "submit_service_request with this ticket_code) in the SAME turn.\n\n"
            "AFTER THE DRAFT IS SAVED the ticket stays open for VALUE CHANGES "
            "only ('change cpu to 1', 'generate dockerfile not needed'): each "
            "one returns isReady again and you call "
            "create_service_and_save_draft again, which re-saves the same "
            "draft. Requests to see the preview / changes / settings, or to "
            "submit / withdraw / discard, are NOT chat messages — call the "
            "service tools with the ticket_code instead.\n\n"
            "INVARIANTS:\n"
            "  • One dialog (or one question) per turn. Wait for the user's "
            "reply before calling `chat` again. Do not loop on `chat` "
            "autonomously.\n"
            "  • Never call `trigger_resource_deployment` or "
            "`create_service_and_save_draft` before the chat response carries "
            "`isReady: true` and the matching next_action.type — they will "
            "reject with fields_incomplete. For a resource, also never before "
            "the user has explicitly confirmed the summary.\n"
            "  • Never expose `ticket_code` or other internal codes to the user.\n"
            "  • Surface `message` verbatim — it's already user-friendly.\n"
            "  • The user MAY reply with several values at once (e.g. "
            "'mumbai stage core'); pass that string straight through to "
            "`chat` — the chatbot's extractor handles batched input. You "
            "still only ASK one thing at a time.\n\n"
            "EXISTING SERVICES: `chat` also answers 'does demo exist', 'can I "
            "edit it / what is my role', 'edit demo', 'status of demo' from "
            "verified facts. Such replies carry `facts`, may return "
            "next_action.type == 'choose' (present `options`, send the chosen "
            "`value` back), and NEVER lead to trigger_resource_deployment."
        ),
    )

    # ========================================================
    # create_service_and_save_draft — EKS service creation + settings draft
    # ========================================================
    async def create_service_and_save_draft(
        ticket_code: Annotated[
            str | None,
            Field(default=None, description=_TICKET_CODE_DESCRIPTION),
        ] = None,
        project_id: Annotated[
            str | None,
            Field(default=None, description=_PROJECT_ID_DESCRIPTION),
        ] = None,
        cluster_code: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Which EKS cluster the service runs on. Only needed after a "
                    "`status: 'needs_cluster'` response: pass the `value` of the "
                    "option the user chose. Omit otherwise — the cluster is "
                    "inferred from the placement. Opaque; never show it."
                ),
            ),
        ] = None,
    ) -> dict:
        return await create_service_and_save_draft_impl(
            ticket_code=ticket_code,
            project_id=project_id,
            cluster_code=cluster_code,
        )

    mcp.add_tool(
        create_service_and_save_draft,
        name="create_service_and_save_draft",
        description=(
            "Create the EKS service the user configured via `chat` and save "
            "its configuration as a DRAFT for review. Pass the ticket_code "
            "from the chat session — the server pulls the resolved service "
            "and configuration from the chatbot, creates the service and its "
            "base configuration if they do not exist yet (mirroring the web's "
            "Create & Add), then parks the configured values as a draft "
            "change request. It does NOT submit, approve or deploy anything.\n\n"
            "Also the save for a Kong gateway session (edit_service_configuration "
            "section='gateway'): when the ticket collected routes, it merges them "
            "into the service's gateway and saves them as the gateway half of the "
            "same draft (`action: 'gateway_draft_saved'`, `gateway_changes`). "
            "Every save ends by asking the user what next (submit / add Kong "
            "route / keep editing) — follow `next_action.instruction`.\n\n"
            "PRECONDITION: only callable after the most recent `chat` response "
            "for this ticket_code carries `isReady: true` AND "
            "`next_action.type == 'create_service_and_save_draft'`. Calling "
            "earlier returns `{status: 'error', reason: 'fields_incomplete', "
            "next_action: {type: 'continue_chat', ...}}` — go back to `chat` "
            "with the same ticket_code and finish the form.\n\n"
            "If the service and its configuration for that environment already "
            "exist, nothing is created; only the draft is saved (an edit). "
            "Safe to retry with the same ticket_code.\n\n"
            "RESPONSE: `message` (surface verbatim), `action` ('created' or "
            "'draft_saved'), `queue_status` ('draft'). `status: 'no_changes'` "
            "means the values already match what is deployed.\n\n"
            "`status: 'needs_cluster'` means several EKS clusters serve that "
            "environment and region, so the placement does not name one. This "
            "is a QUESTION, not a failure: follow `next_action` — show the "
            "options, let the USER pick, then call this tool again with the "
            "same ticket_code and the chosen value as `cluster_code`. Never "
            "pick a cluster yourself and never tell the user to ask someone "
            "else.\n\n"
            "On `status: 'error'` surface `message` verbatim; a permission "
            "refusal is final — do not retry or look for another route."
        ),
    )

    # ========================================================
    # get_service_configuration — service details + draft preview (read-only)
    # ========================================================
    async def get_service_configuration(
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        ticket_code: Annotated[str | None, Field(default=None, description=_TICKET_CODE_DESCRIPTION)] = None,
        service_config_code: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Service configuration code (`sc-…`) from a previous response, "
                    "or the chosen value after a `choose` action. Opaque."
                ),
            ),
        ] = None,
    ) -> dict:
        return await get_service_configuration_impl(
            service_name=service_name,
            queue_code=queue_code,
            ticket_code=ticket_code,
            service_config_code=service_config_code,
        )

    mcp.add_tool(
        get_service_configuration,
        name="get_service_configuration",
        description=(
            "Show a service's configuration: the LIVE settings for its "
            "environment plus the pending change request (draft / submitted / "
            "approved) with a Field | Current | Proposed diff, plus the pending "
            "Kong gateway route changes when the request has any — the same view "
            "as the DevLift service page and its preview tab. Read-only and safe "
            "to call whenever the user asks to see, show, preview, review or "
            "check a service's configuration or what they changed. Works for "
            "the author and for reviewers; the backend decides visibility "
            "(drafts are visible to their author only).\n\n"
            "Identify the service by service_name, or by queue_code / "
            "ticket_code / service_config_code from an earlier response. "
            "`next_action.type == 'choose'` → present the options and call "
            "again with the chosen value as service_config_code. Follow "
            "`next_action.instruction` for how to render the result."
        ),
    )

    # ========================================================
    # edit_service_configuration — start an edit session pre-filled with current values
    # ========================================================
    async def edit_service_configuration(
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        ticket_code: Annotated[str | None, Field(default=None, description=_TICKET_CODE_DESCRIPTION)] = None,
        service_config_code: Annotated[
            str | None,
            Field(default=None, description="Service configuration code (`sc-…`) from a previous response. Opaque."),
        ] = None,
        section: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "'configuration' (default): the settings form, pre-filled with "
                    "the current values. 'gateway': the Kong routes form for the "
                    "service's API gateway — placement and service are pre-filled "
                    "and the form asks the rest of the card itself, so call it "
                    "without gathering method / auth / path first."
                ),
            ),
        ] = None,
        route_action: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "section='gateway' only. 'Add', 'Remove', 'Rename' or "
                    "'Plugins' — set it whenever the user's own words say "
                    "which, so the form does not ask them again:\n"
                    "  'add a path' / 'expose GET ...'            -> Add\n"
                    "  'remove /x' / 'delete a path'              -> Remove\n"
                    "  'rename /a to /b' / 'edit a path' /\n"
                    "  'change a path' / 'update a path'          -> Rename\n"
                    "  'add user id injection to <group>'         -> Plugins\n"
                    "A verb aimed at a PATH is a Rename whatever word it uses: "
                    "editing, changing or updating a path all mean the path "
                    "becomes a different one, which is what Rename does (the "
                    "route keeps its identity rather than being deleted and "
                    "recreated). 'I want to edit a kong path' has said it.\n"
                    "LEAVE IT OUT only when NO path is named and the verb "
                    "could mean any of the four — 'edit the gateway', 'change "
                    "the routes', 'update the gateway' — and the form will "
                    "ask. Guessing THERE sends the user to the wrong path "
                    "question, which they have to notice and undo; asking when "
                    "they already told you is a turn spent confirming their "
                    "own words."
                ),
            ),
        ] = None,
    ) -> dict:
        return await edit_service_configuration_impl(
            service_name=service_name,
            queue_code=queue_code,
            ticket_code=ticket_code,
            service_config_code=service_config_code,
            section=section,
            route_action=route_action,
        )

    mcp.add_tool(
        edit_service_configuration,
        name="edit_service_configuration",
        description=(
            "Start editing an EXISTING service. Two sections, like the tabs "
            "of the DevLift service page:\n"
            "  • section='configuration' (default): loads the current values "
            "(the pending request's if one exists, else the live "
            "configuration) into a new chat session so nothing is re-asked; "
            "the user then only says what to change ('set cpu of X to 2', "
            "'edit X').\n"
            "  • section='gateway': opens the Kong gateway routes form with "
            "placement and service pre-filled, and ASKS the card itself — what "
            "to do, HTTP method, auth, path(s), then tag / priority / plugins "
            "— as dialogs carrying the real options ('add a kong route to X', "
            "'expose GET ~/api/v1/users$ on X', the 'Add Kong route' choice "
            "after a draft). PASS route_action WHENEVER THE INTENT IS ALREADY "
            "SAID — 'Add' for a request to add or expose a path and for the "
            "'Add Kong route' choice, 'Remove', 'Rename', or 'Plugins' for "
            "'add user id injection to <group>'. It is the first question the "
            "form asks, and asking it back after the user already said it is "
            "a turn spent confirming their own words. CALL IT IN THE SAME TURN "
            "the user asks. Do not "
            "announce it and wait for a go-ahead, and do not ask for method / "
            "auth / path in prose beforehand: the questions ARE this tool's "
            "response, the prose version gets them wrong (it cannot know which "
            "route groups exist, or which path field is being asked), and it "
            "costs a round trip. Opening the form writes nothing and deploys "
            "nothing, so there is nothing to confirm first.\n"
            "NOT for creating a new service — that is a plain `chat`.\n\n"
            "RESPONSE: `ticket_code` for the session and `next_action` — for "
            "the gateway that is 'ask_section', whose `fields` carry the "
            "options to render; follow it rather than inventing questions. "
            "Send the user's replies to `chat` with that "
            "ticket_code; never replay the existing values. When chat answers "
            "with next_action 'create_service_and_save_draft', call it — it "
            "saves the changes (settings, or gateway routes) as a draft on the "
            "existing service."
        ),
    )

    # ========================================================
    # start_resource_edit — open an edit session on a live bucket or queue
    # ========================================================
    async def start_resource_edit(
        resource_name: Annotated[
            str,
            Field(description="Name of the bucket or queue to edit, as the user says it."),
        ],
        environment: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Only to pick between resources that SHARE a name across "
                    "environments. This never moves a resource — placement is read "
                    "from the row itself."
                ),
            ),
        ] = None,
        product: Annotated[
            str | None,
            Field(
                default=None,
                description="Only to disambiguate when two products share a resource name.",
            ),
        ] = None,
    ) -> dict:
        return await start_resource_edit_impl(
            resource_name=resource_name,
            environment=environment,
            product=product,
        )

    mcp.add_tool(
        start_resource_edit,
        name="start_resource_edit",
        description=(
            "Start changing a setting on an EXISTING S3 bucket or SQS queue "
            "('turn on versioning for my-gallery', 'raise the visibility timeout "
            "on orders-queue'). Read-only: it loads the current values and says "
            "which fields can change.\n\n"
            "NOT for creating a resource — that is provision_resource. Not "
            "available for DynamoDB (a table cannot be updated in place).\n\n"
            "RESPONSE: `edit_id`, `editable_fields` with their current values, "
            "and `locked_fields`. Show the user the labels, not the field names. "
            "Then call apply_resource_edit with only what they changed.\n\n"
            "A resource's name, product, environment and region CANNOT be "
            "changed and are not inputs here. If the user asks to move or rename "
            "one, say so plainly and offer to create a new resource instead."
        ),
    )

    # ========================================================
    # apply_resource_edit — confirm, then write
    # ========================================================
    async def apply_resource_edit(
        edit_id: Annotated[
            str,
            Field(description="The `edit_id` from start_resource_edit. Opaque."),
        ],
        changes: Annotated[
            dict,
            Field(
                description=(
                    "Only the fields the user actually changed, keyed by the "
                    "`field` value from `editable_fields` — for example "
                    '{"versioning": true}. Never include a locked field.'
                ),
            ),
        ],
        confirmed: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Leave false on the first call: the tool answers with a "
                    "before/after summary and writes nothing. Pass true only "
                    "after the user has said yes to that summary."
                ),
            ),
        ] = False,
        project_id: Annotated[
            str | None,
            Field(default=None, description=_PROJECT_ID_DESCRIPTION),
        ] = None,
    ) -> dict:
        return await apply_resource_edit_impl(
            edit_id=edit_id,
            changes=changes,
            confirmed=confirmed,
            project_id=project_id,
        )

    mcp.add_tool(
        apply_resource_edit,
        name="apply_resource_edit",
        description=(
            "Apply the changes collected after start_resource_edit.\n\n"
            "Two steps, always. First call it WITHOUT confirmed: it returns "
            "`diff` (before/after) and writes nothing. Show that to the user in "
            "their own words and ask. Only if they agree, call it again with the "
            "same edit_id and changes plus confirmed=true.\n\n"
            "On success it returns a `draft_id` and asks you to call "
            "trigger_resource_deployment in the SAME response — the user already "
            "confirmed, so do not ask a second time.\n\n"
            "A field the tool refuses cannot be forced: report what it says and "
            "offer to create a new resource instead."
        ),
    )

    # ========================================================
    # view_service_settings — every field + value, grouped like the Settings tab
    # ========================================================
    async def view_service_settings(
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        ticket_code: Annotated[str | None, Field(default=None, description=_TICKET_CODE_DESCRIPTION)] = None,
        service_config_code: Annotated[
            str | None,
            Field(default=None, description="Service configuration code (`sc-…`) from a previous response. Opaque."),
        ] = None,
        include_gateway: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Include the Kong gateway routes. FALSE by default: "
                    "'show the config of X' asks about settings, and a wall of "
                    "route paths buries the answer. Pass true ONLY when the "
                    "user asks for the routes/gateway, or for everything "
                    "('full details', 'everything about X'). When it is false "
                    "the reply still says how many routes exist, so you can "
                    "offer them in one line."
                ),
            ),
        ] = False,
    ) -> dict:
        return await view_service_settings_impl(
            service_name=service_name,
            queue_code=queue_code,
            ticket_code=ticket_code,
            service_config_code=service_config_code,
            include_gateway=include_gateway,
        )

    mcp.add_tool(
        view_service_settings,
        name="view_service_settings",
        description=(
            "Read a service's configuration: every settings field with its "
            "value, grouped like the DevLift Settings tab (Placement, "
            "Repository, Dockerfile, Manifest, AWS Resource Provisioning), and "
            "its Kong gateway routes as the Gateway tab shows them (method, "
            "auth, tag, paths). Shows the pending request's values when one "
            "exists (marked 'draft') and the live values otherwise. Read-only; "
            "use when the user asks what a service's settings/values/"
            "configuration/routes ARE (e.g. 'what port does X use', 'show the "
            "settings of X', 'which routes does X expose'). For the "
            "from/to diff of a pending change use get_service_configuration "
            "instead.\n\n"
            "NOT for building a new service from an existing one ('create X "
            "like Y', 'same configuration as Y'). This renders settings for a "
            "person to read; to COPY them you need the raw `config` object, "
            "which comes from query_data over the `service_configs` view.\n\n"
            "Identify by service_name, or by queue_code / ticket_code "
            "/ service_config_code from an earlier response. Follow "
            "`next_action.instruction` for how to render the result."
        ),
    )

    # ========================================================
    # open_variables_editor — variables & secrets are entered in the browser
    # ========================================================
    async def open_variables_editor(
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        ticket_code: Annotated[str | None, Field(default=None, description=_TICKET_CODE_DESCRIPTION)] = None,
        service_config_code: Annotated[
            str | None,
            Field(default=None, description="Service configuration code (`sc-…`) from a previous response. Opaque."),
        ] = None,
    ) -> dict:
        return await open_variables_editor_impl(
            service_name=service_name,
            ticket_code=ticket_code,
            service_config_code=service_config_code,
        )

    mcp.add_tool(
        open_variables_editor,
        name="open_variables_editor",
        description=(
            "Give the user the link to a service's Variables tab in the "
            "DevLift dashboard, where environment variables and secrets are "
            "added, edited and deleted. Use it whenever the user wants to add, "
            "change, remove or look at variables, env vars, config values or "
            "secrets of a service ('add DB_PASSWORD to X', 'set the API key on "
            "payments-api', 'where do I put env vars', the 'Add variables & "
            "secrets' choice after a draft).\n\n"
            "DevLift policy: variables and secrets are managed only in the "
            "dashboard. Never ask the user for a value, never accept one, never "
            "pass one to this or any other tool or to `chat`. If the user "
            "offers a value anyway, do not repeat it - restate the policy in "
            "one line and give them the link.\n\n"
            "Identify the service by service_name, or by ticket_code / "
            "service_config_code from an earlier response. The service must "
            "already exist in DevLift (a draft is enough); for a brand-new "
            "service run create_service_and_save_draft first.\n\n"
            "RESPONSE: surface `message` exactly as given, with `url` as a "
            "clickable link. Add nothing to it - no warnings about other open "
            "requests or the review lane, no caveats, no apology. When the user "
            "says they have saved, offer submit_service_request. Follow "
            "`next_action.instruction`."
        ),
    )

    # ========================================================
    # Review lane — the author's own change request
    # ========================================================
    async def submit_service_request(
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        ticket_code: Annotated[str | None, Field(default=None, description=_TICKET_CODE_DESCRIPTION)] = None,
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        comment: Annotated[
            str | None,
            Field(default=None, description="Optional note for the reviewers (shown with the request)."),
        ] = None,
    ) -> dict:
        return await submit_service_request_impl(
            queue_code=queue_code, ticket_code=ticket_code, service_name=service_name, comment=comment
        )

    mcp.add_tool(
        submit_service_request,
        name="submit_service_request",
        description=(
            "Send a saved service configuration DRAFT for review (draft → "
            "submitted). Call ONLY when the user asks to submit / send for "
            "review / request approval — never right after "
            "create_service_and_save_draft on your own.\n\n"
            "Identify the request by queue_code (preferred), else ticket_code "
            "from the chat, else service_name. Only the author's own drafts "
            "can be submitted; one live request per service at a time.\n\n"
            "RESPONSE: surface `message` verbatim; `changes` is the frozen "
            "from/to diff you may summarise. `status: 'error'` messages are "
            "final (wrong state, not yours, no permission, lane busy) — do not "
            "retry. `next_action.type == 'choose'` → present the options and "
            "call again with the chosen value as queue_code."
        ),
    )

    async def withdraw_service_request(
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        ticket_code: Annotated[str | None, Field(default=None, description=_TICKET_CODE_DESCRIPTION)] = None,
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        comment: Annotated[
            str | None,
            Field(default=None, description="Optional reason, recorded in the request history."),
        ] = None,
    ) -> dict:
        return await withdraw_service_request_impl(
            queue_code=queue_code, ticket_code=ticket_code, service_name=service_name, comment=comment
        )

    mcp.add_tool(
        withdraw_service_request,
        name="withdraw_service_request",
        description=(
            "Pull the user's own SUBMITTED change request back out of review "
            "(submitted → draft) so they can keep editing. Only the submitter "
            "can withdraw; a reviewer sending it back is request-changes, not "
            "this. Call only when the user asks. Identify by queue_code, "
            "ticket_code or service_name. Errors are final — do not retry."
        ),
    )

    async def discard_service_request(
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        ticket_code: Annotated[str | None, Field(default=None, description=_TICKET_CODE_DESCRIPTION)] = None,
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
    ) -> dict:
        return await discard_service_request_impl(
            queue_code=queue_code, ticket_code=ticket_code, service_name=service_name
        )

    mcp.add_tool(
        discard_service_request,
        name="discard_service_request",
        description=(
            "Throw away the user's own configuration DRAFT (draft → gone). The "
            "live configuration and the service itself are untouched. Only a "
            "draft can be discarded, and only by its author; a submitted "
            "request must be withdrawn first. This is destructive: call only "
            "when the user clearly asks to discard / delete / throw away the "
            "draft. Identify by queue_code, ticket_code or service_name."
        ),
    )

    # ========================================================
    # Review lane — the reviewer's side
    # ========================================================
    async def list_pending_approvals() -> dict:
        return await list_pending_approvals_impl()

    mcp.add_tool(
        list_pending_approvals,
        name="list_pending_approvals",
        description=(
            "List the change requests waiting for THIS user's review (services "
            "they may approve), plus the ones they already approved and can "
            "still revoke. Read-only; call when the user asks what is pending, "
            "what needs their approval, or wants to review requests. Returns "
            "queue codes to pass to review_service_request. Never decide on a "
            "request from this list without the user's explicit choice."
        ),
    )

    async def review_service_request(
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
    ) -> dict:
        return await review_service_request_impl(queue_code=queue_code, service_name=service_name)

    mcp.add_tool(
        review_service_request,
        name="review_service_request",
        description=(
            "Open ONE change request for review: who submitted it, the frozen "
            "Field | Deployed → Requested diff of what they changed, the history, "
            "and `decisions_available` for this user (approve / reject / revoke, "
            "or withdraw when it is their own). Read-only. Identify it by "
            "queue_code from list_pending_approvals, or by service_name. Show the "
            "diff and the available decisions, then WAIT for the user to decide."
        ),
    )

    async def approve_service_request(
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        comment: Annotated[
            str | None, Field(default=None, description="Optional approval note, recorded in the request history."),
        ] = None,
    ) -> dict:
        return await approve_service_request_impl(
            queue_code=queue_code, service_name=service_name, comment=comment
        )

    mcp.add_tool(
        approve_service_request,
        name="approve_service_request",
        description=(
            "APPROVE a submitted change request (submitted → approved; the "
            "approved values are sealed). Call ONLY when the user explicitly "
            "says to approve it. Requires approval permission on the service; "
            "approving one's own request is allowed only where the resource "
            "group permits self-approval — a refusal is final, do not retry. "
            "Identify by queue_code or service_name."
        ),
    )

    async def reject_service_request(
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        reason: Annotated[
            str | None,
            Field(default=None, description="REQUIRED. Why it is rejected — shown to the author with the request."),
        ] = None,
    ) -> dict:
        return await reject_service_request_impl(
            queue_code=queue_code, service_name=service_name, reason=reason
        )

    mcp.add_tool(
        reject_service_request,
        name="reject_service_request",
        description=(
            "REJECT a submitted change request: it goes back to its author as a "
            "draft with the values intact and your reason attached (submitted → "
            "draft), so they can fix and resubmit. A reason is REQUIRED — ask the "
            "user for it before calling; the tool refuses without one. Call ONLY "
            "when the user explicitly decides to reject. Requires approval "
            "permission; refusals are final. If the request is the user's own, "
            "use withdraw_service_request instead."
        ),
    )

    async def revoke_approval(
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        comment: Annotated[
            str | None, Field(default=None, description="Optional note, recorded in the request history."),
        ] = None,
    ) -> dict:
        return await revoke_approval_impl(queue_code=queue_code, service_name=service_name, comment=comment)

    mcp.add_tool(
        revoke_approval,
        name="revoke_approval",
        description=(
            "REVOKE an approval the user (or another approver) gave: approved → "
            "back to submitted, so it can be re-decided. Refused once a deployment "
            "of it is in flight. Call ONLY when the user explicitly asks to revoke. "
            "Identify by queue_code (see approved_by_you in list_pending_approvals) "
            "or service_name."
        ),
    )

    # ========================================================
    # deploy_service_request — ship an APPROVED service change
    # ========================================================
    async def deploy_service_request(
        service_name: Annotated[str | None, Field(default=None, description=_SERVICE_NAME_DESCRIPTION)] = None,
        queue_code: Annotated[str | None, Field(default=None, description=_QUEUE_CODE_DESCRIPTION)] = None,
        ticket_code: Annotated[str | None, Field(default=None, description=_TICKET_CODE_DESCRIPTION)] = None,
        confirmed: Annotated[
            bool | None,
            Field(
                default=None,
                description=(
                    "false/omitted: the tool only returns what would ship and asks "
                    "for confirmation (`needs_confirmation`). true: the user chose "
                    "Deploy in that question — ship it. Never pass true before the "
                    "user has answered."
                ),
            ),
        ] = None,
        confirm_service_name: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "PRODUCTION only: the service name exactly as the user typed it "
                    "to confirm. Required together with confirmed=true for a prod deploy."
                ),
            ),
        ] = None,
        project_id: Annotated[str | None, Field(default=None, description=_PROJECT_ID_DESCRIPTION)] = None,
    ) -> dict:
        return await deploy_service_request_impl(
            service_name=service_name,
            queue_code=queue_code,
            ticket_code=ticket_code,
            confirmed=confirmed,
            confirm_service_name=confirm_service_name,
            project_id=project_id,
        )

    mcp.add_tool(
        deploy_service_request,
        name="deploy_service_request",
        description=(
            "DEPLOY an APPROVED service change request — the web's Deploy button. "
            "Ships everything the request holds in one batch: the configuration "
            "change and the approved Kong gateway routes. Only for a request in "
            "status 'approved' by a user with the deploy right; the tool refuses "
            "anything else with a plain sentence (draft → submit first; waiting "
            "for approval; not allowed; already deploying).\n\n"
            "TWO-STEP CONTRACT: call it first WITHOUT `confirmed` — it answers "
            "`status: 'needs_confirmation'` with the Changes tables and a "
            "`next_action` that asks the user (Deploy / Cancel; on production the "
            "user types the service name). Only after the user says yes, call it "
            "again with `confirmed=true` (plus `confirm_service_name` on prod). "
            "Success returns `workflow_id` and a `next_action` that starts "
            "`/loop` with get_deployment_status — follow it. A failed deployment "
            "puts the request back to approved; the same call is the retry.\n\n"
            "Never use trigger_resource_deployment for a service. Identify the "
            "request by service_name, queue_code or ticket_code."
        ),
    )

    # ========================================================
    # trigger_resource_deployment
    # ========================================================
    async def trigger_resource_deployment(
        ticket_code: Annotated[
            str | None,
            Field(default=None, description=_TICKET_CODE_DESCRIPTION),
        ] = None,
        resource_type: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "DEPRECATED legacy parameter — only used when triggering "
                    "by draft_id. Ignored when ticket_code is set."
                ),
            ),
        ] = None,
        project_id: Annotated[
            str | None,
            Field(default=None, description=_PROJECT_ID_DESCRIPTION),
        ] = None,
        draft_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "DEPRECATED legacy parameter — only used by the old "
                    "provision_resource flow. Prefer ticket_code."
                ),
            ),
        ] = None,
    ) -> dict:
        return await trigger_resource_deployment_impl(
            ticket_code=ticket_code,
            resource_type=resource_type,
            project_id=project_id,
            draft_id=draft_id,
        )

    mcp.add_tool(
        trigger_resource_deployment,
        name="trigger_resource_deployment",
        description=(
            "Deploy the resource the user collected via `chat`. Pass the "
            "ticket_code from the chat session — the server pulls the resolved "
            "attribute and placement parameters from the chatbot, creates the "
            "queue item + draft, and starts the deployment.\n\n"
            "PRECONDITION: only callable after the most recent `chat` response "
            "for this ticket_code carries `isReady: true` AND "
            "`next_action.type == 'confirm_deployment'`, AND the user has "
            "explicitly confirmed the summarized settings in this "
            "conversation. Calling before the form is complete will "
            "return `{status: 'error', reason: 'fields_incomplete', "
            "next_action: {type: 'continue_chat', ...}}` — when you see that, "
            "go back to `chat` with the same ticket_code and finish the form.\n\n"
            "RESPONSE: returns quickly with `action: 'queued'` (or 'deployed' "
            "on PaaS) and a `next_action` telling you to invoke `/loop` with "
            "`get_deployment_status`. Follow it in the same turn. The "
            "deployment (pull request, plan, apply, merge) runs server-side; "
            "this call does NOT wait for it. Do not call this tool twice for "
            "the same ticket_code — a retry returns `action: 'already_queued'`."
        ),
    )

    # ========================================================
    # get_deployment_status
    # ========================================================
    async def get_deployment_status(
        project_id: Annotated[
            str | None,
            Field(default=None, description=_PROJECT_ID_DESCRIPTION),
        ] = None,
    ) -> dict:
        return await get_deployment_status_impl(project_id=project_id)

    mcp.add_tool(
        get_deployment_status,
        name="get_deployment_status",
        description=(
            "Surface deployment progress for resources triggered in this or any "
            "recent conversation. Call it from the `/loop` that "
            "`trigger_resource_deployment` asks for.\n"
            "  • PaaS tenants: per-resource Jenkins pipeline status (PENDING / "
            "RUNNING / COMPLETED / FAILED), build stages, log URL, and errors.\n"
            "  • Enterprise tenants: per-resource `status` (PENDING / RUNNING / "
            "COMPLETED / FAILED), the current `stage` (e.g. 'infra: create pr', "
            "'infra: plan pr', 'infra: apply pr', 'infra: merge pr'), a "
            "ready-to-show `message`, `pr_url` once the pull request exists, "
            "and `error_message` on failure.\n"
            "Response carries `all_completed` and a `next_action` — follow it: "
            "`continue_polling` means let `/loop` run again; "
            "`present_completion` / `present_pr_links` means stop the loop and "
            "show the final result. Always pass `project_id` when you have "
            "one — it scopes the result to this project's deployments."
        ),
    )

    # ========================================================
    # get_application_status
    # ========================================================
    async def get_application_status(
        project_id: Annotated[
            str | None,
            Field(default=None, description=_PROJECT_ID_DESCRIPTION),
        ] = None,
        service_name: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Limit the answer to one service, by the name the user "
                    "knows it by. Omit to report every EKS service deployed in "
                    "this conversation."
                ),
            ),
        ] = None,
    ) -> dict:
        return await get_application_status_impl(
            project_id=project_id, service_name=service_name
        )

    mcp.add_tool(
        get_application_status,
        name="get_application_status",
        description=(
            "Is the deployed application actually RUNNING? Reads live sync and "
            "health from the tenant's ArgoCD — a different question from "
            "get_deployment_status, which only reports whether DevLift's own "
            "pipeline finished.\n\n"
            "WHEN: right after get_deployment_status reports a COMPLETED EKS "
            "service deploy, start a second `/loop 20s get_application_status("
            "project_id=<same project_id>)`. Also call it any time the user "
            "asks whether a service is up, healthy or working.\n\n"
            "WHY BOTH: DevLift finishes before ArgoCD has even looked at the "
            "change, so a service can be 'deployed' and not yet serving. This "
            "tool waits for ArgoCD to act on the new revision before calling "
            "anything healthy.\n\n"
            "RESPONSE: per service a `state` (up / starting / unhealthy / "
            "waiting_for_argocd / not_picked_up / missing / not_found / "
            "not_configured / unavailable), a ready-to-show `message`, "
            "`argocd_url`, `health_url` and `running_image`. Follow "
            "`next_action`: `continue_polling` means let `/loop` run again and "
            "do NOT declare success or failure yet; `present_completion` means "
            "stop and show the result. Polling stops on its own about five "
            "minutes after the deploy finished.\n\n"
            "EKS services only. Other resource types and PaaS accounts answer "
            "`not_applicable` — say nothing about live status for those."
        ),
    )

    # ========================================================
    # Data-question fallback: describe_data_schema + query_data
    # ========================================================
    if settings.mcp_data_query_enabled:

        async def describe_data_schema() -> dict:
            return await describe_data_schema_impl()

        mcp.add_tool(
            describe_data_schema,
            name="describe_data_schema",
            description=(
                "List the read-only views you may query with `query_data`: "
                "name, description, columns with types, join hints, and the "
                "query rules. Call it ONCE per conversation, before the first "
                "`query_data`, when the user asks a question about their "
                "DevLift data that no other tool answers (which services run "
                "in stage, who owns X, what deployed last week, is there an "
                "open PR for Y). Every view is already scoped to the "
                "signed-in user's account."
            ),
        )

        async def query_data(
            sql: Annotated[
                str,
                Field(
                    description=(
                        "One SELECT statement (CTEs allowed) over the views "
                        "returned by describe_data_schema. Postgres syntax. No "
                        "schema prefix needed. Base tables, other schemas, "
                        "pg_*, information_schema, DML/DDL, SET, EXPLAIN and "
                        "multiple statements are rejected. Do not add tenant "
                        "filters; the views are already scoped."
                    )
                ),
            ],
            purpose: Annotated[
                str | None,
                Field(
                    default=None,
                    description=(
                        "One line: the user's question this query answers. "
                        "Logged for audit; not shown to the user."
                    ),
                ),
            ] = None,
        ) -> dict:
            return await query_data_impl(sql=sql, purpose=purpose)

        mcp.add_tool(
            query_data,
            name="query_data",
            description=(
                "Run a read-only SELECT over the user's DevLift data. "
                "FALLBACK ONLY: use it when the user asks about their data and "
                "no concrete tool (chat, get_deployment_status, "
                "list_supported_resources) answers it. Never for provisioning "
                "or for deployment progress right after a trigger.\n\n"
                "FLOW: describe_data_schema once -> write ONE SELECT over the "
                "listed views -> query_data(sql, purpose) -> answer from `rows`.\n\n"
                "RESPONSE: `status: ok` carries `columns`, `rows` (capped at "
                "`max_rows`; `truncated: true` means more exist), and a "
                "`next_action` telling you how to present it. `status: error` "
                "with reason `sql_rejected` or `sql_error` carries a `message` "
                "explaining what to fix; retry at most twice, then tell the "
                "user what you could not look up.\n\n"
                "PRESENTING: plain language. Never show the SQL, *_code or id "
                "values, or raw column names unless the user asks. Prefer "
                "names, emails, statuses and dates. Aggregate (COUNT, GROUP "
                "BY) instead of listing rows to count them."
            ),
        )

    # ========================================================
    # Deprecated registrations — kept for revert, not exposed.
    # ========================================================
    # async def describe_resource(...): ...
    # mcp.add_tool(describe_resource, name="describe_resource", description=...)
    #
    # async def provision_resource(...): ...
    # mcp.add_tool(provision_resource, name="provision_resource", description=...)
    #
    # async def provision_service(...): ...
    # mcp.add_tool(provision_service, name="provision_service", description=...)
    #
    # async def trigger_service_deployment(...): ...
    # mcp.add_tool(trigger_service_deployment, name="trigger_service_deployment", description=...)

    return mcp
