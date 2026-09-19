"""ApprovalService.discard — the whole draft goes in one call.

Pure-stub tests: the repo, the FGA `require` and the secret-service client
are replaced, so no DB, no OpenFGA, no HTTP.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.approval_service as svc_module
from app.core.enum import WorkflowSourceTableEnum
from app.db.models.transaction_queue_model import TransactionQueueStatusEnum as S
from app.services.approval_service import (
    GATEWAY_CASE_REF,
    SETTINGS_CASE_REF,
    VARIABLES_CASE_REF,
    ApprovalService,
)


def _row(code, case_ref, status=S.DRAFT, user="u-1", changes=None):
    r = SimpleNamespace(
        code=code, case_ref_code=case_ref, status=status, user_code=user,
        transaction_code="sc-1", tenant_code="t1", is_deleted=False,
        table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
        config_snapshot={"service_name": "demo"}, history=[], changes=changes or {},
        deleted_at=None,
    )

    def soft_delete():
        r.is_deleted = True

    r.soft_delete = soft_delete
    return r


class _Repo:
    def __init__(self, rows):
        self.rows = rows
        self.locked = False
        self.list_calls = 0

    async def get_by_code(self, code, tenant_code=None):
        return next((r for r in self.rows if r.code == code), None)

    async def list_change_set(self, transaction_code, tenant_code, statuses, user_code=None):
        self.list_calls += 1
        return [r for r in self.rows if r.status in statuses and not r.is_deleted]

    async def lock_change_set(self, transaction_code, tenant_code, statuses,
                              user_code=None, exclude_case_ref=None):
        assert not _Client.calls, "our rows are locked BEFORE the secret call"
        assert exclude_case_ref == VARIABLES_CASE_REF, "the variables row must stay unlocked"
        self.locked = True
        return [r for r in self.rows
                if r.status in statuses and not r.is_deleted
                and r.case_ref_code != exclude_case_ref]


class _Client:
    calls = []
    fail = None

    def __init__(self, timeout=60.0):
        self.timeout = timeout

    async def discard_variables_draft(self, **kw):
        _Client.calls.append(kw)
        if _Client.fail:
            raise _Client.fail
        # the secret service soft-deletes the variables row itself
        for r in _Client.rows:
            if r.case_ref_code == VARIABLES_CASE_REF:
                r.is_deleted = True
        return {"file_deleted": True, "keys_dropped": ["A"], "orphans_purged": 1,
                "queue_row_retired": "tq-var"}


@pytest.fixture
def stubs(monkeypatch):
    required = []

    async def _require(request, user, permission, ref, status, message):
        required.append(permission)

    monkeypatch.setattr(svc_module, "require", _require)
    import app.integrations.secret_config_client as client_module
    monkeypatch.setattr(client_module, "SecretConfigClient", _Client)
    _Client.calls, _Client.fail, _Client.rows = [], None, []
    return required


def _service(rows):
    s = ApprovalService.__new__(ApprovalService)
    s.repo = _Repo(rows)
    _Client.rows = rows
    return s


async def test_settings_click_bins_settings_gateway_and_variables(stubs):
    settings, gateway, variables = (
        _row("tq-set", SETTINGS_CASE_REF), _row("tq-gw", GATEWAY_CASE_REF),
        _row("tq-var", VARIABLES_CASE_REF),
    )
    s = _service([settings, gateway, variables])

    out = await s.discard(request=None, user="me", user_code="u-1", queue_code="tq-set")

    assert out is settings
    assert settings.is_deleted and gateway.is_deleted and variables.is_deleted
    assert settings.history[-1]["event"] == "discarded"
    assert _Client.calls == [{"transaction_code": "sc-1", "user_code": "u-1", "tenant_code": "t1"}]
    assert _Client.calls and s.repo.list_calls == 1
    # every lane's own right, once each: settings, routes, and — the variables
    # row carries no kinds here — both variable rights (fails closed)
    assert stubs == ["can_write_settings", "can_write_gateway",
                     "can_write_config", "can_write_secret"]


async def test_secret_failure_leaves_everything_untouched(stubs):
    settings, variables = _row("tq-set", SETTINGS_CASE_REF), _row("tq-var", VARIABLES_CASE_REF)
    s = _service([settings, variables])
    _Client.fail = RuntimeError("boom")

    with pytest.raises(HTTPException) as exc:
        await s.discard(request=None, user="me", user_code="u-1", queue_code="tq-set")

    assert exc.value.status_code == 502
    assert not settings.is_deleted and not variables.is_deleted


async def test_variables_only_draft_uses_kind_permission_and_returns_none(stubs):
    variables = _row("tq-var", VARIABLES_CASE_REF,
                     changes={"variables": [{"kind": "secret"}]})
    s = _service([variables])

    out = await s.discard(request=None, user="me", user_code="u-1", queue_code="tq-var")

    assert out is None
    assert variables.is_deleted
    assert stubs == ["can_write_secret"]
    assert len(_Client.calls) == 1


async def test_no_variables_row_means_no_secret_call(stubs):
    settings = _row("tq-set", SETTINGS_CASE_REF)
    s = _service([settings])

    out = await s.discard(request=None, user="me", user_code="u-1", queue_code="tq-set")

    assert out is settings and settings.is_deleted
    assert _Client.calls == []


async def test_only_author_and_only_draft(stubs):
    s = _service([_row("tq-set", SETTINGS_CASE_REF, user="someone-else")])
    with pytest.raises(HTTPException) as exc:
        await s.discard(request=None, user="me", user_code="u-1", queue_code="tq-set")
    assert exc.value.status_code == 403

    s = _service([_row("tq-set", SETTINGS_CASE_REF, status=S.SUBMIT)])
    with pytest.raises(HTTPException) as exc:
        await s.discard(request=None, user="me", user_code="u-1", queue_code="tq-set")
    assert exc.value.status_code == 409

    s = _service([])
    with pytest.raises(HTTPException) as exc:
        await s.discard(request=None, user="me", user_code="u-1", queue_code="nope")
    assert exc.value.status_code == 404
    assert _Client.calls == []


async def test_anchor_moved_between_read_and_lock_is_409(stubs):
    settings = _row("tq-set", SETTINGS_CASE_REF)
    s = _service([settings, _row("tq-var", VARIABLES_CASE_REF)])
    orig = s.repo.lock_change_set

    async def _lock(*a, **kw):
        settings.status = S.SUBMIT  # another tab submitted meanwhile
        return await orig(*a, **kw)

    s.repo.lock_change_set = _lock
    with pytest.raises(HTTPException) as exc:
        await s.discard(request=None, user="me", user_code="u-1", queue_code="tq-set")
    assert exc.value.status_code == 409
    assert _Client.calls == [] and not settings.is_deleted


async def test_revoked_variables_right_blocks_a_settings_click(monkeypatch):
    """Discard from the settings row still needs the write right for the
    variables it would remove — a revoked author cannot wipe staged secrets."""
    async def _require(request, user, permission, ref, status, message):
        if permission == "can_write_secret":
            raise HTTPException(status, message)

    monkeypatch.setattr(svc_module, "require", _require)
    import app.integrations.secret_config_client as client_module
    monkeypatch.setattr(client_module, "SecretConfigClient", _Client)
    _Client.calls, _Client.fail = [], None

    settings = _row("tq-set", SETTINGS_CASE_REF)
    variables = _row("tq-var", VARIABLES_CASE_REF, changes={"variables": [{"kind": "secret"}]})
    s = _service([settings, variables])

    with pytest.raises(HTTPException) as exc:
        await s.discard(request=None, user="me", user_code="u-1", queue_code="tq-set")

    assert exc.value.status_code == 403
    assert _Client.calls == []
    assert not settings.is_deleted and not variables.is_deleted
