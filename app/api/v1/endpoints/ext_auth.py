"""
External client auth code endpoints
"""
from typing import Tuple, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.ext_auth_schemas import (
    ExtAuthExchangeRequest,
    ExtAuthExchangeResponse,
    ExtAuthAuthorizeResponse
)
from app.services.ext_auth_service import ExtAuthService

router = APIRouter(prefix="/auth/ext", tags=["Authentication"])


@router.get(
    "/authorize",
    response_model=ExtAuthAuthorizeResponse,
    summary="Authorize external client (VSCode, MCP, etc.)"
)
async def authorize_ext_client(
    client_id: str = Query(..., description="External client ID"),
    state: Optional[str] = Query(None, description="Optional state for CSRF protection"),
    authorization: str = Header(..., alias="Authorization"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header"
        )

    clerk_jwt = authorization.replace("Bearer ", "", 1).strip()

    user, tenant = user_and_tenant
    service = ExtAuthService(db)
    code = await service.create_auth_code(
        user=user,
        tenant=tenant,
        client_id=client_id,
        state=state,
        clerk_jwt=clerk_jwt
    )

    return ExtAuthAuthorizeResponse(
        code=code,
        state=state
    )


@router.post(
    "/exchange",
    response_model=ExtAuthExchangeResponse,
    summary="Exchange auth code for JWT"
)
async def exchange_ext_code(
    request: ExtAuthExchangeRequest,
    db: AsyncSession = Depends(get_db)
):
    service = ExtAuthService(db)
    result = await service.exchange_code(
        code=request.code,
        client_id=request.client_id,
        state=request.state
    )
    return result
