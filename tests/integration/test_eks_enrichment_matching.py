"""
Integration test: EKS service-node enrichment matching.

Reproduces the canvas duplication bug for aspora CORE stage, end to end, using:
  * the REAL enrichment code path (VpcAndResourceDiscoveryService.get_canvas_data),
  * the REAL aspora CORE_STAGE static scan data (via _MOCK_CANVAS_DATA), and
  * a READ-ONLY connection to the live app database.

Why this exists
---------------
Scanned EKS service nodes (e.g. "alphadesk-api-service") were rendering as TWO
canvas nodes: the running pod (no serviceCode/resourceDbCode) plus a DRAFT node
re-emitted by _append_draft_service_nodes. Two compounding causes:

  1. get_all_services() defaults to order_by(created_at DESC).limit(100), so when
     an application has > 100 services the oldest are dropped and can NEVER match.
  2. The scanned name carries a "-service"/"_service" suffix (and case/underscore
     variants) that services_mst.name lacks, so even loaded services miss on an
     exact-string lookup.

This test fails while either bug is present and passes once both are fixed.

Running
-------
Read-only; point it at the live DB via env vars (do NOT commit credentials):

    export DB_HOST=... DB_PORT=5432 DB_NAME=app_db DB_USER=app_user DB_PASSWORD=...
    pytest tests/integration/test_eks_enrichment_matching.py -v -s

Skips automatically if the DB env vars are not set.
"""
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.repository.services_mst_repository import ServicesMstRepository
from app.services.vpc_and_resource_discovery_service import VpcAndResourceDiscoveryService

# aspora → core application (see _MOCK_CANVAS_DATA in the discovery service)
TENANT_CODE = "aspora"
CORE_APPLICATION_CODE = "d19899af-78e8-44aa-b95f-afd932a019e3"
ENVIRONMENT = "stage"

_REQUIRED_ENV = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")


def _dsn() -> str:
    host = os.environ["DB_HOST"]
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ["DB_NAME"]
    user = os.environ["DB_USER"]
    pwd = os.environ["DB_PASSWORD"]
    return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{name}"


pytestmark = pytest.mark.skipif(
    not all(os.environ.get(k) for k in _REQUIRED_ENV),
    reason=f"Set {', '.join(_REQUIRED_ENV)} to run this live read-only integration test",
)


@pytest.fixture
async def readonly_db():
    """Read-only async session against the live DB (separate from the _test fixture)."""
    engine = create_async_engine(_dsn(), echo=False, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()


def _is_eks_service(node) -> bool:
    return (
        node.resourceType in ("service", "model-serving")
        and str(node.settings.get("resourceSubtype", "")).startswith("eks")
    )


@pytest.mark.asyncio
async def test_get_all_services_is_not_truncated(readonly_db):
    """Bug #1: the enrichment lookup must include EVERY service, not the newest 100.

    _enrich_service_nodes calls get_all_services without overriding limit (=100).
    If total > len(services), older services are invisible to matching.
    """
    repo = ServicesMstRepository(readonly_db)
    result = await repo.get_all_services(TENANT_CODE, application_code=CORE_APPLICATION_CODE)

    total = result.get("total", 0)
    loaded = len(result.get("services", []))
    print(f"\n[bug#1] services_mst total={total}  loaded_by_enrichment={loaded}")

    assert loaded == total, (
        f"get_all_services truncated the lookup: {loaded} of {total} services loaded. "
        f"The {total - loaded} dropped (oldest) services can never match and will "
        f"render as duplicate DRAFT nodes. Raise/remove the limit in _enrich_service_nodes."
    )


@pytest.mark.asyncio
async def test_no_duplicate_eks_service_nodes(readonly_db):
    """Bug #1 + #2: every scanned EKS service should be enriched (serviceCode set)
    and must not also appear as a separate DRAFT node for the same workload."""
    service = VpcAndResourceDiscoveryService()
    canvas = await service.get_canvas_data(
        TENANT_CODE, CORE_APPLICATION_CODE, ENVIRONMENT, readonly_db
    )
    assert canvas is not None

    eks = [n for n in canvas.nodes if _is_eks_service(n)]

    # A scanned pod node carries replicas/containers; a DB-draft node does not.
    scanned = [n for n in eks if "eks-dep-" in str(n.id)]
    unmatched = [n for n in scanned if not n.settings.get("serviceCode")]

    # Group by namespace to surface "two nodes in one namespace" duplicates.
    from collections import defaultdict

    by_ns = defaultdict(list)
    for n in eks:
        by_ns[n.settings.get("namespace")].append(n.name)
    dup_ns = {
        ns: names
        for ns, names in by_ns.items()
        if ns and not ns.endswith("-system") and len(names) > 1
    }

    print(f"\n[scan] eks service nodes={len(eks)} scanned={len(scanned)} "
          f"unmatched(no serviceCode)={len(unmatched)}")
    if unmatched:
        print("  unmatched scanned services:")
        for n in sorted(unmatched, key=lambda x: x.name):
            print(f"    - {n.name:<34} ns={n.settings.get('namespace')}")
    if dup_ns:
        print("  namespaces with >1 node (duplicates):")
        for ns, names in sorted(dup_ns.items()):
            print(f"    - {ns}: {sorted(names)}")

    assert not unmatched, (
        f"{len(unmatched)} scanned EKS services were not enriched (no serviceCode) — "
        f"they will be re-emitted as DRAFT duplicates by _append_draft_service_nodes."
    )
    assert not dup_ns, f"Duplicate EKS service nodes found in namespaces: {list(dup_ns)}"
