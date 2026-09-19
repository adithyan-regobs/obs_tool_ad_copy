"""open_variables_editor - the browser hand-off for variables and secrets.

No values ever pass through here: the tool resolves the service and returns
the dashboard deep link to its Variables tab. Pure-stub: auth, JWT, the
loopback client, Redis and the DB lookup are all replaced.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.config import settings
from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError
from app.services import frontend_links
from tests.unit.mcp_servers.test_service_request import AUTH, REDIS_ENTRY, TICKET

SC = REDIS_ENTRY["transaction_code"]
CONTEXT = {"environment": "stage", "service_name": "sample-mcp-service", "app_code": "app-core", "workspace_code": "ws-1"}


def _run(handler, *, context=CONTEXT, redis_entries=None, listing=None, frontend="https://app.devlift.ai/", **kwargs):
    client = MagicMock()
    client.ObsToolAPIError = ObsToolAPIError
    client.list_approvals = AsyncMock(return_value={"approvals": listing or [], "total": 0})
    patches = [
        patch.object(dispatcher, "get_auth_context", AsyncMock(return_value=AUTH)),
        patch("app.mcp_servers.devlift_mcp._internal_jwt.mint_internal_jwt", return_value="jwt"),
        patch("app.mcp_servers.devlift_mcp.obs_tool_client", client),
        patch.object(dispatcher, "get_all_drafts", AsyncMock(return_value=[REDIS_ENTRY] if redis_entries is None else redis_entries)),
        patch.object(dispatcher, "_load_service_link_context", AsyncMock(return_value=context)),
        patch("app.services.frontend_links.tenant_url_slug", AsyncMock(return_value="vance")),
        patch.object(settings, "frontend_base_url", frontend),
    ]

    async def go():
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return await handler(**kwargs)

    return go()


@pytest.mark.asyncio
async def test_by_ticket_returns_variables_tab_link():
    result = await _run(dispatcher.open_variables_editor_handler, ticket_code=TICKET)

    assert result["status"] == "success"
    assert result["service_config_code"] == SC
    assert result["environment"] == "stage"
    parsed = urlparse(result["url"])
    assert parsed.scheme == "https" and parsed.netloc == "app.devlift.ai"
    assert parsed.path == "/vance/dashboard/projects"  # subdomain of tenant `aspora`
    q = parse_qs(parsed.query)
    assert q == {"resource": [SC], "env": ["stage"], "app": ["app-core"], "ws": ["ws-1"], "tab": ["variables"]}
    assert result["next_action"]["type"] == "present_link"

    # Policy first, then the link, then the next step - and nothing else.
    msg = result["message"]
    assert msg.startswith("DevLift policy requires variables and secrets to be managed in the dashboard.")
    # The link is markdown in the message itself, not a bare URL the model is
    # asked to wrap: "show verbatim" and "make it clickable" used to be two
    # instructions in tension, and the bare URL is what won.
    assert f"]({result['url']})" in msg
    assert f": {result['url']}" not in msg
    assert "submit it for review" in msg
    for leaked in ("secret service", "AI tool", "chat", "never"):
        assert leaked not in msg, f"message explains the mechanism: {leaked!r}"

    # The extra warnings the model used to invent are forbidden, not just unmentioned.
    instruction = result["next_action"]["instruction"].lower()
    assert "exactly as given" in instruction
    assert "review lane" in instruction and "add nothing" in instruction
    assert "never ask for, accept or repeat" in instruction


@pytest.mark.asyncio
async def test_by_service_config_code_skips_lookup_by_name():
    result = await _run(dispatcher.open_variables_editor_handler, service_config_code="sc-other", redis_entries=[])
    assert result["status"] == "success"
    assert parse_qs(urlparse(result["url"]).query)["resource"] == ["sc-other"]


def _no_db():
    """A session whose by-name lookups find nothing, so the resolver's database
    step runs without a database."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session():
        db = MagicMock(name="db")
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        result.first.return_value = None
        db.execute = AsyncMock(return_value=result)
        yield db

    repo = MagicMock()
    repo.find_by_name_for_mcp = AsyncMock(return_value=None)
    return [
        patch.object(dispatcher, "AsyncSessionLocal", session),
        patch.object(dispatcher, "ServicesMstRepository", return_value=repo),
    ]


@pytest.mark.asyncio
async def test_unknown_service_is_not_found_and_says_create_first():
    with ExitStack() as stack:
        for p in _no_db():
            stack.enter_context(p)
        result = await _run(dispatcher.open_variables_editor_handler, service_name="ghost", redis_entries=[])
    assert result["status"] == "error"
    assert result["reason"] == "not_found"
    assert "create it first" in result["message"]
    assert "url" not in result


@pytest.mark.asyncio
async def test_missing_frontend_url_is_a_clear_error():
    result = await _run(dispatcher.open_variables_editor_handler, ticket_code=TICKET, frontend="")
    assert result["status"] == "error"
    assert "FRONTEND_BASE_URL" in result["message"]


@pytest.mark.asyncio
async def test_link_still_works_when_context_lookup_fails():
    result = await _run(dispatcher.open_variables_editor_handler, ticket_code=TICKET, context=None)
    assert result["status"] == "success"
    q = parse_qs(urlparse(result["url"]).query)
    assert q == {"resource": [SC], "tab": ["variables"]}


def test_variables_tab_link_shape():
    with patch.object(settings, "frontend_base_url", "https://x.devlift.ai"):
        url = frontend_links.variables_tab_link(tenant_slug="t1", resource_code="sc-1", environment="prod", app_code="a b")
    assert url == "https://x.devlift.ai/t1/dashboard/projects?resource=sc-1&env=prod&app=a+b&tab=variables"


def test_link_is_none_without_frontend_url():
    with patch.object(settings, "frontend_base_url", ""):
        assert frontend_links.service_panel_link(tenant_slug="t1", resource_code="sc-1") is None


@pytest.mark.asyncio
async def test_slug_is_the_subdomain_not_the_tenant_code():
    """The frontend serves tenant `aspora` at `vance`; a link built from the
    code lands on a tenant that does not exist."""
    row = MagicMock()
    row.scalar_one_or_none.return_value = "vance"
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session():
        db = MagicMock()
        db.execute = AsyncMock(return_value=row)
        yield db

    with patch.object(frontend_links, "AsyncSessionLocal", session):
        assert await frontend_links.tenant_url_slug("aspora") == "vance"

    row.scalar_one_or_none.return_value = None
    with patch.object(frontend_links, "AsyncSessionLocal", session):
        assert await frontend_links.tenant_url_slug("wankmo") == "wankmo"


@pytest.mark.asyncio
async def test_slug_falls_back_to_the_code_when_the_lookup_breaks():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def boom():
        raise RuntimeError("db down")
        yield

    with patch.object(frontend_links, "AsyncSessionLocal", boom):
        assert await frontend_links.tenant_url_slug("aspora") == "aspora"
