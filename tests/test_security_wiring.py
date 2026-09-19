"""The access-declaration contract, verified app-wide.

SecureRouter already makes a missing declaration an import-time TypeError;
this walks every mounted route (including plain APIRouters pulled in via
include_router) so nothing can slip in through a side door. Runs without
docker — no FGA store needed.
"""

import pytest
from fastapi.routing import APIRoute

from app.main import app
from app.core.authz.security import Authorization, AuthorizationFromBody, SecureRouter


def _access_of(dependencies):
    for d in dependencies or []:
        if getattr(d.dependency, "_access", None) is not None:
            return d.dependency._access
    return None


def _walk(routes, inherited=None):
    """FastAPI 0.137 keeps included routers nested — walk them, letting an
    include-level declaration (public_surface on include_router) cover the
    routes beneath it, exactly as at runtime."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r, (_access_of(r.dependencies) or inherited)
        elif type(r).__name__ == "_IncludedRouter":
            ctx = r.include_context
            yield from _walk(r.original_router.routes,
                             _access_of(getattr(ctx, "dependencies", None)) or inherited)


def test_every_route_declares_access():
    undeclared = [
        f"{sorted(route.methods)} {route.path}"
        for route, access in _walk(app.routes)
        if access is None
    ]
    assert not undeclared, (
        "Routes without an access declaration (add access=... or include with "
        "public_surface(...)):\n" + "\n".join(undeclared)
    )


def test_undeclared_route_cannot_even_be_registered():
    router = SecureRouter()
    with pytest.raises(TypeError):

        @router.get("/oops")  # no access= — must blow up at definition time
        async def oops():
            return {}


# The six routes that guard a named service object in OpenFGA. Listed here on
# purpose: a downgrade to AuthenticationOnly is a silent loss of the FGA check,
# and only an explicit expectation catches it in review.
FGA_CARDED_ROUTES = {
    ("GET", "/api/v1/kong-route-configs/gateway/by-config/{service_config_code}"): "can_view_gateway",
    ("POST", "/api/v1/kong-route-configs/gateway/by-config/{service_config_code}/save"): "can_write_gateway",
    ("GET", "/api/v1/service-configs/by-code/{code}"): "can_view_settings",
    ("PUT", "/api/v1/service-configs/update-service-config/{code}"): "can_write_settings",
    ("POST", "/api/v1/transaction-queue/by-config/{service_config_code}/create-pr"): "can_deploy",
    ("POST", "/api/v1/deployments/multiple-deploy"): "can_deploy",
}


def _cards_on(route):
    """Every declaration attached to a route. include_router(dependencies=...)
    prepends the surface-level card, so a route can carry more than one and
    the route's own card is not necessarily first."""
    return [
        d.dependency._access
        for d in route.dependencies or []
        if getattr(d.dependency, "_access", None) is not None
    ]


def test_fga_carded_routes_still_authorize():
    found = {
        (method, route.path): _cards_on(route)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if (method, route.path) in FGA_CARDED_ROUTES
    }
    missing = sorted(set(FGA_CARDED_ROUTES) - set(found))
    assert not missing, f"routes moved or disappeared, update this list: {missing}"

    downgraded = {
        key: [type(c).__name__ for c in cards]
        for key, cards in found.items()
        if not any(
            isinstance(c, (Authorization, AuthorizationFromBody))
            and c.permission == FGA_CARDED_ROUTES[key]
            for c in cards
        )
    }
    assert not downgraded, (
        "Routes lost their OpenFGA authorization card:\n"
        + "\n".join(f"{m} {p} -> {c}" for (m, p), c in downgraded.items())
    )
