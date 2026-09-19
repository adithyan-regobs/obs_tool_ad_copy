"""Finding the EKS workflow a service already deploys through.

DevLift names the workflow it generates
`deploy-{service}-service-eks-{env}-{geo}.yml`. Services onboarded before
DevLift carry whatever name their author chose — `aspora-stage-api.yml`,
`aspora-stage-worker.yml` — and no convention was ever imposed on those names.
Writing the generated name beside such a file left the service with TWO
workflows firing on every push, so the locator adopts the existing file
instead.

Names are therefore used for NOTHING but ordering. What identifies a workflow
is the `with:` block it passes to the shared-lib build:

    with:
      service_name: pulse-backend-service
      organization: vance-core
      environment:  stage

A file is this service's pipeline when all three agree with the service being
deployed. That is also what separates an API from its worker: they are
distinct services with distinct `service_name` inputs (`pulse-backend-service`
vs `pulse-backend-worker-service`), so nothing has to reason about the words
"api" or "worker" in a file name.

Temporary: once workflow naming is standardized this whole module goes, along
with its two call sites.
"""

import logging
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

import yaml

logger = logging.getLogger(__name__)

WORKFLOW_EXTENSIONS = (".yml", ".yaml")

#: Environment spellings that mean the same environment. The locator holds the
#: folder env ('stage'), the deployments API holds whatever the UI sent
#: ('staging'), and a hand-written file says whatever its author typed.
ENV_ALIASES = (
    ("dev", "development"),
    ("stage", "staging", "stg"),
    ("qa",),
    ("prod", "production", "prd"),
)


def env_aliases(env: str) -> Set[str]:
    """Every spelling of `env`. Looked up as a MEMBER of an alias group, not
    as a key, so callers holding different spellings resolve to one set."""
    value = (env or "").strip().lower()
    if not value:
        return set()
    for group in ENV_ALIASES:
        if value in group:
            return set(group)
    return {value}


def _tokens(name: str) -> Set[str]:
    """File name split into lowercase words on every non-alphanumeric run.

    Token equality, not substring: `deploy` contains `dep` and `production`
    contains `prod`, so a substring test would match names that have nothing
    to do with the environment.
    """
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


def _service_key(name: str) -> str:
    """A service name reduced to what identifies it, so the many spellings of
    one service compare equal: `Arpo_Service`, `arpo`, `arpo-service`."""
    value = re.sub(r"[\s_]+", "-", (name or "").strip().lower())
    value = re.sub(r"-+", "-", value).strip("-")
    return value[: -len("-service")] if value.endswith("-service") else value


def parse_workflow_inputs(content: str) -> Dict[str, Any]:
    """The `with:` inputs a reusable-workflow call passes, flattened across
    every job — `service_name`, `environment`, `aws_region`, `language`, …

    Parsed as YAML so only real job inputs are read: a job-level
    `environment:` (the GitHub deployment environment) sits outside `with:`
    and must not be mistaken for the service's environment. A file that will
    not parse — a template still holding `{{PLACEHOLDERS}}`, a broken edit —
    yields {}, which reads downstream as 'cannot be verified' and therefore
    'do not adopt'.
    """
    if not content:
        return {}
    try:
        doc = yaml.safe_load(content)
    except Exception:  # noqa: BLE001 — any malformed file is simply unverifiable
        logger.debug("Workflow file could not be parsed as YAML", exc_info=True)
        return {}
    if not isinstance(doc, dict):
        return {}

    inputs: Dict[str, Any] = {}
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    for job in jobs.values():
        if isinstance(job, dict) and isinstance(job.get("with"), dict):
            for key, value in job["with"].items():
                inputs.setdefault(str(key), value)
    return inputs


def workflow_matches_service(
    content: str,
    service_name: str,
    env: str,
    organization: str,
) -> bool:
    """True when this workflow file's own inputs say it deploys THIS service.

    All three are required. `service_name` alone is not enough — two products
    can run a service of the same name — and `organization` is what shared-lib
    builds the ECR path and the secrets/config paths from, so it is the thing
    that actually separates them. A file stating none of them is not evidence
    of anything, and adopting it would hand the service someone else's
    pipeline.

    `aws_region` is deliberately NOT compared: plenty of hand-written files
    never pass it, and requiring it would decline exactly the files this
    exists to find.

    A file with no `jobs.*.with` block at all — a test-only workflow, a
    release job, anything not calling the shared-lib build — cannot match,
    which is how non-deploy workflows in the same directory are passed over.
    """
    if not (service_name and organization):
        return False

    inputs = parse_workflow_inputs(content)
    if not inputs:
        return False

    file_service = _service_key(str(inputs.get("service_name") or ""))
    if not file_service or file_service != _service_key(service_name):
        return False

    file_org = str(inputs.get("organization") or "").strip().lower()
    if not file_org or file_org != organization.strip().lower():
        return False

    file_env = str(inputs.get("environment") or "").strip().lower()
    if not file_env or file_env not in env_aliases(env):
        return False

    return True


def _ordered_candidates(
    file_names: Iterable[str],
    env: str,
    service_name: str,
    canonical_name: str,
) -> List[str]:
    """Every workflow file except the generated one, likeliest first.

    No file is excluded on its name: `aspora-stage-api.yml` carries neither
    the service, nor `eks`, nor anything else a filter could key on, and it is
    exactly the kind of file this exists to find. The name only decides the
    ORDER in which files are opened, so the usual answer costs one request and
    the long shots are still reachable.
    """
    names = [
        n for n in file_names
        if n and n.lower().endswith(WORKFLOW_EXTENSIONS)
        and n.lower() != canonical_name.lower()
    ]
    envs = env_aliases(env)
    service_tokens = _tokens(service_name) - {"service"}

    def rank(name: str):
        tokens = _tokens(name)
        return (
            0 if service_tokens and service_tokens <= tokens else 1,
            0 if tokens & envs else 1,
            0 if "eks" in tokens else 1,
            name,
        )

    return sorted(names, key=rank)


async def resolve_eks_workflow(
    file_names: Iterable[str],
    env: str,
    service_name: str,
    canonical_name: str,
    fetch_content: Callable[[str], Awaitable[Optional[str]]],
    organization: Optional[str] = None,
) -> Optional[str]:
    """The workflow file this service already deploys through, or None to mean
    'use the generated name'.

    `fetch_content(name) -> str | None` reads one file from the same branch
    the listing came from; the caller supplies it so this stays independent of
    how each call site talks to GitHub.
    """
    names = [n for n in file_names if n and n.lower().endswith(WORKFLOW_EXTENSIONS)]
    if not names:
        return None

    # The generated file already exists — this IS the file, nothing to adopt
    # and nothing to verify: DevLift wrote it.
    for name in names:
        if name.lower() == canonical_name.lower():
            return name

    if not organization:
        # Nothing to verify against — adopting on service+environment alone
        # would risk another product's pipeline. Say so rather than open every
        # file to no purpose.
        logger.info(
            "No organization for %s/%s — cannot verify any workflow, using the "
            "generated name", service_name, env,
        )
        return None

    candidates = _ordered_candidates(names, env, service_name, canonical_name)
    if not candidates:
        return None

    for name in candidates:
        try:
            content = await fetch_content(name)
        except Exception:  # noqa: BLE001 — an unreadable file is just not adopted
            logger.warning("Could not read workflow %s", name, exc_info=True)
            continue
        if content and workflow_matches_service(
            content, service_name, env, organization
        ):
            return name

    return None
