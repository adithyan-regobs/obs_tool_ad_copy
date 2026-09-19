"""The AuthTripwire safety net, proven as tests. No docker/FGA needed.

Covers the failure the design must catch: a route that declared NO access
card at all (plain APIRouter, no Public declaration). The tripwire must
replace its response with a 500 so data never leaks. Any declared card —
including AuthenticationOnly — satisfies the tripwire by itself.

Ported from the openfga 4 PoC. One adaptation: authenticated_user delegates to
obs_tool's real get_current_user_and_tenant (Clerk/MCP), so the tests override
that dependency with a stub identity instead of sending x-user-id headers.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request

from app.api.dependencies import get_current_user_and_tenant
from app.core.authz.security import AuthenticationOnly, AuthTripwire, Public, SecureRouter, mark_checked

pytestmark = pytest.mark.asyncio

STUB_USER = SimpleNamespace(code="test-user-code")
STUB_TENANT = SimpleNamespace(code="test-tenant-code")


def tiny_app(authenticated: bool = True) -> FastAPI:
    """A minimal app with the same wiring as the real one."""
    app = FastAPI()
    app.add_middleware(AuthTripwire)
    router = SecureRouter()

    @router.get("/carded", access=AuthenticationOnly(reason="handler scopes its own queries"))
    async def carded():
        return {"data": "fine"}  # no explicit mark_checked — the card suffices

    @router.get("/remembered", access=AuthenticationOnly(reason="handler checks explicitly too"))
    async def remembered(request: Request):
        mark_checked(request)  # redundant but harmless — e.g. a real require() call
        return {"data": "fine"}

    @router.get("/open", access=Public(reason="declared public on purpose"))
    async def open_route():
        return {"data": "public"}

    app.include_router(router)

    # The failure mode the tripwire exists for: a plain APIRouter route with
    # NO card and no Public declaration slipped in past review.
    uncarded = APIRouter()

    @uncarded.get("/uncarded")
    async def uncarded_route():
        return {"secret": "leaked?"}

    app.include_router(uncarded)
    if authenticated:
        app.dependency_overrides[get_current_user_and_tenant] = lambda: (STUB_USER, STUB_TENANT)
    return app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=tiny_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
async def anonymous_client():
    transport = httpx.ASGITransport(app=tiny_app(authenticated=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_uncarded_route_becomes_500_and_leaks_nothing(client):
    r = await client.get("/uncarded")
    assert r.status_code == 500
    assert "secret" not in r.text  # the original body was discarded, not sent
    assert "authorization check" in r.json()["detail"]


async def test_authentication_only_card_alone_satisfies_tripwire(client):
    r = await client.get("/carded")
    assert r.status_code == 200
    assert r.json() == {"data": "fine"}


async def test_handler_that_checks_passes_through(client):
    r = await client.get("/remembered")
    assert r.status_code == 200
    assert r.json() == {"data": "fine"}


async def test_declared_public_route_passes_without_any_check(client):
    r = await client.get("/open")  # no identity at all
    assert r.status_code == 200


async def test_auth_only_still_requires_authentication(anonymous_client):
    r = await anonymous_client.get("/remembered")  # no Authorization header
    assert r.status_code in (401, 422)  # real dependency demands the header
