"""API endpoints for managing tenant log provider configurations."""

import logging
from typing import List, Tuple
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.repository.log_provider_config_repository import LogProviderConfigRepository
from app.schemas.log_provider_config_schemas import (
    CreateLogProviderConfigRequest,
    UpdateLogProviderConfigRequest,
    LogProviderConfigResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/", response_model=LogProviderConfigResponse, status_code=201)
async def create_log_provider_config(
    data: CreateLogProviderConfigRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create a log provider config for the authenticated tenant."""
    _, tenant = user_and_tenant
    repo = LogProviderConfigRepository(db)

    existing = await repo.get_by_tenant_and_provider(tenant.code, data.provider)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"{data.provider.value} log provider already configured for this tenant",
        )

    config = await repo.create(
        code=f"lpc-{uuid4().hex[:12]}",
        name=data.name or f"{data.provider.value} Log Provider",
        description=data.description or "",
        tenants_mst_code=tenant.code,
        provider=data.provider,
        auth_config=data.auth_config,
        is_default=data.is_default,
    )
    await db.commit()
    await db.refresh(config)
    return config


@router.get("/", response_model=List[LogProviderConfigResponse])
async def list_log_provider_configs(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """List all log provider configs for the authenticated tenant."""
    _, tenant = user_and_tenant
    repo = LogProviderConfigRepository(db)
    return await repo.get_all_for_tenant(tenant.code)


@router.put("/{code}", response_model=LogProviderConfigResponse)
async def update_log_provider_config(
    code: str,
    data: UpdateLogProviderConfigRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Update a log provider config."""
    _, tenant = user_and_tenant
    repo = LogProviderConfigRepository(db)

    config = await repo.get_by(code=code, tenants_mst_code=tenant.code, is_deleted=False)
    if not config:
        raise HTTPException(status_code=404, detail="Log provider config not found")

    updates = data.model_dump(exclude_none=True)
    if updates:
        config = await repo.update(config, updates)
        await db.commit()
        await db.refresh(config)
    return config


@router.delete("/{code}", status_code=204)
async def delete_log_provider_config(
    code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Soft delete a log provider config."""
    _, tenant = user_and_tenant
    repo = LogProviderConfigRepository(db)

    config = await repo.get_by(code=code, tenants_mst_code=tenant.code, is_deleted=False)
    if not config:
        raise HTTPException(status_code=404, detail="Log provider config not found")

    config.soft_delete()
    await db.commit()
