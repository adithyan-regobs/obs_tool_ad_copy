"""
Temporal client singleton.

Call get_temporal_client() to get a connected client.
The client is lazily created on first call and reused.
"""

import logging
from temporalio.client import Client, TLSConfig
from temporalio.service import RPCError, RPCStatusCode
from app.core.config import settings

logger = logging.getLogger(__name__)

_temporal_client: Client | None = None


async def get_temporal_client() -> Client:
    global _temporal_client
    if _temporal_client is None:
        target = f"{settings.temporal_host}:{settings.temporal_port}"
        logger.info(f"Connecting to Temporal at {target} (tls={settings.temporal_tls_enabled}, namespace={settings.temporal_namespace})")
        tls = TLSConfig(domain=settings.temporal_tls_domain) if settings.temporal_tls_enabled else False
        _temporal_client = await Client.connect(
            target,
            namespace=settings.temporal_namespace,
            tls=tls,
        )
        logger.info("Temporal client connected")
    return _temporal_client


def workflow_query_http_error(exc: Exception, workflow_id: str):
    """Turn a failed workflow query into the RIGHT HTTP error.

    Every caller of this used to answer `except Exception` with a flat 404, so a
    Temporal blip — a connection drop, a worker restart mid-query, a deadline —
    was indistinguishable from a workflow that had genuinely completed and aged
    out. The deploy poller reads 404 as "it's over": it stopped tracking, wiped
    its localStorage key and re-enabled the Deploy button while the deploy was
    still running, and a second click queued another orchestrator behind the
    first one's locks with nothing on screen explaining the wait.

    Only NOT_FOUND means gone. Everything else means WE could not find out,
    which is a 503 the caller should retry rather than act on:

      404  the workflow genuinely does not exist (or has been purged)
      503  anything else — unreachable, timed out, query handler failed

    A query that FAILS is explicitly in the 503 group: the workflow answered
    badly, which means it is still there.
    """
    from fastapi import HTTPException

    if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
        return HTTPException(
            status_code=404,
            detail=f"Workflow {workflow_id} not found — it may have completed and been purged.",
        )

    logger.warning(
        "temporal query failed for %s (not a NOT_FOUND — reporting 503): %s",
        workflow_id, exc, exc_info=True,
    )
    return HTTPException(
        status_code=503,
        detail=(
            "Could not reach the workflow service to check this deploy. "
            "The deploy itself is unaffected — this will retry."
        ),
    )
