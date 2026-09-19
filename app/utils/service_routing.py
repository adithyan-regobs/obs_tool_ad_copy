"""Service routing defaults — one rule for the ingress path and the probe path.

The same rule is used when a config is saved, when k8s-manifests are rendered
and when the service URL is shown, so the row, the rendered Ingress and the
displayed link cannot disagree. Two generators once defaulted an empty
service_path differently ("/" vs "/<name>-service"), which is how a stored ALB
URL came to point at a path nothing routed; `resolved_service_path` was added
to record what a render actually used. Settling the value at save time removes
the guess altogether.
"""

from typing import Any, Mapping, Optional, Tuple

from app.utils.language_helpers import is_java_language

EKS_INFRA_TYPE_REF = "eks_infrastructuretype_ref"

# Service types that get an Ingress on EKS. Workers (BACKGROUND_SERVICE) probe
# over tcpSocket and have no path; MODEL_SERVING has its own KServe URL.
INGRESS_SERVICE_TYPES = frozenset({"API", "OPS_TOOLS"})

# Written by the deploy pipeline, never by a client. A client that round-trips
# a config it read (promote, change-cluster, the old Deployment tab) carries
# these along; they are dropped and the stored value, if any, is kept.
BACKEND_OWNED_KEYS = ("alb_url", "resolved_service_path")


def normalize_service_name(name: Optional[str]) -> str:
    """The name the platform deploys under: lowercase, spaces and underscores
    to hyphens, a `-service` suffix when it is missing. "helloworld",
    "helloworld-service" and "Hello_World" all land on the same name."""
    n = (name or "").strip().lower().replace(" ", "-").replace("_", "-")
    if not n:
        return ""
    return n if n.endswith("-service") else f"{n}-service"


def default_service_path(service_name: Optional[str]) -> str:
    n = normalize_service_name(service_name)
    return f"/{n}" if n else ""


def clean_service_path(value: Any) -> str:
    """A usable ingress path, or "" for blank and catch-all values.

    Only slashes and wildcards ("/", "//", "/*") is a catch-all Ingress rule
    that takes over every other service on the shared ALB, so it counts as
    unset. A trailing /* is an ALB listener pattern, not part of the path —
    k8s-manifests rules use a Prefix pathType without a wildcard.
    """
    sp = str(value or "").strip()
    if not sp.strip("/*"):
        return ""
    if sp.endswith("/*"):
        sp = sp[:-2]
    if not sp.startswith("/"):
        sp = "/" + sp
    return sp


def clean_health_path(value: Any) -> str:
    h = str(value or "").strip()
    if not h:
        return ""
    return h if h.startswith("/") else "/" + h


def default_health_path(service_path: Optional[str], language_name: Optional[str] = None) -> str:
    """The probe path for an app served under `service_path`.

    The Ingress does no rewrite, so the pod sees the ingress path on the front
    of every request and the probe path must carry it too. Spring Boot exposes
    actuator; everything else exposes /health. An unknown language keeps the
    actuator path, which was the only default before languages were consulted.
    """
    base = clean_service_path(service_path)
    if language_name and not is_java_language(language_name):
        return f"{base}/health"
    return f"{base}/actuator/health"


def resolve_service_path(config: Mapping[str, Any], service_name: Optional[str]) -> str:
    """The ingress path a render uses: the stored value, else the default."""
    return clean_service_path(config.get("service_path")) or default_service_path(service_name)


def resolve_health_path(
    config: Mapping[str, Any],
    service_name: Optional[str],
    language_name: Optional[str] = None,
) -> str:
    """The probe path a render uses: the stored value, else the default under
    the resolved ingress path."""
    return clean_health_path(config.get("health")) or default_health_path(
        resolve_service_path(config, service_name), language_name
    )


def display_service_path(config: Mapping[str, Any]) -> str:
    """The ingress path to join onto the ALB for display: what the user set,
    else what the last render recorded, else nothing.

    No name default here, on purpose. A model server, a PaaS service served at
    the root or an ECS `/*` rule has no path, and a guessed `/<name>-service`
    would be glued onto a URL that is already complete. A row that has a path
    carries it — filled at save, or recorded by the render.
    """
    return (
        clean_service_path(config.get("service_path"))
        or clean_service_path(config.get("resolved_service_path"))
    )


def has_alb_route(service_type: Any, alb_selection: Optional[str]) -> bool:
    """True for a service the shared ALB routes to. An unknown type counts as
    routable — only a worker, a model server or an explicit no_alb is not."""
    st = str(getattr(service_type, "value", service_type) or "").upper()
    if st and st not in INGRESS_SERVICE_TYPES:
        return False
    return (alb_selection or "existing_alb") != "no_alb"


def needs_ingress(
    infrastructuretype_ref_code: Optional[str],
    service_type: Any,
    alb_selection: Optional[str],
) -> bool:
    """True for an EKS service that gets an Ingress rule on the shared ALB."""
    if (infrastructuretype_ref_code or "") != EKS_INFRA_TYPE_REF:
        return False
    st = str(getattr(service_type, "value", service_type) or "").upper()
    return st in INGRESS_SERVICE_TYPES and has_alb_route(st, alb_selection)


def alb_base_to_write(
    config: Optional[Mapping[str, Any]],
    *,
    mapped_base: Optional[str],
    service_type: Any,
    alb_selection: Optional[str],
) -> Tuple[Optional[str], str]:
    """What the deploy should store as the row's ALB, and the reason when nothing.

    The ALB is written once. A value already on the row — the first deploy's
    base, or a base set by hand for a service that rides its own ALB — is kept,
    which is what lets a hand-set value survive every redeploy.
    """
    cfg = config or {}
    if str(cfg.get("alb_url") or "").strip():
        return None, "already set"
    if not has_alb_route(service_type, alb_selection):
        return None, "no ALB route"
    base = str(mapped_base or "").strip().rstrip("/")
    if not base:
        return None, "no ALB mapping"
    return base, "first deploy"


def fill_routing_defaults(
    config: Optional[Mapping[str, Any]],
    *,
    service_name: Optional[str],
    language_name: Optional[str],
    infrastructuretype_ref_code: Optional[str],
    service_type: Any,
    alb_selection: Optional[str],
    stored: Optional[Mapping[str, Any]] = None,
    deployed: bool = False,
) -> dict:
    """A copy of `config` with service_path and health settled.

    - A stored value always beats a blank incoming one: blank means "no
      change", whether the client omitted the key or sent "".
    - Nothing stored and never deployed: the name/language default. Health
      waits for the language — the canvas creates the row before one is
      chosen, and a stamped actuator path would then stick to a Go service.
      It is settled on the first settings save, which carries the language.
    - Nothing stored but already deployed: left alone. The repo holds the
      truth (an ingress.path may have been hand-edited there) and the sync tool
      backfills it; guessing here would overwrite that path on the next render.

    Backend-owned keys are dropped from the input and restored from `stored`.
    """
    out = dict(config or {})
    stored = stored or {}

    for key in BACKEND_OWNED_KEYS:
        out.pop(key, None)
        if stored.get(key):
            out[key] = stored[key]

    if not needs_ingress(infrastructuretype_ref_code, service_type, alb_selection):
        return out

    service_path = clean_service_path(out.get("service_path"))
    if not service_path:
        service_path = clean_service_path(stored.get("service_path"))
        if not service_path and not deployed:
            service_path = default_service_path(service_name)
        if service_path:
            out["service_path"] = service_path
        else:
            out.pop("service_path", None)

    health = clean_health_path(out.get("health"))
    if not health:
        health = clean_health_path(stored.get("health"))
        if not health and not deployed and language_name:
            health = default_health_path(
                service_path or default_service_path(service_name), language_name
            )
        if health:
            out["health"] = health
        else:
            out.pop("health", None)

    return out
