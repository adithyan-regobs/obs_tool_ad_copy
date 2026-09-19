"""service_routing — the one rule for a service's ingress path and probe path.

Saving, rendering and displaying all go through these functions, so a blank
value settles the same way everywhere and a hand-set ALB survives a redeploy.
"""
import pytest

from app.utils.service_routing import (
    alb_base_to_write,
    clean_service_path,
    default_health_path,
    display_service_path,
    fill_routing_defaults,
    has_alb_route,
    needs_ingress,
    normalize_service_name,
    resolve_health_path,
    resolve_service_path,
)

EKS = "eks_infrastructuretype_ref"
ECS = "ecs_ec2_infrastructuretype_ref"


def _fill(config, **overrides):
    kwargs = dict(
        service_name="payment",
        language_name="Java Maven",
        infrastructuretype_ref_code=EKS,
        service_type="API",
        alb_selection="existing_alb",
    )
    kwargs.update(overrides)
    return fill_routing_defaults(config, **kwargs)


# ── names and paths ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("typed", ["payment", "payment-service", "payment_service", "Payment Service"])
def test_the_service_name_decides_the_path_however_it_was_typed(typed):
    assert normalize_service_name(typed) == "payment-service"
    assert resolve_service_path({}, typed) == "/payment-service"


@pytest.mark.parametrize("value,expected", [
    ("/api/v1", "/api/v1"),
    ("api/v1", "/api/v1"),
    ("/goms-service/*", "/goms-service"),      # ALB listener pattern, not a path
    ("  /x  ", "/x"),
    ("", ""), (None, ""), ("/", ""), ("//", ""), ("/*", ""),   # blank or catch-all
])
def test_clean_service_path(value, expected):
    assert clean_service_path(value) == expected


def test_a_stored_path_wins_over_the_default():
    assert resolve_service_path({"service_path": "/api/v1"}, "payment") == "/api/v1"


def test_a_catch_all_path_is_treated_as_unset():
    assert resolve_service_path({"service_path": "/*"}, "payment") == "/payment-service"


# ── health ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("language,expected", [
    ("Java Maven", "/payment-service/actuator/health"),
    ("Java Gradle", "/payment-service/actuator/health"),
    ("Go 1.23", "/payment-service/health"),
    ("Node.js 20 LTS", "/payment-service/health"),
    ("Python 3.12", "/payment-service/health"),
    (None, "/payment-service/actuator/health"),   # unknown keeps the old default
])
def test_default_health_follows_the_language(language, expected):
    assert default_health_path("/payment-service", language) == expected


def test_default_health_sits_under_the_custom_service_path():
    """Case C: a custom ingress path with a blank health used to get the
    name-based default, which the ingress never routed to."""
    assert resolve_health_path({"service_path": "/api/v1/pay"}, "payment", "Go 1.23") == "/api/v1/pay/health"


def test_a_typed_health_is_kept_as_is():
    assert resolve_health_path({"health": "healthz"}, "payment") == "/healthz"


# ── display ──────────────────────────────────────────────────────────────────

def test_display_prefers_the_setting_then_the_last_render():
    assert display_service_path({"service_path": "/a", "resolved_service_path": "/b"}) == "/a"
    assert display_service_path({"resolved_service_path": "/b"}) == "/b"


@pytest.mark.parametrize("config", [{}, {"service_path": "/*"}, {"service_path": "/"}])
def test_display_never_guesses_a_path(config):
    """A model server, a root-served PaaS service or an ECS catch-all rule has
    no path — the ALB URL is already the whole URL."""
    assert display_service_path(config) == ""


# ── who gets an ingress ──────────────────────────────────────────────────────

@pytest.mark.parametrize("infra,service_type,alb,expected", [
    (EKS, "API", "existing_alb", True),
    (EKS, "OPS_TOOLS", "existing_alb", True),
    (EKS, "API", None, True),
    (EKS, "BACKGROUND_SERVICE", "no_alb", False),
    (EKS, "MODEL_SERVING", "existing_alb", False),
    (EKS, "API", "no_alb", False),
    (ECS, "API", "existing_alb", False),         # ECS defaults to /* — not filled here
    (EKS, None, "existing_alb", False),          # unknown type: do not guess
])
def test_needs_ingress(infra, service_type, alb, expected):
    assert needs_ingress(infra, service_type, alb) is expected


def test_has_alb_route_treats_an_unknown_type_as_routable():
    assert has_alb_route(None, None) is True
    assert has_alb_route("BACKGROUND_SERVICE", None) is False
    assert has_alb_route("API", "no_alb") is False


# ── fill on save ─────────────────────────────────────────────────────────────

def test_a_new_service_gets_the_defaults():
    out = _fill({"port": "8080"})
    assert out["service_path"] == "/payment-service"
    assert out["health"] == "/payment-service/actuator/health"
    assert out["port"] == "8080"


def test_health_waits_for_the_language():
    """The canvas creates the row before a language is picked. A stamped
    actuator path would stick to a Go service, so only the path is filled."""
    out = _fill({}, language_name=None)
    assert out["service_path"] == "/payment-service"
    assert "health" not in out

    later = _fill({}, language_name="Go 1.23", stored=out)
    assert later["health"] == "/payment-service/health"


def test_typed_values_are_kept():
    out = _fill({"service_path": "/api/v1", "health": "/api/v1/ping"})
    assert out["service_path"] == "/api/v1"
    assert out["health"] == "/api/v1/ping"


@pytest.mark.parametrize("incoming", [{}, {"service_path": "", "health": ""}, {"service_path": None}])
def test_blank_never_overwrites_a_stored_value(incoming):
    stored = {"service_path": "/api/v1", "health": "/api/v1/health"}
    out = _fill(incoming, stored=stored, deployed=True)
    assert out["service_path"] == "/api/v1"
    assert out["health"] == "/api/v1/health"


def test_a_deployed_service_with_nothing_stored_is_left_for_the_backfill():
    """The repo may hold a hand-edited ingress.path; guessing here would
    overwrite it on the next render."""
    out = _fill({"service_path": "", "health": ""}, stored={"port": "8080"}, deployed=True)
    assert "service_path" not in out
    assert "health" not in out


def test_workers_and_ecs_are_not_filled():
    assert "service_path" not in _fill({}, service_type="BACKGROUND_SERVICE", alb_selection="no_alb")
    assert "service_path" not in _fill({}, infrastructuretype_ref_code=ECS)


def test_a_client_cannot_set_the_alb_url():
    """Promote and change-cluster send back the config they read, full URL
    included. The stored value is what stays."""
    out = _fill({"alb_url": "https://source/other-service", "resolved_service_path": "/other-service"})
    assert "alb_url" not in out
    assert "resolved_service_path" not in out

    out = _fill(
        {"alb_url": "https://source/other-service"},
        stored={"alb_url": "https://mine", "resolved_service_path": "/payment-service"},
    )
    assert out["alb_url"] == "https://mine"
    assert out["resolved_service_path"] == "/payment-service"


def test_input_is_not_mutated():
    incoming = {"service_path": ""}
    _fill(incoming)
    assert incoming == {"service_path": ""}


# ── the deploy writes the ALB once ───────────────────────────────────────────

def test_the_first_deploy_stores_the_mapped_base():
    base, reason = alb_base_to_write({}, mapped_base="https://alb/", service_type="API", alb_selection=None)
    assert (base, reason) == ("https://alb", "first deploy")


def test_a_stored_alb_url_is_never_overwritten():
    for stored in ("https://alb", "https://alb/payment-service", "https://my-own-alb"):
        base, reason = alb_base_to_write(
            {"alb_url": stored}, mapped_base="https://alb", service_type="API", alb_selection=None
        )
        assert base is None and reason == "already set"


def test_rows_without_a_route_or_a_mapping_get_nothing():
    assert alb_base_to_write({}, mapped_base="https://alb", service_type="BACKGROUND_SERVICE", alb_selection=None)[0] is None
    assert alb_base_to_write({}, mapped_base=None, service_type="API", alb_selection=None) == (None, "no ALB mapping")
