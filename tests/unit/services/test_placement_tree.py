"""Placement options are the combinations that actually have infrastructure.

The selectors used to be a cartesian product — every product, a hardcoded
dev/stage/qa/prod list, and every geo location the tenant owned — so a user
could pick a placement with no cluster behind it and only find out when
provisioning failed. `get_placement_tree` asks the same question, with the same
predicates, as `find_eks_cluster_for_mcp`, so the dropdown and the resolver
cannot disagree.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.enum import EnvironmentEnum
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository


EKS = "eks_infrastructuretype_ref"
S3 = "s3_infrastructuretype_ref"


def _repo(rows):
    """A repository whose single query returns `rows`, as
    (applications_mst_code, environments_enum, geo_loc_mst_code, locator)."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    return InfrastructureMstRepository(session)


REGISTERED = {"isRegistered": True}


@pytest.mark.asyncio
async def test_returns_the_combinations_that_exist():
    repo = _repo([
        ("app-1", EnvironmentEnum.stage, "geo-mumbai", REGISTERED),
        ("app-1", EnvironmentEnum.prod, "geo-london", REGISTERED),
    ])
    tree = await repo.get_placement_tree("t1", EKS)
    assert tree == [
        {"applications_mst_code": "app-1", "environment": "stage", "geo_loc_mst_code": "geo-mumbai"},
        {"applications_mst_code": "app-1", "environment": "prod", "geo_loc_mst_code": "geo-london"},
    ]


@pytest.mark.asyncio
async def test_a_cluster_needs_isregistered():
    """`isRegistered` means 'service creation allowed' for cluster types."""
    repo = _repo([
        ("app-1", EnvironmentEnum.stage, "geo-mumbai", REGISTERED),
        ("app-1", EnvironmentEnum.qa, "geo-mumbai", {}),
        ("app-1", EnvironmentEnum.dev, "geo-mumbai", None),
    ])
    envs = {row["environment"] for row in await repo.get_placement_tree("t1", EKS)}
    assert envs == {"stage"}


@pytest.mark.asyncio
async def test_non_cluster_types_are_not_gated_on_isregistered():
    """The flag is never set on S3/SQS/DynamoDB rows — requiring it there would
    erase every one of those placements."""
    repo = _repo([
        ("app-1", EnvironmentEnum.stage, "geo-mumbai", {}),
        ("app-1", EnvironmentEnum.prod, "geo-london", None),
    ])
    envs = {row["environment"] for row in await repo.get_placement_tree("t1", S3)}
    assert envs == {"stage", "prod"}


@pytest.mark.asyncio
async def test_tenant_level_infrastructure_is_offered_under_every_product():
    """A NULL applications_mst_code is a cluster shared across every
    application, not an unattributed row. aspora's QA clusters are all
    tenant-level, which is why the web UI offers QA — dropping them here would
    hide a whole environment the user can legitimately deploy to."""
    repo = _repo([
        ("app-1", EnvironmentEnum.stage, "geo-mumbai", REGISTERED),
        (None, EnvironmentEnum.qa, "geo-mumbai", REGISTERED),
    ])
    tree = await repo.get_placement_tree("t1", EKS, ["app-1", "app-2"])
    qa = {row["applications_mst_code"] for row in tree if row["environment"] == "qa"}
    assert qa == {"app-1", "app-2"}


@pytest.mark.asyncio
async def test_a_shared_cluster_does_not_duplicate_a_scoped_one():
    repo = _repo([
        ("app-1", EnvironmentEnum.stage, "geo-mumbai", REGISTERED),
        (None, EnvironmentEnum.stage, "geo-mumbai", REGISTERED),
    ])
    tree = await repo.get_placement_tree("t1", EKS, ["app-1"])
    assert tree == [
        {"applications_mst_code": "app-1", "environment": "stage", "geo_loc_mst_code": "geo-mumbai"},
    ]


@pytest.mark.asyncio
async def test_duplicate_placements_collapse():
    repo = _repo([
        ("app-1", EnvironmentEnum.stage, "geo-mumbai", REGISTERED),
        ("app-1", EnvironmentEnum.stage, "geo-mumbai", REGISTERED),
        ("app-2", EnvironmentEnum.stage, "geo-mumbai", REGISTERED),
    ])
    tree = await repo.get_placement_tree("t1", EKS)
    assert len(tree) == 2
    assert {row["applications_mst_code"] for row in tree} == {"app-1", "app-2"}


@pytest.mark.asyncio
async def test_no_infrastructure_means_no_placements():
    assert await _repo([]).get_placement_tree("t1", EKS) == []


class TestResourceGroupKindFilter:
    """A service belongs in a 'service' group; 'infra' groups hold S3, SQS and
    the like. The web UI filters these out client-side; the chatbot did not, so
    a service could be filed under an infra group."""

    @pytest.mark.asyncio
    async def test_kind_is_passed_through_to_the_repository(self):
        from app.services.resource_group_mst_service import ResourceGroupMstService

        service = ResourceGroupMstService(AsyncMock())
        service.resource_groups_repository.get_all_resource_groups = AsyncMock(
            return_value={"total": 0, "resource_groups": []}
        )
        service.applications_repository.get_codes_by_workspaces = AsyncMock(
            return_value=["app-1"]
        )

        from app.services import resource_group_mst_service as mod

        original = mod.WorkspaceService
        try:
            mod.WorkspaceService = lambda _s: MagicMock(
                verify_app_workspace_access=AsyncMock(return_value=True)
            )
            await service.get_all_resource_groups(
                tenant_code="t1", user_code="u1",
                application_code="app-1", kind="service",
            )
        finally:
            mod.WorkspaceService = original

        kwargs = service.resource_groups_repository.get_all_resource_groups.await_args.kwargs
        assert kwargs["kind"] == "service"

    @pytest.mark.asyncio
    async def test_kind_defaults_to_none_so_existing_callers_are_unchanged(self):
        from app.services.resource_group_mst_service import ResourceGroupMstService

        service = ResourceGroupMstService(AsyncMock())
        service.resource_groups_repository.get_all_resource_groups = AsyncMock(
            return_value={"total": 0, "resource_groups": []}
        )
        from app.services import resource_group_mst_service as mod

        original = mod.WorkspaceService
        try:
            mod.WorkspaceService = lambda _s: MagicMock(
                verify_app_workspace_access=AsyncMock(return_value=True)
            )
            await service.get_all_resource_groups(
                tenant_code="t1", user_code="u1", application_code="app-1",
            )
        finally:
            mod.WorkspaceService = original

        kwargs = service.resource_groups_repository.get_all_resource_groups.await_args.kwargs
        assert kwargs["kind"] is None


@pytest.mark.asyncio
async def test_the_resolver_falls_back_to_a_tenant_level_cluster():
    """The dropdown and the resolver must agree: a placement offered because of
    a shared cluster has to resolve to that cluster, or provisioning dead-ends
    with 'no cluster found' — the bug this whole change exists to remove."""
    session = AsyncMock()
    scoped = MagicMock()
    scoped.scalars.return_value.all.return_value = []          # nothing for this app
    shared_cluster = MagicMock(code="infra-qa-shared", name="qa-shared", locator=REGISTERED)
    shared = MagicMock()
    shared.scalars.return_value.all.return_value = [shared_cluster]
    session.execute = AsyncMock(side_effect=[scoped, shared])

    repo = InfrastructureMstRepository(session)
    code, candidates = await repo.find_eks_cluster_for_mcp(
        tenant_code="t1", application_code="app-1",
        environment=EnvironmentEnum.qa, geo_loc_mst_code="geo-mumbai",
    )
    assert code == "infra-qa-shared"
    assert len(candidates) == 1
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_an_application_scoped_cluster_wins_over_a_shared_one():
    session = AsyncMock()
    own = MagicMock(code="infra-app-own", name="own", locator=REGISTERED)
    scoped = MagicMock()
    scoped.scalars.return_value.all.return_value = [own]
    session.execute = AsyncMock(return_value=scoped)

    repo = InfrastructureMstRepository(session)
    code, _ = await repo.find_eks_cluster_for_mcp(
        tenant_code="t1", application_code="app-1",
        environment=EnvironmentEnum.qa, geo_loc_mst_code="geo-mumbai",
    )
    assert code == "infra-app-own"
    assert session.execute.await_count == 1   # no fallback query needed


@pytest.mark.asyncio
async def test_an_unregistered_cluster_is_never_offered_for_a_new_service():
    """`isRegistered` IS the "service creation allowed" flag, and the web's
    picker already hides unregistered clusters. Without this the MCP offered
    three clusters for stage/Mumbai where the dashboard showed fewer, and a
    service could land on one nobody had cleared for use."""
    session = AsyncMock()
    rows = [
        MagicMock(code="infra-cleared", name="cleared", locator={"isRegistered": True}),
        MagicMock(code="infra-not-flagged", name="not flagged", locator={}),
        MagicMock(code="infra-explicit-false", name="off", locator={"isRegistered": False}),
        MagicMock(code="infra-no-locator", name="none", locator=None),
    ]
    scoped = MagicMock()
    scoped.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=scoped)

    repo = InfrastructureMstRepository(session)
    code, candidates = await repo.find_eks_cluster_for_mcp(
        tenant_code="t1", application_code="app-1",
        environment=EnvironmentEnum.stage, geo_loc_mst_code="geo-mumbai",
    )
    assert [c["code"] for c in candidates] == ["infra-cleared"]
    # One survivor means it resolves outright — the user is not asked to pick.
    assert code == "infra-cleared"


@pytest.mark.asyncio
async def test_only_unregistered_clusters_means_no_cluster():
    """Better a clear "none available" than silently placing a service on a
    cluster nobody cleared. The caller's message already says to ask DevOps to
    register one."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        MagicMock(code="infra-off", name="off", locator={"isRegistered": False}),
    ]
    session.execute = AsyncMock(return_value=result)

    repo = InfrastructureMstRepository(session)
    code, candidates = await repo.find_eks_cluster_for_mcp(
        tenant_code="t1", application_code="app-1",
        environment=EnvironmentEnum.stage, geo_loc_mst_code="geo-mumbai",
    )
    assert code is None and candidates == []


class TestAllowedEnvironments:
    """Per-deployment environment allowlist, the mirror of the dashboard's
    NEXT_PUBLIC_ALLOWED_ENVS.

    One database holds every environment, so a stage deployment's placements
    legitimately include prod — those clusters and services really exist, and
    no amount of data-driven filtering will hide them. Narrowing that is a
    deployment decision, and this is where it is declared. It limits what is
    OFFERED only; deploying still requires can_deploy on the target.
    """

    def _settings(self, value):
        import os
        from app.core.config import Settings

        os.environ["ALLOWED_ENVIRONMENTS"] = value
        return Settings()

    def test_unset_means_no_restriction(self):
        s = self._settings("")
        assert s.allowed_env_codes is None
        assert all(s.is_env_allowed(e) for e in ("dev", "stage", "qa", "prod"))

    def test_a_single_environment(self):
        s = self._settings("prod")
        assert s.allowed_env_codes == ["prod"]
        assert s.is_env_allowed("prod") and not s.is_env_allowed("stage")

    def test_several_environments(self):
        s = self._settings("stage,qa")
        assert s.allowed_env_codes == ["stage", "qa"]
        assert s.is_env_allowed("qa") and not s.is_env_allowed("prod")

    def test_spacing_and_case_are_tolerated(self):
        """Typed into a deployment config by hand, and parsed the same way the
        dashboard parses its own value, so one answer configures both."""
        s = self._settings(" Stage , QA ")
        assert s.allowed_env_codes == ["stage", "qa"]
        assert s.is_env_allowed("STAGE")

    def test_blank_entries_are_dropped(self):
        assert self._settings("stage,,").allowed_env_codes == ["stage"]
