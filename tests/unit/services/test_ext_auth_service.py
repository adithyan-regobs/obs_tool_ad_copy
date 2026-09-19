"""
Unit tests for ExtAuthService
"""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.core.auth.clerk_jwt import clerk_jwt_validator
from app.core.config import settings
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.db.models.ext_auth_code_model import ExtAuthCodeModel
from app.services.ext_auth_service import ExtAuthService
from sqlalchemy import select


@pytest.mark.asyncio
async def test_create_and_exchange_auth_code_success(test_db, monkeypatch):
    old_allowed = settings.ext_auth_allowed_client_ids
    settings.ext_auth_allowed_client_ids = "your.extension.id"

    def fake_validate(token):
        return True, {"exp": int(time.time()) + 3600}, None

    monkeypatch.setattr(clerk_jwt_validator, "validate_token", fake_validate)

    tenant = TenantsMstModel(
        code="tenant1",
        name="Tenant One",
        subdomain="tenant1",
        is_active=True,
        is_deleted=False
    )
    user = UserMstModel(
        code="user1",
        name="User One",
        first_name="User",
        last_name="One",
        email_id="user1@example.com",
        tenants_mst_code="tenant1",
        is_active=True,
        is_deleted=False
    )
    test_db.add_all([tenant, user])
    await test_db.commit()

    service = ExtAuthService(test_db)
    code = await service.create_auth_code(
        user=user,
        tenant=tenant,
        client_id="your.extension.id",
        state="state123",
        clerk_jwt="test.jwt"
    )

    result = await service.exchange_code(code, "your.extension.id", "state123")

    assert result["access_token"] == "test.jwt"
    assert result["user"]["user_code"] == "user1"
    assert result["tenant"]["tenant_code"] == "tenant1"

    settings.ext_auth_allowed_client_ids = old_allowed


@pytest.mark.asyncio
async def test_invalid_client_id_rejected(test_db, monkeypatch):
    old_allowed = settings.ext_auth_allowed_client_ids
    settings.ext_auth_allowed_client_ids = "good.client"

    def fake_validate(token):
        return True, {"exp": int(time.time()) + 3600}, None

    monkeypatch.setattr(clerk_jwt_validator, "validate_token", fake_validate)

    tenant = TenantsMstModel(
        code="tenant2",
        name="Tenant Two",
        subdomain="tenant2",
        is_active=True,
        is_deleted=False
    )
    user = UserMstModel(
        code="user2",
        name="User Two",
        first_name="User",
        last_name="Two",
        email_id="user2@example.com",
        tenants_mst_code="tenant2",
        is_active=True,
        is_deleted=False
    )
    test_db.add_all([tenant, user])
    await test_db.commit()

    service = ExtAuthService(test_db)

    with pytest.raises(HTTPException):
        await service.create_auth_code(
            user=user,
            tenant=tenant,
            client_id="bad.client",
            state=None,
            clerk_jwt="test.jwt"
        )

    settings.ext_auth_allowed_client_ids = old_allowed


@pytest.mark.asyncio
async def test_used_code_rejected(test_db, monkeypatch):
    old_allowed = settings.ext_auth_allowed_client_ids
    settings.ext_auth_allowed_client_ids = "your.extension.id"

    def fake_validate(token):
        return True, {"exp": int(time.time()) + 3600}, None

    monkeypatch.setattr(clerk_jwt_validator, "validate_token", fake_validate)

    tenant = TenantsMstModel(
        code="tenant3",
        name="Tenant Three",
        subdomain="tenant3",
        is_active=True,
        is_deleted=False
    )
    user = UserMstModel(
        code="user3",
        name="User Three",
        first_name="User",
        last_name="Three",
        email_id="user3@example.com",
        tenants_mst_code="tenant3",
        is_active=True,
        is_deleted=False
    )
    test_db.add_all([tenant, user])
    await test_db.commit()

    service = ExtAuthService(test_db)
    code = await service.create_auth_code(
        user=user,
        tenant=tenant,
        client_id="your.extension.id",
        state=None,
        clerk_jwt="test.jwt"
    )

    await service.exchange_code(code, "your.extension.id", None)

    with pytest.raises(HTTPException):
        await service.exchange_code(code, "your.extension.id", None)

    settings.ext_auth_allowed_client_ids = old_allowed


@pytest.mark.asyncio
async def test_expired_code_rejected(test_db, monkeypatch):
    old_allowed = settings.ext_auth_allowed_client_ids
    settings.ext_auth_allowed_client_ids = "your.extension.id"

    def fake_validate(token):
        return True, {"exp": int(time.time()) + 3600}, None

    monkeypatch.setattr(clerk_jwt_validator, "validate_token", fake_validate)

    tenant = TenantsMstModel(
        code="tenant4",
        name="Tenant Four",
        subdomain="tenant4",
        is_active=True,
        is_deleted=False
    )
    user = UserMstModel(
        code="user4",
        name="User Four",
        first_name="User",
        last_name="Four",
        email_id="user4@example.com",
        tenants_mst_code="tenant4",
        is_active=True,
        is_deleted=False
    )
    test_db.add_all([tenant, user])
    await test_db.commit()

    service = ExtAuthService(test_db)
    code = await service.create_auth_code(
        user=user,
        tenant=tenant,
        client_id="your.extension.id",
        state=None,
        clerk_jwt="test.jwt"
    )

    code_hash = service._hash_code(code)
    stmt = select(ExtAuthCodeModel).where(ExtAuthCodeModel.code_hash == code_hash)
    result = await test_db.execute(stmt)
    auth_code = result.scalar_one()
    auth_code.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await test_db.commit()

    with pytest.raises(HTTPException):
        await service.exchange_code(code, "your.extension.id", None)

    settings.ext_auth_allowed_client_ids = old_allowed
