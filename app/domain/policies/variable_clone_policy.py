"""
Variable Clone Recommendation Policy

Pure functions that pick the best default target environment/region for the
env-variable clone flow. No DB access — keeps the rules unit-testable and
independent of the service layer.
"""

from typing import Any, Callable, Dict, List, Optional

from app.core.enum import EnvironmentEnum, InfraVendorEnum

# Promotion order used for "closest environment" matching.
ENV_RANK: List[EnvironmentEnum] = [
    EnvironmentEnum.dev,
    EnvironmentEnum.qa,
    EnvironmentEnum.stage,
    EnvironmentEnum.prod,
]


def _rank(env: EnvironmentEnum) -> float:
    """Rank of an environment; envs added to the enum later but missing from
    ENV_RANK slot between stage and prod so they sort deterministically and
    never displace the prod guard."""
    try:
        return float(ENV_RANK.index(env))
    except ValueError:
        return ENV_RANK.index(EnvironmentEnum.stage) + 0.5


def sort_environments(envs: List[EnvironmentEnum]) -> List[EnvironmentEnum]:
    return sorted(envs, key=lambda e: (_rank(e), e.value))


def pick_environment(
    source_env: EnvironmentEnum,
    available: List[EnvironmentEnum],
) -> Optional[EnvironmentEnum]:
    """
    Pick the best target environment.

    Rules:
      1. Exact match wins.
      2. Otherwise nearest by rank distance (dev < qa < stage < prod),
         preferring the lower-ranked env on a tie.
      3. Never suggest prod unless the source is prod — except when prod is
         the only option.
    """
    if not available:
        return None
    if source_env in available:
        return source_env

    pool = available
    if source_env != EnvironmentEnum.prod:
        non_prod = [e for e in available if e != EnvironmentEnum.prod]
        if non_prod:
            pool = non_prod

    src_rank = _rank(source_env)
    return min(pool, key=lambda e: (abs(_rank(e) - src_rank), _rank(e), e.value))


def pick_geo_loc(
    source_geo_loc_code: Optional[str],
    available: List[str],
) -> Optional[str]:
    """Pick the best target region (geo_loc): exact match > only option > first sorted."""
    if not available:
        return None
    if source_geo_loc_code and source_geo_loc_code in available:
        return source_geo_loc_code
    if len(available) == 1:
        return available[0]
    return sorted(available)[0]


# ── Cloud-region resolution (per infra vendor) ─────────────────────────────
# service_configs stores the vendor-neutral geo_loc; the concrete cloud region
# (e.g. ap-south-1) lives in the config JSONB written at deploy time. Each
# vendor gets its own extractor so GCP/Azure can plug in without touching
# callers.

def _aws_cloud_region(config: Optional[Dict[str, Any]]) -> Optional[str]:
    if not config:
        return None
    return config.get("region") or config.get("cloud_region_id")


_CLOUD_REGION_RESOLVERS: Dict[InfraVendorEnum, Callable[[Optional[Dict[str, Any]]], Optional[str]]] = {
    InfraVendorEnum.aws: _aws_cloud_region,
}


def resolve_cloud_region(
    vendor: Optional[InfraVendorEnum],
    config: Optional[Dict[str, Any]],
) -> Optional[str]:
    resolver = _CLOUD_REGION_RESOLVERS.get(vendor) if vendor else None
    return resolver(config) if resolver else None
