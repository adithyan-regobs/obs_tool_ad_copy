"""`_assign_ingress_group_order` — slot allocation for EKS ingresses.

The pool has been exhausted twice on aspora/stage/region-aspora-mumbai. Both
times the cause was the same: the grid stepped by ten, so it held only 95
slots, and deleted service configs kept their slot forever so it could only
ever drain. 4feb0ad3 shifted the grid +5 to buy a fresh 95, which were gone
~2.5 months later. The allocator now steps by one and reclaims deleted slots.
These tests pin both down, plus the reserved orders that other generators
write directly.
"""
import logging

import pytest
from fastapi import HTTPException

from app.core.enum import ResourceStatusEnum
from app.services.service_config_service import (
    _INGRESS_ORDER_LOW_WATERMARK,
    _INGRESS_ORDER_MAX,
    _INGRESS_ORDER_MIN,
    _INGRESS_ORDER_RESERVED,
    _assign_ingress_group_order,
)

GRID = [
    n for n in range(_INGRESS_ORDER_MIN, _INGRESS_ORDER_MAX + 1)
    if n not in _INGRESS_ORDER_RESERVED
]


class _FakeResult:
    def __init__(self, orders):
        self._rows = [(o,) for o in orders]

    def fetchall(self):
        return self._rows


class _FakeSession:
    """Answers the advisory-lock statement, then the SELECT of used orders."""

    def __init__(self, used_orders):
        self._used = used_orders
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append(statement)
        if params is not None:          # the pg_advisory_xact_lock call
            return _FakeResult([])
        return _FakeResult(self._used)


async def _assign(used_orders):
    session = _FakeSession(used_orders)
    order = await _assign_ingress_group_order(
        session=session,
        tenant_code="aspora",
        environment="stage",
        geo_loc="region-aspora-mumbai",
        infrastructure_mst_code="infra-vance-eks-staging-mumbai-app-01",
    )
    return order, session


async def test_hands_out_the_lowest_free_slot():
    order, _ = await _assign([55, 56, 58])
    assert order == 57


async def test_steps_by_one_not_by_ten():
    """The capacity fix: 55 taken means 56 next, not 65."""
    order, _ = await _assign([55])
    assert order == 56


async def test_old_step_ten_values_read_as_occupied():
    """Both retired grids are inside the range now, so their values are simply
    seen as used rather than needing a fresh offset to dodge them."""
    order, _ = await _assign(list(range(55, 100)) + [100])
    assert order == 101


async def test_never_hands_out_a_reserved_order():
    """kustomize_generator_service, default_eks_deploy_script_gen_component and
    eks_generators hardcode group.order '100' for service ingresses. No
    service_configs row reports it, so only the reserved set holds it back."""
    assert 100 in _INGRESS_ORDER_RESERVED
    order, _ = await _assign(list(range(55, 100)))
    assert order == 101


async def test_deleted_configs_release_their_slot():
    """The leak: the query must exclude SOFT/HARD_DELETED configs.

    Asserted against the compiled SQL because the filter lives in the WHERE
    clause — a fake session can only show us the statement, not the rows a
    real database would have withheld.
    """
    _, session = await _assign([55])
    select_stmt = session.statements[-1]
    sql = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "status" in sql and "NOT IN" in sql.upper()
    for deleted in (ResourceStatusEnum.SOFT_DELETED, ResourceStatusEnum.HARD_DELETED):
        assert deleted.value in sql


async def test_exhausted_pool_raises_409_not_an_unhandled_error():
    """A full pool used to escape as a bare ValueError, which FastAPI turned
    into a 500 with no usable detail — that is what the MCP surfaced."""
    with pytest.raises(HTTPException) as exc:
        await _assign(GRID)
    assert exc.value.status_code == 409
    assert "infra-vance-eks-staging-mumbai-app-01" in exc.value.detail


async def test_warns_before_the_pool_runs_dry(caplog):
    taken = GRID[:-_INGRESS_ORDER_LOW_WATERMARK]
    with caplog.at_level(logging.WARNING, logger="app.services.service_config_service"):
        order, _ = await _assign(taken)
    assert order == GRID[-_INGRESS_ORDER_LOW_WATERMARK]
    assert f"Only {_INGRESS_ORDER_LOW_WATERMARK} of {len(GRID)} slots left" in caplog.text


async def test_quiet_while_the_pool_is_healthy(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.service_config_service"):
        await _assign(GRID[:10])
    assert "slots left" not in caplog.text
