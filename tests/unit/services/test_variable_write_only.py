"""
Unit tests for the write-only secret rules in VariableService._build_save_entry.

_build_save_entry is pure (no DB / no AWS), so the guards can be exercised
directly by constructing the service without __init__.
"""
from types import SimpleNamespace

import pytest

from app.core.enum import EnvironmentEnum, VariableTypeEnum
from app.schemas.variable_schemas import SaveVariableItem
from app.services.variable_service import (
    WRITE_ONLY_MASK,
    VariableService,
    _PathContext,
)


@pytest.fixture
def service() -> VariableService:
    # __init__ only wires repositories, which _build_save_entry never touches.
    return object.__new__(VariableService)


@pytest.fixture
def ctx() -> _PathContext:
    return _PathContext(
        workspace_name="ws",
        application_code="APP-1",
        application_name="app",
        environment=EnvironmentEnum.dev,
        region_name="ap-south-1",
        infra_type="eks",
        service_name="billing",
    )


@pytest.fixture
def user() -> SimpleNamespace:
    return SimpleNamespace(code="USR-1", email_id="dev@example.com")


@pytest.fixture
def tenant() -> SimpleNamespace:
    return SimpleNamespace(code="tenant1")


def _item(**overrides) -> SaveVariableItem:
    base = dict(
        transaction_code="SVC-CFG-1",
        table_name="service_config",
        key="STRIPE_KEY",
        value="sk_live_123",
        type="secret",
    )
    base.update(overrides)
    return SaveVariableItem(**base)


def _build(service, item, ctx, user, tenant, write_only_keys=frozenset()):
    return service._build_save_entry(
        item, item.value, user, tenant, ctx, {},
        set(), True, set(write_only_keys),
    )


class TestWriteOnlyFlagRules:
    def test_new_write_only_secret_sets_the_column(self, service, ctx, user, tenant):
        _r, entry, new_row, _u, _p = _build(
            service, _item(is_write_only=True), ctx, user, tenant
        )
        assert new_row.is_write_only is True
        assert new_row.variable_type == VariableTypeEnum.SECRET
        assert entry["write_only"] is True

    def test_plain_variable_cannot_be_write_only(self, service, ctx, user, tenant):
        with pytest.raises(ValueError, match="secrets only"):
            _build(service, _item(type="variable", is_write_only=True), ctx, user, tenant)

    def test_flag_cannot_be_turned_off(self, service, ctx, user, tenant):
        item = _item(variable_code="VAR-1", is_write_only=False)
        with pytest.raises(ValueError, match="cannot be made readable"):
            _build(service, item, ctx, user, tenant, write_only_keys={"STRIPE_KEY"})

    def test_omitting_the_flag_leaves_an_existing_row_write_only(
        self, service, ctx, user, tenant
    ):
        # No is_write_only in the payload at all — an old client must not be able
        # to strip the flag, and the staged entry still carries the marker.
        item = _item(variable_code="VAR-1")
        assert "is_write_only" not in item.model_fields_set
        _r, entry, _n, _u, _p = _build(
            service, item, ctx, user, tenant, write_only_keys={"STRIPE_KEY"}
        )
        assert entry["write_only"] is True

    def test_readable_secret_stays_unmarked(self, service, ctx, user, tenant):
        _r, entry, new_row, _u, _p = _build(service, _item(), ctx, user, tenant)
        assert new_row.is_write_only is False
        assert "write_only" not in entry


class TestWriteOnlyValueRules:
    def test_mask_value_is_rejected(self, service, ctx, user, tenant):
        item = _item(variable_code="VAR-1", value=WRITE_ONLY_MASK)
        with pytest.raises(ValueError, match="never shown"):
            _build(service, item, ctx, user, tenant, write_only_keys={"STRIPE_KEY"})

    def test_new_key_needs_a_value(self, service, ctx, user, tenant):
        with pytest.raises(ValueError, match="a value is required"):
            _build(service, _item(value="   ", is_write_only=True), ctx, user, tenant)

    def test_flag_can_be_turned_on_without_resupplying_the_value(
        self, service, ctx, user, tenant
    ):
        # Marking an already-deployed readable secret write-only must not force
        # the user to retype a value they may not have — empty means "carry the
        # deployed value over", which deploy already implements.
        item = _item(variable_code="VAR-1", value="", is_write_only=True)
        result, entry, _n, _u, _p = _build(service, item, ctx, user, tenant)
        assert result.status == "success"
        assert entry["write_only"] is True
        assert entry["newValue"] == ""

    def test_rename_may_carry_the_value_over(self, service, ctx, user, tenant):
        # Empty value on a rename means "keep the deployed value"; deploy reads
        # it back internally, so it stays legal for write-only keys.
        item = _item(key="STRIPE_SECRET", value="", variable_code="VAR-1", old_key="STRIPE_KEY")
        _r, entry, _n, _u, _p = _build(
            service, item, ctx, user, tenant, write_only_keys={"STRIPE_KEY"}
        )
        assert entry["old_key"] == "STRIPE_KEY"
        assert entry["write_only"] is True

    def test_delete_needs_no_value(self, service, ctx, user, tenant):
        item = _item(value="", variable_code="VAR-1", operation="delete")
        result, entry, _n, _u, _p = _build(
            service, item, ctx, user, tenant, write_only_keys={"STRIPE_KEY"}
        )
        assert result.status == "success"
        assert entry["operation"] == "delete"
