"""build_service_url / build_health_url — joining the ALB, the ingress path and
the pod's health path.

The row stores the ALB alone since the write-once rule; rows written before it
(and Jenkins tenants) hold the ALB plus the ingress path. The halves overlap for
apps served under a context path and do not for apps served at the root, so no
join is a plain concatenation.
"""
import pytest

from app.utils.service_urls import build_health_url, build_service_url

BASE = "https://alb.internal.example"


@pytest.mark.parametrize(
    "alb_url,health,expected",
    [
        # Health already carries the ingress path — do not repeat it.
        (f"{BASE}/kairos", "/kairos/api/health", f"{BASE}/kairos/api/health"),
        (f"{BASE}/pots-service", "/pots-service/actuator/health", f"{BASE}/pots-service/actuator/health"),
        (f"{BASE}/goms-service", "/goms-service/actuator/healthy", f"{BASE}/goms-service/actuator/healthy"),
        # Health is relative to the pod root — keep the ingress path.
        (f"{BASE}/amal-svc", "/health", f"{BASE}/amal-svc/health"),
        (f"{BASE}/goms-service", "healthy", f"{BASE}/goms-service/healthy"),
        # Root-mounted service.
        (f"{BASE}/", "/health", f"{BASE}/health"),
        # A shared text prefix is not a shared segment.
        (f"{BASE}/foo", "/foobar/health", f"{BASE}/foo/foobar/health"),
        # A base-only ALB with a prefixed health path.
        (BASE, "/pots-service/actuator/health", f"{BASE}/pots-service/actuator/health"),
    ],
)
def test_join(alb_url, health, expected):
    assert build_health_url(alb_url, health) == expected


@pytest.mark.parametrize(
    "alb_url,health,service_path,expected",
    [
        # Base-only ALB: the ingress path goes in front of a bare health path…
        (BASE, "/actuator/health", "/pots-service", f"{BASE}/pots-service/actuator/health"),
        # …and is not repeated when the health path already carries it.
        (BASE, "/pots-service/actuator/health", "/pots-service", f"{BASE}/pots-service/actuator/health"),
        # An old full URL is not doubled either.
        (f"{BASE}/pots-service", "/actuator/health", "/pots-service", f"{BASE}/pots-service/actuator/health"),
        (f"{BASE}/pots-service", "/pots-service/actuator/health", "/pots-service/*", f"{BASE}/pots-service/actuator/health"),
        # No path known: same as before.
        (BASE, "/health", "", f"{BASE}/health"),
    ],
)
def test_join_with_service_path(alb_url, health, service_path, expected):
    assert build_health_url(alb_url, health, service_path) == expected


@pytest.mark.parametrize("alb_url,health", [(f"{BASE}/svc", ""), ("", "/health"), ("", "")])
def test_missing_half_yields_nothing(alb_url, health):
    assert build_health_url(alb_url, health) == ""


@pytest.mark.parametrize(
    "alb_url,service_path,expected",
    [
        (BASE, "/pots-service", f"{BASE}/pots-service"),
        (f"{BASE}/", "/pots-service", f"{BASE}/pots-service"),
        (BASE, "pots-service", f"{BASE}/pots-service"),
        # Rows written before the base-only rule already carry the path.
        (f"{BASE}/pots-service", "/pots-service", f"{BASE}/pots-service"),
        (f"{BASE}/api", "/api/v1", f"{BASE}/api/v1"),
        # Nothing to add.
        (BASE, "", BASE),
        (BASE, None, BASE),
        # A hand-set ALB for a service on its own load balancer.
        ("https://internal-alb.example", "/pots-service", "https://internal-alb.example/pots-service"),
    ],
)
def test_service_url(alb_url, service_path, expected):
    assert build_service_url(alb_url, service_path) == expected


def test_service_url_without_an_alb_is_empty():
    assert build_service_url("", "/pots-service") == ""
    assert build_service_url(None, "/pots-service") == ""
