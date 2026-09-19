"""All authentication and authorization flows through this module.

The rule: a route CANNOT exist without declaring its access, because
`SecureRouter`'s decorators take a required keyword-only `access=` argument.
There is no default. Forgetting it is a TypeError at import time — the app
will not start, in any environment, including CI.

A developer must pick exactly one card:

    access=Public(reason="...")          no auth at all — the escape hatch,
                                         greppable and visible in review
    access=AuthenticationOnly(reason="...")        authenticated; the handler is
                                         trusted to scope its own queries
                                         (tenant isolation, list patterns)
    access=Authorization(permission=...,      authenticated + authorized against
             obj_type=..., param=...)    OpenFGA; object id from the URL
    access=AuthorizationFromBody(permission=...,  authenticated + authorized against
             obj_type=..., field=...)    OpenFGA; object id from the JSON body

How it fits together (all in this file):

    app start:   @router.post(..., access=card)
                   -> SecureRouter._register_with_guard
                   -> _guard_for_card -> _make_*_guard(card) -> guard
                 the guard is attached to the route as a FastAPI dependency.

    per request: FastAPI runs authenticated_user (authentication, 401 gate),
                 then the guard (authorization, 403/404 gate),
                 then — only if both passed — the handler.

Defense in depth: `AuthTripwire` middleware fails closed — any successful
response from a route that neither declared Public nor recorded an
authorization check is replaced with a 500. See tests/test_tripwire.py.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRouter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from . import fga


# ── access declarations (the "cards") ────────────────────────────────────────
@dataclass(frozen=True)
class Public:
    """No authentication, no authorization. State why."""

    reason: str


@dataclass(frozen=True)
class AuthenticationOnly:
    """Authenticated, but authorization happens inside the handler
    (tenant-scoped queries, ListObjects filtering, …). The card itself
    satisfies the tripwire; the handler is trusted to scope its queries."""

    reason: str


@dataclass(frozen=True)
class Authorization:
    """Authenticated + checked against OpenFGA before the handler runs.
    The object id is read from a path parameter."""

    permission: str
    obj_type: str  # e.g. "service" — combined with the path param below
    param: str  # name of the path parameter holding the object id
    deny_status: int = 404  # 404 hides existence; pass 403 to be explicit


@dataclass(frozen=True)
class AuthorizationFromBody:
    """Like Authorization, but the object id lives in the JSON body, not the URL.

    For create-style routes: the new object has no tuples yet, so the check
    runs against a PARENT object named in the body (e.g. its environment).

    One check by default (permission/obj_type/field). For a route that touches
    more than one object named in its body — a clone WRITES the target and READS
    the source — pass `checks`: a tuple of (permission, obj_type, field) triples,
    ALL of which must pass. Use the single-field form OR `checks`, not both.
    """

    permission: str | None = None
    obj_type: str | None = None  # e.g. "environment"
    field: str | None = None  # body field holding the parent object's id
    deny_status: int = 403
    checks: tuple | None = None  # ((permission, obj_type, field), ...)


Access = Public | AuthenticationOnly | Authorization | AuthorizationFromBody

# A guard is an async dependency FastAPI runs before the handler.
Guard = Callable[..., Awaitable[Any]]


# ── authentication ───────────────────────────────────────────────────────────
def _get_current_user_and_tenant():
    """obs_tool's production authentication (Clerk RS256 / MCP HS256 / MCP
    OAuth, incl. tenant isolation). Imported lazily to avoid an
    app.core -> app.api import cycle at module-load time."""
    from app.api.dependencies import get_current_user_and_tenant

    return get_current_user_and_tenant


async def authenticated_user(
    request: Request, user_tenant=Depends(_get_current_user_and_tenant())
) -> str:
    """The one place a caller's identity is established.

    Delegates to get_current_user_and_tenant — the same authentication every
    existing obs_tool route uses (Clerk JWT / MCP tokens). The FGA subject is
    user:{user_mst.code}.
    """
    # AUTHENTICATION = "who are you?"
    # Called by: FastAPI — every guard declares `user: str = Depends(authenticated_user)`.
    # When:      on EVERY request, BEFORE authorization.
    # Fail:      raises 401/403 in get_current_user_and_tenant → nothing else runs.
    # Pass:      returns "user:<user_mst.code>"; FastAPI caches it for this
    #            request, so it runs ONCE even if guard + handler both ask.
    db_user, tenant = user_tenant
    user = f"user:{db_user.code}"
    request.state.user = user
    request.state.authz_tenant = tenant
    return user


# ── authorization ────────────────────────────────────────────────────────────
def _now() -> dict:
    """Context for condition evaluation, sent on every Check."""
    return {"current_time": datetime.now(timezone.utc).isoformat()}


def mark_checked(request: Request) -> None:
    """Record that this request's authorization was decided. Every guard
    stamps it — `require` for the FGA cards, the login-only guard for
    AuthenticationOnly — so handlers do not need to call it."""
    request.state.authz_checked = True


async def require(
    request: Request,
    user: str,
    permission: str,
    obj: str,
    deny_status: int = 404,
    detail: str | None = None,
) -> None:
    """Check with OpenFGA; raise on deny, mark the request on allow."""
    # AUTHORIZATION = "are you allowed to do this?"
    # Called by: the guards below, or by AuthenticationOnly handlers directly.
    # When:      on EVERY request, AFTER authentication gave us `user`.
    # Fail:      raises 403/404 → handler never runs.
    # Pass:      stamps the request so AuthTripwire lets the response out.
    if not await fga.check(user, permission, obj, context=_now()):
        raise HTTPException(deny_status, detail or "Not found")
    mark_checked(request)


# ── guard factories: one card in, one guard out ──────────────────────────────
def _make_permission_guard(card: Authorization) -> Guard:
    # FACTORY — called by _guard_for_card, ONCE per route, at app start.
    # Input: one `Authorization` card. Output: the guard function below.

    async def guard(request: Request, user: str = Depends(authenticated_user)) -> str:
        # THE GUARD — called by FastAPI on EVERY request, before the handler.
        # Step 1, authentication: already done — Depends(authenticated_user) above
        #         made FastAPI establish `user` first (401 if it failed).
        # Step 2, authorization: below.
        obj = f"{card.obj_type}:{request.path_params[card.param]}"
        detail = None
        if card.deny_status != 404:
            # 403 routes may explain the denial. 404 routes stay silent,
            # so they never leak that the object exists.
            detail = f"{user} lacks {card.permission} on {obj}"
        await require(request, user, card.permission, obj, card.deny_status, detail)
        return user

    guard._access = card  # label for tests/reports: audits every route's declaration
    return guard


def _make_body_permission_guard(card: AuthorizationFromBody) -> Guard:
    # FACTORY — like _make_permission_guard, but the object id comes from the
    # body. Reading request.json() here is safe: Starlette caches the raw
    # body, so FastAPI still parses/validates the handler's Pydantic model
    # afterwards from the same cached bytes.

    # One check (the single fields) or several (checks=); normalise to a tuple so
    # the loop is the only path. Every check must pass. Single-check behaviour is
    # unchanged: checks is None -> exactly the one triple below.
    checks = card.checks or ((card.permission, card.obj_type, card.field),)

    async def guard(request: Request, user: str = Depends(authenticated_user)) -> str:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(422, "body must be valid JSON")
        for permission, obj_type, field in checks:
            value = data.get(field) if isinstance(data, dict) else None
            if not value:
                raise HTTPException(422, f"body field '{field}' is required")
            obj = f"{obj_type}:{value}"
            detail = f"{user} lacks {permission} on {obj}"
            await require(request, user, permission, obj, card.deny_status, detail)
        return user

    guard._access = card
    return guard


def _make_public_marker(reason: str) -> Guard:
    # No checks at all — only stamps "deliberately public" for the tripwire.

    async def guard(request: Request) -> None:
        request.state.access_public = True

    guard._access = Public(reason)
    return guard


def _make_login_only_guard(card: AuthenticationOnly) -> Guard:
    # Authentication only; the declared card satisfies the tripwire — the
    # handler owns (and is trusted with) any further scoping of its queries.

    async def guard(request: Request, user: str = Depends(authenticated_user)) -> str:
        mark_checked(request)
        return user

    guard._access = card
    return guard


def _guard_for_card(card: Access) -> list[Any]:
    """Pick the right guard for a card. Called once per route, at app start."""
    match card:
        case Public(reason=reason):
            return [Depends(_make_public_marker(reason))]
        case AuthenticationOnly():
            return [Depends(_make_login_only_guard(card))]
        case Authorization():
            return [Depends(_make_permission_guard(card))]
        case AuthorizationFromBody():
            return [Depends(_make_body_permission_guard(card))]
    raise TypeError(
        f"access must be Public, AuthenticationOnly, Authorization or AuthorizationFromBody, got {card!r}"
    )


def public_surface(reason: str) -> Any:
    """Declare a whole router public at include time:
    app.include_router(r, dependencies=[public_surface("demo admin panel")])"""
    return Depends(_make_public_marker(reason))


# ── the choke point ──────────────────────────────────────────────────────────
class SecureRouter(APIRouter):
    """APIRouter whose route decorators demand an access declaration.

    `access` is keyword-only with NO default: omitting it is a TypeError the
    moment the module imports — the app cannot start with an undeclared route.
    """

    def _register_with_guard(
        self, super_method: Callable, path: str, access: Access, kwargs: dict
    ):
        # Called by get/post/put/... below, ONCE per route, at app start.
        # Turns the access card into a guard, attaches the guard to the
        # route's dependency list, then registers the route with FastAPI.
        deps = list(kwargs.pop("dependencies", None) or [])
        deps.extend(_guard_for_card(access))
        return super_method(path, dependencies=deps, **kwargs)

    def get(self, path: str, *, access: Access, **kwargs):
        return self._register_with_guard(super().get, path, access, kwargs)

    def post(self, path: str, *, access: Access, **kwargs):
        return self._register_with_guard(super().post, path, access, kwargs)

    def put(self, path: str, *, access: Access, **kwargs):
        return self._register_with_guard(super().put, path, access, kwargs)

    def patch(self, path: str, *, access: Access, **kwargs):
        return self._register_with_guard(super().patch, path, access, kwargs)

    def delete(self, path: str, *, access: Access, **kwargs):
        return self._register_with_guard(super().delete, path, access, kwargs)

    def head(self, path: str, *, access: Access, **kwargs):
        return self._register_with_guard(super().head, path, access, kwargs)

    def options(self, path: str, *, access: Access, **kwargs):
        return self._register_with_guard(super().options, path, access, kwargs)


# ── fail-closed backstop ─────────────────────────────────────────────────────
# Framework-served paths that carry no user data and have no route of ours.
FRAMEWORK_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect",
                   "/", "/health", "/health/qdrant", "/health/redis", "/health/mcp"}

# Mounted sub-apps (static files, MCP servers, the React console) — their
# responses never pass through our guards, so the tripwire exempts them.
MOUNTED_PREFIXES = ("/ui", "/static", "/mcp", "/devlift-mcp", "/.well-known")

# OAuth / OIDC discovery documents (RFC 8414, RFC 9728). Public by spec, carry
# no user data, and are registered as bare Starlette routes in main.py so they
# never receive a guard stamp — without this exemption the tripwire turns them
# into 500s and MCP clients cannot complete OAuth discovery.
PUBLIC_PREFIXES = ("/.well-known/",)


class AuthTripwire(BaseHTTPMiddleware):
    """Last line of defense. A successful response from a route that neither
    declared itself Public nor recorded an authorization check is a bug —
    replace it with a 500 rather than leak.

    Deliberately dumb: it needs no route knowledge, only the stamps set by
    the guards (`access_public`) and `mark_checked` (`authz_checked`)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if response.status_code >= 300:  # errors and redirects carry no data
            return response
        if (
            request.url.path in FRAMEWORK_PATHS
            or request.url.path.startswith(MOUNTED_PREFIXES)
            or request.url.path.startswith(PUBLIC_PREFIXES)
        ):
            # mounted sub-apps (static console build, MCP servers) and OAuth
            # discovery docs — files and protocol endpoints, not data routes.
            return response
        state = request.state
        if getattr(state, "access_public", False) or getattr(state, "authz_checked", False):
            return response
        return JSONResponse(
            {"detail": "Internal error: response produced without an authorization check"},
            status_code=500,
        )
