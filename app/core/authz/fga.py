"""Authorization checks for obs_tool.

obs_tool does not talk to OpenFGA directly — devlift-secret-config-manager is
the only service permitted to reach it. Every authorization decision is made
by calling that service's internal check endpoint, reusing the same base URL
and X-Internal-Key as the variable-deploy call.

Tuple/model management (write, read, model edit) lives entirely in
devlift-secret-config-manager and is reached from the frontend; obs_tool no
longer carries an OpenFGA client or any admin surface.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


async def check(user: str, relation: str, obj: str, context: dict | None = None) -> bool:
    """Answer: may `user` perform `relation` on `obj`?

    Delegates to devlift-secret-config-manager's `/internal/authz/check`.
    `context` supplies runtime values that conditions evaluate against
    (e.g. current_time) and is forwarded on the request body.
    """
    url = settings.secret_service_url.rstrip("/") + "/api/v1/internal/authz/check"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"user": user, "relation": relation, "object": obj, "context": context},
                headers={"X-Internal-Key": settings.secret_service_internal_key},
            )
        resp.raise_for_status()
        return bool(resp.json()["allowed"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        # Fail CLOSED: any transport error, non-2xx, or unexpected body shape
        # denies the request rather than 500-ing. require() turns a False into
        # the route's deny_status; the cause is logged for diagnosis.
        # For a non-2xx, include the upstream status + body so the real reason
        # (e.g. 404 wrong prefix, 403 bad key, OpenFGA validation) is visible.
        detail = ""
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f" upstream_status={exc.response.status_code} body={exc.response.text!r}"
        logger.exception(
            "authz check failed, denying: user=%s relation=%s obj=%s error=%r%s",
            user, relation, obj, exc, detail,
        )
        return False


async def batch_check(
    checks: Sequence[Tuple[str, str, str]],
    context: Optional[Dict[str, Any]] = None,
) -> List[bool]:
    """Answer many (user, relation, object) questions in ONE round trip.

    Delegates to devlift-secret-config-manager's `/internal/authz/batch-check`,
    which fronts OpenFGA's native BatchCheck. Same transport and the same
    X-Internal-Key as `check()` — obs_tool still never reaches OpenFGA itself.

    Exists because the approvals surface asks in bulk: the inbox needs
    can_approve and can_deploy for every service on the page, and one record's
    button state needs three answers about one service. Asking those one request
    at a time is what this replaces.

    Returns a list of booleans POSITIONALLY MATCHED to `checks` — the endpoint
    correlates by index because OpenFGA may reorder its results, so callers can
    unpack directly:

        write, approve, deploy = await batch_check([
            (user, "can_write_settings", ref),
            (user, "can_approve", ref),
            (user, "can_deploy", ref),
        ])

    Relation names must exist in the published model (see rbac-model.fga). An
    unknown one is not a loud error here: the check errors upstream and this
    function's except-branch turns it into False, so a typo reads as a denial
    rather than a crash. That is the right default, but it means a wrong name
    is invisible until someone reports being locked out — which is why
    `can_update` was replaced above rather than left as a stale example.
    """
    if not checks:
        return []

    url = settings.secret_service_url.rstrip("/") + "/api/v1/internal/authz/batch-check"
    payload = {
        "checks": [
            {"user": user, "relation": relation, "object": obj}
            for user, relation, obj in checks
        ],
        "context": context,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"X-Internal-Key": settings.secret_service_internal_key},
            )
        resp.raise_for_status()
        allowed = resp.json()["allowed"]
        if len(allowed) != len(checks):
            # A short or long list would silently misalign every answer with the
            # wrong question, which is worse than denying: the caller unpacks by
            # position and would grant on someone else's verdict.
            raise ValueError(
                f"batch-check returned {len(allowed)} answers for {len(checks)} checks"
            )
        return [bool(a) for a in allowed]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        # Fail CLOSED, exactly as check() does, and for the whole batch: a
        # partial answer cannot be told from a full one by position, so half a
        # verdict is not safer than none. require() turns each False into the
        # route's deny_status; the cause is logged.
        detail = ""
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f" upstream_status={exc.response.status_code} body={exc.response.text!r}"
        logger.exception(
            "authz batch check failed, denying all %d: error=%r%s",
            len(checks), exc, detail,
        )
        return [False] * len(checks)


async def list_objects(
    user: str,
    relation: str,
    obj_type: str,
    context: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Every object of `obj_type` on which `user` holds `relation`.

    The reverse of check, and the one that scales. batch_check asks N
    questions and is capped (100) so no caller can fan out unboundedly inside
    OpenFGA; past the cap the request 400s and — authz failing closed — every
    verdict comes back False, which a filtered list renders as "you may do
    nothing". Indistinguishable from an empty inbox, and invisible to the
    browser: the approvals page returned 200 with no rows while the very same
    user could approve a change from the service panel, because that asked
    about ONE service. It broke at 113.

    This asks once, whatever the size, and the caller intersects the answer
    with its own candidates.

    Returns FULLY-QUALIFIED ids ("service:sc-123"), the form OpenFGA uses and
    the form fga_ref builds, so callers compare without re-assembling either
    side.

    Fails CLOSED, like everything here: an unreachable authz service means an
    empty list, so the caller sees nothing rather than everything. A type the
    published model does not define (infra_mst today) errors upstream and
    lands here too, which is why callers ask per type — one undefined type
    must not deny the types that are defined.
    """
    url = settings.secret_service_url.rstrip("/") + "/api/v1/internal/authz/list-objects"
    payload = {
        "user": user,
        "relation": relation,
        "object_type": obj_type,
        "context": context,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"X-Internal-Key": settings.secret_service_internal_key},
            )
        resp.raise_for_status()
        return [str(o) for o in resp.json()["objects"]]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        detail = ""
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f" upstream_status={exc.response.status_code} body={exc.response.text!r}"
        logger.exception(
            "authz list-objects failed, returning none: user=%s relation=%s "
            "type=%s error=%r%s",
            user, relation, obj_type, exc, detail,
        )
        return []
