"""ArgoCD status enrichment — cache, name mapping and quiet failure.

Pure-stub tests: Redis, the ArgoCD client and the tenant config lookup are all
replaced, so no Redis, no DB, no HTTP.
"""
import json
from types import SimpleNamespace

import pytest

import app.services.argocd_status_service as svc
from app.integrations.argocd_integration import ArgoCDError, ArgoCDIntegration

HOST = "argocd.internal.example"
SETTINGS = {
    "host": HOST,
    "project": "argocd-project-core-stage-applications-service",
    "app_namespace": "argocd-system",
    "token_env": "ARGOCD_TOKEN_TEST",
}
APPS = {"ad-test-2255-service": {"sync": "Synced", "health": "Healthy"}}


class FakeRedis:
    """In-memory stand-in; TTLs are recorded, never enforced."""

    def __init__(self, seed=None):
        self.store = dict(seed or {})
        self.ttls = {}
        self.locks = set()

    async def get_json(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    async def set_json(self, key, value, ttl=None):
        self.store[key] = json.dumps(value)
        self.ttls[key] = ttl
        return True

    async def set(self, key, value, ttl=None):
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    async def exists(self, key):
        return key in self.store

    async def acquire_lock(self, key, ttl=10):
        if key in self.locks:
            return False
        self.locks.add(key)
        return True

    async def release_lock(self, key):
        self.locks.discard(key)
        return True


@pytest.fixture
def env(monkeypatch):
    """Wire the stubs; returns the fake Redis and a list of ArgoCD calls made."""
    calls = []

    def _install(redis, apps=APPS, error=None, argocd=None):
        monkeypatch.setenv("ARGOCD_TOKEN_TEST", "tok")
        monkeypatch.setattr(svc, "RedisIntegration", redis)

        async def fake_get_tenant_config(tenant_code, db):
            return SimpleNamespace(argocd_for=lambda e: argocd if argocd is not None else SETTINGS)

        monkeypatch.setattr(svc, "get_tenant_config", fake_get_tenant_config)

        async def fake_list(self, project=""):
            calls.append(project)
            if error:
                raise error
            return apps

        monkeypatch.setattr(ArgoCDIntegration, "list_application_status", fake_list)
        return calls

    return _install


async def test_healthy_service_is_enriched(env):
    redis = FakeRedis()
    env(redis)

    out = await svc.status_for_services(
        "aspora", [("sc-1", "ad-test-2255", "stage")], db=None
    )

    assert out["sc-1"]["argocd_sync_status"] == "Synced"
    assert out["sc-1"]["argocd_health_status"] == "Healthy"
    assert out["sc-1"]["argocd_url"] == (
        f"https://{HOST}/applications/argocd-system/ad-test-2255-service"
    )
    assert out["sc-1"]["argocd_as_of"]
    assert out["sc-1"]["argocd_state"] == svc.STATE_OK


async def test_name_already_suffixed_is_not_doubled(env):
    redis = FakeRedis()
    env(redis)

    out = await svc.status_for_services(
        "aspora", [("sc-1", "ad-test-2255-service", "stage")], db=None
    )

    assert out["sc-1"]["argocd_health_status"] == "Healthy"


async def test_unknown_service_is_reported_as_not_found(env):
    redis = FakeRedis()
    env(redis)

    out = await svc.status_for_services("aspora", [("sc-9", "not-there", "stage")], db=None)

    assert out["sc-9"]["argocd_state"] == svc.STATE_NOT_FOUND
    assert out["sc-9"]["argocd_health_status"] is None


async def test_one_call_serves_many_services(env):
    redis = FakeRedis()
    calls = env(redis)

    await svc.status_for_services("aspora", [("sc-1", "ad-test-2255", "stage")], db=None)
    await svc.status_for_services("aspora", [("sc-2", "ad-test-2255", "stage")], db=None)

    assert len(calls) == 1


async def test_failure_falls_back_to_last_known_good(env):
    last = {"apps": APPS, "as_of": "2026-09-15T10:00:00+00:00"}
    redis = FakeRedis({"argocd:status:aspora:stage:last": json.dumps(last)})
    env(redis, error=ArgoCDError("boom"))

    out = await svc.status_for_services(
        "aspora", [("sc-1", "ad-test-2255", "stage")], db=None
    )

    assert out["sc-1"]["argocd_health_status"] == "Healthy"
    assert out["sc-1"]["argocd_as_of"] == last["as_of"]
    assert "argocd:status:aspora:stage:cooldown" in redis.store


async def test_failure_with_no_history_is_reported_as_unavailable(env):
    redis = FakeRedis()
    env(redis, error=ArgoCDError("boom"))

    out = await svc.status_for_services(
        "aspora", [("sc-1", "ad-test-2255", "stage")], db=None
    )

    assert out["sc-1"]["argocd_state"] == svc.STATE_UNAVAILABLE
    assert out["sc-1"]["argocd_health_status"] is None


async def test_cooldown_skips_the_call(env):
    last = {"apps": APPS, "as_of": "2026-09-15T10:00:00+00:00"}
    redis = FakeRedis({
        "argocd:status:aspora:stage:last": json.dumps(last),
        "argocd:status:aspora:stage:cooldown": "1",
    })
    calls = env(redis)

    out = await svc.status_for_services(
        "aspora", [("sc-1", "ad-test-2255", "stage")], db=None
    )

    assert calls == []
    assert out["sc-1"]["argocd_health_status"] == "Healthy"


async def test_no_tenant_config_is_reported_as_not_configured(env):
    redis = FakeRedis()
    calls = env(redis, argocd={})

    out = await svc.status_for_services(
        "aspora", [("sc-1", "ad-test-2255", "stage")], db=None
    )

    assert out["sc-1"]["argocd_state"] == svc.STATE_NOT_CONFIGURED
    assert calls == []


async def test_missing_token_is_reported_as_not_configured(env, monkeypatch):
    redis = FakeRedis()
    calls = env(redis)
    monkeypatch.delenv("ARGOCD_TOKEN_TEST", raising=False)

    out = await svc.status_for_services(
        "aspora", [("sc-1", "ad-test-2255", "stage")], db=None
    )

    assert out["sc-1"]["argocd_state"] == svc.STATE_NOT_CONFIGURED
    assert calls == []
