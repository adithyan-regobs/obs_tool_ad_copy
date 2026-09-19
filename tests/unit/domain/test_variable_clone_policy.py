"""
Unit tests for the variable clone recommendation policy.
"""
import pytest

from app.core.enum import EnvironmentEnum
from app.domain.policies.variable_clone_policy import (
    ENV_RANK,
    pick_environment,
    pick_geo_loc,
    sort_environments,
)


@pytest.mark.unit
def test_every_enum_member_is_rankable():
    # Runtime must never crash on an env missing from ENV_RANK.
    for env in EnvironmentEnum:
        assert pick_environment(env, list(EnvironmentEnum)) is not None


@pytest.mark.unit
def test_env_rank_covers_all_enum_members():
    # Fails loud in CI when EnvironmentEnum grows: add the new member to
    # ENV_RANK so its promotion order is an explicit decision.
    assert set(ENV_RANK) == set(EnvironmentEnum)


@pytest.mark.unit
def test_exact_match_wins():
    assert pick_environment(
        EnvironmentEnum.stage, [EnvironmentEnum.stage, EnvironmentEnum.prod]
    ) == EnvironmentEnum.stage


@pytest.mark.unit
def test_nearest_rank_prefers_lower_on_tie():
    assert pick_environment(
        EnvironmentEnum.stage, [EnvironmentEnum.dev, EnvironmentEnum.qa, EnvironmentEnum.prod]
    ) == EnvironmentEnum.qa


@pytest.mark.unit
def test_prod_never_suggested_unless_only_option():
    assert pick_environment(
        EnvironmentEnum.dev, [EnvironmentEnum.prod, EnvironmentEnum.qa]
    ) == EnvironmentEnum.qa
    assert pick_environment(
        EnvironmentEnum.dev, [EnvironmentEnum.prod]
    ) == EnvironmentEnum.prod


@pytest.mark.unit
def test_prod_source_can_get_prod():
    assert pick_environment(
        EnvironmentEnum.prod, [EnvironmentEnum.dev, EnvironmentEnum.prod]
    ) == EnvironmentEnum.prod


@pytest.mark.unit
def test_empty_options():
    assert pick_environment(EnvironmentEnum.stage, []) is None
    assert pick_geo_loc("mumbai", []) is None


@pytest.mark.unit
def test_sort_environments_promotion_order():
    shuffled = [EnvironmentEnum.prod, EnvironmentEnum.dev, EnvironmentEnum.stage, EnvironmentEnum.qa]
    assert sort_environments(shuffled) == ENV_RANK


@pytest.mark.unit
def test_pick_geo_loc_rules():
    assert pick_geo_loc("mumbai", ["london", "mumbai"]) == "mumbai"
    assert pick_geo_loc("mumbai", ["london"]) == "london"
    assert pick_geo_loc(None, ["us", "london"]) == "london"


@pytest.mark.unit
async def test_target_env_fully_excluded_when_geo_unknown():
    # Without a target region the service can't tell which combo the
    # variables would land on, so the entire target environment must be
    # excluded for the target service — otherwise the recommendation points
    # back at the target itself.
    from unittest.mock import AsyncMock

    from app.services.variable_clone_service import VariableCloneService

    service = VariableCloneService.__new__(VariableCloneService)
    service.config_repo = AsyncMock()
    service.config_repo.get_env_geo_options_for_service.return_value = [
        {
            "environment": EnvironmentEnum.dev,
            "geo_loc_code": "GEO-IN",
            "geo_loc_name": "Mumbai",
            "infra_vendor_enum": None,
            "config": None,
        },
        {
            "environment": EnvironmentEnum.dev,
            "geo_loc_code": "GEO-UK",
            "geo_loc_name": "London",
            "infra_vendor_enum": None,
            "config": None,
        },
        {
            "environment": EnvironmentEnum.qa,
            "geo_loc_code": "GEO-IN",
            "geo_loc_name": "Mumbai",
            "infra_vendor_enum": None,
            "config": None,
        },
    ]

    options = await service._build_env_options(
        tenant_code="T1",
        service_code="SVC-1",
        target_environment=EnvironmentEnum.dev,
        target_geo_loc_code=None,
        exclude_target_combo=True,
    )
    assert [o.environment for o in options] == [EnvironmentEnum.qa]

    # With the target region known, only that exact combo is excluded.
    options = await service._build_env_options(
        tenant_code="T1",
        service_code="SVC-1",
        target_environment=EnvironmentEnum.dev,
        target_geo_loc_code="GEO-IN",
        exclude_target_combo=True,
    )
    dev = next(o for o in options if o.environment == EnvironmentEnum.dev)
    assert [r.geo_loc_code for r in dev.regions] == ["GEO-UK"]
