"""Integration tests for external client auth endpoints"""
import time

import pytest

from app.main import app
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.auth.clerk_jwt import clerk_jwt_validator
from app.core.config import settings
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel


@pytest.mark.asyncio
async def test_ext_authorize_and_exchange(client, test_db, monkeypatch):
    old_allowed = settings.ext_auth_allowed_client_ids
    settings.ext_auth_allowed_client_ids = "your.extension.id"

    def fake_validate(token):
        return True, {"exp": int(time.time()) + 3600}, None

    monkeypatch.setattr(clerk_jwt_validator, "validate_token", fake_validate)

    tenant = TenantsMstModel(
        code="tenant_vs",
        name="Tenant VS",
        subdomain="tenant_vs",
        is_active=True,
        is_deleted=False
    )
    user = UserMstModel(
        code="user_vs",
        name="User VS",
        first_name="User",
        last_name="VS",
        email_id="user_vs@example.com",
        tenants_mst_code="tenant_vs",
        is_active=True,
        is_deleted=False
    )
    test_db.add_all([tenant, user])
    await test_db.commit()

    async def override_get_db():
        yield test_db

    async def override_get_current_user_and_tenant():
        return (user, tenant)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_and_tenant] = override_get_current_user_and_tenant

    response = await client.get(
        "/api/v1/auth/ext/authorize?client_id=your.extension.id&state=abc",
        headers={"Authorization": "Bearer test.jwt"},
        follow_redirects=False
    )

    assert response.status_code == 200
    data = response.json()
    code = data.get("code")
    assert code is not None
    assert data.get("state") == "abc"

    exchange_response = await client.post(
        "/api/v1/auth/ext/exchange",
        json={
            "code": code,
            "client_id": "your.extension.id",
            "state": "abc"
        }
    )

    assert exchange_response.status_code == 200
    data = exchange_response.json()
    assert data["access_token"] == "test.jwt"
    assert data["user"]["user_code"] == "user_vs"
    assert data["tenant"]["tenant_code"] == "tenant_vs"

    app.dependency_overrides = {}
    settings.ext_auth_allowed_client_ids = old_allowed
