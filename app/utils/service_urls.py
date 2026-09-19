"""Derived service URLs.

The service URL is built rather than stored, because its halves are stored for
different reasons and none is a URL on its own: `config["alb_url"]` is the
shared ALB the service rides (written once on its first successful deploy, or
by hand for a service that needs its own ALB), `config["service_path"]` is the
Ingress path, and `config["health"]` is the path the POD serves — it feeds the
liveness and readiness probes and the ALB healthcheck annotation.

Rows written before the base-only rule hold the ALB plus the ingress path in
`alb_url`, and the Jenkins webhook still writes a full URL for other tenants,
so every join here drops a segment run the base already ends with.
"""

from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from app.utils.service_routing import clean_service_path


def _segments(path: Optional[str]) -> List[str]:
    return [s for s in (path or "").split("/") if s]


def _split_alb_url(alb_url: str) -> Tuple[str, List[str]]:
    """(scheme://host, path segments) — a bare host has no segments."""
    parts = urlsplit(alb_url)
    if parts.scheme:
        return f"{parts.scheme}://{parts.netloc}", _segments(parts.path)
    return alb_url.rstrip("/"), []


def _append(base: List[str], extra: List[str]) -> List[str]:
    """`base + extra`, minus the longest run of segments `extra` starts with
    that `base` already ends with.

    Compared segment by segment: a raw prefix test would treat the ingress path
    "/foo" as already present in the health path "/foobar/health".
    """
    overlap = 0
    for i in range(min(len(base), len(extra)), 0, -1):
        if base[-i:] == extra[:i]:
            overlap = i
            break
    return base + extra[overlap:]


def _url(base: str, segments: List[str]) -> str:
    return base.rstrip("/") + ("/" + "/".join(segments) if segments else "")


def build_service_url(alb_url: str, service_path: str) -> str:
    """The service's externally reachable root: the ALB plus its ingress path.

    Returns "" without an ALB. Without a path it is the ALB itself.
    """
    alb_url = (alb_url or "").strip()
    if not alb_url:
        return ""
    base, segments = _split_alb_url(alb_url)
    return _url(base, _append(segments, _segments(clean_service_path(service_path))))


def build_health_url(alb_url: str, health_path: str, service_path: Optional[str] = None) -> str:
    """The service's externally reachable health endpoint.

    Returns "" when either the ALB or the health path is missing.

    The ingress has no rewrite (see kustomize_generator_service._generate_ingress),
    so a request arrives at the pod with the ingress path still on the front.
    An app served under its own context path therefore stores that path inside
    `health` — "/goms-service/actuator/health" is correct for it, not a typo —
    while an app served at the root stores just "/health". Joining blindly
    repeats the segment for the first kind, so the shared leading segment is
    dropped when it is already there.

    With `service_path` given, the ingress path is put in front of a health
    path that does not already carry it, so the URL shown reaches the pod the
    way the ALB routes to it.
    """
    alb_url = (alb_url or "").strip()
    health_path = (health_path or "").strip()
    if not alb_url or not health_path:
        return ""

    base, segments = _split_alb_url(alb_url)
    if service_path:
        segments = _append(segments, _segments(clean_service_path(service_path)))
    return _url(base, _append(segments, _segments(health_path)))
