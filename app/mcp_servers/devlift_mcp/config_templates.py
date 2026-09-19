"""Per-language starting values for a new EKS service.

`templates/service-config/eks-language-config-template.json` holds, for each
language, what 107 live deployments in stage/ap-south-1 actually run with. It
covers the form's sections 5-11 (Runtime, Dockerfile, Resources, Networking,
Scaling, IAM, Extras) — everything that is a property of the language rather
than of this particular service.

It deliberately does NOT cover sections 1-4: product, environment, region,
resource group, service name, service type, repository, branches, language and
version are the ten things only the user can answer, and they are answered
before this is ever loaded — the template is keyed by language, so it cannot
be chosen until section 4 is done.

A `null` means "no reliable default, ask" (service_path and health always;
port for Python; the replica fields where the sample was too small). Dropping
those from the answers leaves the form to ask for them, which is what should
happen. A field that is optional-now-but-required-later comes back through
`get_unanswered_fields`, which re-asks a skipped field the moment a
conditional rule makes it required.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3]
    / "templates" / "service-config" / "eks-language-config-template.json"
)

# The template names a field the way the deployment does; the form names it the
# way it asks for it. Only the autoscaling block differs.
_FIELD_ALIASES = {
    "hpa.enabled": "hpa_enabled",
    "hpa.min_replicas": "min_replicas",
    "hpa.max_replicas": "max_replicas",
}

# Present in the deployments this was derived from, absent from the form: the
# manifests carry them, but nobody is ever asked for them, so sending them as
# answers would be sending fields that do not exist.
_NOT_FORM_FIELDS = frozenset({
    "namespace", "ebs_enabled", "ebs.volume", "ebs.size", "ebs.type",
    "secrets_enabled", "secret_keys", "ci_provider",
})

# The form takes its booleans as the strings its dropdowns offer.
_BOOLEAN_FIELDS = frozenset({
    "generate_dockerfile", "go_use_aws_secrets", "hpa_enabled",
    "create_ecr", "create_secrets", "create_ssm", "create_argo",
})



_cache: Optional[dict] = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("config_templates: cannot read %s", _TEMPLATE_PATH)
            _cache = {}
    return _cache


def language_names() -> list[str]:
    """The languages a template exists for, as the form labels them."""
    return sorted(k for k in _load() if not k.startswith("_"))


def template_for_language(language: Optional[str]) -> Optional[dict]:
    """The raw template block for a language label, or None.

    The label is the one the form's Language dropdown shows, which
    `language_ref_service` builds as "Java Maven" / "Java Gradle" / the first
    word of the name. Matched case-insensitively so "go" finds "Go".
    """
    if not language:
        return None
    blocks = _load()
    wanted = str(language).strip().lower()
    for name, block in blocks.items():
        if not name.startswith("_") and name.lower() == wanted:
            return block
    return None


def normalise_service_name(service_name: Optional[str]) -> str:
    """The name the platform actually deploys under.

    Mirrors `_normalize_service_name` in the manifest generator: lowercased,
    spaces and underscores to hyphens, a `-service` suffix when it is missing.
    So "helloworld", "helloworld-service" and "helloworld_service" all land on
    helloworld-service, whichever way the user typed it.
    """
    from app.utils.service_routing import normalize_service_name

    return normalize_service_name(service_name)


def _resolve_placeholders(value: str, service_name: Optional[str]) -> Optional[str]:
    """`/{service_name}` and `{service_path}/health`, filled in per service.

    These two cannot be stored as fixed values the way a CPU request can —
    they depend on what the user calls the service. None when there is no name
    yet, which drops the field and leaves the form to ask, as before.
    """
    name = normalise_service_name(service_name)
    if not name:
        return None
    out = value.replace("{service_name}", name)
    return out.replace("{service_path}", f"/{name}")


def template_answers(language: Optional[str], service_name: Optional[str] = None) -> dict[str, Any]:
    """The template for `language` as `chat(answers=...)` — form field ids.

    Empty when the language has no template, so the caller simply carries on
    asking the user field by field.
    """
    block = template_for_language(language)
    if not block:
        return {}

    answers: dict[str, Any] = {}
    for key, value in block.items():
        if key in _NOT_FORM_FIELDS:
            continue
        if value is None:
            continue                       # "no reliable default" — ask for it
        field_id = _FIELD_ALIASES.get(key, key)
        if value == "" or (isinstance(value, list) and not value):
            continue                       # "leave it unset" — see template_skips
        if field_id in _BOOLEAN_FIELDS:
            text = "true" if bool(value) else "false"
        elif isinstance(value, list):
            answers[field_id] = value
            continue
        else:
            text = str(value)
            if "{" in text:
                resolved = _resolve_placeholders(text, service_name)
                if resolved is None:
                    continue               # no service name yet — let the form ask
                text = resolved
        answers[field_id] = text
    return answers


def template_skips(language: Optional[str]) -> list[str]:
    """Fields the template deliberately leaves unset.

    An empty value — `[]` for "no IAM policies", `""` for "let the JVM pick
    its own heap" — is not an answer the form can take: structured answers
    drop both on the way in, so the field stays unanswered and gets asked.
    That is how Custom IAM Policies, Additional Trigger Paths and then Xms /
    Xmx each came back as questions immediately after the template had
    settled them.

    "Leave it unset" is a skip, which is exactly what the form's own Skip
    option records for an optional field — and a skipped optional field is not
    asked again. Distinct from `null`, which means nobody knows the right
    value and the user genuinely should be asked.
    """
    block = template_for_language(language) or {}
    return [
        _FIELD_ALIASES.get(k, k)
        for k, v in block.items()
        if k not in _NOT_FORM_FIELDS
        and (v == "" or (isinstance(v, list) and not v))
    ]


# What has to be answered before a template means anything. The template says
# how a service RUNS; these say what it IS and where it lives, and no template
# can supply one of them — they resolve to applications_mst_code,
# resource_group_code, services_mst.name and service_type, environment,
# geo_loc_mst_code, language_ref_code and the repository the build reads.
#
# Offering before they are in produces a review of twenty settings for a
# service that has no resource group yet, and — because service_path and
# health are built from the name — a path resolved from nothing: one service
# came out as /chat-bot-poc with a bare /health while the next got
# /demo-serv-mcp-service and a matching probe.
_PREREQUISITE_FIELDS = (
    "product",           # applications_mst_code
    "resource_group",    # resource_group_code
    "service_name",
    "service_type",
    "environment",
    "geo_location",      # geo_loc_mst_code
    "repository",
    "branches",
    "language",
    "version",
)


def prerequisites_met(collected: Optional[dict]) -> bool:
    """True once every field the template cannot answer has been answered."""
    data = collected or {}
    return all(data.get(f) not in (None, "", [], {}) for f in _PREREQUISITE_FIELDS)


def missing_prerequisites(collected: Optional[dict]) -> list[str]:
    """Which of them are still open — for logging, in prerequisite order."""
    data = collected or {}
    return [f for f in _PREREQUISITE_FIELDS if data.get(f) in (None, "", [], {})]


def unset_fields(language: Optional[str], service_name: Optional[str] = None) -> list[str]:
    """Form fields the template leaves for the user, in template order.

    A placeholder field counts as unset only while the service has no name —
    once it does, service_path and health are worked out rather than asked.
    """
    block = template_for_language(language) or {}
    named = bool(normalise_service_name(service_name))
    return [
        _FIELD_ALIASES.get(k, k)
        for k, v in block.items()
        if k not in _NOT_FORM_FIELDS
        and (v is None or (isinstance(v, str) and "{" in v and not named))
    ]
